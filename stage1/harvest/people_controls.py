"""People CONTROL-pull harvester (source 'people_controls') — rulings A7/A3.

"people = Wikidata **sitelink-decile** matched living persons. Pull jobs
pending." (A7, 2026-07-31.) "~50 LIVING control persons ... whose current
leads are present-tense AND true" (A3, 2026-07-30). Principal ruling
2026-08-05: the matching basis is the DRAWN 50 treatment people
(eval/data/draws/draws.json -> domains.people.taken, read-only); their
sitelink deciles are the target distribution. This is the pull job's
networked half: ``python3 -m stage1.harvest --source people_controls --cutoff
X --asof Y`` writes a FROZEN snapshot the OFFLINE people_controls adapter
(stage1/adapters/people_controls.py) derives from. Four steps:

  (a) THE MATCHING BASIS (no network) — read eval/data/draws/draws.json
      (``--opt draws=...``) and the treatment people release (``--opt
      treatment_release=...``, default stage1/releases/dev-people): map each
      drawn fact_id to its release record, take its sitelink count
      (provenance.finder.sitelinks), compute the DECILE BOUNDARIES
      (nearest-rank, the shared pure function in the adapter module), the
      50-person target histogram, and the per-decile selection quotas
      (ceil(target * headroom), headroom default 1.5 -> ~75 selected so the
      eval draw has headroom for 50 after verification losses). Both input
      files are sha1-bound into the sidecar. The requested --cutoff must
      EQUAL the treatment release's cutoff (the control's knowable-before-
      cutoff anchor) and --asof must be >= its asof; both refused loudly.

  (b) DISCOVERY — the BOUNDED deterministic strategy. The global "all living
      humans" SPARQL is documented-infeasible (the P31=Q5 clause 504-times-
      out WDQS — see stage1/harvest/wiki_people.py's discovery notes), so
      discovery enumerates en.wikipedia **Category:Living people** members
      via the MediaWiki API in its STABLE sortkey order (cmsort=sortkey,
      cmdir=asc, mainspace only), screening each scanned title:
      mononym/short display names (the eval underdetermined-entity mirror),
      unresolvable QIDs, treatment-release QID overlap; survivors get their
      sitelink count batch-fetched through the SHARED wbgetentities
      machinery (stage1.tools.fetch_wikidata_people.wbget_batch — reused,
      never duplicated; title->QID via the people harvester's own
      _discover_sample pageprops batching) and are assigned a decile by the
      drawn-50 boundaries. Scanning proceeds in chunks ONLY as far as needed
      to fill every quota (capped at ``max_scan``); the scan depth, every
      scanned row with its outcome, and any unfilled quota are recorded
      honestly. KNOWN BIAS, acknowledged not hidden: the scanned prefix is
      alphabetical by category sortkey, so the candidate universe
      over-represents early-sortkey surnames; the seeded selection
      (sha256(f"{seed}:{title}") lowest-first — eval's exact seeded-key
      construction) randomizes WITHIN the scanned prefix, not over the whole
      category. Recorded in the manifest notes.

      THE KNOWABILITY ANCHOR (principal ruling addendum, 2026-08-05) screens
      INSIDE the quota fill: each would-be selection's FIRST enwiki revision
      is probed (one cheap ids|timestamp call, checkpointed) and only an
      article created at/before the anchor date (default 2024-03-31 — before
      the earliest roster model cutoff ~June 2024) takes a quota slot; a
      too-recent or unverifiable article is skipped and the next-lowest hash
      backfills, scanning further when a decile's pool runs dry. Every probe
      is recorded in the sidecar's anchor audit; the derive-side
      ``control_anchor`` gate re-checks the frozen timestamp offline.

  (c) VERIFICATION EVIDENCE per selected person — the CURRENT article
      revision (newest at/before the pull --asof, day end) AND the CUTOFF-
      pinned revision (the knowability floor), full wikitext with
      revid/sha1/ts via the shared fetch_side; cached in the SAME row shape
      as the death snapshot's people_wikitext.jsonl ('cutoff'/'current'
      sides) so eval's zero-network held_sources jobs can read it verbatim.
      Resumable per title (.part checkpoint stamped with the window pins;
      stale-window rows refetched — the shared stale-checkpoint fix).

  (d) THE WIKIDATA DEATH-SCOPED STATE per selected person — the pull-date
      claims restricted to DEATH_PROPERTY_WHITELIST (P570/P20/P509/P119) via
      wbget_batch + fetch_current_state (all decode machinery imported).
      Batch-resilient: one unreachable batch degrades ONLY its own rows'
      verification (recorded; the adapter reviews), never the run. Label
      resolution is deliberately SKIPPED: the derive gate consumes only P570
      PRESENCE (a time-typed value that decodes without labels).

COVERAGE — cutoff_exact AND asof_exact: the 'before' anchors to the
treatment release's cutoff (verified equal at harvest) and both pinned
revisions resolve at the two exact day-end pins; a derive with any other
window would mean different evidence. back_datable=FALSE, honestly:
Category:Living people is a CURRENT-state category (members leave it as they
die), so this scan cannot be reconstructed for a past window — like FDA, the
harvest is only sound near the window it ran for. Sidecars carry NO
wall-clock (the snapshot's only wall-clock is snapshot_manifest.harvested_at)
so a re-harvest of byte-identical data yields a reproducible derive
fingerprint. NO LLM anywhere.

Politeness: enwiki calls (category pages, pageprops, pinned revisions) sleep
``enwiki_sleep`` (default 0.2 s, well under 5 req/s) with the shared clients'
own maxlag/backoff; wbgetentities batches at 50 ids with ``wd_sleep``
(default 1.0 s) between batches — the same pacing the people harvest runs.

``--sample "Title,Title"`` skips the category scan and treats the given
titles as the scanned universe (screens and decile assignment still apply;
quotas are reported unfilled) — the small-live-test route.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date
from pathlib import Path

from stage1.adapters.people_controls import (
    ANCHOR_BASIS,
    DEFAULT_ANCHOR_DATE,
    SCAN_FILENAME,
    SELECTED_FILENAME,
    SELECTION_SIDECAR_FILENAME,
    WD_CACHE_FILENAME,
    WD_SIDECAR_FILENAME,
    WIKITEXT_CACHE_FILENAME,
    WIKITEXT_SIDECAR_FILENAME,
    decile_boundaries,
    decile_histogram,
    decile_of,
    decile_quotas,
    selection_key,
    underdetermined_entity,
)
from stage1.adapters.wiki_people import DEATH_PROPERTY_WHITELIST
from stage1.harvest import Harvester
from stage1.harvest.snapshot import sha1_file
from stage1.harvest.sports import (
    DEFAULT_ENWIKI_SLEEP,
    DEFAULT_WD_SLEEP,
    _append_part,
    _load_part,
)
from stage1.harvest.wiki_people import (
    HARVESTER as PEOPLE_HARVESTER,
    _asof_pin,
    _cutoff_pin,
)
from stage1.tools.fetch_wikidata_p54 import qid_sort_key
from stage1.tools.fetch_wikidata_people import (
    BATCH_SIZE as WD_BATCH_SIZE,
    DECODER_VERSION,
    ENWIKI_API,
    USER_AGENT as WD_USER_AGENT,
    WIKIDATA_API,
    api_get as people_api_get,
    fetch_current_state,
    wbget_batch,
)
from stage1.tools.fetch_wiki_revisions import (
    API as ENWIKI_REV_API,
    RV_PROPS,
    USER_AGENT as ENWIKI_USER_AGENT,
    api_get as rev_api_get,
    fetch_side,
)

TOOL_VERSION = "harvest_people_controls:v1"

DEFAULT_DRAWS = "../2_faithfulness_eval/data/draws/draws.json"
DEFAULT_TREATMENT_RELEASE = "stage1/releases/dev-people"
TREATMENT_SOURCE = "wiki_people"
# The seed is the principal's ruling date — recorded in the manifest and in
# every selected row's provenance so the draw is regenerable.
DEFAULT_SEED = 20260805
DEFAULT_HEADROOM = 1.5
DEFAULT_MAX_SCAN = 40000
SCAN_CHUNK = 500  # cmlimit per category page (the API maximum for anon bots)

LIVING_CATEGORY = "Category:Living people"

# Checkpoint for the first-revision (knowability anchor) probes. Never a
# snapshot file: the probe results land on the selected rows and in the
# selection sidecar's anchor audit. A first-revision timestamp is immutable,
# so checkpoint rows carry no window pins (an errored row is re-probed).
ANCHOR_PART_FILENAME = "people_controls_anchors.part"

ALPHABETICAL_BIAS_NOTE = (
    "discovery scans Category:Living people in its stable sortkey order and "
    "stops once every decile quota is fillable, so the candidate universe is "
    "an ALPHABETICAL PREFIX of the category (early-sortkey surnames "
    "over-represented); the seeded selection randomizes within that prefix, "
    "not over the whole category"
)


def _iso(value) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


# --------------------------------------------------------------------------- #
# (a) the matching basis (no network)
# --------------------------------------------------------------------------- #
def load_matching_basis(draws_path, release_path, cutoff, asof,
                        headroom=DEFAULT_HEADROOM):
    """Read the eval draw and the treatment people release, and compute the
    decile machinery. Returns (basis, binding) or raises LookupError with a
    precise message — the runner's LookupError -> exit 2 path handles it
    loudly.

    Window rules (mirroring the sports control pull):
      * release manifest args.cutoff MUST EQUAL the requested cutoff — the
        controls anchor to the same knowable-before-cutoff world as the
        treatment;
      * release manifest args.asof MUST BE <= the requested asof — liveness
        is verified AT the pull, never before the treatment attestation.
    """
    draws_file = Path(draws_path)
    if not draws_file.is_file():
        raise LookupError(
            f"eval draws file {draws_file} not found: the matching basis is the "
            "DRAWN 50 treatment people (ruling 2026-08-05) — point --opt draws=… "
            "at eval/data/draws/draws.json"
        )
    try:
        draws = json.loads(draws_file.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise LookupError(f"eval draws file {draws_file} is not valid JSON: {exc}")
    people = (draws.get("domains") or {}).get("people") if isinstance(draws, dict) else None
    taken = people.get("taken") if isinstance(people, dict) else None
    if not isinstance(taken, list) or not taken:
        raise LookupError(
            f"{draws_file} has no domains.people.taken list: there is no drawn "
            "treatment set to match against"
        )

    base = Path(release_path)
    facts_path = base / "facts.jsonl"
    manifest_path = base / "manifest.json"
    if not facts_path.is_file() or not manifest_path.is_file():
        raise LookupError(
            f"treatment release {base} is not a built release (facts.jsonl/"
            "manifest.json missing): the drawn fact_ids resolve to records of the "
            "treatment people release — point --opt treatment_release=… at one"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise LookupError(f"treatment release manifest {manifest_path} is not valid JSON: {exc}")
    if not isinstance(manifest, dict) or manifest.get("source") != TREATMENT_SOURCE:
        raise LookupError(
            f"treatment release {base} has source {manifest.get('source')!r}, "
            f"expected {TREATMENT_SOURCE!r} (the people controls match the people "
            "treatment)"
        )
    args = manifest.get("args") if isinstance(manifest.get("args"), dict) else {}
    release_cutoff = args.get("cutoff")
    release_asof = args.get("asof")
    if release_cutoff != _iso(cutoff):
        raise LookupError(
            f"--cutoff {_iso(cutoff)} does not equal the treatment release's cutoff "
            f"{release_cutoff!r}: controls anchor to the same knowable-before-cutoff "
            f"world as the treatment (pass --cutoff {release_cutoff}, or a different "
            "--opt treatment_release=…)"
        )
    if not isinstance(release_asof, str) or release_asof > _iso(asof):
        raise LookupError(
            f"--asof {_iso(asof)} is earlier than the treatment release's asof "
            f"{release_asof!r}: verifying controls BEFORE the treatment attestation "
            "is not a control pull"
        )

    by_fact_id = {}
    treatment_qids = set()
    with open(facts_path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise LookupError(
                    f"treatment release {facts_path} line {line_no} is unparseable "
                    f"({exc}): refusing to compute a matching basis from a corrupt release"
                )
            if not isinstance(record, dict):
                continue
            fid = record.get("fact_id")
            if isinstance(fid, str):
                by_fact_id[fid] = record
            entity = record.get("entity") if isinstance(record.get("entity"), dict) else {}
            ids = entity.get("ids") if isinstance(entity.get("ids"), dict) else {}
            qid = ids.get("wikidata_qid")
            if not qid:
                prov = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
                page = prov.get("page") if isinstance(prov.get("page"), dict) else {}
                qid = page.get("qid")
            if isinstance(qid, str) and qid:
                treatment_qids.add(qid)

    missing = sorted(f for f in taken if f not in by_fact_id)
    if missing:
        raise LookupError(
            f"{len(missing)} drawn fact_id(s) are not in the treatment release "
            f"{base}: {missing[:5]}{'…' if len(missing) > 5 else ''} — the draw and "
            "the release disagree; refusing to compute a matching basis"
        )

    drawn = []
    for fid in taken:
        record = by_fact_id[fid]
        prov = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
        finder = prov.get("finder") if isinstance(prov.get("finder"), dict) else {}
        recog = prov.get("recognisability") if isinstance(prov.get("recognisability"), dict) else {}
        sitelinks = finder.get("sitelinks")
        if not isinstance(sitelinks, int) or isinstance(sitelinks, bool):
            sitelinks = recog.get("sitelinks")
        if not isinstance(sitelinks, int) or isinstance(sitelinks, bool):
            raise LookupError(
                f"drawn fact {fid} ({(record.get('entity') or {}).get('name')!r}) "
                "carries no integer sitelink count in provenance.finder/"
                "recognisability: the decile basis cannot be computed"
            )
        entity = record.get("entity") if isinstance(record.get("entity"), dict) else {}
        ids = entity.get("ids") if isinstance(entity.get("ids"), dict) else {}
        drawn.append({
            "fact_id": fid,
            "title": entity.get("name"),
            "qid": ids.get("wikidata_qid"),
            "sitelinks": sitelinks,
        })

    counts = [d["sitelinks"] for d in drawn]
    try:
        boundaries = decile_boundaries(counts)
    except ValueError as exc:
        raise LookupError(f"decile boundaries uncomputable: {exc}")
    target_hist = decile_histogram(counts, boundaries)
    quotas = decile_quotas(target_hist, headroom)

    basis = {
        "drawn": drawn,
        "boundaries": boundaries,
        "target_hist": target_hist,
        "quotas": quotas,
        "treatment_qids": sorted(treatment_qids, key=qid_sort_key),
        "drawn_qids": sorted({d["qid"] for d in drawn if d["qid"]}, key=qid_sort_key),
    }
    binding = {
        "draws_path": str(draws_path),
        "draws_sha1": sha1_file(draws_file),
        "treatment_release": str(release_path),
        "treatment_facts_sha1": sha1_file(facts_path),
        "treatment_manifest_sha1": sha1_file(manifest_path),
        "treatment_release_cutoff": release_cutoff,
        "treatment_release_asof": release_asof,
        "drawn_n": len(drawn),
        "headroom": headroom,
    }
    return basis, binding


# --------------------------------------------------------------------------- #
# (b) seeded quota selection with the knowability anchor (pure given a probe)
# --------------------------------------------------------------------------- #
def anchor_outcome(probe_row, anchor_date) -> str:
    """'anchored' | 'anchor_too_recent' | 'anchor_unavailable' for one probe
    result ({'first_rev_ts', 'first_revid', 'error'?}) against the anchor
    date (article created at/before it passes — principal ruling addendum
    2026-08-05). Pure and total."""
    ts = probe_row.get("first_rev_ts") if isinstance(probe_row, dict) else None
    if isinstance(ts, str) and ts:
        return "anchored" if ts[:10] <= anchor_date else "anchor_too_recent"
    return "anchor_unavailable"


def select_candidates(candidates, quotas, seed, anchor_date, anchor_probe):
    """The seeded deterministic selection over the scanned prefix, with the
    knowability-anchor screen and quota backfill: per decile, candidates
    sorted by (sha256(f'{seed}:{title}'), title) lowest-first; each is
    anchor-probed lazily (``anchor_probe(title)`` -> {'first_rev_ts',
    'first_revid', 'error'?}, cached by the caller) and only an ANCHORED
    candidate (article created at/before ``anchor_date``) takes a quota slot
    — a too-recent or unverifiable article is skipped and the next-lowest
    hash backfills, so anchored candidates fill the deciles. A QID already
    selected in an earlier decile is skipped (one entity can never appear
    twice via a redirect pair). ``quotas=None`` selects every anchored
    candidate (the --sample path; quotas are then reported unfilled, never
    faked). Pure given the probe — returns (selected_rows sorted by title,
    per_decile_selected_counts)."""
    by_decile: dict = {}
    for cand in candidates:
        by_decile.setdefault(cand["decile"], []).append(cand)
    selected = []
    selected_hist = [0] * 10
    seen_qids: set = set()
    for k in range(1, 11):
        quota = None if quotas is None else quotas[k - 1]
        if quota is not None and quota <= 0:
            continue
        pool = sorted(by_decile.get(k, []),
                      key=lambda c: (selection_key(seed, c["title"]), c["title"]))
        for cand in pool:
            if quota is not None and selected_hist[k - 1] >= quota:
                break
            if cand["qid"] in seen_qids:
                continue
            probe_row = anchor_probe(cand["title"])
            if anchor_outcome(probe_row, anchor_date) != "anchored":
                continue  # recorded via the probe cache's anchor audit
            seen_qids.add(cand["qid"])
            row = dict(cand)
            row["selection_hash"] = selection_key(seed, cand["title"])
            row["first_rev_ts"] = probe_row.get("first_rev_ts")
            row["first_revid"] = probe_row.get("first_revid")
            selected.append(row)
            selected_hist[k - 1] += 1
    selected.sort(key=lambda c: c["title"])
    return selected, selected_hist


def unfilled_quotas(selected_hist, quotas) -> dict:
    """{decile: deficit} for every decile whose quota the selection did not
    fill (honest reporting, and the scan loop's continue signal)."""
    return {str(k): quotas[k - 1] - selected_hist[k - 1]
            for k in range(1, 11) if selected_hist[k - 1] < quotas[k - 1]}


class PeopleControlsHarvester(Harvester):
    source = "people_controls"
    tool_version = TOOL_VERSION

    # -- coverage (PURE, no network) ---------------------------------------
    def coverage(self, cfg: dict) -> dict:
        return {
            "cutoff": _iso(cfg["cutoff"]),
            "asof": _iso(cfg["asof"]),
            # 'before' anchors to the treatment release's cutoff (verified
            # equal at harvest) and BOTH pinned revisions resolve at the two
            # exact day-end pins; any other window means different evidence.
            "cutoff_exact": True,
            "asof_exact": True,
            "precision": "day",
            "window_basis": "treatment_release_cutoff;liveness_verified_at_asof",
            # Category:Living people is a CURRENT-state category (the dead
            # leave it): the scan cannot be reconstructed for a past window —
            # like FDA, this harvest is only sound near the window it ran for.
            "back_datable": False,
        }

    # -- harvest (the ONLY networked method) -------------------------------
    def harvest(self, cfg: dict, writer, ctx: dict) -> None:
        cutoff = cfg["cutoff"]
        asof = cfg["asof"]
        resume = bool(cfg.get("resume"))
        enwiki_sleep = float(cfg.get("enwiki_sleep", DEFAULT_ENWIKI_SLEEP))
        wd_sleep = float(cfg.get("wd_sleep", DEFAULT_WD_SLEEP))
        seed = int(cfg.get("seed", DEFAULT_SEED))
        headroom = float(cfg.get("headroom", DEFAULT_HEADROOM))
        max_scan = int(cfg.get("max_scan", DEFAULT_MAX_SCAN))
        draws_path = cfg.get("draws") or DEFAULT_DRAWS
        release_path = cfg.get("treatment_release") or DEFAULT_TREATMENT_RELEASE
        http_errors: list = []

        # ---- (a) the matching basis (no network) --------------------------
        basis, binding = load_matching_basis(draws_path, release_path, cutoff,
                                             asof, headroom)
        print(f"[harvest people_controls] basis: {binding['drawn_n']} drawn treatment "
              f"people; boundaries={basis['boundaries']}; "
              f"target={basis['target_hist']}; quotas={basis['quotas']} "
              f"(headroom {headroom}, seed {seed})", file=sys.stderr, flush=True)

        # ---- (b) discovery: bounded category scan (or --sample), with the
        #      knowability-anchor screen filling quotas via backfill ---------
        anchor_date = str(cfg.get("anchor_date") or DEFAULT_ANCHOR_DATE)
        anchor_cache, anchor_probe = self._make_anchor_probe(
            writer.out_dir, resume, enwiki_sleep, http_errors)
        sample = cfg.get("sample")
        if sample:
            titles = sorted({t.strip() for t in sample if t and t.strip()})
            if not titles:
                raise LookupError("people_controls --sample produced no titles")
            discovery = "sample_titles"
            scan_rows, candidates = self._classify(titles, 0, basis, wd_sleep,
                                                   http_errors)
            scan_depth = len(scan_rows)
            category_exhausted = False
            # A smoke test selects every surviving anchored sampled candidate;
            # quotas are reported unfilled, never faked.
            selected, selected_hist = select_candidates(
                candidates, None, seed, anchor_date, anchor_probe)
        else:
            discovery = "categorymembers_living_people"
            (scan_rows, candidates, scan_depth, category_exhausted,
             selected, selected_hist) = self._scan(
                basis, seed, anchor_date, anchor_probe, max_scan, resume,
                writer.out_dir, enwiki_sleep, wd_sleep, http_errors)
        unfilled = unfilled_quotas(selected_hist, basis["quotas"])
        anchor_audit, anchor_stats = self._anchor_audit(
            anchor_cache, anchor_date, selected)
        print(f"[harvest people_controls] discovery={discovery}: scanned "
              f"{scan_depth}, {len(candidates)} candidate(s), {len(selected)} "
              f"selected {selected_hist}; anchors {anchor_stats}"
              + (f"; UNFILLED quotas {unfilled}" if unfilled else ""),
              file=sys.stderr, flush=True)

        # ---- (c) pinned revisions for the selected pool --------------------
        wikitext_rows = self._fetch_wikitext(selected, cutoff, asof,
                                             writer.out_dir, resume,
                                             enwiki_sleep, http_errors)

        # ---- (d) death-scoped Wikidata states ------------------------------
        wd_rows = self._fetch_wd_states(selected, wd_sleep, http_errors)

        stats = {
            "discovery": discovery,
            "scan_depth": scan_depth,
            "category_exhausted": category_exhausted,
            "candidates": len(candidates),
            "selected": len(selected),
            "selected_hist": selected_hist,
            "unfilled_quotas": unfilled,
            "anchor_date": anchor_date,
            "anchor_stats": anchor_stats,
            "titles_fetched": len(wikitext_rows),
            "wikitext_fetch_errors": sum(1 for r in wikitext_rows if r.get("fetch_errors")),
            "wd_rows": len(wd_rows),
            "wd_rows_with_errors": sum(1 for r in wd_rows if r.get("errors")),
            "http_errors": http_errors,
            "resumed": resume,
        }
        self._emit(writer, cfg, discovery, basis, binding, seed, headroom,
                   max_scan, anchor_date, anchor_audit, anchor_stats,
                   scan_rows, scan_depth, category_exhausted,
                   selected, selected_hist, unfilled, wikitext_rows, wd_rows,
                   enwiki_sleep, wd_sleep, stats)

        for name in (WIKITEXT_CACHE_FILENAME + ".part", SCAN_FILENAME + ".part",
                     ANCHOR_PART_FILENAME):
            part = writer.out_dir / name
            if part.exists():
                part.unlink()

    # -- (b) the bounded category scan --------------------------------------
    def _scan(self, basis, seed, anchor_date, anchor_probe, max_scan, resume,
              out_dir, enwiki_sleep, wd_sleep, http_errors):
        """Enumerate Category:Living people in stable sortkey order, classify
        each member, and stop as soon as every decile quota is filled by
        ANCHORED candidates (or max_scan / the category end is reached) —
        the anchored selection runs after every chunk with its probes cached,
        so an anchor-screened candidate is backfilled by the next-lowest hash
        and, when a decile's scanned pool runs dry, by scanning further.
        Checkpointed per chunk into SCAN_FILENAME.part (rows + the cmcontinue
        token), stamped with {seed, boundaries} pins so a checkpoint from
        another basis is ignored, never silently reused. Returns (scan_rows,
        candidates, scan_depth, category_exhausted, selected,
        selected_hist)."""
        pins = {"seed": seed, "boundaries": basis["boundaries"]}
        part_path = out_dir / (SCAN_FILENAME + ".part")
        scan_rows, cmcontinue, exhausted = ([], None, False)
        if resume:
            scan_rows, cmcontinue, exhausted = self._load_scan_part(part_path, pins)
        if scan_rows:
            print(f"[harvest people_controls] scan: resumed {len(scan_rows)} "
                  f"scanned row(s) (cmcontinue={'set' if cmcontinue else 'none'})",
                  file=sys.stderr, flush=True)
        candidates = [dict(r) for r in scan_rows if r.get("status") == "candidate"]
        seen_titles = {r["title"] for r in scan_rows}

        selected, selected_hist = select_candidates(
            candidates, basis["quotas"], seed, anchor_date, anchor_probe)
        with open(part_path, "a", encoding="utf-8", newline="\n") as part:
            while (not exhausted and len(scan_rows) < max_scan
                   and unfilled_quotas(selected_hist, basis["quotas"])):
                members, cmcontinue = self._category_page(cmcontinue, enwiki_sleep)
                new_titles = [t for t in members
                              if t not in seen_titles]
                seen_titles.update(new_titles)
                rows, cands = self._classify(new_titles, len(scan_rows), basis,
                                             wd_sleep, http_errors)
                for row in rows:
                    _append_part(part, {**row, "pins": pins})
                scan_rows.extend(rows)
                candidates.extend(cands)
                _append_part(part, {"checkpoint": True, "title": "",
                                    "cmcontinue": cmcontinue, "pins": pins})
                if cmcontinue is None:
                    exhausted = True
                selected, selected_hist = select_candidates(
                    candidates, basis["quotas"], seed, anchor_date, anchor_probe)
                deficit = unfilled_quotas(selected_hist, basis["quotas"])
                print(f"[harvest people_controls]   scanned {len(scan_rows)} "
                      f"({len(candidates)} candidate(s); anchored-selected "
                      f"{sum(selected_hist)}"
                      + (f"; deficits {deficit}" if deficit else "; quotas FILLED")
                      + ")", file=sys.stderr, flush=True)
        return (scan_rows, candidates, len(scan_rows), exhausted,
                selected, selected_hist)

    # -- the knowability-anchor probe (principal ruling addendum 2026-08-05) --
    def _make_anchor_probe(self, out_dir, resume, sleep, http_errors):
        """(cache, probe): ``probe(title)`` returns the article's
        first-revision row ({'first_rev_ts', 'first_revid'} or an 'error'),
        fetching once, caching, and appending every probe to the anchor .part
        checkpoint (a first-revision timestamp is immutable, so checkpoint
        rows carry no window pins; errored rows are re-probed on resume)."""
        cache: dict = {}
        part_path = out_dir / ANCHOR_PART_FILENAME
        if resume and part_path.is_file():
            with open(part_path, encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue  # torn last line of an interrupted append
                    if (isinstance(row, dict) and isinstance(row.get("title"), str)
                            and not row.get("error")):
                        cache[row["title"]] = {k: row.get(k) for k in
                                               ("first_rev_ts", "first_revid")}
            if cache:
                print(f"[harvest people_controls] anchors: resumed "
                      f"{len(cache)} probe(s)", file=sys.stderr, flush=True)

        def probe(title):
            if title in cache:
                return cache[title]
            row = self._fetch_first_revision(title, sleep, http_errors)
            cache[title] = row
            with open(part_path, "a", encoding="utf-8", newline="\n") as part:
                _append_part(part, {"title": title, **row})
            return row

        return cache, probe

    @staticmethod
    def _fetch_first_revision(title, sleep, http_errors):
        """The article's FIRST revision (rvdir=newer, rvlimit=1 — ids and
        timestamp only, no content) via the shared enwiki client. A
        persistent failure degrades to a recorded error row (-> the candidate
        is skipped at selection and the gate would review), never a crash."""
        try:
            payload = rev_api_get({
                "action": "query", "prop": "revisions", "titles": title,
                "rvprop": "ids|timestamp", "rvlimit": "1", "rvdir": "newer",
            })
        except RuntimeError as exc:
            http_errors.append({"stage": "first_revision", "title": title,
                                "error": str(exc)})
            time.sleep(sleep)
            return {"first_rev_ts": None, "first_revid": None,
                    "error": f"fetch failed after retries: {exc}"}
        time.sleep(sleep)
        pages = payload.get("query", {}).get("pages", [])
        if not pages:
            return {"first_rev_ts": None, "first_revid": None,
                    "error": "empty API response (no pages)"}
        page = pages[0]
        if page.get("invalid"):
            return {"first_rev_ts": None, "first_revid": None,
                    "error": f"invalid title: {page.get('invalidreason', '')!r}"}
        if page.get("missing"):
            return {"first_rev_ts": None, "first_revid": None,
                    "error": "page missing (deleted or never existed)"}
        revs = page.get("revisions") or []
        if not revs:
            return {"first_rev_ts": None, "first_revid": None,
                    "error": "no revisions returned"}
        return {"first_rev_ts": revs[0].get("timestamp"),
                "first_revid": revs[0].get("revid")}

    @staticmethod
    def _anchor_audit(anchor_cache, anchor_date, selected):
        """(audit_rows, stats) over EVERY probe made — complete and honest:
        each probed title with its first-revision result, its outcome against
        the anchor date, and whether it was selected. Sorted by title
        (deterministic given the frozen probe results)."""
        selected_titles = {c["title"] for c in selected}
        audit = []
        stats = {"probed": 0, "anchored": 0, "anchor_too_recent": 0,
                 "anchor_unavailable": 0}
        for title in sorted(anchor_cache):
            row = anchor_cache[title]
            outcome = anchor_outcome(row, anchor_date)
            stats["probed"] += 1
            stats[outcome if outcome != "anchored" else "anchored"] += 1
            entry = {"title": title, "outcome": outcome,
                     "first_rev_ts": row.get("first_rev_ts"),
                     "first_revid": row.get("first_revid"),
                     "selected": title in selected_titles}
            if row.get("error"):
                entry["error"] = row["error"]
            audit.append(entry)
        return audit, stats

    def _category_page(self, cmcontinue, sleep):
        """One Category:Living people page (mainspace members, stable sortkey
        order) -> (titles, next_cmcontinue | None). FATAL on a persistent API
        failure (the shared discovery rule: aborting beats silently
        under-covering the candidate universe)."""
        params = {
            "action": "query", "list": "categorymembers",
            "cmtitle": LIVING_CATEGORY, "cmprop": "title",
            "cmnamespace": "0", "cmtype": "page",
            "cmlimit": str(SCAN_CHUNK), "cmsort": "sortkey", "cmdir": "asc",
            "formatversion": "2",
        }
        if cmcontinue:
            params["cmcontinue"] = cmcontinue
        payload = people_api_get(ENWIKI_API, params)
        time.sleep(sleep)
        if "error" in payload:
            raise RuntimeError(
                f"categorymembers discovery failed ({payload['error']}): the "
                "living-people universe is unreachable, so the snapshot would "
                "silently UNDER-COVER discovery — aborting (rerun with --resume; "
                "the scan checkpoint continues from its cmcontinue token)"
            )
        members = [m.get("title")
                   for m in payload.get("query", {}).get("categorymembers", [])
                   if isinstance(m, dict) and isinstance(m.get("title"), str)]
        return members, payload.get("continue", {}).get("cmcontinue")

    def _classify(self, titles, index_base, basis, wd_sleep, http_errors):
        """Screen + decile-assign one batch of scanned titles. Screens in
        cost order: the mononym mirror (pure), then QID resolution (the
        people harvester's own pageprops batching, reused), the treatment
        QID overlap, then sitelink counts (the shared wbget_batch). Every
        title yields exactly one scan row naming its outcome — recorded,
        never silently dropped. Returns (scan_rows, candidates)."""
        treatment_qids = set(basis["treatment_qids"])
        rows = []
        to_resolve = []
        for i, title in enumerate(titles):
            row = {"scan_index": index_base + i, "title": title}
            if underdetermined_entity(title):
                row["status"] = "excluded_mononym"
                rows.append(row)
                continue
            to_resolve.append(row)
            rows.append(row)

        resolved = {}
        if to_resolve:
            cands = self._resolve_qids([r["title"] for r in to_resolve],
                                       wd_sleep, http_errors)
            resolved = {c["title"]: c for c in cands}
        need_sitelinks = []
        for row in to_resolve:
            cand = resolved.get(row["title"], {})
            qid = cand.get("qid")
            row["title_status"] = cand.get("title_status")
            if not qid:
                row["status"] = "qid_unresolved"
                continue
            row["qid"] = qid
            if qid in treatment_qids:
                row["status"] = "excluded_treatment_overlap"
                continue
            need_sitelinks.append(row)

        sitelink_counts = self._sitelink_counts(
            [r["qid"] for r in need_sitelinks], wd_sleep, http_errors)
        candidates = []
        for row in need_sitelinks:
            n = sitelink_counts.get(row["qid"])
            if not isinstance(n, int):
                row["status"] = "sitelinks_unknown"
                continue
            row["sitelinks"] = n
            row["decile"] = decile_of(n, basis["boundaries"])
            row["status"] = "candidate"
            candidates.append({"title": row["title"], "qid": row["qid"],
                               "sitelinks": n, "decile": row["decile"],
                               "scan_index": row["scan_index"]})
        return rows, candidates

    def _resolve_qids(self, titles, sleep, http_errors):
        """title -> QID via the people harvester's batched pageprops
        resolution (reused verbatim — the shared machinery, never
        duplicated)."""
        return PEOPLE_HARVESTER._discover_sample(titles, sleep, http_errors)

    def _sitelink_counts(self, qids, sleep, http_errors):
        """qid -> sitelink count via the shared wbget_batch (props=sitelinks
        only — the cheap scan fetch; the full death-scoped claims fetch runs
        only for SELECTED people). Batch-resilient: a failed batch records
        its qids in http_errors and yields no counts (those scan rows become
        'sitelinks_unknown', recorded not dropped)."""
        counts: dict = {}
        ordered = sorted(set(qids), key=qid_sort_key)
        for i in range(0, len(ordered), WD_BATCH_SIZE):
            batch = ordered[i:i + WD_BATCH_SIZE]
            try:
                entities = wbget_batch(batch, "sitelinks", {}, sleep)
            except RuntimeError as exc:
                http_errors.append({"stage": "sitelink_counts",
                                    "qids": list(batch), "error": str(exc)})
                continue
            for qid in batch:
                ent = entities.get(qid)
                if isinstance(ent, dict) and isinstance(ent.get("sitelinks"), dict):
                    counts[qid] = len(ent["sitelinks"])
        return counts

    @staticmethod
    def _load_scan_part(path, pins):
        """(scan_rows, last_cmcontinue, exhausted) from a scan checkpoint.
        Rows stamped with OTHER pins (another seed/boundaries basis) are
        ignored wholesale — the stale-checkpoint fix; a torn last line is
        skipped."""
        rows = []
        cmcontinue = None
        exhausted = False
        if not path.is_file():
            return rows, cmcontinue, exhausted
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # torn last line of an interrupted append
                if not isinstance(row, dict) or row.get("pins") != pins:
                    continue
                if row.get("checkpoint"):
                    cmcontinue = row.get("cmcontinue")
                    exhausted = cmcontinue is None
                    continue
                row = {k: v for k, v in row.items() if k != "pins"}
                rows.append(row)
        rows.sort(key=lambda r: r.get("scan_index", 0))
        return rows, cmcontinue, exhausted

    # -- (c) pinned revisions ------------------------------------------------
    def _fetch_wikitext(self, selected, cutoff, asof, out_dir, resume, sleep,
                       http_errors):
        """One row per selected title, in the death snapshot's
        people_wikitext.jsonl shape: {'title', 'cutoff': side, 'current':
        side, 'fetch_errors'} — the cutoff side is the knowability floor, the
        current side the liveness evidence and eval's future held-source.
        Resumable: a .part row with no fetch errors AND a matching window pin
        is reused; an errored or other-window row is refetched (the shared
        stale-checkpoint fix)."""
        cutoff_pin = _cutoff_pin(cutoff)
        asof_pin = _asof_pin(asof)
        pins = {"before": cutoff_pin, "after": asof_pin}
        part_path = out_dir / (WIKITEXT_CACHE_FILENAME + ".part")
        part_rows = _load_part(part_path) if resume else {}
        done = {t: r for t, r in part_rows.items()
                if not r.get("fetch_errors") and r.get("pins") == pins}
        stale = sum(1 for r in part_rows.values() if r.get("pins") != pins)
        todo = [c for c in selected if c["title"] not in done]
        print(f"[harvest people_controls] revisions: {len(done)} resumed "
              f"({stale} checkpoint row(s) from another window ignored), "
              f"{len(todo)} to fetch (cutoff<={cutoff_pin}, current<={asof_pin})",
              file=sys.stderr, flush=True)

        results = dict(done)
        with open(part_path, "a", encoding="utf-8", newline="\n") as part:
            for i, cand in enumerate(todo, 1):
                title = cand["title"]
                cutoff_side, cutoff_err = self._fetch_revision(
                    title, cutoff_pin, "cutoff_revision", sleep, http_errors)
                current_side, current_err = self._fetch_revision(
                    title, asof_pin, "current_revision", sleep, http_errors)
                fetch_errors = []
                if cutoff_err:
                    fetch_errors.append(f"cutoff: {cutoff_err}")
                if current_err:
                    fetch_errors.append(f"current: {current_err}")
                row = {"title": title, "pins": pins, "cutoff": cutoff_side,
                       "current": current_side, "fetch_errors": fetch_errors}
                results[title] = row
                _append_part(part, row)
                if i % 25 == 0 or i == len(todo):
                    print(f"[harvest people_controls]   pinned {i}/{len(todo)} title(s)",
                          file=sys.stderr, flush=True)
        # Deterministic order; the .part 'pins' stamp stays out of the frozen
        # cache row (checkpoint metadata, not evidence). NOTE: a cutoff-side
        # 'no revision at or before' outcome is EVIDENCE (page created after
        # the cutoff -> the precutoff_presence gate fails it), so a row with
        # ONLY that error is still a complete fetch — but the .part reuse
        # test above keys on fetch_errors, so such rows refetch on resume:
        # one redundant polite call, never a wrong pin.
        rows = []
        for title in sorted(results):
            row = results[title]
            rows.append({"title": row["title"], "cutoff": row.get("cutoff"),
                         "current": row.get("current"),
                         "fetch_errors": row.get("fetch_errors") or []})
        return rows

    @staticmethod
    def _fetch_revision(title, pin, stage, sleep, http_errors):
        """fetch_side with the shared relabel-to-row-timestamp convention;
        a RuntimeError (endpoint unreachable after retries) degrades to a
        recorded fetch error, never a crash."""
        try:
            side, err = fetch_side(title, pin, "row")
        except RuntimeError as exc:
            http_errors.append({"stage": stage, "title": title, "error": str(exc)})
            time.sleep(sleep)
            return None, f"fetch failed after retries: {exc}"
        time.sleep(sleep)
        if side is not None:
            side = dict(side)
            side["pinned_ts"] = side.get("ts")
        return side, err

    # -- (d) death-scoped Wikidata states ------------------------------------
    def _fetch_wd_states(self, selected, sleep, http_errors):
        """One row per selected person: the pull-date death-scoped Wikidata
        state ({'title', 'qid', 'sitelinks', 'current': STATE_BLOCK,
        'errors'}). Batch-resilient like every shared wbgetentities consumer.
        Label resolution is deliberately skipped: the derive gate consumes
        only P570 PRESENCE (time-typed, decodes without labels)."""
        whitelist = list(DEATH_PROPERTY_WHITELIST)
        by_qid = {c["qid"]: c for c in selected}
        qids = sorted(by_qid, key=qid_sort_key)
        unresolved_types: set = set()
        states: dict = {}
        for i in range(0, len(qids), WD_BATCH_SIZE):
            batch = qids[i:i + WD_BATCH_SIZE]
            try:
                entities = wbget_batch(batch, "info|claims|sitelinks", {}, sleep)
            except RuntimeError as exc:
                http_errors.append({"stage": "wd_current_state", "qids": list(batch),
                                    "error": str(exc)})
                for qid in batch:
                    states[qid] = (None, "current_not_fetched")
                continue
            for qid in batch:
                states[qid] = fetch_current_state(qid, entities.get(qid),
                                                  unresolved_types, whitelist)
        rows = []
        for qid in qids:
            cand = by_qid[qid]
            state, err = states.get(qid, (None, "current_not_fetched"))
            rows.append({
                "title": cand["title"],
                "qid": qid,
                "sitelinks": cand.get("sitelinks"),
                "current": state if isinstance(state, dict) else None,
                "errors": [err] if err else [],
            })
        return rows

    # -- snapshot emission (PURE given already-fetched rows) -----------------
    def _emit(self, writer, cfg, discovery, basis, binding, seed, headroom,
              max_scan, anchor_date, anchor_audit, anchor_stats,
              scan_rows, scan_depth, category_exhausted, selected,
              selected_hist, unfilled, wikitext_rows, wd_rows, enwiki_sleep,
              wd_sleep, stats) -> None:
        """Route everything through the SnapshotWriter: four caches via
        add_jsonl, three sidecars via add_json (sha1-pinned, NO wall-clock).
        No network — tests drive this with fixture rows."""
        out_dir = writer.out_dir

        writer.add_jsonl(SCAN_FILENAME, scan_rows)
        scan_sha1 = sha1_file(out_dir / SCAN_FILENAME)

        selected_rows = [{"title": c["title"], "qid": c["qid"],
                          "sitelinks": c["sitelinks"], "decile": c["decile"],
                          "selection_hash": c["selection_hash"],
                          "first_rev_ts": c.get("first_rev_ts"),
                          "first_revid": c.get("first_revid"),
                          "scan_index": c.get("scan_index")}
                         for c in selected]
        writer.add_jsonl(SELECTED_FILENAME, selected_rows)
        selected_sha1 = sha1_file(out_dir / SELECTED_FILENAME)

        writer.add_json(SELECTION_SIDECAR_FILENAME, {
            "tool_version": TOOL_VERSION,
            **binding,
            "seed": seed,
            "selection_key": "sha256('{seed}:{title}') lowest-first "
                             "(eval seeded_key construction)",
            "decile_boundaries": basis["boundaries"],
            "target_hist": basis["target_hist"],
            "quotas": basis["quotas"],
            "selected_hist": selected_hist,
            "unfilled_quotas": unfilled,
            "anchor_date": anchor_date,
            "anchor_basis": ANCHOR_BASIS,
            "anchor_stats": anchor_stats,
            "anchor_audit": anchor_audit,
            "discovery": discovery,
            "category": LIVING_CATEGORY,
            "scan_depth": scan_depth,
            "category_exhausted": category_exhausted,
            "max_scan": max_scan,
            "notes": [ALPHABETICAL_BIAS_NOTE],
            "treatment_qids": basis["treatment_qids"],
            "drawn_qids": basis["drawn_qids"],
            "scan_file": SCAN_FILENAME,
            "scan_file_sha1": scan_sha1,
            "selected_file": SELECTED_FILENAME,
            "selected_file_sha1": selected_sha1,
        })

        writer.add_jsonl(WIKITEXT_CACHE_FILENAME, wikitext_rows)
        wt_sha1 = sha1_file(out_dir / WIKITEXT_CACHE_FILENAME)
        err_titles = sorted(r["title"] for r in wikitext_rows if r.get("fetch_errors"))
        writer.add_json(WIKITEXT_SIDECAR_FILENAME, {
            "tool_version": TOOL_VERSION,
            "endpoint": ENWIKI_REV_API,
            "user_agent": ENWIKI_USER_AGENT,
            "query_params": {
                "action": "query", "prop": "revisions", "rvprop": RV_PROPS,
                "rvslots": "main", "rvlimit": "1", "rvdir": "older",
                "rvstart": "cutoff=treatment cutoff day end, current=pull asof "
                           "day end; cache pinned to each revision's own timestamp",
                "maxlag": "5",
            },
            "pins": {
                "cutoff": _cutoff_pin(cfg["cutoff"]),
                "current": _asof_pin(cfg["asof"]),
            },
            "selected_file": SELECTED_FILENAME,
            "selected_file_sha1": selected_sha1,
            "counts": {
                "titles": len(wikitext_rows),
                "rows_ok": len(wikitext_rows) - len(err_titles),
                "rows_with_fetch_errors": len(err_titles),
            },
            "fetch_error_titles": err_titles,
            "cache_sha1": wt_sha1,
        })

        writer.add_jsonl(WD_CACHE_FILENAME, wd_rows)
        wd_sha1 = sha1_file(out_dir / WD_CACHE_FILENAME)
        writer.add_json(WD_SIDECAR_FILENAME, {
            "tool_version": TOOL_VERSION,
            "decoder_version": DECODER_VERSION,
            "endpoints": {"enwiki": ENWIKI_API, "wikidata": WIKIDATA_API},
            "user_agent": WD_USER_AGENT,
            "property_whitelist": list(DEATH_PROPERTY_WHITELIST),
            "query_params": {
                "current_call": {"action": "wbgetentities",
                                 "props": "info|claims|sitelinks",
                                 "batch_size": WD_BATCH_SIZE},
                "labels": "not resolved (the derive gate consumes only P570 "
                          "presence — time-typed, no labels needed)",
            },
            "selected_file": SELECTED_FILENAME,
            "selected_file_sha1": selected_sha1,
            "counts": {
                "rows": len(wd_rows),
                "rows_with_errors": sum(1 for r in wd_rows if r.get("errors")),
            },
            "cache_sha1": wd_sha1,
        })

        writer.set_params({
            "discovery": discovery,
            "category": LIVING_CATEGORY,
            "enwiki_endpoint": ENWIKI_API,
            "enwiki_rev_endpoint": ENWIKI_REV_API,
            "wikidata_endpoint": WIKIDATA_API,
            "enwiki_user_agent": ENWIKI_USER_AGENT,
            "wikidata_user_agent": WD_USER_AGENT,
            "wd_decoder_version": DECODER_VERSION,
            "death_property_whitelist": list(DEATH_PROPERTY_WHITELIST),
            "seed": seed,
            "headroom": headroom,
            "max_scan": max_scan,
            "scan_depth": scan_depth,
            "category_exhausted": category_exhausted,
            "decile_boundaries": basis["boundaries"],
            "target_hist": basis["target_hist"],
            "quotas": basis["quotas"],
            "selected_hist": selected_hist,
            "unfilled_quotas": unfilled,
            "anchor_date": anchor_date,
            "anchor_basis": ANCHOR_BASIS,
            "anchor_stats": anchor_stats,
            "draws_path": binding["draws_path"],
            "draws_sha1": binding["draws_sha1"],
            "treatment_release": binding["treatment_release"],
            "treatment_facts_sha1": binding["treatment_facts_sha1"],
            "treatment_manifest_sha1": binding["treatment_manifest_sha1"],
            "cutoff_pin": _cutoff_pin(cfg["cutoff"]),
            "asof_pin": _asof_pin(cfg["asof"]),
            "enwiki_sleep_s": enwiki_sleep,
            "wd_sleep_s": wd_sleep,
            "notes": [ALPHABETICAL_BIAS_NOTE],
            "sample": sorted({c["title"] for c in selected}) if cfg.get("sample") else None,
            "files": [SCAN_FILENAME, SELECTED_FILENAME, WIKITEXT_CACHE_FILENAME,
                      WD_CACHE_FILENAME],
        })
        writer.set_stats(stats)
        print(f"[harvest people_controls] DONE. scanned {stats['scan_depth']}; "
              f"{stats['candidates']} candidate(s); {stats['selected']} selected; "
              f"{stats['titles_fetched']} pinned "
              f"({stats['wikitext_fetch_errors']} with fetch errors); "
              f"{stats['wd_rows']} WD row(s); "
              f"{len(stats['http_errors'])} http miss(es).",
              file=sys.stderr, flush=True)


HARVESTER = PeopleControlsHarvester()
