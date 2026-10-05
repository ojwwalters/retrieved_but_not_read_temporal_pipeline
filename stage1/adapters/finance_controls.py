"""Finance CONTROL adapter (source 'finance_controls').

RULING (human, 2026-08-05): finance controls are PINNED TO Q1 2024 — the
calendar quarter 2024-01-01..2024-03-31. Per control instrument the TRUE fact
is the entity's actual Q1-2024 quarterly revenue (immutable — filed in 2024);
the FALSE side's value is the ADJACENT quarter's (Q4-2023, ending 2023-12-31)
actual revenue — a real, wrong-for-that-quarter number mirroring the treatment
trap. Nothing is ever invented. A |Q1-2024 − Q4-2023| / Q1-2024 gap > 10% is
REQUIRED (the same tolerance×multiple logic as the treatment trap gate:
eval finance_grading tolerance_pct 5 × min_gap_multiple 2 — see
eval/src/live_truth.py trap_is_admissible) and is gated ONCE here at build
time, deterministically — historical filings never move, so there is NO
probe-time pull for controls. Rationale on the record: the control floor is a
within-model statistic and Q1-2024 filings predate the EARLIEST roster model
cutoff (~June 2024), so one shared set serves every model.

THE POOL (ruling A7 "same instrument"): the entities of the CURRENT finance
treatment take — eval/data/draws/draws.json ``domains.finance.taken``, each
fact id resolved to its entity + ticker via eval/data/facts/facts.jsonl and
deduped by ticker. The FULL verified pool ships in the release (no adapter-side
cap): the eval's seeded draw selects its ~50, exactly as it does for treatment.

Inputs (all read-only, under --data-dir — a finance_controls harvest snapshot
written by stage1/harvest/finance_controls.py):

* finance_controls_pool.jsonl        — one row per deduped pool entity
  (ticker, entity name, the eval-side CIK when the fact carried one, the
  backing fact ids). Enumerated in full; enumeration never decides inclusion.
* finance_controls_pool.meta.json    — sidecar binding the pool to the exact
  eval draw/facts bytes (draws.json + facts.jsonl sha1s, selection rule).
* finance_controls_ticker_map.json   — SEC's company_tickers.json, pinned
  verbatim (the ticker→CIK authority), + its .meta.json sidecar.
* finance_controls_companyfacts.jsonl — per entity, the us-gaap revenue-concept
  USD entries extracted from EDGAR companyfacts (start/end/val/accn/fy/fp/
  form/filed/frame), filtered to the period-end window that can carry Q1-2024,
  Q4-2023 and the FY-2023 subtraction components, + its sidecar.

Record shape: source 'finance_controls', property 'quarterly_revenue',
value_type 'quantity'. ``after`` is the TRUE side (the pinned Q1-2024 value),
``before`` is the FALSE side (the adjacent Q4-2023 value) — the exact mirror
of the treatment's quarterly_revenue records, whose after/before become the
eval's true/false claims. ``change_date`` is the RULING PIN 2024-03-31 (the
calendar quarter end; the entity's own matched fiscal period end is evidence,
never identity). ``provenance['population'] = 'control'`` and
``provenance['property_display'] = 'Q1 2024 quarterly revenue'`` carry the pin
explicitly so eval-side wording renders it.

VALUE DERIVATION (deterministic, offline, per entity — see
``resolve_control_values``):

* Concept fallback chain (CONCEPT_CHAIN, in order): the FIRST us-gaap concept
  for which BOTH quarters resolve cleanly wins; the chosen concept and every
  earlier concept's failure reason are RECORDED.
* Fiscal grids: targets map to PERIOD END DATES — the entity period end
  nearest each calendar target (2024-03-31 / 2023-12-31) within
  FISCAL_ALIGN_MAX_DAYS (45), the two matched ends one quarter apart
  (75–105 days). An entity whose grid does not align goes to REVIEW with the
  mismatch recorded, never silently mapped.
* Per-quarter value: a clean calendar frame (frame="CY2024Q1"/"CY2023Q4")
  is used when present (basis 'frame'); else the quarterly-duration entry at
  the matched end (basis 'quarterly_duration'); for Q4 only, when no discrete
  quarterly figure exists (the folded fiscal-Q4 of a 10-K), Q4 = the annual
  figure at the matched end minus its three interior quarters (basis
  'annual_minus_three_quarters', components recorded). If none is clean the
  entity goes to review, never guessed. Restatements: multiple filings of the
  same period use the latest-filed value; distinct values are recorded, and a
  spread beyond VALUE_CONFLICT_TOLERANCE_PCT (1%) is review ('value_conflict').

Gate order (first fail names the disposition; every gate always runs):

1. ``control_pool``     (review-only) — the row is a genuine pool member with
   a pool binding (draw+facts sha1s) and a ticker.
2. ``cik_resolution``   (review-only) — the ticker resolved to a CIK via the
   pinned SEC map (basis 'ticker_map'); an entity absent from the map but
   carrying the treatment fact's own CIK resolves with basis 'facts_cik'
   (recorded, not a guess); a map/facts disagreement or no CIK at all is
   review.
3. ``fiscal_alignment`` (review-only) — the entity's fiscal grid aligns with
   calendar Q1-2024 as above.
4. ``revenue_present``  (review-only) — both quarters resolved cleanly (the
   concept and derivation bases recorded).
5. ``gap_inadmissible`` — THE admissibility gate (named for its disposition,
   ``excluded:gap_inadmissible``, per the 2026-08-05 ruling): FAIL when
   |Q1−Q4|/|Q1|·100 <= GAP_THRESHOLD_PCT (10) — the trap and the correct
   answer would not be distinguishable under the eval's ±5% tolerance; the
   record keeps both values. A zero Q1 is likewise inadmissible.
6. ``evidence_resolvable`` / 7. ``dedup`` (shared, last).

Offline-only (--online raises). Deterministic: same snapshot, byte-identical
facts.jsonl. NO LLM anywhere.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date as _date
from pathlib import Path

import stage1.normalize.quantity  # noqa: F401  (registers 'quantity')
from stage1.adapters import Adapter
from stage1.adapters.finance import _is_num, _num_str
from stage1.adapters.sports_controls import SportsControlsAdapter
from stage1.gates import Gate
from stage1.gates.standard import DedupGate, EvidenceResolvableGate
from stage1.predictability import ANNOUNCED
from stage1.schema import (
    ChangeDate,
    Evidence,
    FactChangeRecord,
    GateResult,
    ValueState,
    compute_fact_id,
    compute_record_id,
)

SOURCE = "finance_controls"
PROPERTY = "quarterly_revenue"
VALUE_TYPE = "quantity"
POPULATION = "control"

# ---------------------------------------------------------------------------
# THE PIN (ruling 2026-08-05). These constants are the single source of truth,
# imported by the harvester — the pull job and the derive can never disagree
# about which quarter the controls attest.
# ---------------------------------------------------------------------------
PINNED_QUARTER_LABEL = "Q1 2024"
PINNED_CUTOFF = "2024-01-01"          # calendar quarter start (harvest --cutoff)
PINNED_ASOF = "2024-03-31"            # calendar quarter end   (harvest --asof)
Q1_TARGET_END = "2024-03-31"          # TRUE side's calendar period end
FALSE_QUARTER_LABEL = "Q4 2023"
Q4_TARGET_END = "2023-12-31"          # FALSE side's calendar period end
Q1_FRAME = "CY2024Q1"                 # EDGAR's own calendar-quarter frame tags
Q4_FRAME = "CY2023Q4"
PROPERTY_DISPLAY = "Q1 2024 quarterly revenue"

# The deterministic us-gaap concept fallback chain. 'Revenues' first (the
# total-revenue umbrella tag — the closest match to the treatment's Polygon
# income-statement 'revenues'), then the ASC-606 contract-revenue tags most
# filers use instead, then the financial-sector and legacy tags. The FIRST
# concept for which BOTH quarters resolve wins; everything is recorded.
CONCEPT_CHAIN = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "RevenuesNetOfInterestExpense",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
)
UNIT = "USD"

FISCAL_ALIGN_MAX_DAYS = 45            # ruling: "~45 days of calendar Q1-2024"
QUARTER_DURATION_DAYS = (80, 100)     # 13-week (91d) / 14-week (98d) / calendar
ANNUAL_DURATION_DAYS = (350, 380)     # 52/53-week and calendar fiscal years
ADJACENT_SPACING_DAYS = (75, 105)     # matched Q1/Q4 ends one quarter apart

# The admissibility threshold: eval finance_grading tolerance_pct (5) ×
# min_gap_multiple (2) — the same tolerance-×-multiple form as
# eval/src/live_truth.py trap_is_admissible, fixed here by the ruling.
GAP_THRESHOLD_PCT = 10.0
# Restated same-period values: latest-filed wins; a spread beyond this is
# review ('value_conflict'), never a silent pick between disagreeing filings.
VALUE_CONFLICT_TOLERANCE_PCT = 1.0

# The period-end window the harvester freezes (everything the derivation can
# read): Q1-2024 ends as late as mid-May-2024 fiscal grids, the FY-2023
# subtraction needs interior quarters ending from spring 2023, and next-year
# comparative re-reports of Q1-2024 keep the same period end.
ENTRY_END_MIN = "2023-01-01"
ENTRY_END_MAX = "2024-09-30"

# Version stamp for the harvest-side extraction (concept chain + filter);
# baked into the .part checkpoint pins so a stale-version checkpoint refetches.
EXTRACT_VERSION = "finance_controls_extract:v1"

# The fixed snapshot filenames this adapter reads by name (written by
# stage1/harvest/finance_controls.py).
POOL_FILENAME = "finance_controls_pool.jsonl"
POOL_SIDECAR_FILENAME = "finance_controls_pool.meta.json"
TICKER_MAP_FILENAME = "finance_controls_ticker_map.json"
TICKER_MAP_SIDECAR_FILENAME = "finance_controls_ticker_map.meta.json"
FACTS_FILENAME = "finance_controls_companyfacts.jsonl"
FACTS_SIDECAR_FILENAME = "finance_controls_companyfacts.meta.json"

COMPANYFACTS_URL_TEMPLATE = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"

POOL_CTX_KEY = "finance_controls_pool_meta"
TICKER_MAP_CTX_KEY = "finance_controls_ticker_map"
TICKER_MAP_INFO_CTX_KEY = "finance_controls_ticker_map_info"
FACTS_CTX_KEY = "finance_controls_companyfacts_by_ticker"
FACTS_INFO_CTX_KEY = "finance_controls_companyfacts_info"
LOAD_ERRORS_CTX_KEY = "input_load_errors"
EXTRA_INPUT_META_CTX_KEY = "extra_input_meta"


# ---------------------------------------------------------------------------
# Pool selection (pure — shared by the harvester and any auditor)
# ---------------------------------------------------------------------------

def taken_finance_fact_ids(draws: dict) -> list:
    """The eval draw's finance take (ruling A7 'same instrument' universe):
    ``domains.finance.taken``, order preserved. Raises LookupError when the
    draw does not carry it — a malformed draw must refuse, never yield an
    empty pool silently."""
    domains = draws.get("domains") if isinstance(draws, dict) else None
    finance = domains.get("finance") if isinstance(domains, dict) else None
    taken = finance.get("taken") if isinstance(finance, dict) else None
    if not isinstance(taken, list) or not taken:
        raise LookupError(
            "draws.json carries no non-empty domains.finance.taken list: "
            "the control universe (the current finance treatment take) cannot "
            "be selected"
        )
    return [t for t in taken if isinstance(t, str) and t]


def select_pool_entities(taken_ids, facts_by_id) -> tuple:
    """Resolve the taken fact ids to DEDUPED pool entity rows.

    Returns (pool_rows, problems). Each row:
        {"ticker", "entity_name", "display_name", "facts_cik",
         "fact_ids": [...], "properties": [...]}
    Dedup key is the ticker (two facts on one entity fold into one row, both
    fact ids kept). A taken id missing from facts.jsonl raises LookupError
    (corrupt draw/facts state — refuse loudly). A fact with no ticker still
    becomes a pool row (ticker None) and is recorded in problems — held for
    the control_pool gate, never silently dropped. Rows sorted by ticker."""
    by_key: dict = {}
    problems: list = []
    for fact_id in taken_ids:
        fact = facts_by_id.get(fact_id)
        if not isinstance(fact, dict):
            raise LookupError(
                f"taken finance fact id {fact_id!r} is not present in facts.jsonl: "
                "the draw and the facts file disagree — refusing to build a pool "
                "from inconsistent eval state"
            )
        entity = fact.get("entity") if isinstance(fact.get("entity"), dict) else {}
        ids = entity.get("ids") if isinstance(entity.get("ids"), dict) else {}
        ticker = ids.get("ticker") if isinstance(ids.get("ticker"), str) and ids.get("ticker") else None
        cik = ids.get("cik") if isinstance(ids.get("cik"), str) and ids.get("cik") else None
        name = entity.get("name") if isinstance(entity.get("name"), str) and entity.get("name") else None
        display = entity.get("display_name") if isinstance(entity.get("display_name"), str) else name
        prop = ((fact.get("meta") or {}).get("property")
                if isinstance(fact.get("meta"), dict) else None)
        if ticker is None:
            problems.append({"fact_id": fact_id, "problem": "fact has no ticker"})
        key = ticker if ticker is not None else f"__no_ticker__{fact_id}"
        row = by_key.get(key)
        if row is None:
            row = {"ticker": ticker, "entity_name": name, "display_name": display,
                   "facts_cik": cik, "fact_ids": [], "properties": []}
            by_key[key] = row
        row["fact_ids"].append(fact_id)
        if isinstance(prop, str) and prop and prop not in row["properties"]:
            row["properties"].append(prop)
        if row.get("facts_cik") is None and cik:
            row["facts_cik"] = cik
    rows = sorted(by_key.values(), key=lambda r: (r["ticker"] or "", r["fact_ids"][0]))
    for row in rows:
        row["fact_ids"] = sorted(row["fact_ids"])
        row["properties"] = sorted(row["properties"])
    return rows, problems


# ---------------------------------------------------------------------------
# Ticker → CIK resolution (pure — shared by the harvester)
# ---------------------------------------------------------------------------

def pad_cik(value):
    """Zero-padded 10-digit CIK string, or None when not coercible."""
    try:
        return f"{int(str(value).strip()):010d}"
    except (TypeError, ValueError):
        return None


def load_ticker_map(obj) -> dict:
    """{TICKER: {'cik10', 'title'}} from SEC's company_tickers.json shape
    ({"0": {"cik_str": ..., "ticker": ..., "title": ...}, ...}). First entry
    wins on a duplicate ticker (deterministic: rows iterated in integer key
    order). Malformed entries are skipped — the map is an authority file, not
    candidate data."""
    out: dict = {}
    if not isinstance(obj, dict):
        return out
    def _key(k):
        try:
            return (0, int(k))
        except (TypeError, ValueError):
            return (1, str(k))
    for key in sorted(obj, key=_key):
        entry = obj[key]
        if not isinstance(entry, dict):
            continue
        ticker = entry.get("ticker")
        cik10 = pad_cik(entry.get("cik_str"))
        if not (isinstance(ticker, str) and ticker and cik10):
            continue
        ticker = ticker.upper()
        if ticker not in out:
            out[ticker] = {"cik10": cik10,
                           "title": entry.get("title") if isinstance(entry.get("title"), str) else None}
    return out


def resolve_cik(ticker, ticker_map, facts_cik) -> dict:
    """One entity's CIK resolution, deterministically.

    Primary: the pinned SEC ticker map (basis 'ticker_map'). Fallback: the
    treatment fact's own CIK (basis 'facts_cik' — the eval pipeline's attested
    id, recorded, never a guess). A map hit that DISAGREES with a present
    facts CIK is a conflict (the gate reviews it). Returns
    {"cik10", "basis", "map_cik", "facts_cik", "conflict"}."""
    map_entry = ticker_map.get(ticker.upper()) if isinstance(ticker, str) else None
    map_cik = map_entry.get("cik10") if isinstance(map_entry, dict) else None
    facts_cik10 = pad_cik(facts_cik) if facts_cik else None
    if map_cik:
        return {"cik10": map_cik, "basis": "ticker_map", "map_cik": map_cik,
                "facts_cik": facts_cik10,
                "conflict": bool(facts_cik10 and facts_cik10 != map_cik)}
    if facts_cik10:
        return {"cik10": facts_cik10, "basis": "facts_cik", "map_cik": None,
                "facts_cik": facts_cik10, "conflict": False}
    return {"cik10": None, "basis": None, "map_cik": None,
            "facts_cik": facts_cik10, "conflict": False}


# ---------------------------------------------------------------------------
# Quarter-value resolution (pure, total — the offline derivation core)
# ---------------------------------------------------------------------------

def _iso(value):
    if not isinstance(value, str):
        return None
    try:
        _date.fromisoformat(value)
    except ValueError:
        return None
    return value


def _days(a_iso: str, b_iso: str) -> int:
    return (_date.fromisoformat(a_iso) - _date.fromisoformat(b_iso)).days


def _norm_entries(entries) -> list:
    """Duration entries with valid iso start/end and a finite numeric val,
    annotated with 'duration_days'. Everything else is dropped here (a
    malformed EDGAR entry is not a candidate value)."""
    out = []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        start, end = _iso(e.get("start")), _iso(e.get("end"))
        if not (start and end) or not _is_num(e.get("val")):
            continue
        row = dict(e)
        row["duration_days"] = _days(end, start)
        out.append(row)
    return out


def _quarterly(entries) -> list:
    lo, hi = QUARTER_DURATION_DAYS
    return [e for e in entries if lo <= e["duration_days"] <= hi]


def _annual(entries) -> list:
    lo, hi = ANNUAL_DURATION_DAYS
    return [e for e in entries if lo <= e["duration_days"] <= hi]


def match_end(candidate_ends, target_end_iso):
    """The candidate period end nearest the calendar target within
    FISCAL_ALIGN_MAX_DAYS: (end_iso, signed_delta_days) or (None, None).
    Tie on |delta| breaks to the LATER end (fresher period), deterministically."""
    best = None
    for end in sorted(set(candidate_ends)):
        delta = _days(end, target_end_iso)
        if abs(delta) > FISCAL_ALIGN_MAX_DAYS:
            continue
        if best is None or abs(delta) < abs(best[1]) or (
                abs(delta) == abs(best[1]) and end > best[0]):
            best = (end, delta)
    return best if best is not None else (None, None)


def _entry_brief(e) -> dict:
    return {"start": e.get("start"), "end": e.get("end"), "val": e.get("val"),
            "accn": e.get("accn"), "filed": e.get("filed"), "form": e.get("form"),
            "fy": e.get("fy"), "fp": e.get("fp"), "frame": e.get("frame")}


def _pick_latest_filed(hits):
    """(chosen, alternates, conflict_pct). Latest-filed entry wins (tie:
    lexicographically larger accn). Distinct values across the hits are the
    alternates; conflict_pct is the value spread as a % of the chosen value
    (0.0 when all agree or the chosen value is 0)."""
    ordered = sorted(hits, key=lambda e: (e.get("filed") or "", e.get("accn") or ""))
    chosen = ordered[-1]
    vals = sorted({float(e["val"]) for e in ordered})
    conflict_pct = 0.0
    if len(vals) > 1 and float(chosen["val"]) != 0:
        conflict_pct = (vals[-1] - vals[0]) / abs(float(chosen["val"])) * 100.0
    alternates = [_entry_brief(e) for e in ordered[:-1]
                  if float(e["val"]) != float(chosen["val"])]
    return chosen, alternates, conflict_pct


def _frame_end(qents, frame, target_end_iso):
    """The single period end EDGAR's own calendar frame designates, when the
    concept carries one within the alignment window; None otherwise (multiple
    distinct frame ends are defensive impossibility → fall through)."""
    ends = sorted({e["end"] for e in qents
                   if e.get("frame") == frame
                   and abs(_days(e["end"], target_end_iso)) <= FISCAL_ALIGN_MAX_DAYS})
    return ends[0] if len(ends) == 1 else None


def _resolve_quarter_direct(qents, matched_end, frame):
    """One quarter's value from the discrete quarterly entries at the matched
    end. Basis 'frame' when EDGAR's calendar frame designates this end,
    else 'quarterly_duration'; the VALUE is the latest-filed entry at the end
    either way (so a restatement filed later — which carries no frame tag —
    still surfaces, with the original recorded as an alternate). Returns a
    result dict or None when no discrete entry exists at the matched end."""
    hits = [e for e in qents if e["end"] == matched_end]
    if not hits:
        return None
    frame_designated = any(e.get("frame") == frame for e in hits)
    chosen, alternates, conflict_pct = _pick_latest_filed(hits)
    result = {
        "value": float(chosen["val"]),
        "basis": "frame" if frame_designated else "quarterly_duration",
        "matched_end": matched_end,
        "entry": _entry_brief(chosen),
        "restated": bool(alternates),
        "alternates": alternates,
        "conflict_pct": round(conflict_pct, 6),
    }
    if conflict_pct > VALUE_CONFLICT_TOLERANCE_PCT:
        result["conflict"] = True
    return result


def _resolve_q4_subtraction(aents, qents, matched_end):
    """Q4 = the annual figure ending at the matched end minus its three
    interior quarters (the folded fiscal-Q4 of a 10-K). Requires exactly three
    distinct interior quarter ends — anything else is a named failure, never
    a guess. Returns a result dict or {"failure": reason, ...}."""
    annual_hits = [e for e in aents if e["end"] == matched_end]
    if not annual_hits:
        return {"failure": "no_annual_at_matched_end"}
    annual, a_alternates, a_conflict = _pick_latest_filed(annual_hits)
    if a_conflict > VALUE_CONFLICT_TOLERANCE_PCT:
        return {"failure": "annual_value_conflict",
                "annual": _entry_brief(annual), "conflict_pct": round(a_conflict, 6)}
    a_start, a_end = annual["start"], annual["end"]
    interior_ends = sorted({e["end"] for e in qents
                            if a_start < e["end"] < a_end and e["start"] >= a_start})
    if len(interior_ends) != 3:
        return {"failure": "annual_interior_quarters_incomplete",
                "annual": _entry_brief(annual), "interior_ends_found": interior_ends}
    components = []
    total = 0.0
    for end in interior_ends:
        hits = [e for e in qents if e["end"] == end and a_start <= e["start"]]
        chosen, _, conflict_pct = _pick_latest_filed(hits)
        if conflict_pct > VALUE_CONFLICT_TOLERANCE_PCT:
            return {"failure": "interior_quarter_value_conflict", "quarter_end": end,
                    "conflict_pct": round(conflict_pct, 6)}
        components.append(_entry_brief(chosen))
        total += float(chosen["val"])
    value = float(annual["val"]) - total
    return {
        "value": value,
        "basis": "annual_minus_three_quarters",
        "matched_end": matched_end,
        "entry": _entry_brief(annual),
        "components": {"annual": _entry_brief(annual), "interior_quarters": components},
        "restated": bool(a_alternates),
        "alternates": a_alternates,
        "conflict_pct": round(a_conflict, 6),
    }


def resolve_concept(entries) -> dict:
    """Resolve BOTH pinned quarters from one concept's entries.

    Returns {"ok", "reason", "alignment", "q1", "q4"}; ok only when the fiscal
    grid aligns AND both values resolved without conflict. Pure and total."""
    ents = _norm_entries(entries)
    qents = _quarterly(ents)
    aents = _annual(ents)
    result = {"ok": False, "reason": None, "alignment": None, "q1": None, "q4": None}
    if not qents:
        result["reason"] = "no_quarterly_entries"
        return result

    q_ends = [e["end"] for e in qents]
    a_ends = [e["end"] for e in aents]
    # Matched ends: EDGAR's own calendar frame designation wins when present,
    # else the nearest period end within the alignment window.
    q1_end = _frame_end(qents, Q1_FRAME, Q1_TARGET_END)
    q1_end, q1_delta = ((q1_end, _days(q1_end, Q1_TARGET_END)) if q1_end
                        else match_end(q_ends, Q1_TARGET_END))
    q4_end = _frame_end(qents, Q4_FRAME, Q4_TARGET_END)
    q4_end, q4_delta = ((q4_end, _days(q4_end, Q4_TARGET_END)) if q4_end
                        else match_end(q_ends + a_ends, Q4_TARGET_END))

    alignment = {
        "q1_target_end": Q1_TARGET_END, "q1_matched_end": q1_end, "q1_delta_days": q1_delta,
        "q4_target_end": Q4_TARGET_END, "q4_matched_end": q4_end, "q4_delta_days": q4_delta,
        "spacing_days": _days(q1_end, q4_end) if q1_end and q4_end else None,
        "max_delta_days": FISCAL_ALIGN_MAX_DAYS,
        "aligned": False,
    }
    result["alignment"] = alignment
    if q1_end is None:
        alignment["reason"] = "q1_no_period_end_within_window"
        result["reason"] = "fiscal_mismatch:q1_no_period_end_within_window"
        return result
    if q4_end is None:
        alignment["reason"] = "q4_no_period_end_within_window"
        result["reason"] = "fiscal_mismatch:q4_no_period_end_within_window"
        return result
    lo, hi = ADJACENT_SPACING_DAYS
    if not (lo <= alignment["spacing_days"] <= hi):
        alignment["reason"] = "matched_ends_not_adjacent_quarters"
        result["reason"] = "fiscal_mismatch:matched_ends_not_adjacent_quarters"
        return result
    alignment["aligned"] = True

    q1 = _resolve_quarter_direct(qents, q1_end, Q1_FRAME)
    if q1 is None:
        result["reason"] = "q1_no_entry_at_matched_end"
        return result
    if q1.get("conflict"):
        result["q1"] = q1
        result["reason"] = "q1_value_conflict"
        return result
    result["q1"] = q1

    q4 = _resolve_quarter_direct(qents, q4_end, Q4_FRAME)
    if q4 is not None and q4.get("conflict"):
        result["q4"] = q4
        result["reason"] = "q4_value_conflict"
        return result
    if q4 is None:
        q4 = _resolve_q4_subtraction(aents, qents, q4_end)
        if "failure" in q4:
            result["reason"] = f"q4_{q4['failure']}"
            result["q4"] = None
            result["q4_failure"] = q4
            return result
    result["q4"] = q4
    result["ok"] = True
    return result


def resolve_control_values(concepts_by_name) -> dict:
    """The full deterministic derivation for one entity: walk CONCEPT_CHAIN in
    order, choose the FIRST concept for which resolve_concept succeeds, and
    record every earlier concept's failure. The alignment diagnosis is the
    chosen concept's, or the first concept's that got far enough (so a
    fiscal-mismatch review names the actual grid seen)."""
    attempts = []
    diagnosis_alignment = None
    for name in CONCEPT_CHAIN:
        entries = (concepts_by_name or {}).get(name)
        if not entries:
            attempts.append({"concept": name, "ok": False, "reason": "no_entries"})
            continue
        res = resolve_concept(entries)
        attempts.append({"concept": name, "ok": res["ok"], "reason": res.get("reason")})
        if diagnosis_alignment is None and res.get("alignment") is not None:
            diagnosis_alignment = res["alignment"]
        if res["ok"]:
            return {"status": "ok", "concept": name, "attempts": attempts,
                    "alignment": res["alignment"], "q1": res["q1"], "q4": res["q4"]}
    return {"status": "unresolved", "concept": None, "attempts": attempts,
            "alignment": diagnosis_alignment, "q1": None, "q4": None}


def gap_pct(q1_value, q4_value):
    """|Q1 − Q4| / |Q1| · 100 — the trap-gap form of trap_is_admissible (the
    TRUE side is the denominator). None when Q1 is 0 (no gap is computable)."""
    q1 = float(q1_value)
    if q1 == 0:
        return None
    return abs(q1 - float(q4_value)) / abs(q1) * 100.0


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

class FinanceControlPoolGate(Gate):
    """Review-only structural check that the enumerated row IS a pool member:
    a deduped entity row with a ticker, at least one backing treatment fact
    id, and a pool binding (draws+facts sha1s) in the sidecar. Anything else
    is held for a human — never silently included or dropped."""

    name = "control_pool"
    version = "control_pool:finance_v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        pool_meta = ctx.get(POOL_CTX_KEY) if isinstance(ctx.get(POOL_CTX_KEY), dict) else {}
        prov_pool = (record.provenance.get("pool")
                     if isinstance(record.provenance, dict) else None) or {}
        ids = record.entity.get("ids") if isinstance(record.entity, dict) else {}
        ticker = ids.get("ticker") if isinstance(ids, dict) else None
        problems = []
        if not (isinstance(ticker, str) and ticker):
            problems.append("pool row has no ticker")
        if not prov_pool.get("fact_ids"):
            problems.append("pool row carries no backing treatment fact ids")
        if not pool_meta.get("draws_sha1"):
            problems.append("no pool binding: eval draws.json sha1 is missing")
        if not pool_meta.get("facts_sha1"):
            problems.append("no pool binding: eval facts.jsonl sha1 is missing")
        evidence = {
            "selection": "eval draws domains.finance.taken, deduped by ticker",
            "ticker": ticker,
            "fact_ids": prov_pool.get("fact_ids"),
            "draws_sha1": pool_meta.get("draws_sha1"),
            "facts_sha1": pool_meta.get("facts_sha1"),
        }
        if problems:
            evidence["problem"] = "; ".join(problems)
            return GateResult(name=self.name, version=self.version,
                              verdict="review", evidence=evidence)
        return GateResult(name=self.name, version=self.version,
                          verdict="pass", evidence=evidence)


class CikResolutionGate(Gate):
    """Review-only: the entity resolved to a CIK. Basis 'ticker_map' (the
    pinned SEC authority) or 'facts_cik' (the treatment fact's own id — the
    sanctioned fallback, recorded) passes; a map/facts disagreement or no CIK
    at all is review — a control must never attach another issuer's filings."""

    name = "cik_resolution"
    version = "cik_resolution:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        res = (record.provenance.get("cik_resolution")
               if isinstance(record.provenance, dict) else None) or {}
        evidence = {
            "basis": res.get("basis"),
            "map_cik": res.get("map_cik"),
            "facts_cik": res.get("facts_cik"),
            "cik10": res.get("cik10"),
        }
        if res.get("conflict"):
            evidence["problem"] = (
                f"cik_conflict: the pinned SEC ticker map says {res.get('map_cik')!r} "
                f"but the treatment fact carried {res.get('facts_cik')!r} — a human "
                "must decide which issuer this entity is"
            )
            return GateResult(name=self.name, version=self.version,
                              verdict="review", evidence=evidence)
        if res.get("basis") in ("ticker_map", "facts_cik") and res.get("cik10"):
            return GateResult(name=self.name, version=self.version,
                              verdict="pass", evidence=evidence)
        evidence["problem"] = (
            "cik_unresolved: the ticker is not in the pinned SEC map and the "
            "treatment fact carried no CIK — no EDGAR history can be attached "
            "(typical for post-cutoff IPO entities that did not file in 2024)"
        )
        return GateResult(name=self.name, version=self.version,
                          verdict="review", evidence=evidence)


class FiscalAlignmentGate(Gate):
    """Review-only (the ruling: a mismatched fiscal grid 'goes to review with
    the mismatch recorded, never silently mapped'). Passes only when the
    chosen/diagnosed grid maps both calendar targets to entity period ends
    within 45 days, one quarter apart."""

    name = "fiscal_alignment"
    version = "fiscal_alignment:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        resolution = (record.provenance.get("resolution")
                      if isinstance(record.provenance, dict) else None) or {}
        alignment = resolution.get("alignment")
        evidence: dict = {"alignment": alignment}
        if not isinstance(alignment, dict):
            evidence["problem"] = (
                "no fiscal-grid diagnosis: no revenue concept carried quarterly "
                "entries (companyfacts unavailable, unfetched, or empty), so "
                "alignment with calendar Q1-2024 cannot be established"
            )
            return GateResult(name=self.name, version=self.version,
                              verdict="review", evidence=evidence)
        if alignment.get("aligned"):
            return GateResult(name=self.name, version=self.version,
                              verdict="pass", evidence=evidence)
        evidence["problem"] = (
            f"fiscal_mismatch ({alignment.get('reason')}): the entity's fiscal "
            f"quarters do not align with calendar {PINNED_QUARTER_LABEL} within "
            f"{FISCAL_ALIGN_MAX_DAYS} days — held for a human, never silently mapped"
        )
        return GateResult(name=self.name, version=self.version,
                          verdict="review", evidence=evidence)


class RevenuePresentGate(Gate):
    """Review-only: BOTH pinned quarters resolved cleanly from one concept
    (the ruling's revenue_present gate). The chosen concept, each side's
    derivation basis, and every earlier concept's failure are in evidence."""

    name = "revenue_present"
    version = "revenue_present:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        resolution = (record.provenance.get("resolution")
                      if isinstance(record.provenance, dict) else None) or {}
        evidence: dict = {
            "concept": resolution.get("concept"),
            "concept_chain": list(CONCEPT_CHAIN),
            "attempts": resolution.get("attempts"),
            "q1_basis": (resolution.get("q1") or {}).get("basis"),
            "q4_basis": (resolution.get("q4") or {}).get("basis"),
        }
        if resolution.get("status") == "ok":
            return GateResult(name=self.name, version=self.version,
                              verdict="pass", evidence=evidence)
        evidence["problem"] = (
            "revenue_unresolved: no concept in the fallback chain yields BOTH "
            "a clean Q1-2024 and a clean Q4-2023 figure (per-concept reasons in "
            "'attempts') — held for a human, never guessed"
        )
        return GateResult(name=self.name, version=self.version,
                          verdict="review", evidence=evidence)


class GapInadmissibleGate(Gate):
    """THE admissibility gate (ruling 2026-08-05): |Q1−Q4|/|Q1|·100 must
    EXCEED GAP_THRESHOLD_PCT (10 = eval tolerance_pct 5 × min_gap_multiple 2,
    the trap_is_admissible form) or the control's false side would sit inside
    the grading tolerance of its true side and measure nothing.

    Named for its disposition: a FAIL yields ``excluded:gap_inadmissible``
    (the ruling's exact string), with BOTH values kept in evidence. A zero Q1
    is likewise inadmissible (no gap is computable — mirroring the eval gate's
    'live value is zero'). Unresolved values are review (revenue_present has
    already named why), never a fail."""

    name = "gap_inadmissible"
    version = "gap_inadmissible:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        evidence: dict = {
            "threshold_pct": GAP_THRESHOLD_PCT,
            "threshold_basis": "eval finance_grading tolerance_pct 5 x min_gap_multiple 2 "
                               "(the trap_is_admissible form, ruled 2026-08-05)",
        }
        resolution = (record.provenance.get("resolution")
                      if isinstance(record.provenance, dict) else None) or {}
        q1 = (resolution.get("q1") or {}).get("value")
        q4 = (resolution.get("q4") or {}).get("value")
        if not (_is_num(q1) and _is_num(q4)):
            evidence["problem"] = ("values_unresolved: one or both quarters did not "
                                   "resolve, so no gap can be judged")
            return GateResult(name=self.name, version=self.version,
                              verdict="review", evidence=evidence)
        evidence["q1_2024_value"] = q1
        evidence["q4_2023_value"] = q4
        gap = gap_pct(q1, q4)
        if gap is None:
            evidence["problem"] = ("true_value_zero: Q1-2024 revenue is 0, so no gap "
                                   "is computable — the trap and the correct answer "
                                   "are not distinguishable")
            return GateResult(name=self.name, version=self.version,
                              verdict="fail", evidence=evidence)
        evidence["gap_pct"] = round(gap, 6)
        if gap <= GAP_THRESHOLD_PCT:
            evidence["problem"] = (
                f"gap_inadmissible: Q4-2023 {q4:g} is {gap:.2f}% from Q1-2024 {q1:g}, "
                f"inside the required {GAP_THRESHOLD_PCT:g}% gap — the trap and the "
                "correct answer are not distinguishable under the eval tolerance"
            )
            return GateResult(name=self.name, version=self.version,
                              verdict="fail", evidence=evidence)
        return GateResult(name=self.name, version=self.version,
                          verdict="pass", evidence=evidence)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class FinanceControlsAdapter(Adapter):
    """Adapter for source 'finance_controls'. Stateless; caches load once per
    run into cfg (the sanctioned runner channel)."""

    source = SOURCE

    # -- enumeration --------------------------------------------------------

    def enumerate_candidates(self, cfg: dict):
        """Yield every row of {data_dir}/finance_controls_pool.jsonl sorted by
        (ticker, line). The file IS the pool (frozen by the harvester from the
        eval draw); a row that is not a valid pool member still becomes a
        record and is held by the control_pool gate — enumeration never
        decides inclusion. Unparseable lines become build_errors."""
        self._require_offline(cfg)
        path = self._data_path(cfg, POOL_FILENAME)
        candidates = []
        with open(path, encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError(f"row is {type(row).__name__}, expected object")
                except ValueError as exc:
                    candidates.append({"_line": line_no, "_parse_error": str(exc)})
                    continue
                candidates.append({"_line": line_no, "pool_row": row})
        candidates.sort(key=lambda c: (((c.get("pool_row") or {}).get("ticker")) or "",
                                       c["_line"]))
        yield from candidates

    # -- record building ----------------------------------------------------

    def build_record(self, candidate: dict, cfg: dict) -> FactChangeRecord:
        if "_parse_error" in candidate:
            raise ValueError(
                f"{POOL_FILENAME} line {candidate.get('_line')}: unparseable JSON "
                f"({candidate['_parse_error']})"
            )
        pool_row = candidate.get("pool_row") or {}
        self._ensure_loaded(cfg)

        ticker = pool_row.get("ticker") if isinstance(pool_row.get("ticker"), str) else None
        entity_name = pool_row.get("entity_name")
        if not (isinstance(entity_name, str) and entity_name):
            entity_name = pool_row.get("display_name")
        if not (isinstance(entity_name, str) and entity_name):
            entity_name = ticker or f"pool row {candidate.get('_line')}"

        # ---- CIK resolution (same pure function the harvester used) --------
        ticker_map = cfg.get(TICKER_MAP_CTX_KEY) or {}
        resolution_cik = resolve_cik(ticker or "", ticker_map, pool_row.get("facts_cik"))
        cik10 = resolution_cik.get("cik10")
        companyfacts_url = (COMPANYFACTS_URL_TEMPLATE.format(cik10=cik10)
                            if cik10 else None)

        # ---- value derivation from the frozen companyfacts extract ----------
        cf_row = (cfg.get(FACTS_CTX_KEY) or {}).get(ticker) if ticker else None
        concepts = (cf_row or {}).get("concepts") if isinstance(cf_row, dict) else None
        fetch_errors = [e for e in ((cf_row or {}).get("fetch_errors") or [])
                        if isinstance(e, str)] if isinstance(cf_row, dict) else []
        resolution = resolve_control_values(concepts if isinstance(concepts, dict) else {})
        q1 = resolution.get("q1")
        q4 = resolution.get("q4")

        def side_ref(role, target_end, label, side):
            ref = {
                "role": role,
                "quarter_label": label,
                "target_calendar_end": target_end,
                "concept": resolution.get("concept"),
                "unit": UNIT,
                "ticker": ticker,
                "cik": cik10,
            }
            if isinstance(side, dict):
                ref.update({
                    "matched_end": side.get("matched_end"),
                    "basis": side.get("basis"),
                    "entry": side.get("entry"),
                    "restated": side.get("restated"),
                    "alternates": side.get("alternates"),
                })
                if side.get("components"):
                    ref["components"] = side["components"]
            else:
                ref["status"] = "unresolved"
            if fetch_errors:
                ref["fetch_errors"] = fetch_errors
            return ref

        after_ev = Evidence(
            kind="edgar_companyfacts", url=companyfacts_url,
            ref=side_ref("true_side_pinned_quarter", Q1_TARGET_END,
                         PINNED_QUARTER_LABEL, q1),
            as_of=(q1 or {}).get("entry", {}).get("filed") if isinstance(q1, dict) else None,
        )
        before_ev = Evidence(
            kind="edgar_companyfacts", url=companyfacts_url,
            ref=side_ref("false_side_adjacent_quarter", Q4_TARGET_END,
                         FALSE_QUARTER_LABEL, q4),
            as_of=(q4 or {}).get("entry", {}).get("filed") if isinstance(q4, dict) else None,
        )
        after_raw = _num_str((q1 or {}).get("value")) if isinstance(q1, dict) else ""
        before_raw = _num_str((q4 or {}).get("value")) if isinstance(q4, dict) else ""

        # ---- change_date: the RULING PIN, identical for every control -------
        change_date = ChangeDate(
            value=PINNED_ASOF, precision="day",
            evidence=Evidence(
                kind="ruling_pin", url=None, as_of=None,
                ref={
                    "basis": "pinned_calendar_quarter_end",
                    "note": (
                        "a control is an UNCHANGED (immutable) fact; this date is the "
                        f"ruled calendar pin — {PINNED_QUARTER_LABEL} ends {PINNED_ASOF} "
                        "(ruling 2026-08-05). The entity's own matched fiscal period end "
                        "is evidence on each side, never identity."
                    ),
                    "entity_matched_q1_end": (q1 or {}).get("matched_end")
                        if isinstance(q1, dict) else None,
                },
            ),
        )

        pool_meta = cfg.get(POOL_CTX_KEY) or {}
        map_info = cfg.get(TICKER_MAP_INFO_CTX_KEY) or {"file": None, "sha1": None}
        cf_info = cfg.get(FACTS_INFO_CTX_KEY) or {"file": None, "sha1": None}
        gap = (gap_pct(q1["value"], q4["value"])
               if isinstance(q1, dict) and isinstance(q4, dict)
               and _is_num(q1.get("value")) and _is_num(q4.get("value")) else None)

        provenance = {
            "predictability": ANNOUNCED,
            "population": POPULATION,
            "property_display": PROPERTY_DISPLAY,
            "line": candidate.get("_line"),
            "display_name": pool_row.get("display_name"),
            "pinned_quarter": {
                "label": PINNED_QUARTER_LABEL,
                "calendar_start": PINNED_CUTOFF,
                "calendar_end": PINNED_ASOF,
                "entity_matched_end": (q1 or {}).get("matched_end")
                    if isinstance(q1, dict) else None,
            },
            "false_quarter": {
                "label": FALSE_QUARTER_LABEL,
                "calendar_end": Q4_TARGET_END,
                "entity_matched_end": (q4 or {}).get("matched_end")
                    if isinstance(q4, dict) else None,
                "derivation_basis": (q4 or {}).get("basis")
                    if isinstance(q4, dict) else None,
            },
            "gap_pct": round(gap, 6) if gap is not None else None,
            "cik_resolution": resolution_cik,
            "resolution": {
                "status": resolution.get("status"),
                "concept": resolution.get("concept"),
                "attempts": resolution.get("attempts"),
                "alignment": resolution.get("alignment"),
                "q1": q1,
                "q4": q4,
            },
            "pool": {
                "draws_file": pool_meta.get("draws_file"),
                "draws_sha1": pool_meta.get("draws_sha1"),
                "facts_file": pool_meta.get("facts_file"),
                "facts_sha1": pool_meta.get("facts_sha1"),
                "fact_ids": pool_row.get("fact_ids"),
                "properties": pool_row.get("properties"),
                "selection": pool_meta.get("selection"),
            },
            "ticker_map_cache": {"file": map_info.get("file"), "sha1": map_info.get("sha1")},
            "companyfacts_cache": {
                "file": cf_info.get("file"), "sha1": cf_info.get("sha1"),
                "row_present": isinstance(cf_row, dict),
                "sec_entity_name": (cf_row or {}).get("entity_name_sec")
                    if isinstance(cf_row, dict) else None,
            },
        }

        entity_ids = {}
        if ticker:
            entity_ids["ticker"] = ticker
        if cik10:
            entity_ids["cik"] = cik10
        fact_id = compute_fact_id(SOURCE, entity_name, PROPERTY, PINNED_ASOF)
        return FactChangeRecord(
            fact_id=fact_id,
            record_id=compute_record_id(
                fact_id, f"{ticker or ''}|{cik10 or ''}|{PINNED_QUARTER_LABEL}"),
            source=SOURCE,
            entity={"name": entity_name, "ids": entity_ids},
            property=PROPERTY,
            value_type=VALUE_TYPE,
            before=ValueState(raw=before_raw, canonical=None, evidence=before_ev),
            after=ValueState(raw=after_raw, canonical=None, evidence=after_ev),
            change_date=change_date,
            provenance=provenance,
        )

    # -- gates --------------------------------------------------------------

    def gate_list(self, cfg: dict):
        self._require_offline(cfg)
        self._ensure_loaded(cfg)
        return [
            FinanceControlPoolGate(),
            CikResolutionGate(),
            FiscalAlignmentGate(),
            RevenuePresentGate(),
            GapInadmissibleGate(),
            EvidenceResolvableGate(),
            DedupGate(),
        ]

    def snapshot_inputs(self, cfg: dict):
        return [
            POOL_FILENAME, POOL_SIDECAR_FILENAME,
            TICKER_MAP_FILENAME, TICKER_MAP_SIDECAR_FILENAME,
            FACTS_FILENAME, FACTS_SIDECAR_FILENAME,
        ]

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _require_offline(cfg: dict) -> None:
        if not cfg.get("offline", True):
            raise NotImplementedError(
                "the finance_controls adapter is offline-only: the EDGAR evidence "
                "is fetched once by `python3 -m stage1.harvest --source finance_controls`"
            )

    @staticmethod
    def _data_path(cfg: dict, filename: str) -> Path:
        data_dir = cfg.get("data_dir")
        if data_dir is None:
            raise LookupError(
                f"the finance_controls adapter requires --data-dir (a finance_controls "
                f"harvest snapshot containing {filename})"
            )
        path = Path(data_dir) / filename
        if not path.is_file():
            raise LookupError(f"finance_controls input file not found: {path}")
        return path

    def _ensure_loaded(self, cfg: dict) -> None:
        """Load the pool sidecar, the pinned ticker map, and the companyfacts
        cache into cfg exactly once per run. Malformed content becomes
        input_load_errors, never a silent skip; a missing cache degrades to
        review via the gates, never a crash."""
        if POOL_CTX_KEY not in cfg:
            cfg[POOL_CTX_KEY] = self._load_json_sidecar(cfg, POOL_SIDECAR_FILENAME)
        if TICKER_MAP_CTX_KEY not in cfg:
            cfg[TICKER_MAP_CTX_KEY], cfg[TICKER_MAP_INFO_CTX_KEY] = \
                self._load_ticker_map_file(cfg)
            SportsControlsAdapter._surface_cache_meta(cfg, cfg[TICKER_MAP_INFO_CTX_KEY])
        if FACTS_CTX_KEY not in cfg:
            store, info, errors = SportsControlsAdapter._load_cache(
                cfg.get("data_dir"), FACTS_FILENAME, FACTS_SIDECAR_FILENAME,
                key="ticker",
            )
            cfg[FACTS_CTX_KEY] = store
            cfg[FACTS_INFO_CTX_KEY] = info
            SportsControlsAdapter._surface_cache_meta(cfg, info)
            if errors:
                cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(errors)

    def _load_json_sidecar(self, cfg: dict, name: str) -> dict:
        """A single-object JSON sidecar; a missing/unreadable one degrades to
        an empty dict (the control_pool gate then reviews every record for the
        missing binding — visible, not fatal) with the problem recorded."""
        data_dir = cfg.get("data_dir")
        if data_dir is None:
            return {}
        path = Path(data_dir) / name
        if not path.is_file():
            cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).append(
                {"file": name, "line": 0,
                 "error": "pool sidecar missing: pool binding unavailable"})
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                meta = json.load(fh)
        except ValueError as exc:
            cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).append(
                {"file": name, "line": 0, "error": f"unparseable pool sidecar: {exc}"})
            return {}
        return meta if isinstance(meta, dict) else {}

    def _load_ticker_map_file(self, cfg: dict):
        """(parsed {TICKER: {...}}, {'file','sha1','meta'}). A missing map
        yields an empty dict (cik_resolution then reviews map-less rows via
        the facts fallback or not at all); a corrupt one is a load error."""
        info: dict = {"file": None, "sha1": None, "meta": None}
        data_dir = cfg.get("data_dir")
        if data_dir is None:
            return {}, info
        path = Path(data_dir) / TICKER_MAP_FILENAME
        if not path.is_file():
            return {}, info
        digest = hashlib.sha1()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                digest.update(chunk)
        info["file"] = TICKER_MAP_FILENAME
        info["sha1"] = digest.hexdigest()
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except ValueError as exc:
            cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).append(
                {"file": TICKER_MAP_FILENAME, "line": 0,
                 "error": f"unparseable ticker map: {exc}"})
            return {}, info
        sidecar = Path(data_dir) / TICKER_MAP_SIDECAR_FILENAME
        if sidecar.is_file():
            try:
                with open(sidecar, encoding="utf-8") as fh:
                    meta = json.load(fh)
                info["meta"] = meta if isinstance(meta, dict) else None
            except ValueError as exc:
                cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).append(
                    {"file": TICKER_MAP_SIDECAR_FILENAME, "line": 0,
                     "error": f"unparseable sidecar: {exc}"})
        return load_ticker_map(raw), info


ADAPTER = FinanceControlsAdapter()
