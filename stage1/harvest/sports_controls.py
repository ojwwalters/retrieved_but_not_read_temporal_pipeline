"""Sports CONTROL harvester, V2 DISCOVERY design (source 'sports_controls').

PRINCIPAL SPEC (2026-08-05): "Player who has been at the same club
continuously across the cutoff and still is. Draw from the same league tiers
and prominence deciles as the transfer set" — plus the same-day addendum:
match TENURE BANDS too (tenure is a training-data confound), priority
tier > tenure band > decile, and report the floor geometry honestly (the
2024-03-31 knowability anchor makes short-tenure control bands structurally
empty).

``python3 -m stage1.harvest --source sports_controls --cutoff X --asof Y``
writes a FROZEN snapshot the OFFLINE v2 adapter derives from. Steps:

  (a) TREATMENT UNIVERSE + MARGINS (no network for the release itself) —
      read the treatment sports release (--opt treatment_release=..., default
      stage1/releases/dev): its included rows give the CLUB universe (the
      corroboration match's team QID), the players' prominence sample, and —
      via the treatment's own ARCHIVED P54 cache
      (stage1/cache/sports_wd_p54.jsonl, sha1-verified against the treatment
      manifest's pin, so tenure is computed from the exact frozen bytes the
      release was built on; no refetch) — each treatment player's
      TENURE-BEFORE-TRANSFER (transfer change_date minus the OLD club's
      latest P580 start at/before it). The requested --cutoff must EQUAL the
      treatment cutoff (the cutoff-era revision pin is only meaningful
      there); --asof is the pull date.

  (b) NETWORKED MARGIN INPUTS — treatment players' sitelink counts and the
      clubs' league/tier info (P118 league; P3983 league level; labels;
      enwiki titles), via bounded VALUES-batched WDQS queries through the
      polite shared ``sparql_get``.

  (c) DISCOVERY, per club — a BOUNDED per-club WDQS query (avoids the global
      SPARQL problem): humans with an OPEN P54 membership of that club whose
      start is at/before the anchor, with enwiki title, sitelink count, and
      the start's true precision. LIMIT-capped; a club returning the cap is
      recorded as truncated in the manifest (the frame is a sampling
      universe, not a completeness claim — unlike treatment discovery, which
      aborts). Resumable per club (.part).

  (d) STRATIFICATION + SEEDED VERIFY-SELECTION (pure) — candidates deduped
      (multi-club QIDs dropped as ambiguous, treatment players excluded,
      v1 anchored survivors injected quota-exempt with top priority),
      stratified by (tier x tenure band x decile), and verify-selected per
      stratum in seeded order with an oversample factor against the
      treatment-derived quotas (exact-cell budget, then tier-band top-up,
      then tier top-up — mirroring the derive-side priority ladder).

  (e) VERIFICATION EVIDENCE, per selected title — TWO pinned revisions via
      the shared fetch_side (newest <= cutoff day start; newest <= pull asof
      day end), resumable with window-stamped .part; and the pull-date full
      P54 statements via the treatment harvester's batch-resilient
      ``_fetch_p54``. All fetch code imported, never duplicated.

COVERAGE — cutoff_exact AND asof_exact: the before is the cutoff-day pinned
revision, the after the asof-day pinned revision; any other window means
different evidence. Sidecars carry NO wall-clock. NO LLM anywhere.
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import date
from pathlib import Path

from stage1.adapters.sports_controls import (
    ANCHOR_DATE,
    CANDIDATES_FILENAME,
    MATCHING_FILENAME,
    REVISIONS_FILENAME,
    REVISIONS_SIDECAR_FILENAME,
    TREATMENT_SOURCE,
    WD_CACHE_FILENAME,
    WD_SIDECAR_FILENAME,
    allocate_quotas,
    band_of,
    cell_key,
    decile_boundaries,
    decile_of,
    seeded_rank,
    tenure_years,
    tier_band_key,
    tier_key,
)
from stage1.adapters.sports import _statement_match, wd_time_to_change_date
from stage1.harvest import Harvester
from stage1.harvest.snapshot import sha1_file
from stage1.harvest.sports import (
    DEFAULT_ENWIKI_SLEEP,
    DEFAULT_WD_SLEEP,
    HARVESTER as SPORTS_HARVESTER,
    SPARQL_ENDPOINT,
    _append_part,
    _asof_pin,
    _cutoff_pin,
    sparql_get,
)
from stage1.normalize import get_comparator
from stage1.tools.fetch_wiki_revisions import (
    API as ENWIKI_API,
    RV_PROPS,
    USER_AGENT as ENWIKI_USER_AGENT,
    fetch_side,
)
from stage1.tools.fetch_wikidata_p54 import (
    API as WD_API,
    BATCH_SIZE as WD_BATCH_SIZE,
    USER_AGENT as WD_USER_AGENT,
    qid_sort_key,
)

TOOL_VERSION = "harvest_sports_controls:v2"

DEFAULT_TREATMENT_RELEASE = "stage1/releases/dev"
DEFAULT_V1_RELEASE = "stage1/releases/dev-sports-controls"
TREATMENT_P54_CACHE_ID = "stage1/cache/sports_wd_p54.jsonl"
DEFAULT_SEED = "sports_controls_v2:2026-08-05"
DEFAULT_TARGET_TOTAL = 100
DEFAULT_OVERSAMPLE = 3
DEFAULT_DISCOVERY_LIMIT = 200
VALUES_BATCH = 100

DISCOVERY_PART = "sports_controls_discovery.part"

_QID_RE = re.compile(r"Q\d+$")


def _iso(value) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def _qid_of(uri) -> str | None:
    if not isinstance(uri, str):
        return None
    tail = uri.rsplit("/", 1)[-1]
    return tail if _QID_RE.fullmatch(tail) else None


def _binding_value(binding, name):
    entry = binding.get(name) if isinstance(binding, dict) else None
    return entry.get("value") if isinstance(entry, dict) else None


def _plus_time(value) -> str | None:
    """WDQS returns '2020-08-01T00:00:00Z'; the cache/P54 convention is
    '+2020-08-01T00:00:00Z'. Normalize."""
    if not isinstance(value, str) or not value:
        return None
    return value if value.startswith(("+", "-")) else "+" + value


def _load_part_by(path: Path, key: str) -> dict:
    """Resume rows of a .part file keyed by ``key`` (torn last lines
    tolerated)."""
    out: dict = {}
    if not path.is_file():
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get(key), str):
                out[row[key]] = row
    return out


# --------------------------------------------------------------------------- #
# (a) treatment universe, tenure, margins (pure given the release bytes)
# --------------------------------------------------------------------------- #

def load_treatment(treatment_release: str, cutoff) -> tuple:
    """(players, clubs, binding) from the treatment sports release's included
    rows. players: [{qid, title, club_qid, before_raw, change_date}].
    clubs: {club_qid: {'enwiki_title','label'}} (from the corroboration
    match). Raises LookupError with a precise message on a missing/wrong
    release or a cutoff mismatch."""
    base = Path(treatment_release)
    facts_path = base / "facts.jsonl"
    manifest_path = base / "manifest.json"
    if not facts_path.is_file() or not manifest_path.is_file():
        raise LookupError(
            f"treatment release {base} is not a built release (facts.jsonl/"
            "manifest.json missing): the v2 control universe is the treatment "
            "transfer set's clubs — point --opt treatment_release=… at one"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise LookupError(f"treatment manifest {manifest_path} is not valid JSON: {exc}")
    if not isinstance(manifest, dict) or manifest.get("source") != TREATMENT_SOURCE:
        raise LookupError(
            f"treatment release {base} has source {manifest.get('source')!r}, "
            f"expected {TREATMENT_SOURCE!r}"
        )
    args = manifest.get("args") if isinstance(manifest.get("args"), dict) else {}
    if args.get("cutoff") != _iso(cutoff):
        raise LookupError(
            f"--cutoff {_iso(cutoff)} does not equal the treatment release's cutoff "
            f"{args.get('cutoff')!r}: the control's cutoff-era revision pin must "
            "anchor at the treatment cutoff (pass --cutoff "
            f"{args.get('cutoff')})"
        )
    players = []
    clubs: dict = {}
    with open(facts_path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise LookupError(
                    f"treatment release {facts_path} line {line_no} is unparseable "
                    f"({exc}): refusing to build a universe from a corrupt release"
                )
            if not isinstance(row, dict) or row.get("disposition") != "included":
                continue
            entity = row.get("entity") or {}
            qid = (entity.get("ids") or {}).get("wikidata_qid")
            wd = ((row.get("after") or {}).get("evidence") or {}).get("ref", {})
            match = (wd.get("wikidata_p54") or {}).get("match") or {}
            club_qid = match.get("team_qid")
            if club_qid:
                clubs.setdefault(club_qid, {
                    "enwiki_title": match.get("team_enwiki_title") or "",
                    "label": match.get("team_label_en") or "",
                })
            players.append({
                "qid": qid,
                "title": entity.get("name"),
                "club_qid": club_qid,
                "before_raw": (row.get("before") or {}).get("raw"),
                "change_date": (row.get("change_date") or {}).get("value"),
            })
    if not players:
        raise LookupError(f"treatment release {base} has no included rows")
    p54_id = None
    input_files = manifest.get("input_files") if isinstance(manifest.get("input_files"), dict) else {}
    for key, value in input_files.items():
        if str(key).endswith("sports_wd_p54.jsonl"):
            p54_id = (str(key), str(value))
            break
    binding = {
        "treatment_release": str(treatment_release),
        "treatment_facts_sha1": sha1_file(facts_path),
        "treatment_manifest_sha1": sha1_file(manifest_path),
        "treatment_cutoff": args.get("cutoff"),
        "treatment_asof": args.get("asof"),
        "treatment_included": len(players),
        "treatment_clubs": len(clubs),
        "treatment_p54_cache_pin": p54_id,
    }
    return players, clubs, binding


def load_treatment_p54_cache(binding: dict) -> dict:
    """The treatment's ARCHIVED P54 payloads ({player_qid: row}), sha1-verified
    against the treatment manifest's pin. The coordinator authorized a paced
    refetch only when the archive is absent; a PRESENT-but-different cache is
    refused loudly — tenure-before-transfer must come from the exact bytes the
    treatment was built on (a current refetch could not reproduce them)."""
    pin = binding.get("treatment_p54_cache_pin")
    path = Path(TREATMENT_P54_CACHE_ID)
    if not path.is_file():
        raise LookupError(
            f"the treatment's archived P54 cache {TREATMENT_P54_CACHE_ID} is missing: "
            "tenure-before-transfer cannot be computed from the frozen bytes "
            "(restore the cache; a live refetch would not reproduce the "
            "treatment-era statements)"
        )
    actual = sha1_file(path)
    if pin and pin[1] != actual:
        raise LookupError(
            f"the archived P54 cache {TREATMENT_P54_CACHE_ID} (sha1 {actual}) does not "
            f"match the treatment manifest's pin {pin[1]} under {pin[0]!r}: the archive "
            "was overwritten since the treatment release was built — refusing to compute "
            "tenure from unpinned bytes"
        )
    store: dict = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("player_qid"), str):
                store[row["player_qid"]] = row
    return store


def treatment_tenures(players: list, p54_store: dict) -> dict:
    """{player_qid: {'tenure_years': float|None, 'band': str, 'old_club_start':
    iso|None}} — tenure-before-transfer: the transfer change_date minus the
    OLD club's latest usable P580 start at/before it, org-matching the
    record's before value against the archived statements (the treatment's
    own squad-aware match). Pure; unmatched/undatable -> band 'unknown'."""
    comparator = get_comparator("org")
    out: dict = {}
    for player in players:
        qid = player.get("qid")
        before_raw = player.get("before_raw")
        change_iso = player.get("change_date")
        entry = {"tenure_years": None, "band": band_of(None), "old_club_start": None}
        out[qid] = entry
        row = p54_store.get(qid) if qid else None
        statements = row.get("statements") if isinstance(row, dict) else None
        if not isinstance(statements, list) or not isinstance(before_raw, str) or not before_raw:
            continue
        parsed = comparator.parse(before_raw)
        if not parsed.ok:
            continue
        best_start = None
        for statement in statements:
            if not isinstance(statement, dict):
                continue
            outcome, _name, _detail = _statement_match(
                parsed.canonical, before_raw, statement, comparator)
            if outcome != "equal":
                continue
            cd = wd_time_to_change_date(statement.get("p580"))
            if cd is None:
                continue
            start_iso = cd[0]
            if isinstance(change_iso, str) and start_iso > change_iso:
                continue  # started after the transfer: a later spell, not the old one
            if best_start is None or start_iso > best_start:
                best_start = start_iso
        if best_start is None:
            continue
        years = tenure_years(best_start, change_iso)
        entry["old_club_start"] = best_start
        entry["tenure_years"] = years
        entry["band"] = band_of(years)
    return out


def build_matching(players, tenures, sitelinks_by_qid, club_info, seed,
                   target_total, oversample, binding) -> dict:
    """The frozen matching file content (pure): decile boundaries from the
    treatment prominence sample, the treatment tier/band/decile margins and
    3-margin joint, and the nested quota tables (cells by largest remainder;
    tier-band and tier envelopes as SUMS of the cells, so the derive-side
    priority ladder's envelopes are consistent by construction)."""
    counts = [sitelinks_by_qid.get(p.get("qid")) for p in players]
    boundaries = decile_boundaries([c for c in counts if c is not None])
    tiers: dict = {}
    bands: dict = {}
    deciles: dict = {}
    joint: dict = {}
    tier_bands: dict = {}
    for player in players:
        qid = player.get("qid")
        info = club_info.get(player.get("club_qid")) or {}
        tier = info.get("tier")
        band = (tenures.get(qid) or {}).get("band") or band_of(None)
        decile = decile_of(sitelinks_by_qid.get(qid), boundaries)
        tiers[tier_key(tier)] = tiers.get(tier_key(tier), 0) + 1
        bands[band] = bands.get(band, 0) + 1
        deciles[str(decile)] = deciles.get(str(decile), 0) + 1
        joint[cell_key(tier, band, decile)] = joint.get(cell_key(tier, band, decile), 0) + 1
        tier_bands[tier_band_key(tier, band)] = tier_bands.get(tier_band_key(tier, band), 0) + 1
    cell_quotas = allocate_quotas(joint, int(target_total))
    tb_quotas: dict = {}
    t_quotas: dict = {}
    for cell, quota in cell_quotas.items():
        # cell key shape: 'tier=<t>|band=<b>|decile=<d>'
        tier_part, band_part, _ = cell.split("|", 2)
        tb = f"{tier_part}|{band_part}"
        tb_quotas[tb] = tb_quotas.get(tb, 0) + quota
        t_quotas[tier_part] = t_quotas.get(tier_part, 0) + quota
    return {
        "tool_version": TOOL_VERSION,
        "seed": seed,
        "target_total": int(target_total),
        "oversample": int(oversample),
        "priority": "tier>tenure_band>decile",
        "anchor_date": ANCHOR_DATE.isoformat(),
        "decile_boundaries": boundaries,
        "tenure_note": (
            "control tenure bands under ~2.4y are STRUCTURALLY EMPTY: the "
            "2024-03-31 anchor forbids later starts, so short-tenure treatment "
            "mass has no control counterpart — reported, not hidden"
        ),
        "treatment_margins": {
            "tiers": dict(sorted(tiers.items())),
            "bands": dict(sorted(bands.items())),
            "deciles": dict(sorted(deciles.items())),
            "tier_bands": dict(sorted(tier_bands.items())),
            "joint": dict(sorted(joint.items())),
        },
        "quotas": {
            "cells": dict(sorted(cell_quotas.items())),
            "tier_bands": dict(sorted(tb_quotas.items())),
            "tiers": dict(sorted(t_quotas.items())),
        },
        **binding,
    }


def select_for_verification(candidates: list, matching: dict) -> None:
    """Mark verify_selected in-place, mirroring the derive-side priority
    ladder with an oversample factor: per exact cell up to quota*oversample in
    seeded order; then per (tier, band) top-up to its envelope*oversample;
    then per tier top-up to its envelope*oversample. v1 survivors are always
    selected. Pure given the candidate list."""
    oversample = int(matching.get("oversample") or DEFAULT_OVERSAMPLE)
    quotas = matching.get("quotas") or {}
    cell_budget = {k: int(v) * oversample for k, v in (quotas.get("cells") or {}).items()}
    tb_budget = {k: int(v) * oversample for k, v in (quotas.get("tier_bands") or {}).items()}
    t_budget = {k: int(v) * oversample for k, v in (quotas.get("tiers") or {}).items()}
    cell_used: dict = {}
    tb_used: dict = {}
    t_used: dict = {}
    ordered = sorted(candidates, key=lambda c: (c.get("seeded_rank") or "", c.get("qid") or ""))
    for cand in ordered:
        if cand.get("origin") == "v1_pool":
            cand["verify_selected"] = True
            continue
        k3 = cell_key(cand.get("tier"), cand.get("tenure_band"), cand.get("decile") or 0)
        k2 = tier_band_key(cand.get("tier"), cand.get("tenure_band"))
        k1 = tier_key(cand.get("tier"))
        if t_used.get(k1, 0) >= t_budget.get(k1, 0):
            cand["verify_selected"] = False
            continue
        # Tier envelope has room: exact-cell budget first (guaranteed to thin
        # cells against fat-cell competition), then tier-band slack, then
        # tier slack — the selection-time mirror of the derive-side ladder.
        in_cell = cell_used.get(k3, 0) < cell_budget.get(k3, 0)
        in_tb = tb_used.get(k2, 0) < tb_budget.get(k2, 0)
        cand["verify_selected"] = True
        t_used[k1] = t_used.get(k1, 0) + 1
        if in_cell or in_tb:
            tb_used[k2] = tb_used.get(k2, 0) + 1
        if in_cell:
            cell_used[k3] = cell_used.get(k3, 0) + 1


# --------------------------------------------------------------------------- #
# Harvester
# --------------------------------------------------------------------------- #

class SportsControlsHarvester(Harvester):
    source = "sports_controls"
    tool_version = TOOL_VERSION

    # -- coverage (PURE, no network) ---------------------------------------
    def coverage(self, cfg: dict) -> dict:
        return {
            "cutoff": _iso(cfg["cutoff"]),
            "asof": _iso(cfg["asof"]),
            "cutoff_exact": True,
            "asof_exact": True,
            "precision": "day",
            "window_basis": "cutoff_revision_to_reverify_asof",
        }

    # -- harvest (the ONLY networked method) -------------------------------
    def harvest(self, cfg: dict, writer, ctx: dict) -> None:
        cutoff = cfg["cutoff"]
        asof = cfg["asof"]
        resume = bool(cfg.get("resume"))
        enwiki_sleep = float(cfg.get("enwiki_sleep", DEFAULT_ENWIKI_SLEEP))
        wd_sleep = float(cfg.get("wd_sleep", DEFAULT_WD_SLEEP))
        seed = str(cfg.get("seed") or DEFAULT_SEED)
        target_total = int(cfg.get("target_total") or DEFAULT_TARGET_TOTAL)
        oversample = int(cfg.get("oversample") or DEFAULT_OVERSAMPLE)
        discovery_limit = int(cfg.get("discovery_limit") or DEFAULT_DISCOVERY_LIMIT)
        treatment_release = cfg.get("treatment_release") or DEFAULT_TREATMENT_RELEASE
        v1_release = cfg.get("v1_release") or DEFAULT_V1_RELEASE
        http_errors: list = []

        # ---- (a) treatment universe + archived-cache tenure ----------------
        players, clubs, binding = load_treatment(treatment_release, cutoff)
        p54_store = load_treatment_p54_cache(binding)
        tenures = treatment_tenures(players, p54_store)
        survivors = self._load_v1_survivors(v1_release, http_errors)
        print(f"[harvest sports_controls] universe: {len(clubs)} treatment club(s), "
              f"{len(players)} treatment player(s), {len(survivors)} v1 survivor(s)",
              file=sys.stderr, flush=True)

        sample = cfg.get("sample")
        club_qids = sorted(clubs, key=qid_sort_key)
        if sample:
            wanted = {q.strip() for q in sample if q and q.strip()}
            unknown = sorted(wanted - set(club_qids))
            if unknown:
                raise LookupError(
                    f"--sample club QID(s) not in the treatment club universe: {unknown}"
                )
            club_qids = [q for q in club_qids if q in wanted]

        # ---- (b) margin inputs: club info + treatment sitelinks -----------
        all_club_qids = sorted(set(club_qids) | {s["club_qid"] for s in survivors
                                                 if s.get("club_qid")}, key=qid_sort_key)
        club_info = self._fetch_club_info(all_club_qids, clubs, wd_sleep, http_errors)
        sitelink_qids = sorted(
            {p["qid"] for p in players if p.get("qid")}
            | {s["qid"] for s in survivors if s.get("qid")},
            key=qid_sort_key)
        treatment_sitelinks = self._fetch_sitelink_counts(
            sitelink_qids, wd_sleep, http_errors)

        # ---- (c) per-club bounded discovery --------------------------------
        discovered, per_club_stats = self._discover(
            club_qids, discovery_limit, writer.out_dir, resume, wd_sleep)

        # ---- (d) candidates + matching + verify-selection (pure) ----------
        matching = build_matching(players, tenures, treatment_sitelinks,
                                  club_info, seed, target_total, oversample, binding)
        candidates, assembly_stats = self._assemble_candidates(
            discovered, survivors, players, club_info, matching, seed, asof,
            sitelinks_by_qid=treatment_sitelinks)
        select_for_verification(candidates, matching)
        selected = [c for c in candidates if c.get("verify_selected")]
        print(f"[harvest sports_controls] discovery: {len(candidates)} candidate(s), "
              f"{len(selected)} verify-selected", file=sys.stderr, flush=True)

        # ---- (e) verification evidence -------------------------------------
        titles = sorted({c["title"] for c in selected if c.get("title")})
        revision_rows = self._fetch_revisions(
            titles, cutoff, asof, writer.out_dir, resume, enwiki_sleep, http_errors)
        qids = sorted({c["qid"] for c in selected if c.get("qid")}, key=qid_sort_key)
        p54_rows = SPORTS_HARVESTER._fetch_p54(qids, wd_sleep, http_errors)

        stats = {
            "treatment_release": str(treatment_release),
            "clubs_queried": len(club_qids),
            "clubs_truncated": sorted(q for q, s in per_club_stats.items()
                                      if s.get("truncated")),
            "discovered_rows": sum(s.get("rows", 0) for s in per_club_stats.values()),
            "candidates": len(candidates),
            "verify_selected": len(selected),
            "titles_fetched": len(revision_rows),
            "revision_fetch_errors": sum(1 for r in revision_rows if r.get("fetch_errors")),
            "p54_players": len(p54_rows),
            "v1_survivors": len(survivors),
            **assembly_stats,
            "http_errors": http_errors,
            "resumed": resume,
        }
        self._emit(writer, cfg, candidates, revision_rows, p54_rows, matching,
                   per_club_stats, enwiki_sleep, wd_sleep, stats)

        for name in (DISCOVERY_PART, REVISIONS_FILENAME + ".part"):
            part = writer.out_dir / name
            if part.exists():
                part.unlink()

    # -- v1 survivors -------------------------------------------------------
    @staticmethod
    def _load_v1_survivors(v1_release, http_errors) -> list:
        """The v1 release's included rows (the anchored survivors), carried
        into the v2 pool quota-exempt. A missing default release is a
        recorded note, never fatal (a fresh checkout can still build v2)."""
        base = Path(v1_release)
        facts = base / "facts.jsonl"
        if not facts.is_file():
            http_errors.append({"stage": "v1_survivors", "path": str(facts),
                                "error": "v1 release absent; no survivors injected"})
            return []
        survivors = []
        with open(facts, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("disposition") != "included":
                    continue
                entity = row.get("entity") or {}
                reverify = (((row.get("after") or {}).get("evidence") or {})
                            .get("ref", {}) or {}).get("wikidata_p54_reverify") or {}
                open_match = reverify.get("open_match") or {}
                survivors.append({
                    "qid": (entity.get("ids") or {}).get("wikidata_qid"),
                    "title": entity.get("name"),
                    "club_qid": open_match.get("team_qid"),
                    "club_title": open_match.get("team_enwiki_title") or "",
                    "club_label": open_match.get("team_label_en") or "",
                    "p580": open_match.get("p580"),
                })
        return survivors

    # -- (b) margin inputs --------------------------------------------------
    def _fetch_club_info(self, club_qids, clubs, sleep, http_errors) -> dict:
        """{club_qid: {'enwiki_title','label','league_qid','league_label',
        'tier'}} via VALUES-batched WDQS. League choice is deterministic:
        prefer a league WITH a P3983 level, lowest level, then lowest QID. A
        failed batch degrades those clubs to tier 'unknown' (recorded)."""
        info = {q: {"enwiki_title": (clubs.get(q) or {}).get("enwiki_title", ""),
                    "label": (clubs.get(q) or {}).get("label", ""),
                    "league_qid": None, "league_label": None, "tier": None}
                for q in club_qids}
        for i in range(0, len(club_qids), VALUES_BATCH):
            batch = club_qids[i:i + VALUES_BATCH]
            values = " ".join(f"wd:{q}" for q in batch)
            query = (
                "SELECT ?club ?clubLabel ?clubTitle ?league ?leagueLabel ?level WHERE {\n"
                f"  VALUES ?club {{ {values} }}\n"
                "  OPTIONAL { ?clubArticle schema:about ?club ;\n"
                "             schema:isPartOf <https://en.wikipedia.org/> ;\n"
                "             schema:name ?clubTitle . }\n"
                "  OPTIONAL { ?club rdfs:label ?clubLabel .\n"
                "             FILTER(LANG(?clubLabel) = \"en\") }\n"
                "  OPTIONAL { ?club wdt:P118 ?league .\n"
                "    OPTIONAL { ?league wdt:P3983 ?level . }\n"
                "    OPTIONAL { ?league rdfs:label ?leagueLabel .\n"
                "               FILTER(LANG(?leagueLabel) = \"en\") } }\n"
                "}"
            )
            payload = sparql_get(query, sleep)
            if payload is None:
                http_errors.append({"stage": "club_info", "qids": list(batch),
                                    "error": "WDQS unreachable after retries; "
                                             "those clubs' tier degrades to unknown"})
                continue
            best: dict = {}
            for binding in payload.get("results", {}).get("bindings", []):
                club = _qid_of(_binding_value(binding, "club"))
                if club not in info:
                    continue
                title = _binding_value(binding, "clubTitle")
                if title and not info[club]["enwiki_title"]:
                    info[club]["enwiki_title"] = title
                label = _binding_value(binding, "clubLabel")
                if label and not info[club]["label"]:
                    info[club]["label"] = label
                league = _qid_of(_binding_value(binding, "league"))
                if league is None:
                    continue
                raw_level = _binding_value(binding, "level")
                try:
                    level = int(float(raw_level)) if raw_level is not None else None
                except (TypeError, ValueError):
                    level = None
                key = (level is None, level if level is not None else 0,
                       qid_sort_key(league))
                if club not in best or key < best[club][0]:
                    best[club] = (key, league,
                                  _binding_value(binding, "leagueLabel"), level)
            for club, (_key, league, league_label, level) in best.items():
                info[club]["league_qid"] = league
                info[club]["league_label"] = league_label
                info[club]["tier"] = level
        return info

    def _fetch_sitelink_counts(self, qids, sleep, http_errors) -> dict:
        """{qid: int sitelink count} via VALUES-batched WDQS. A failed batch
        leaves those players unknown (decile 0, the conservative bin) and is
        recorded."""
        out: dict = {}
        for i in range(0, len(qids), VALUES_BATCH):
            batch = qids[i:i + VALUES_BATCH]
            values = " ".join(f"wd:{q}" for q in batch)
            query = ("SELECT ?p ?sitelinks WHERE {\n"
                     f"  VALUES ?p {{ {values} }}\n"
                     "  ?p wikibase:sitelinks ?sitelinks . }")
            payload = sparql_get(query, sleep)
            if payload is None:
                http_errors.append({"stage": "treatment_sitelinks",
                                    "qids": list(batch),
                                    "error": "WDQS unreachable after retries"})
                continue
            for binding in payload.get("results", {}).get("bindings", []):
                qid = _qid_of(_binding_value(binding, "p"))
                raw = _binding_value(binding, "sitelinks")
                try:
                    if qid:
                        out[qid] = int(float(raw))
                except (TypeError, ValueError):
                    continue
        return out

    # -- (c) per-club bounded discovery -------------------------------------
    def _discover(self, club_qids, limit, out_dir, resume, sleep):
        """{club_qid: [row]}, {club_qid: {'rows': n, 'truncated': bool}}.
        One bounded query per club; resumable per club; a persistent failure
        for a club is FATAL (an absent club would silently shrink the frame
        below what the manifest claims). A club returning the row cap is
        recorded as truncated — the frame is a sampling universe, so
        truncation is disclosed rather than fatal (unlike treatment
        discovery's completeness abort)."""
        part_path = out_dir / DISCOVERY_PART
        done = _load_part_by(part_path, "club_qid") if resume else {}
        todo = [q for q in club_qids if q not in done]
        print(f"[harvest sports_controls] discovery: {len(done)} club(s) resumed, "
              f"{len(todo)} to query", file=sys.stderr, flush=True)
        anchor_ts = f"{ANCHOR_DATE.isoformat()}T23:59:59Z"
        results = {q: done[q] for q in club_qids if q in done}
        with open(part_path, "a", encoding="utf-8", newline="\n") as part:
            for n, club in enumerate(todo, 1):
                query = (
                    "SELECT ?player ?title ?sitelinks ?start ?prec WHERE {\n"
                    f"  ?player p:P54 ?st .\n"
                    f"  ?st ps:P54 wd:{club} .\n"
                    "  ?st pq:P580 ?start .\n"
                    "  FILTER NOT EXISTS { ?st pq:P582 ?end }\n"
                    "  ?st pqv:P580 ?sn . ?sn wikibase:timePrecision ?prec .\n"
                    f"  FILTER(?start <= \"{anchor_ts}\"^^xsd:dateTime)\n"
                    "  ?player wdt:P31 wd:Q5 .\n"
                    "  ?player wikibase:sitelinks ?sitelinks .\n"
                    "  ?article schema:about ?player ;\n"
                    "           schema:isPartOf <https://en.wikipedia.org/> ;\n"
                    "           schema:name ?title .\n"
                    f"}} LIMIT {limit}"
                )
                payload = sparql_get(query, sleep)
                if payload is None:
                    raise RuntimeError(
                        f"WDQS discovery failed for club {club} after retries: an absent "
                        "club would silently shrink the control frame — aborting (rerun "
                        "resumes from the discovery checkpoint)"
                    )
                rows = []
                for binding in payload.get("results", {}).get("bindings", []):
                    qid = _qid_of(_binding_value(binding, "player"))
                    title = _binding_value(binding, "title")
                    if not qid or not title:
                        continue
                    try:
                        sitelinks = int(float(_binding_value(binding, "sitelinks")))
                    except (TypeError, ValueError):
                        sitelinks = None
                    try:
                        prec = int(float(_binding_value(binding, "prec")))
                    except (TypeError, ValueError):
                        prec = None
                    start = _plus_time(_binding_value(binding, "start"))
                    rows.append({"qid": qid, "title": title, "sitelinks": sitelinks,
                                 "p580": {"time": start, "precision": prec}
                                 if start else None})
                entry = {"club_qid": club, "rows": rows,
                         "truncated": len(payload.get("results", {})
                                          .get("bindings", [])) >= limit}
                results[club] = entry
                _append_part(part, entry)
                if n % 10 == 0 or n == len(todo):
                    print(f"[harvest sports_controls]   discovery {n}/{len(todo)} club(s)",
                          file=sys.stderr, flush=True)
        per_club = {q: {"rows": len(r.get("rows") or []),
                        "truncated": bool(r.get("truncated"))}
                    for q, r in results.items()}
        return results, per_club

    # -- (d) candidate assembly (pure) --------------------------------------
    @staticmethod
    def _assemble_candidates(discovered, survivors, players, club_info,
                             matching, seed, asof, sitelinks_by_qid=None) -> tuple:
        """Deduped candidate rows with strata. Rules (each count recorded):
        a QID discovered under MULTIPLE clubs is dropped (two open senior
        memberships is not a clean current-club fact); a treatment player is
        excluded (a transfer-set member cannot be its own control); a
        survivor QID also discovered keeps the survivor row (origin
        v1_pool, quota-exempt)."""
        treatment_qids = {p.get("qid") for p in players if p.get("qid")}
        by_qid: dict = {}
        multi_club = set()
        for club in sorted(discovered, key=qid_sort_key):
            for row in discovered[club].get("rows") or []:
                qid = row.get("qid")
                if not qid:
                    continue
                if qid in by_qid and by_qid[qid]["club_qid"] != club:
                    multi_club.add(qid)
                    continue
                if qid in by_qid:
                    # same club twice (two open spells): keep the later start
                    old = by_qid[qid].get("p580") or {}
                    new = row.get("p580") or {}
                    if str(new.get("time") or "") > str(old.get("time") or ""):
                        by_qid[qid]["p580"] = row.get("p580")
                    continue
                by_qid[qid] = {"qid": qid, "title": row.get("title"),
                               "club_qid": club, "sitelinks": row.get("sitelinks"),
                               "p580": row.get("p580"), "origin": "discovery"}
        dropped_multi = sorted(multi_club, key=qid_sort_key)
        for qid in dropped_multi:
            by_qid.pop(qid, None)
        dropped_treatment = sorted(
            (q for q in by_qid if q in treatment_qids), key=qid_sort_key)
        for qid in dropped_treatment:
            by_qid.pop(qid, None)
        sitelinks_by_qid = sitelinks_by_qid or {}
        for survivor in survivors:
            qid = survivor.get("qid")
            if not qid:
                continue
            by_qid[qid] = {**survivor,
                           "sitelinks": sitelinks_by_qid.get(
                               qid, by_qid.get(qid, {}).get("sitelinks")),
                           "origin": "v1_pool"}

        boundaries = matching.get("decile_boundaries") or []
        candidates = []
        for qid in sorted(by_qid, key=qid_sort_key):
            cand = by_qid[qid]
            info = club_info.get(cand.get("club_qid")) or {}
            cd = wd_time_to_change_date(cand.get("p580"))
            years = tenure_years(cd[0], asof) if cd else None
            cand.update({
                "club_title": cand.get("club_title") or info.get("enwiki_title") or "",
                "club_label": cand.get("club_label") or info.get("label") or "",
                "league_qid": info.get("league_qid"),
                "league_label": info.get("league_label"),
                "tier": info.get("tier"),
                "tenure_years": years,
                "tenure_band": band_of(years),
                "decile": decile_of(cand.get("sitelinks"), boundaries),
                "seeded_rank": "0" * 40 if cand.get("origin") == "v1_pool"
                               else seeded_rank(seed, qid),
                "verify_selected": False,
            })
            candidates.append(cand)
        stats = {"dropped_multi_club": len(dropped_multi),
                 "dropped_treatment_overlap": len(dropped_treatment)}
        return candidates, stats

    # -- (e) verification evidence ------------------------------------------
    def _fetch_revisions(self, titles, cutoff, asof, out_dir, resume, sleep,
                         http_errors):
        """Two pinned revisions per title via the shared fetch_side, resumable
        with a window-stamped .part (stale-window rows refetched)."""
        cutoff_pin = _cutoff_pin(cutoff)
        asof_pin = _asof_pin(asof)
        pins = {"before": cutoff_pin, "after": asof_pin}
        part_path = out_dir / (REVISIONS_FILENAME + ".part")
        part_rows = _load_part_by(part_path, "title") if resume else {}
        done = {t: r for t, r in part_rows.items()
                if not r.get("fetch_errors") and r.get("pins") == pins}
        stale = sum(1 for r in part_rows.values() if r.get("pins") != pins)
        todo = [t for t in titles if t not in done]
        print(f"[harvest sports_controls] revisions: {len(done)} resumed "
              f"({stale} stale-window row(s) ignored), {len(todo)} to fetch "
              f"(before<={cutoff_pin}, after<={asof_pin})",
              file=sys.stderr, flush=True)
        results = dict(done)
        with open(part_path, "a", encoding="utf-8", newline="\n") as part:
            for i, title in enumerate(todo, 1):
                sides = {}
                errors = []
                for side_key, pin in (("cutoff", cutoff_pin), ("current", asof_pin)):
                    try:
                        side, err = fetch_side(title, pin, "row")
                    except RuntimeError as exc:
                        side, err = None, f"fetch failed after retries: {exc}"
                        http_errors.append({"stage": f"{side_key}_revision",
                                            "title": title, "error": str(exc)})
                    time.sleep(sleep)
                    if side is not None:
                        side = dict(side)
                        side["pinned_ts"] = side.get("ts")
                    sides[side_key] = side
                    if err:
                        errors.append(f"{side_key}: {err}")
                row = {"title": title, "pins": pins, "cutoff": sides.get("cutoff"),
                       "current": sides.get("current"), "fetch_errors": errors}
                results[title] = row
                _append_part(part, row)
                if i % 25 == 0 or i == len(todo):
                    print(f"[harvest sports_controls]   revisions {i}/{len(todo)} title(s)",
                          file=sys.stderr, flush=True)
        rows = []
        for title in sorted(results):
            row = results[title]
            rows.append({"title": row["title"], "cutoff": row.get("cutoff"),
                         "current": row.get("current"),
                         "fetch_errors": row.get("fetch_errors") or []})
        return rows

    # -- snapshot emission (PURE given already-fetched rows) ----------------
    def _emit(self, writer, cfg, candidates, revision_rows, p54_rows, matching,
              per_club_stats, enwiki_sleep, wd_sleep, stats) -> None:
        out_dir = writer.out_dir

        writer.add_jsonl(CANDIDATES_FILENAME, candidates)
        writer.add_json(MATCHING_FILENAME, matching)

        writer.add_jsonl(REVISIONS_FILENAME, revision_rows)
        revisions_sha1 = sha1_file(out_dir / REVISIONS_FILENAME)
        err_titles = sorted(r["title"] for r in revision_rows if r.get("fetch_errors"))
        writer.add_json(REVISIONS_SIDECAR_FILENAME, {
            "tool_version": TOOL_VERSION,
            "endpoint": ENWIKI_API,
            "user_agent": ENWIKI_USER_AGENT,
            "query_params": {
                "action": "query", "prop": "revisions", "rvprop": RV_PROPS,
                "rvslots": "main", "rvlimit": "1", "rvdir": "older",
                "rvstart": "before=cutoff day start, after=pull asof day end; "
                           "cache pinned to each revision's own timestamp",
                "maxlag": "5",
            },
            "cutoff_pin": _cutoff_pin(cfg["cutoff"]),
            "asof_pin": _asof_pin(cfg["asof"]),
            "candidates_file": CANDIDATES_FILENAME,
            "candidates_sha1": sha1_file(out_dir / CANDIDATES_FILENAME),
            "counts": {
                "titles": len(revision_rows),
                "rows_ok": len(revision_rows) - len(err_titles),
                "rows_with_fetch_errors": len(err_titles),
            },
            "fetch_error_titles": err_titles,
            "cache_sha1": revisions_sha1,
        })

        writer.add_jsonl(WD_CACHE_FILENAME, p54_rows)
        wd_sha1 = sha1_file(out_dir / WD_CACHE_FILENAME)
        ok = [r for r in p54_rows if r.get("status") == "ok"]
        missing = sorted((r["player_qid"] for r in p54_rows if r.get("status") != "ok"),
                         key=qid_sort_key)
        writer.add_json(WD_SIDECAR_FILENAME, {
            "tool_version": TOOL_VERSION,
            "endpoint": WD_API,
            "user_agent": WD_USER_AGENT,
            "query_params": {
                "player_call": {"action": "wbgetentities", "props": "claims",
                                "batch_size": WD_BATCH_SIZE},
                "team_call": {"action": "wbgetentities",
                              "props": "labels|aliases|sitelinks", "languages": "en",
                              "sitefilter": "enwiki", "batch_size": WD_BATCH_SIZE},
            },
            "candidates_file": CANDIDATES_FILENAME,
            "candidates_sha1": sha1_file(out_dir / CANDIDATES_FILENAME),
            "counts": {
                "players_requested": len(p54_rows),
                "players_ok": len(ok),
                "players_missing": len(missing),
                "p54_statements": sum(len(r.get("statements") or []) for r in p54_rows),
            },
            "missing_player_qids": missing,
            "cache_sha1": wd_sha1,
        })

        writer.set_params({
            "design": "v2_discovery",
            "treatment_release": matching.get("treatment_release"),
            "treatment_facts_sha1": matching.get("treatment_facts_sha1"),
            "treatment_manifest_sha1": matching.get("treatment_manifest_sha1"),
            "treatment_p54_cache_pin": matching.get("treatment_p54_cache_pin"),
            "seed": matching.get("seed"),
            "target_total": matching.get("target_total"),
            "oversample": matching.get("oversample"),
            "priority": matching.get("priority"),
            "anchor_date": matching.get("anchor_date"),
            "discovery_limit": int(cfg.get("discovery_limit")
                                   or DEFAULT_DISCOVERY_LIMIT),
            "sparql_endpoint": SPARQL_ENDPOINT,
            "wikidata_endpoint": WD_API,
            "enwiki_endpoint": ENWIKI_API,
            "enwiki_sleep_s": enwiki_sleep,
            "wd_sleep_s": wd_sleep,
            "per_club_discovery": {q: per_club_stats[q]
                                   for q in sorted(per_club_stats, key=qid_sort_key)},
            "sample": sorted(cfg.get("sample")) if cfg.get("sample") else None,
            "files": [CANDIDATES_FILENAME, MATCHING_FILENAME, REVISIONS_FILENAME,
                      WD_CACHE_FILENAME],
        })
        writer.set_stats(stats)
        print(f"[harvest sports_controls] DONE. {stats['candidates']} candidate(s); "
              f"{stats['verify_selected']} selected; {stats['titles_fetched']} title(s) "
              f"re-pinned; {stats['p54_players']} P54 player(s); "
              f"{len(stats['http_errors'])} http miss(es).",
              file=sys.stderr, flush=True)


HARVESTER = SportsControlsHarvester()
