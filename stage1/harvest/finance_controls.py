"""Finance CONTROL-pull harvester (source 'finance_controls') — the third
lane of the control pipeline (ruling 2026-08-05: finance controls PINNED TO
Q1 2024, false side = Q4-2023, gap > 10% gated once at build time).

``python3 -m stage1.harvest --source finance_controls --cutoff 2024-01-01
--asof 2024-03-31 --contact you@example.com`` writes a FROZEN snapshot the
OFFLINE finance_controls adapter (stage1/adapters/finance_controls.py)
derives from. Three steps:

  (a) THE POOL (no network) — ruling A7 "same instrument": the entities of
      the CURRENT finance treatment take. Read the eval draw
      (``--opt draws=…``, default eval/data/draws/draws.json)
      ``domains.finance.taken`` and resolve each fact id to its entity +
      ticker via the eval facts file (``--opt facts=…``, default
      eval/data/facts/facts.jsonl), deduped by ticker via the adapter's
      shared ``select_pool_entities`` (single source of truth). Both eval
      files are READ-ONLY here; their sha1s are recorded so the pool is
      bound to exact bytes. The requested window must EQUAL the ruled pin —
      cutoff 2024-01-01, asof 2024-03-31 (the pinned calendar quarter) —
      anything else is refused loudly.

  (b) THE TICKER→CIK AUTHORITY — SEC's company_tickers.json, fetched once
      and pinned VERBATIM into the snapshot; every pool ticker resolves
      through the adapter's shared ``resolve_cik`` (map primary, the
      treatment fact's own CIK as the recorded fallback).

  (c) THE EVIDENCE — per resolved entity, EDGAR companyfacts
      (data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json), from which the
      us-gaap revenue-concept USD entries (the adapter's CONCEPT_CHAIN) are
      extracted, filtered to the period-end window that can carry Q1-2024,
      Q4-2023 and the FY-2023 subtraction components. Resumable per ticker
      (.part checkpoint stamped with the quarter pin + CIK + extract
      version; stale rows refetched — the shared stale-checkpoint fix).

COVERAGE — cutoff_exact AND asof_exact at the ruled pin: the snapshot only
ever attests the Q1-2024/Q4-2023 pair, so a derive with any other window
would mean a different (never-harvested) quarter. back_datable is True —
EDGAR is a permanent archive, historical filings never move (which is also
why there is NO probe-time pull for controls: the gap is gated ONCE at
derive). Sidecars carry NO wall-clock (the snapshot's only wall-clock is
snapshot_manifest.harvested_at). NO LLM anywhere.

Politeness: all traffic through the shared EdgarClient (proper contact
User-Agent — REQUIRED here, EDGAR's access rules — retry/backoff, rate-limit
body sentinel). SEC declares ~10 req/s; this harvester is far gentler and
CAPS itself at 2 req/s (~66 requests total) regardless of --max-rps.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

from stage1.adapters.finance_controls import (
    COMPANYFACTS_URL_TEMPLATE,
    CONCEPT_CHAIN,
    ENTRY_END_MAX,
    ENTRY_END_MIN,
    EXTRACT_VERSION,
    FACTS_FILENAME,
    FACTS_SIDECAR_FILENAME,
    PINNED_ASOF,
    PINNED_CUTOFF,
    PINNED_QUARTER_LABEL,
    POOL_FILENAME,
    POOL_SIDECAR_FILENAME,
    TICKER_MAP_FILENAME,
    TICKER_MAP_SIDECAR_FILENAME,
    UNIT,
    load_ticker_map,
    resolve_cik,
    select_pool_entities,
    taken_finance_fact_ids,
)
from stage1.harvest import Harvester
from stage1.harvest.http import EdgarClient, HttpError
from stage1.harvest.sec import _append_part, _load_part
from stage1.harvest.snapshot import sha1_file

TOOL_VERSION = "harvest_finance_controls:v1"

DEFAULT_DRAWS = "../2_faithfulness_eval/data/draws/draws.json"
DEFAULT_FACTS = "../2_faithfulness_eval/data/facts/facts.jsonl"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"

# SEC declares ~10 req/s; be far gentler. This cap overrides any larger
# --max-rps (the shared EdgarClient additionally caps at 8).
MAX_RPS_CAP = 2.0


def _iso(value) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def require_pinned_window(cutoff, asof) -> None:
    """The ruled pin, enforced loudly: this harvester can only ever attest
    the Q1-2024 calendar quarter (ruling 2026-08-05). Any other window is a
    different control set that was never ruled — refuse, never remap."""
    if _iso(cutoff) != PINNED_CUTOFF or _iso(asof) != PINNED_ASOF:
        raise LookupError(
            f"finance controls are PINNED to {PINNED_QUARTER_LABEL} (ruling "
            f"2026-08-05): the window must be --cutoff {PINNED_CUTOFF} --asof "
            f"{PINNED_ASOF} (the pinned calendar quarter), got --cutoff "
            f"{_iso(cutoff)} --asof {_iso(asof)} — a different window would be a "
            "different, unruled control set"
        )


def load_pool(draws_path, facts_path):
    """Read the eval draw + facts (READ-ONLY) and build the deduped pool.
    Returns (pool_rows, binding, problems) or raises LookupError with a
    precise message (missing files, corrupt JSON, taken id absent from
    facts) — the runner's LookupError -> exit 2 path handles it loudly."""
    draws_path = Path(draws_path)
    facts_path = Path(facts_path)
    if not draws_path.is_file():
        raise LookupError(
            f"eval draw file not found: {draws_path} — the control universe is the "
            "current finance treatment take (pass --opt draws=…)"
        )
    if not facts_path.is_file():
        raise LookupError(
            f"eval facts file not found: {facts_path} — taken fact ids resolve to "
            "entities through it (pass --opt facts=…)"
        )
    try:
        draws = json.loads(draws_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise LookupError(f"eval draw file {draws_path} is not valid JSON: {exc}")
    taken = taken_finance_fact_ids(draws)

    facts_by_id: dict = {}
    with open(facts_path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise LookupError(
                    f"eval facts file {facts_path} line {line_no} is unparseable "
                    f"({exc}): refusing to select a pool from a corrupt facts file"
                )
            if isinstance(row, dict) and isinstance(row.get("fact_id"), str):
                facts_by_id[row["fact_id"]] = row
    pool_rows, problems = select_pool_entities(taken, facts_by_id)
    binding = {
        "draws_file": str(draws_path),
        "draws_sha1": sha1_file(draws_path),
        "facts_file": str(facts_path),
        "facts_sha1": sha1_file(facts_path),
        "taken_fact_ids": len(taken),
        "pool_rows": len(pool_rows),
        "selection": {
            "domain": "finance",
            "list": "domains.finance.taken",
            "dedup_key": "ticker",
            "ruling": "A7 same-instrument universe = the current finance treatment take",
        },
    }
    return pool_rows, binding, problems


def extract_concepts(companyfacts: dict) -> dict:
    """The compact frozen extract of one companyfacts payload: for each
    CONCEPT_CHAIN concept, its USD duration entries with a period end inside
    [ENTRY_END_MIN, ENTRY_END_MAX] — everything the offline derivation can
    read (Q1-2024, Q4-2023, and the FY-2023 subtraction components), nothing
    else. Entries keep start/end/val/accn/fy/fp/form/filed/frame verbatim and
    are deterministically sorted. Pure and total."""
    gaap = ((companyfacts or {}).get("facts") or {}).get("us-gaap") or {}
    out: dict = {}
    for name in CONCEPT_CHAIN:
        units = ((gaap.get(name) or {}).get("units") or {})
        entries = units.get(UNIT) or []
        kept = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            end = e.get("end")
            if not (isinstance(end, str) and ENTRY_END_MIN <= end <= ENTRY_END_MAX):
                continue
            kept.append({k: e.get(k) for k in
                         ("start", "end", "val", "accn", "fy", "fp", "form",
                          "filed", "frame")})
        if kept:
            kept.sort(key=lambda e: (str(e.get("end")), str(e.get("start")),
                                     str(e.get("filed")), str(e.get("accn")),
                                     str(e.get("val")), str(e.get("frame"))))
            out[name] = kept
    return out


class FinanceControlsHarvester(Harvester):
    source = "finance_controls"
    tool_version = TOOL_VERSION

    # -- coverage (PURE, no network) ---------------------------------------
    def coverage(self, cfg: dict) -> dict:
        return {
            "cutoff": _iso(cfg["cutoff"]),
            "asof": _iso(cfg["asof"]),
            # the snapshot attests exactly the ruled Q1-2024/Q4-2023 pair; a
            # derive with any other window would mean a different (never
            # harvested) quarter -> both edges exact.
            "cutoff_exact": True,
            "asof_exact": True,
            "precision": "day",
            "window_basis": "pinned_calendar_quarter",
            # EDGAR is a permanent archive: the candidate set (historical
            # filings) is fully reconstructable at any later pull date.
            "back_datable": True,
        }

    # -- harvest (the ONLY networked method) -------------------------------
    def harvest(self, cfg: dict, writer, ctx: dict) -> None:
        require_pinned_window(cfg["cutoff"], cfg["asof"])
        contact = cfg.get("contact")
        if not contact:
            raise LookupError(
                "the finance_controls harvester requires --contact (EDGAR's access "
                "rules require a descriptive User-Agent with a real contact)"
            )
        resume = bool(cfg.get("resume"))
        try:
            requested_rps = float(cfg.get("max_rps") or MAX_RPS_CAP)
        except (TypeError, ValueError):
            requested_rps = MAX_RPS_CAP
        client = EdgarClient(contact, max_rps=min(requested_rps, MAX_RPS_CAP))
        http_errors: list = []

        # ---- (a) the pool (no network; eval files READ-ONLY) --------------
        draws_path = cfg.get("draws") or DEFAULT_DRAWS
        facts_path = cfg.get("facts") or DEFAULT_FACTS
        pool_rows, binding, pool_problems = load_pool(draws_path, facts_path)
        sample = cfg.get("sample")
        if sample:
            wanted = {t.strip().upper() for t in sample if t and t.strip()}
            have = {r["ticker"] for r in pool_rows if r.get("ticker")}
            unknown = sorted(wanted - have)
            if unknown:
                raise LookupError(
                    f"--sample ticker(s) not in the control pool: {unknown} (the pool "
                    "is the finance treatment take's entities; sample tickers must be "
                    "a subset)"
                )
            pool_rows = [r for r in pool_rows if r.get("ticker") in wanted]
            binding = dict(binding)
            binding["pool_rows"] = len(pool_rows)
            binding["sampled"] = sorted(wanted)
        print(f"[harvest finance_controls] pool: {len(pool_rows)} entity(ies) from "
              f"{draws_path} domains.finance.taken ({binding['taken_fact_ids']} fact ids)",
              file=sys.stderr, flush=True)

        # ---- (b) the pinned ticker→CIK authority --------------------------
        map_path = writer.out_dir / TICKER_MAP_FILENAME
        try:
            status, map_bytes = client.get_bytes(TICKER_MAP_URL, expect_json=True)
        except HttpError as exc:
            raise LookupError(
                f"SEC company_tickers.json could not be fetched ({exc}): without the "
                "ticker→CIK authority no entity can be resolved — aborting rather "
                "than writing an unresolvable snapshot"
            ) from exc
        if status == 404 or not map_bytes:
            raise LookupError(
                f"SEC company_tickers.json returned no data (HTTP {status}): the "
                "ticker→CIK authority is unavailable — aborting"
            )
        with open(map_path, "wb") as fh:
            fh.write(map_bytes)
        try:
            ticker_map = load_ticker_map(json.loads(map_bytes))
        except ValueError as exc:
            raise LookupError(f"SEC company_tickers.json is not valid JSON: {exc}")

        resolutions = {}
        for row in pool_rows:
            ticker = row.get("ticker")
            if not ticker:
                continue
            resolutions[ticker] = resolve_cik(ticker, ticker_map, row.get("facts_cik"))
        basis_hist: dict = {}
        for res in resolutions.values():
            key = res.get("basis") or "unresolved"
            if res.get("conflict"):
                key = "conflict"
            basis_hist[key] = basis_hist.get(key, 0) + 1
        print(f"[harvest finance_controls] cik resolution: {json.dumps(basis_hist, sort_keys=True)} "
              f"over {len(resolutions)} ticker(s), map size {len(ticker_map)}",
              file=sys.stderr, flush=True)

        # ---- (c) companyfacts (resumable) ---------------------------------
        cf_rows = self._fetch_companyfacts(
            client, pool_rows, resolutions, writer.out_dir, resume)

        # ---- emit snapshot files ------------------------------------------
        self._emit(writer, cfg, pool_rows, binding, pool_problems, map_path,
                   ticker_map, resolutions, basis_hist, cf_rows, client,
                   http_errors, resume)

        part = writer.out_dir / (FACTS_FILENAME + ".part")
        if part.exists():
            part.unlink()

    # -- (c) companyfacts fetch --------------------------------------------
    def _fetch_companyfacts(self, client, pool_rows, resolutions, out_dir, resume):
        """One row per pool ticker: the revenue-concept extract of that CIK's
        companyfacts. Resumable: a .part row with no fetch_errors AND a
        matching pin (quarter + CIK + extract version) is reused; an errored,
        other-CIK, or other-version row is refetched (the shared
        stale-checkpoint fix). An unresolved-CIK entity gets a no-fetch row
        (skipped_reason recorded) — never an HTTP call, never a silent drop."""
        part_path = out_dir / (FACTS_FILENAME + ".part")
        part_rows = _load_part(part_path, "ticker") if resume else {}

        def pins_for(cik10):
            return {"quarter": PINNED_QUARTER_LABEL, "cik": cik10,
                    "extract": EXTRACT_VERSION}

        todo = []
        done: dict = {}
        stale = 0
        for row in pool_rows:
            ticker = row.get("ticker")
            if not ticker:
                continue
            cik10 = (resolutions.get(ticker) or {}).get("cik10")
            prev = part_rows.get(ticker)
            if (isinstance(prev, dict) and not prev.get("fetch_errors")
                    and prev.get("pins") == pins_for(cik10)):
                done[ticker] = prev
            else:
                if isinstance(prev, dict):
                    stale += 1
                todo.append((ticker, cik10))
        print(f"[harvest finance_controls] companyfacts: {len(done)} resumed "
              f"({stale} checkpoint row(s) stale/errored, refetched), "
              f"{len(todo)} to fetch", file=sys.stderr, flush=True)

        results = dict(done)
        with open(part_path, "a", encoding="utf-8", newline="\n") as part:
            for i, (ticker, cik10) in enumerate(todo, 1):
                fetch_errors = []
                concepts: dict = {}
                entity_name_sec = None
                skipped_reason = None
                if not cik10:
                    skipped_reason = "cik_unresolved"
                else:
                    url = COMPANYFACTS_URL_TEMPLATE.format(cik10=cik10)
                    try:
                        data = client.get_json(url)
                    except HttpError as exc:
                        client.record_miss("companyfacts", url, exc)
                        fetch_errors.append(f"companyfacts: {exc}")
                        data = None
                    if data == {}:
                        fetch_errors.append(
                            "companyfacts: 404/empty (no XBRL facts for this CIK)")
                    elif isinstance(data, dict):
                        entity_name_sec = data.get("entityName") \
                            if isinstance(data.get("entityName"), str) else None
                        concepts = extract_concepts(data)
                row = {
                    "ticker": ticker,
                    "cik": cik10,
                    "entity_name_sec": entity_name_sec,
                    "concepts": concepts,
                    "fetch_errors": fetch_errors,
                    "pins": pins_for(cik10),
                }
                if skipped_reason:
                    row["skipped_reason"] = skipped_reason
                results[ticker] = row
                _append_part(part, row)
                if i % 10 == 0 or i == len(todo):
                    print(f"[harvest finance_controls]   fetched {i}/{len(todo)} "
                          "companyfacts", file=sys.stderr, flush=True)
        # Deterministic order; the .part 'pins' stamp stays out of the frozen
        # cache row (checkpoint metadata, not evidence).
        rows = []
        for ticker in sorted(results):
            row = dict(results[ticker])
            row.pop("pins", None)
            rows.append(row)
        return rows

    # -- snapshot emission (PURE given already-fetched rows) ---------------
    def _emit(self, writer, cfg, pool_rows, binding, pool_problems, map_path,
              ticker_map, resolutions, basis_hist, cf_rows, client,
              http_errors, resume) -> None:
        """Route everything through the SnapshotWriter: pool + companyfacts
        via add_jsonl, the ticker map verbatim via add_file, sidecars via
        add_json (sha1-pinned, NO wall-clock). No network — tests drive this
        with fixture rows."""
        out_dir = writer.out_dir

        writer.add_jsonl(POOL_FILENAME, pool_rows)
        pool_sha1 = sha1_file(out_dir / POOL_FILENAME)
        writer.add_json(POOL_SIDECAR_FILENAME, {
            "tool_version": TOOL_VERSION,
            **binding,
            "problems": pool_problems,
            "pool_file": POOL_FILENAME,
            "pool_file_sha1": pool_sha1,
        })

        writer.add_file(TICKER_MAP_FILENAME, map_path, rows=len(ticker_map))
        map_sha1 = sha1_file(out_dir / TICKER_MAP_FILENAME)
        unresolved = sorted(t for t, r in resolutions.items() if not r.get("cik10"))
        conflicts = sorted(t for t, r in resolutions.items() if r.get("conflict"))
        writer.add_json(TICKER_MAP_SIDECAR_FILENAME, {
            "tool_version": TOOL_VERSION,
            "endpoint": TICKER_MAP_URL,
            "user_agent": client.user_agent,
            "map_file": TICKER_MAP_FILENAME,
            "map_sha1": map_sha1,
            "counts": {
                "map_entries": len(ticker_map),
                "pool_tickers": len(resolutions),
                "resolution_basis": dict(sorted(basis_hist.items())),
            },
            "unresolved_tickers": unresolved,
            "conflict_tickers": conflicts,
        })

        writer.add_jsonl(FACTS_FILENAME, cf_rows)
        cf_sha1 = sha1_file(out_dir / FACTS_FILENAME)
        err_tickers = sorted(r["ticker"] for r in cf_rows if r.get("fetch_errors"))
        skipped = sorted(r["ticker"] for r in cf_rows if r.get("skipped_reason"))
        writer.add_json(FACTS_SIDECAR_FILENAME, {
            "tool_version": TOOL_VERSION,
            "endpoint": COMPANYFACTS_URL_TEMPLATE,
            "user_agent": client.user_agent,
            "concepts": list(CONCEPT_CHAIN),
            "unit": UNIT,
            "entry_end_window": [ENTRY_END_MIN, ENTRY_END_MAX],
            "extract_version": EXTRACT_VERSION,
            "pinned_quarter": {"label": PINNED_QUARTER_LABEL,
                               "calendar_start": PINNED_CUTOFF,
                               "calendar_end": PINNED_ASOF},
            "pool_file": POOL_FILENAME,
            "pool_file_sha1": pool_sha1,
            "counts": {
                "entities": len(cf_rows),
                "rows_ok": len(cf_rows) - len(err_tickers) - len(skipped),
                "rows_with_fetch_errors": len(err_tickers),
                "rows_skipped_no_cik": len(skipped),
            },
            "fetch_error_tickers": err_tickers,
            "skipped_tickers": skipped,
            "cache_sha1": cf_sha1,
        })

        writer.set_params({
            "draws_file": binding.get("draws_file"),
            "draws_sha1": binding.get("draws_sha1"),
            "facts_file": binding.get("facts_file"),
            "facts_sha1": binding.get("facts_sha1"),
            "selection": binding.get("selection"),
            "pinned_quarter": {"label": PINNED_QUARTER_LABEL,
                               "calendar_start": PINNED_CUTOFF,
                               "calendar_end": PINNED_ASOF},
            "ticker_map_endpoint": TICKER_MAP_URL,
            "companyfacts_endpoint": COMPANYFACTS_URL_TEMPLATE,
            "user_agent": client.user_agent,
            "rate_max_rps": client.max_rps,
            "sample": binding.get("sampled"),
            "files": [POOL_FILENAME, TICKER_MAP_FILENAME, FACTS_FILENAME],
        })
        stats = {
            "pool_rows": len(pool_rows),
            "pool_problems": pool_problems,
            "cik_resolution_basis": dict(sorted(basis_hist.items())),
            "companyfacts_fetched": len(cf_rows),
            "companyfacts_fetch_errors": len(err_tickers),
            "companyfacts_skipped_no_cik": len(skipped),
            "http_errors": http_errors + client.errors,
            "resumed": resume,
        }
        writer.set_stats(stats)
        print(f"[harvest finance_controls] DONE. {stats['pool_rows']} pool entity(ies); "
              f"{stats['companyfacts_fetched']} companyfacts row(s) "
              f"({stats['companyfacts_fetch_errors']} with fetch errors, "
              f"{stats['companyfacts_skipped_no_cik']} skipped without a CIK); "
              f"{len(stats['http_errors'])} http miss(es).",
              file=sys.stderr, flush=True)


HARVESTER = FinanceControlsHarvester()
