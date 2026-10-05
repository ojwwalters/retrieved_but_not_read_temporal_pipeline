"""Finance adapter (source 'finance'): post-cutoff drift of five financial
fact families over the S&P 500 universe plus a market-wide IPO family.

The ground truth is deterministic arithmetic over an authoritative feed
(Polygon.io / "Massive", frozen into per-family caches by
``stage1.tools.fetch_polygon``); ALL judgement lives in the gates, so
selection is entirely model-free and byte-reproducible. The adapter is
OFFLINE-only — it reads the frozen committed caches under ``stage1/cache/``
(or a release's vendored copies under ``--data-dir``); ``--online`` raises.

Families (one adapter emitting five properties, like wiki_people):

* ``share_price``      (quantity, S&P universe, CONTINUOUS) — close(anchor) ->
  close(asof); endpoint-sampled snapshot, change_date = the resolved asof
  trading day, precision day, basis ``continuous_endpoint_sample``.
* ``market_cap``       (quantity, S&P universe, CONTINUOUS, **computed here**)
  — avg_shares(t) x close(t) with shares taken from the FINANCIALS filing
  whose period-of-report end_date is the greatest <= t (the owner decision:
  shares ride free with the revenue pull, no per-date ticker-details call).
  The shares are the income-statement fiscal-period WEIGHTED-AVERAGE (Polygon's
  financials payload exposes no balance-sheet point-in-time shares_outstanding
  line), so the cap is a documented PERIOD-AVERAGE APPROXIMATION, labeled as
  such in evidence.ref (``point_in_time=false``, ``shares_basis``) — it does
  NOT overclaim the point-in-time ideal. NEVER Polygon's precomputed
  market_cap.
* ``quarterly_revenue`` (quantity, S&P universe, DISCRETE) — the last quarter
  actually REPORTED on/before the cutoff (a pre-cutoff-KNOWABLE baseline, keyed
  on filing/acceptance date, not period-end) -> the first post-cutoff quarter
  reported on/before the asof; metric is quarterly total ``revenues``;
  change_date = the after-quarter end_date. A quarter whose period ended before
  the cutoff but was FILED after it is NOT a knowable baseline and never anchors
  the before side. Provenance records the before side's reporting date and a
  before/after adjacency disclosure.
* ``ticker_change``    (text_span, S&P universe, DISCRETE) — old symbol -> new
  symbol at the event date; one candidate per ``ticker_change`` event.
* ``ipo_listing``      (text_span, OWN universe, DISCRETE) — synthetic
  "unlisted" -> "listed:<TICKER>" at the listing_date; the IPO family declares
  its own universe via the deterministic, auditable ``ipo_scope`` gate (major
  US NYSE/Nasdaq, common stock, US issuer), NOT the S&P membership gate.

*** NO HARDCODED DATES *** cutoff and asof are cfg flags. The anchor and asof
trading days are DERIVED from the price cache (the fetch tool's grouped-daily
step-back result); there is no output-affecting date literal in this source.

Selection is TOTAL and model-free: the four S&P families enumerate EVERY
universe ticker (``sec/sp500_universe.csv`` or a vendored copy). A ticker the
fetch failed to cache is NEVER silently dropped — it degrades to a ``review``
record (missing before/after values), exactly like the sports/people
missing-cache pattern. The IPO family enumerates EVERY row of the market-wide
IPO cache; out-of-scope issuers become ``excluded:ipo_scope`` with a
per-issuer reason, never a silent filter.

Gate order (first FAIL in ledger order names the disposition; every gate
always runs):

 1. ``universe_membership`` — S&P families: entity ticker in the universe set
    (a provable non-member FAILs). ``ipo_listing`` is not applicable here and
    PASSes (it declares its own universe via ``ipo_scope``).
 2. ``ipo_scope`` — ``ipo_listing`` only: the major-US / common-stock /
    US-issuer coverage filter, auditable per issuer (kept -> pass, dropped ->
    FAIL with the failed criteria). Non-IPO families PASS (not applicable).
    Placed with the universe gate because it is the IPO family's universe
    equivalent (a provable out-of-scope determination).
 2b. ``window_binding`` — every family: the frozen cache's sidecar window must
    match the run's (cutoff, asof). A mismatch is ``review`` (window-mixed
    provenance held for a human), never a silent pin to the cache's window;
    an agreeing or absent sidecar window PASSes.
 3. ``temporal_window`` — change date inside [cutoff, asof] (shared gate). The
    continuous families sample at asof, so they pass; the discrete families
    carry their real event date (a pre-cutoff or post-asof event FAILs).
 4. ``value_parsed`` — both sides normalized (an empty/missing side -> review;
    this is where a missing-cache degradation surfaces).
 5. ``garbage_value`` — shared screen; no rules configured for quantity /
    text_span (the numbers are authoritative), so it passes with a note.
 6. ``value_changed`` — the typed comparator says before != after. Money is
    compared at float-exact tolerance (quantity:v2), so any genuine move (a
    +1 cap or a revenue delta) passes; an equal pair FAILs as a non-change.
 6b. ``finance_ticker_rename`` — price/cap only: if the symbol underwent an
    in-window ticker_change (the current symbol may have been a DIFFERENT
    security at the anchor date, so the anchor close is contaminated), the
    record is ``review`` (never fail — resolvable by refetching under the
    prior symbol). Placed before cross_source so a renamed symbol reviews on
    this specific reason rather than passing uncorroborated. Non-price/cap
    families PASS (not applicable).
 6c. ``finance_issuer_continuity`` — quarterly_revenue/market_cap only: the
    symbol-keyed FINANCIALS history must be the entity's own continuous
    history. A side's chosen filing whose CIK differs from the entity's
    (Qnity's symbol 'Q' resolving to a Quintiles-IMS 2017 quarter), or a
    before filing reported BEFORE the record's symbol was adopted or
    abandoned (Fiserv's FISV->FI rename truncating the FISV-keyed cache at
    2023), is ``review`` (never fail — resolvable by refetching by CIK).
    Other families PASS (not applicable).
 7. ``cross_source_agreement`` — price/cap only: Polygon vs the independent
    Stooq asof close. Agreement (within ~1%) passes; a disagreement OR an
    unavailable/missing cross-check is ``review``, NEVER a fail (mirroring the
    corroboration pattern — the model's own feed remains ground truth). With
    Stooq currently behind a JS challenge every cross-check is unavailable, so
    price/cap records rest in review until an independent source is wired.
    Non-price/cap families PASS (not applicable).
 8. ``finance_material_change`` — price/cap only, POLICY gate (manifest-
    recorded, one-flag revisable). Default ``price_min_drift = None`` (OFF):
    drift is a pure covariate recorded in provenance, so this PASSes. A
    configured threshold reviews sub-threshold moves (sensitivity analysis).
 9. ``evidence_resolvable`` — both sides' evidence points somewhere (shared).
10. ``dedup`` — last (shared); the (entity, property, change_date) key is
    unique per family, so this is a structural no-op check.

Cache provenance mirrors the sports/people caches exactly: each cache is
declared to the manifest via ``cfg['extra_input_files']`` under a
LOCATION-INDEPENDENT identifier (the bare filename when vendored in data_dir,
else the repo-relative ``stage1/cache/<name>`` / ``sec/sp500_universe.csv``)
plus its sha1, and its ``.meta.json`` sidecar's retrieval metadata is surfaced
into ``cfg['extra_input_meta']`` — so a release fingerprints every byte it
depends on and ``facts.jsonl`` is byte-identical across checkouts. A missing
cache degrades to review, never a crash and never a silent include.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from datetime import date as _date
from pathlib import Path

# Register the two comparators this adapter needs (import side effect).
import stage1.normalize.quantity  # noqa: F401  (registers 'quantity')
import stage1.normalize.text_span  # noqa: F401  (registers 'text_span')
from stage1.adapters import Adapter
from stage1.gates import Gate
from stage1.predictability import (
    ANNOUNCED,
    GUIDED,
    UNPREDICTABLE,
    check_predictability,
)
from stage1.gates.standard import (
    DedupGate,
    EvidenceResolvableGate,
    GarbageValueGate,
    TemporalWindowGate,
    UniverseMembershipGate,
    ValueActuallyChangedGate,
    ValueParsedGate,
)
from stage1.schema import (
    ChangeDate,
    Evidence,
    FactChangeRecord,
    GateResult,
    ValueState,
    compute_fact_id,
    compute_record_id,
)

# --------------------------------------------------------------------------- #
# Families / properties / value types
# --------------------------------------------------------------------------- #
SOURCE = "finance"

SHARE_PRICE = "share_price"
MARKET_CAP = "market_cap"
QUARTERLY_REVENUE = "quarterly_revenue"
TICKER_CHANGE = "ticker_change"
IPO_LISTING = "ipo_listing"

# The four families scoped to the S&P universe (they enumerate every universe
# ticker and are governed by universe_membership). ipo_listing is deliberately
# NOT here — it declares its own universe via ipo_scope.
SP_FAMILIES = (SHARE_PRICE, MARKET_CAP, QUARTERLY_REVENUE, TICKER_CHANGE)
# The two continuous families that carry a Stooq cross-check and a drift
# covariate, and are governed by the (off-by-default) material-change policy.
CONTINUOUS_FAMILIES = (SHARE_PRICE, MARKET_CAP)

QUANTITY = "quantity"
TEXT_SPAN = "text_span"

# --------------------------------------------------------------------------- #
# PREDICTABILITY tag per family (owner decision B, 2026-07-21). Stamped into
# provenance['predictability'] as Stage-2 stratification METADATA — NEVER a
# gate, never affecting a disposition/value/change_date. The three legal values
# are defined once in stage1.predictability so they cannot drift:
#   * share_price / market_cap  -> "unpredictable" (near-random-walk prices)
#   * quarterly_revenue         -> "guided"        (earnings guidance/consensus)
#   * ipo_listing / ticker_change -> "announced"   (disclosed ahead)
# --------------------------------------------------------------------------- #
PREDICTABILITY_BY_FAMILY = {
    SHARE_PRICE: UNPREDICTABLE,
    MARKET_CAP: UNPREDICTABLE,
    QUARTERLY_REVENUE: GUIDED,
    TICKER_CHANGE: ANNOUNCED,
    IPO_LISTING: ANNOUNCED,
}

# --------------------------------------------------------------------------- #
# Cache files (data_dir vendored copy first, else the package default under
# stage1/cache/). Identifiers recorded in provenance/manifest are LOCATION-
# INDEPENDENT so facts.jsonl reproduces across checkouts.
# --------------------------------------------------------------------------- #
PRICE_FILENAME = "price_grouped.jsonl"
FINANCIALS_FILENAME = "financials.jsonl"
EVENTS_FILENAME = "ticker_events.jsonl"
IPO_FILENAME = "ipo.jsonl"
CROSS_CHECK_FILENAME = "cross_check.jsonl"
# Committed price-verification sidecar (finance_price_verify:v1). A one-time
# TARGETED live Polygon re-fetch of the handful of suspicious share_price movers
# is frozen here (ticker, side, grouped vs adjusted close, per-ticker verdict) so
# the derive stays offline + byte-deterministic and the gate merely consults it.
PRICE_VERIFY_FILENAME = "price_verify.jsonl"
UNIVERSE_FILENAME = "sp500_universe.csv"

PACKAGE_CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_UNIVERSE_PATH = REPO_ROOT / "sec" / UNIVERSE_FILENAME
PACKAGE_UNIVERSE_ID = "sec/sp500_universe.csv"

# ctx keys (the runner copies cfg into ctx — the sanctioned channel).
UNIVERSE_MAP_CTX_KEY = "finance_universe"          # ticker -> {name, cik}; also the membership set
PRICE_CTX_KEY = "finance_price_by_ticker"          # ticker -> {role -> row}
PRICE_RESOLVED_CTX_KEY = "finance_price_resolved"  # role -> resolved_trading_day
FINANCIALS_CTX_KEY = "finance_financials_by_ticker"  # ticker -> [rows]
EVENTS_CTX_KEY = "finance_events_by_ticker"        # ticker -> row
IPO_CTX_KEY = "finance_ipo_rows"                    # [rows]
CROSS_CHECK_CTX_KEY = "finance_cross_check_by_ticker"  # ticker -> row
PRICE_VERIFY_CTX_KEY = "finance_price_verify"       # ticker -> {verdict, sides:[rows]}
CACHE_INFO_CTX_KEY = "finance_cache_info"           # family -> {file, sha1, present, meta}
GARBAGE_RULES_CTX_KEY = "garbage_rules"
LOAD_ERRORS_CTX_KEY = "input_load_errors"
EXTRA_INPUTS_CTX_KEY = "extra_input_files"
EXTRA_INPUT_META_CTX_KEY = "extra_input_meta"
POLICY_OVERRIDE_CTX_KEY = "finance_policy"
POLICY_ACTIVE_CTX_KEY = "policy"

# Sidecar keys surfaced into the manifest's extra_input_meta (only those a
# sidecar actually carries are copied). cache_file is deliberately excluded
# (it can be a machine-absolute path; the cache is keyed by its
# location-independent identifier already).
_SIDECAR_META_KEYS = (
    "tool_version", "retrieved_at", "endpoint", "family",
    "cutoff", "asof", "cache_sha1", "universe_sha1", "resolved", "params",
)

# --------------------------------------------------------------------------- #
# IPO scope (owner coverage decision; deterministic + auditable)
# --------------------------------------------------------------------------- #
# Major US primary exchanges (MIC codes): NYSE, Nasdaq. XASE (NYSE American)
# and OTCM (OTC) are deliberately out of scope ("major US (NYSE/Nasdaq),
# exclude OTC"). Common stock only (security_type CS) drops SPAC units (SP),
# generic UNITs and ADRs (ADRC, foreign). US issuers only: the ISIN country
# prefix must be 'US' (a Cayman-domiciled shell carries KYG..., etc.). Direct
# listings (ipo_status 'direct_listing_process') pass the SAME objective
# filter and are included. On the fetched 2026 window this objective filter
# yields zero SPAC-name survivors, so no fuzzy name heuristic is added.
IPO_MAJOR_EXCHANGES = ("XNAS", "XNYS")
IPO_ALLOWED_SECURITY_TYPES = ("CS",)
IPO_ISSUER_COUNTRY = "US"

# --------------------------------------------------------------------------- #
# market_cap shares basis (Finding 3 — honest labeling)
# --------------------------------------------------------------------------- #
# Polygon's financials endpoint exposes NO point-in-time shares_outstanding
# (its balance_sheet carries no shares line; only the income statement's
# fiscal-period weighted-average basic/diluted shares exist), and the owner
# excluded per-date ticker-details calls for request economy. So the computed
# cap uses a PERIOD-AVERAGE share count and is labeled as an approximation, not
# the 'shares_outstanding(t) x close(t)' point-in-time ideal.
SHARES_BASIS = "income_statement_period_average_shares"
MARKET_CAP_FORMULA = (
    "avg_shares(latest filing with period-end <= t REPORTED on/before the "
    "side's knowability bound) x close(t)"
)
MARKET_CAP_APPROX_NOTE = (
    "period-average-shares approximation, NOT point-in-time: Polygon's "
    "financials payload has no balance-sheet shares_outstanding line, and a "
    "per-date ticker-details call was excluded by owner decision (request "
    "economy); shares are the income-statement weighted-average for the fiscal "
    "period whose period-end is the greatest <= t AMONG filings REPORTED "
    "on/before the side's knowability bound (cutoff for the anchor/before "
    "side, asof for the after side — mirroring the quarterly_revenue "
    "last-FILED rule, so a share count published only after the bound can "
    "never enter the computed cap); a filing with no knowable report date "
    "can be chosen for neither side"
)

# --------------------------------------------------------------------------- #
# Cross-source agreement
# --------------------------------------------------------------------------- #
CROSS_CHECK_TOLERANCE = 0.01  # ~1%: > this -> review, never fail (price/cap only)

# --------------------------------------------------------------------------- #
# market_cap plausibility (finance_share_stability:v1)
# --------------------------------------------------------------------------- #
# market_cap is a PERIOD-AVERAGE-shares APPROXIMATION (cap = avg_shares(t) x
# close(t); see _build_market_cap). Because cap = shares x price, the implied
# share count each side is recoverable, and a real share count cannot move
# >1.5x or <0.67x across the ~5-month window absent a corporate action — so an
# implied-share ratio outside [SHARE_STABILITY_MIN, SHARE_STABILITY_MAX] flags
# an unreliable cap (a malformed fiscal-period weighted-average share count, e.g.
# Waters' 59.5M -> 82 BILLION units bug, or a real M&A share-structure change the
# approximation cannot be trusted across). The absolute fallback catches a cap
# too large to be real regardless of the ratio (only a handful of firms approach
# ~$4T in 2026). Both route to review, never fail (resolvable with a genuine
# point-in-time share count).
SHARE_STABILITY_MIN = 0.67
SHARE_STABILITY_MAX = 1.5
CAP_ABSOLUTE_MAX = 5e12

# --------------------------------------------------------------------------- #
# share_price verification (finance_price_verify:v1)
# --------------------------------------------------------------------------- #
# The release close is Polygon grouped-daily (UNADJUSTED). A big endpoint-to-
# endpoint move can be (a) a REAL post-cutoff price move — the signal the
# benchmark wants (the H1-2026 semiconductor rally) — or (b) a split artifact:
# an UNADJUSTED anchor close compared across an in-window stock split to an
# UNADJUSTED asof close manufactures a spurious move (Booking's ~25:1 split
# turned a real ~-11% move into a raw -96%). The committed sidecar records, per
# suspect (ticker, side), the release grouped (unadjusted) close and the split-
# normalized ADJUSTED per-ticker close; a divergence above this tolerance on
# either endpoint means a corporate action sits inside the window and the raw
# grouped move is unreliable. Owner decision 2026-07-23 (ADJUST, do not exclude):
# a ticker that appears in Polygon's AUTHORITATIVE splits reference
# (/v3/reference/splits over the S&P universe for the window — NOT heuristic
# ratio-guessing, since a ~-50% move is just as likely a genuine decline:
# BSX/ACN/CTSH/CSGP/INTU all fell ~-50% in-window with NO split and are left
# untouched) is verdict 'split_adjusted' — the sidecar's ``refetched_close`` is
# the split-adjusted close on the current/post-split basis and _build_share_price
# USES it so the emitted before->after is the REAL economic move (kept, subject to
# value_changed + the material-change policy), never a review. A verified non-split
# mover is 'real_kept' (adjusted == grouped). 'bad_tick_review' remains for an
# uncorrectable corporate action that cannot be put on a consistent basis (review).
PRICE_VERIFY_TOOL = "price_verify:v2"
PRICE_VERIFY_DIVERGENCE_TOLERANCE = 0.02
PRICE_VERIFY_REAL = "real_kept"          # verified genuine move, kept on the grouped basis
PRICE_VERIFY_SPLIT = "split_adjusted"    # authoritative in-window split -> use the adjusted close
PRICE_VERIFY_BAD = "bad_tick_review"     # uncorrectable corporate action -> review
# A share_price endpoint move at/beyond +150% (ratio >= 2.5) or -70% (ratio <=
# 0.30) is a "suspect": exactly the >150%-up / >70%-down band the one-time
# targeted re-fetch enumerated into the sidecar. Used ONLY as a fail-CLOSED
# fallback: when the committed sidecar is ABSENT entirely (a broken/legacy derive
# with no coverage check to catch it), a suspect move can no longer be adjudicated
# real-vs-artifact, so it is held for review rather than silently included. When
# the sidecar IS present it is authoritative — it lists every suspect — so a
# ticker absent from a present sidecar is definitionally a non-suspect and passes
# untouched, and this band is not consulted (release output unchanged).
PRICE_SUSPICIOUS_UP = 2.5
PRICE_SUSPICIOUS_DOWN = 0.30

# --------------------------------------------------------------------------- #
# Owner policy (manifest-recorded, one-flag revisable). Default OFF: drift is a
# pure covariate. A float sets a material-change threshold for sensitivity
# analysis (sub-threshold |drift| -> review).
# --------------------------------------------------------------------------- #
DEFAULT_POLICY = {"price_min_drift": None}


def resolve_policy(cfg) -> dict:
    """The active policy: DEFAULT_POLICY overlaid by cfg['finance_policy'].
    Only a valid ``price_min_drift`` (None or a real number) overrides; a
    malformed override is ignored so a policy typo can never silently change a
    disposition. Pure and total."""
    active = dict(DEFAULT_POLICY)
    override = cfg.get(POLICY_OVERRIDE_CTX_KEY) if isinstance(cfg, dict) else None
    if isinstance(override, dict) and "price_min_drift" in override:
        value = override["price_min_drift"]
        if value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)):
            active["price_min_drift"] = value
    return active


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _num_str(x) -> str:
    """Deterministic string for a numeric value (repr round-trips a float to
    its shortest exact form, identical across platforms). Non-numbers -> ''."""
    if not _is_num(x):
        return ""
    return repr(float(x)) if isinstance(x, float) else repr(x)


def _iso_or_none(value):
    """Return ``value`` when it is a valid 'YYYY-MM-DD' string, else None."""
    if not isinstance(value, str):
        return None
    try:
        _date.fromisoformat(value)
    except ValueError:
        return None
    return value


def _report_date(row: dict):
    """The date a quarterly filing became PUBLIC (its reporting date), as an
    ISO 'YYYY-MM-DD' string, or None when unknown.

    Preference: ``filing_date`` (the SEC filing date), else the date part of
    ``acceptance_datetime`` (when EDGAR accepted it). A period merely ENDING
    on/before a reference date does NOT mean the filing existed then — an
    off-calendar fiscal quarter can end in late December yet only be filed
    weeks after a 1 Jan cutoff. Knowability keys on this reporting date, not on
    the period-end date. Pure and total."""
    fd = _iso_or_none(row.get("filing_date") if isinstance(row, dict) else None)
    if fd:
        return fd
    acc = row.get("acceptance_datetime") if isinstance(row, dict) else None
    if isinstance(acc, str) and len(acc) >= 10:
        return _iso_or_none(acc[:10])
    return None


def _month_index(iso: str):
    """Coarse month ordinal (year*12 + month) for an ISO date, else None. Used
    only to estimate quarter-adjacency for provenance disclosure."""
    d = _iso_or_none(iso)
    if d is None:
        return None
    y, m, _ = d.split("-")
    return int(y) * 12 + int(m)


def ipo_scope_reasons(row: dict) -> list:
    """Deterministic, auditable out-of-scope reasons for one IPO row (empty
    list == in scope). Pure; also used by the ipo_scope gate and tests."""
    reasons = []
    if not isinstance(row, dict):
        return ["row_missing"]
    sec = row.get("security_type")
    if sec not in IPO_ALLOWED_SECURITY_TYPES:
        reasons.append(f"security_type={sec!r}_not_common_stock")
    exch = row.get("primary_exchange")
    if exch not in IPO_MAJOR_EXCHANGES:
        reasons.append(f"exchange={exch!r}_not_major_us")
    isin = row.get("isin")
    if not (isinstance(isin, str) and isin[:2].upper() == IPO_ISSUER_COUNTRY):
        prefix = isin[:2] if isinstance(isin, str) and len(isin) >= 2 else None
        reasons.append(f"issuer_country={prefix!r}_not_us")
    return reasons


def select_quarters(fin_rows, cutoff_iso: str, asof_iso: str):
    """(before, after) — the KNOWABILITY-CORRECT quarterly pair. Among status
    'ok' rows carrying a real end_date and a numeric ``revenues``:

    * ``before`` = the last quarter actually REPORTED on/before the cutoff (the
      greatest end_date among rows whose reporting date — see ``_report_date``
      — is <= cutoff). This is the pre-cutoff-knowable baseline a model could
      have memorized. A quarter whose PERIOD merely ended before the cutoff but
      was FILED after it is NOT knowable at cutoff and is excluded from the
      before side — the load-bearing fix: selecting by period-end alone let a
      post-cutoff-filed off-calendar quarter (e.g. AAPL Q1-FY2026, end
      2025-12-27, filed 2026-01-30) masquerade as the memorized baseline.
    * ``after`` = the first post-cutoff quarter (min end_date > cutoff) that was
      also REPORTED on/before the asof (reporting date <= asof) — the current
      truth at asof. A quarter not yet filed at asof is not a current value and
      is excluded from the after side (the symmetric knowability guard).

    Either may be None (then that side degrades to review via value_parsed).
    Pure and total."""
    ok = [
        r for r in fin_rows
        if isinstance(r, dict) and r.get("status") == "ok"
        and _iso_or_none(r.get("end_date")) and _is_num(r.get("revenues"))
    ]
    pre, post = [], []
    for r in ok:
        reported = _report_date(r)
        if reported is None:
            continue  # a quarter with no knowable reporting date can anchor neither side
        if reported <= cutoff_iso:
            pre.append(r)
        if r["end_date"] > cutoff_iso and reported <= asof_iso:
            post.append(r)
    before = max(pre, key=lambda r: (r["end_date"], r.get("filing_date") or "")) if pre else None
    after = min(post, key=lambda r: (r["end_date"], r.get("filing_date") or "")) if post else None
    return before, after


def quarter_adjacency(before_q, after_q, fin_rows):
    """Provenance disclosure for a quarterly_revenue pair: whether ``before``
    and ``after`` are consecutive quarters or straddle a gap (a skipped
    fiscal-Q4 folded into the 10-K, or a quarter omitted by the knowability
    selection). Returns None when either side is absent, else a dict with the
    two period-ends, the count of OTHER cached 'ok' quarters whose end_date
    falls strictly between them, an approximate month gap, and an
    ``is_quarter_adjacent`` flag (~one quarter apart AND no cached quarter in
    between). Auditable, never a filter. Pure and total."""
    if not (isinstance(before_q, dict) and isinstance(after_q, dict)):
        return None
    b_end = _iso_or_none(before_q.get("end_date"))
    a_end = _iso_or_none(after_q.get("end_date"))
    if not (b_end and a_end and b_end < a_end):
        return None
    between = [
        r for r in fin_rows
        if isinstance(r, dict) and r.get("status") == "ok"
        and _iso_or_none(r.get("end_date")) and b_end < r["end_date"] < a_end
    ]
    b_mi, a_mi = _month_index(b_end), _month_index(a_end)
    months = (a_mi - b_mi) if (b_mi is not None and a_mi is not None) else None
    is_adjacent = not between and months is not None and months <= 4
    return {
        "before_end_date": b_end,
        "after_end_date": a_end,
        "cached_quarters_skipped_between": len(between),
        "approx_months_between": months,
        "is_quarter_adjacent": is_adjacent,
    }


def select_shares(fin_rows, pin_iso: str, filed_by_iso: str):
    """The shares proxy for date ``pin_iso`` under a KNOWABILITY bound
    ``filed_by_iso``: among status 'ok' filings whose period end_date is
    <= pin_iso AND whose REPORT date (see ``_report_date``: filing_date, else
    the acceptance date) is on/before ``filed_by_iso``, the one with the
    greatest end_date — preferring ``basic_average_shares`` then
    ``diluted_average_shares``.

    The bound mirrors ``select_quarters``' last-FILED rule (the market-cap
    knowability fix): the anchor/before cap must be computable from filings
    PUBLIC by the cutoff, and the after cap from filings public by the asof —
    a share count published only after the bound can never enter the value.
    A filing with NO knowable report date can never be chosen (its public
    availability at the bound cannot be proven); when such an undated filing
    has a LATER period-end than the best provably-reported filing (or no
    provably-reported filing exists), the selection is INDETERMINATE — the
    undated filing might have been the correct choice — and the result
    carries shares=None with ``selection`` naming the reason, so the record
    degrades to review rather than silently using either candidate.

    IMPORTANT — this is a PERIOD-AVERAGE approximation, NOT a point-in-time
    share count. Polygon's financials endpoint exposes only the income-
    statement weighted-average shares for the fiscal period (there is NO
    balance-sheet ``shares_outstanding`` line in the payload), and the owner
    excluded per-date ticker-details calls for request economy. The caller
    labels the computed market_cap accordingly so the value never overclaims
    the 'shares_outstanding(t) x close(t)' spec (see ``_build_market_cap``).

    Returns None when no 'ok' filing has period-end <= pin_iso at all, else
    {'shares','field','row','selection','filed_by','report_date',
    'later_filings_excluded_by_bound'} — shares None when the selection is
    indeterminate, no filing is provably reported by the bound, or the chosen
    filing lacks a numeric share count. Pure and total."""
    ok = [
        r for r in fin_rows
        if isinstance(r, dict) and r.get("status") == "ok"
        and _iso_or_none(r.get("end_date")) and r["end_date"] <= pin_iso
    ]
    if not ok:
        return None
    knowable = [
        r for r in ok
        if _report_date(r) is not None and _report_date(r) <= filed_by_iso
    ]
    undated = [r for r in ok if _report_date(r) is None]
    row = max(knowable, key=lambda r: (r["end_date"], r.get("filing_date") or "")) if knowable else None

    base = {"filed_by": filed_by_iso, "report_date": None,
            "later_filings_excluded_by_bound": 0}
    blocking = [r for r in undated if row is None or r["end_date"] > row["end_date"]]
    if blocking:
        blocker = max(blocking, key=lambda r: (r["end_date"], r.get("filing_date") or ""))
        return {**base, "shares": None, "field": None, "row": blocker,
                "selection": "indeterminate_report_date_unknown"}
    if row is None:
        return {**base, "shares": None, "field": None, "row": None,
                "selection": "no_filing_reported_by_bound"}
    base["report_date"] = _report_date(row)
    base["later_filings_excluded_by_bound"] = sum(
        1 for r in ok if r["end_date"] > row["end_date"]
    )
    for field in ("basic_average_shares", "diluted_average_shares"):
        value = row.get(field)
        if _is_num(value):
            return {**base, "shares": float(value), "field": field, "row": row,
                    "selection": "latest_reported_by_bound"}
    return {**base, "shares": None, "field": None, "row": row,
            "selection": "latest_reported_by_bound"}


# --------------------------------------------------------------------------- #
# Finance-specific gates
# --------------------------------------------------------------------------- #
class FinanceUniverseGate(UniverseMembershipGate):
    """S&P membership for the four universe families (ticker in the universe
    set). ``ipo_listing`` declares its own universe (see IpoScopeGate) and is
    not applicable here, so it PASSes with a note — the shared gate would
    otherwise FAIL every IPO issuer as a non-member."""

    def __init__(self):
        super().__init__(id_key="ticker", universe_set_ctx_key=UNIVERSE_MAP_CTX_KEY)

    def evaluate(self, record, ctx: dict) -> GateResult:
        if getattr(record, "property", None) == IPO_LISTING:
            return GateResult(
                name=self.name, version=self.version, verdict="pass",
                evidence={
                    "family": IPO_LISTING,
                    "note": "ipo_listing declares its own universe via the ipo_scope gate",
                },
            )
        return super().evaluate(record, ctx)


class IpoScopeGate(Gate):
    """The IPO family's own-universe gate (its coverage line). For an
    ``ipo_listing`` record it applies the deterministic, auditable scope
    filter (major-US NYSE/Nasdaq, common stock, US issuer — see
    ipo_scope_reasons): in scope -> pass, out of scope -> FAIL with the failed
    criteria named per issuer (a provable coverage determination, mirroring
    universe_membership). Every other family is not applicable and PASSes."""

    name = "ipo_scope"
    version = "ipo_scope:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        if getattr(record, "property", None) != IPO_LISTING:
            return GateResult(
                name=self.name, version=self.version, verdict="pass",
                evidence={"note": "not an ipo_listing record", "not_applicable": True},
            )
        provenance = getattr(record, "provenance", None)
        ipo = provenance.get("ipo") if isinstance(provenance, dict) else None
        ipo = ipo if isinstance(ipo, dict) else {}
        reasons = ipo_scope_reasons(ipo)
        evidence = {
            "security_type": ipo.get("security_type"),
            "primary_exchange": ipo.get("primary_exchange"),
            "isin": ipo.get("isin"),
            "ipo_status": ipo.get("ipo_status"),
            "allowed_exchanges": list(IPO_MAJOR_EXCHANGES),
            "allowed_security_types": list(IPO_ALLOWED_SECURITY_TYPES),
            "issuer_country": IPO_ISSUER_COUNTRY,
            "in_scope": not reasons,
            "reasons": reasons,
        }
        if reasons:
            evidence["problem"] = (
                "ipo_scope: issuer out of the declared IPO universe (major-US "
                "NYSE/Nasdaq common stock, US issuer): " + "; ".join(reasons)
            )
            return GateResult(name=self.name, version=self.version, verdict="fail", evidence=evidence)
        return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)


def _new_ticker(event) -> str:
    """The ``new_ticker`` of a ticker-events event when it is a non-empty
    string, else None. Pure and total."""
    if not isinstance(event, dict):
        return None
    nt = event.get("new_ticker")
    return nt if isinstance(nt, str) and nt else None


def _in_window_change_date(event, cutoff_iso: str, asof_iso: str):
    """The event's ISO date when it is a ``ticker_change`` whose (parseable)
    date falls inside [cutoff_iso, asof_iso], else None. A missing/unparseable
    date NEVER triggers (returns None). Pure and total; compares ISO strings
    (lexicographic == chronological for 'YYYY-MM-DD')."""
    if not isinstance(event, dict) or event.get("type") != "ticker_change":
        return None
    d = _iso_or_none(event.get("date"))
    if d is None or d < cutoff_iso or d > asof_iso:
        return None
    return d


class FinanceTickerRenamePriceGate(Gate):
    """Guard the continuous families (share_price / market_cap) against an
    in-window TICKER RENAME contaminating the anchor close.

    The anchor close is looked up under the CURRENT universe symbol. If a
    company adopted its current symbol WITHIN the study window [cutoff, asof],
    that symbol may have belonged to a DIFFERENT security at the anchor date,
    so the cached anchor close under the current symbol is another issuer's
    price — a fabricated before-value. Verified case: BNY Mellon renamed
    BK -> BNY on 2026-05-21; on the 2025-12-31 anchor the symbol 'BNY' was a
    different issuer ($10.14) while the real BNY Mellon (then 'BK') closed at
    $116.09, so the pipeline emitted a garbage $10.14 -> $144.61 (+1326%).

    For price/cap: if the record's ticker has ANY ``ticker_change`` event whose
    date falls inside [cutoff, asof] (read from the ticker_events cache in
    ctx), the anchor is unsafe -> ``review`` (NEVER fail: the value is
    resolvable by refetching the anchor under the prior symbol, so it is held
    for a human, not excluded). Otherwise ``pass``. Non-price/cap families are
    not applicable and PASS.

    Placed BEFORE cross_source_agreement so a renamed symbol reviews on THIS
    specific, actionable reason rather than passing uncorroborated. The join is
    primarily the events row keyed by the (current) universe symbol; a
    defensive fallback also fires when any in-window rename ADOPTS this symbol,
    so the guard triggers even if the cache were keyed under the prior symbol.
    Missing/unparseable dates never trigger; the gate never raises."""

    name = "finance_ticker_rename"
    version = "finance_ticker_rename:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        if getattr(record, "property", None) not in CONTINUOUS_FAMILIES:
            return GateResult(
                name=self.name, version=self.version, verdict="pass",
                evidence={"note": "ticker-rename anchor guard applies to price/market_cap only",
                          "not_applicable": True},
            )

        entity = getattr(record, "entity", None)
        ids = entity.get("ids") if isinstance(entity, dict) else None
        ticker = ids.get("ticker") if isinstance(ids, dict) else None

        run_cutoff = _ctx_date_iso(ctx, "cutoff")
        run_asof = _ctx_date_iso(ctx, "asof")
        evidence = {"ticker": ticker, "cutoff": run_cutoff, "asof": run_asof}

        # A window we cannot bind (missing/unparseable cutoff or asof) or a
        # record with no ticker can never decide "in-window", so it never
        # triggers (pass) — never a crash, never a fabricated review.
        if not (isinstance(ticker, str) and ticker and run_cutoff and run_asof):
            evidence["note"] = "no ticker or no bindable window; anchor-rename guard not triggered"
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)

        events_store = ctx.get(EVENTS_CTX_KEY) if isinstance(ctx, dict) else None
        events_store = events_store if isinstance(events_store, dict) else {}

        # (date, new_ticker) of every in-window ticker_change touching this
        # symbol. Deterministic: a set de-duplicates, sorted at the end.
        in_window = set()

        # Primary join: the events row keyed by this (current) universe symbol.
        # ANY in-window ticker_change means the symbol's history changed inside
        # the window (the task's spec, and where BNY/ECHO/HON/MRSH fire).
        direct = events_store.get(ticker)
        if isinstance(direct, dict):
            for event in direct.get("events") or []:
                d = _in_window_change_date(event, run_cutoff, run_asof)
                if d is not None:
                    in_window.add((d, _new_ticker(event)))

        # Defensive fallback (cache keyed under the PRIOR symbol): any in-window
        # rename anywhere whose NEW symbol is this ticker also contaminates it.
        for row in events_store.values():
            if not isinstance(row, dict):
                continue
            for event in row.get("events") or []:
                if _new_ticker(event) != ticker:
                    continue
                d = _in_window_change_date(event, run_cutoff, run_asof)
                if d is not None:
                    in_window.add((d, ticker))

        if in_window:
            ordered = sorted(in_window)
            dates = ", ".join(d for d, _ in ordered)
            evidence["in_window_ticker_changes"] = [
                {"date": d, "new_ticker": nt} for d, nt in ordered]
            evidence["problem"] = (
                f"anchor_price_predates_ticker_rename: universe symbol {ticker!r} underwent an "
                f"in-window ticker change ({dates}); the anchor close under the current symbol "
                "may be a different security — resolve by refetching the anchor under the prior symbol"
            )
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)

        evidence["note"] = "no in-window ticker rename for this symbol; anchor rests under a stable symbol"
        return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)


def _cik_int(value):
    """Canonical integer form of a CIK ('0000798354' == '798354'), or None
    when missing/non-numeric. Pure and total."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _symbol_history_events(events_store, ticker):
    """(away, adoptions): the deterministic symbol-history signals for one
    universe symbol, read from the ticker_events cache.

    * ``away``      — (date, new_ticker) of every parseable-dated
      ``ticker_change`` on the symbol's OWN events row whose ``new_ticker``
      DIFFERS from the symbol: the issuer moved AWAY from this symbol on that
      date, so filings after it live under another symbol and the
      symbol-keyed financials history is truncated there (the Fiserv
      FISV->FI class).
    * ``adoptions`` — (date, prior_ticker) of every PROVEN adoption of this
      symbol: an own-row event whose ``new_ticker`` equals the symbol AND
      whose next-older event names a DIFFERENT symbol (BK->BNY, MMC->MRSH),
      plus — the defensive fallback mirroring FinanceTickerRenamePriceGate —
      any other row's event adopting this symbol. A single same-symbol event
      with no older different-symbol sibling is NOT an adoption (Polygon also
      records exchange transfers as same-symbol ticker_change events, e.g.
      XOM/DD 2026 NYSE-Texas moves; treating those as renames would fabricate
      discontinuity for stable symbols).

    Both lists are sorted for determinism. Pure; never raises."""
    away = set()
    adoptions = set()
    store = events_store if isinstance(events_store, dict) else {}
    own = store.get(ticker)
    if isinstance(own, dict):
        events = [e for e in (own.get("events") or [])
                  if isinstance(e, dict) and e.get("type") == "ticker_change"]
        for index, event in enumerate(events):
            d = _iso_or_none(event.get("date"))
            if d is None:
                continue
            new = _new_ticker(event)
            if new is not None and new != ticker:
                away.add((d, new))
            elif new == ticker and index + 1 < len(events):
                older = _new_ticker(events[index + 1])
                if older is not None and older != ticker:
                    adoptions.add((d, older))
    for key, row in store.items():
        if key == ticker or not isinstance(row, dict):
            continue
        for event in row.get("events") or []:
            if not isinstance(event, dict) or event.get("type") != "ticker_change":
                continue
            if _new_ticker(event) != ticker:
                continue
            d = _iso_or_none(event.get("date"))
            if d is not None:
                adoptions.add((d, key))
    return sorted(away), sorted(adoptions)


class FinanceIssuerContinuityGate(Gate):
    """Guard the FINANCIALS-backed families (quarterly_revenue / market_cap)
    against a symbol-keyed history that is not the entity's own continuous
    history — the second rename class the anchor-price guard cannot see.

    The financials cache is fetched BY CURRENT UNIVERSE SYMBOL, but a symbol
    is not an issuer: Polygon's per-symbol history can mix issuers and
    truncate at renames. Two verified defects motivate the two checks:

    * ISSUER MISMATCH — ticker 'Q' (Qnity Electronics, CIK 2058873, adopted
      the symbol 2025-10-27) returns Quintiles-IMS-era filings (CIK 1478242),
      so 'last quarter FILED on/before the cutoff' resolved to another
      issuer's 2017 quarter. Check: a side's chosen filing carries a CIK that
      differs from the entity's universe CIK -> review.
    * SYMBOL-HISTORY DISCONTINUITY — Fiserv renamed FISV->FI on 2023-06-07;
      the FISV-keyed cache has no 2023–2025 filings (they live under FI), so
      the before side landed on a 3-year-old quarter whose CIK still matches.
      Check: the symbol was ADOPTED by (or the issuer moved AWAY from) the
      record's symbol AFTER the before filing's report date -> the cache
      cannot be shown to contain every later pre-bound filing (adoption: the
      pre-adoption rows may be another issuer's; away: the post-rename rows
      are missing) -> review.

    ``review``, NEVER fail — both are resolvable by refetching the history by
    CIK / under the issuer's other symbols, so a human resolves them; nothing
    is silently dropped and nothing contaminated is silently included. The
    CIK check runs on BOTH sides' filings; the discontinuity check anchors on
    the BEFORE side (the after side's filing postdates any adoption of the
    current symbol whenever it is the issuer's own — and if it is not, its
    CIK mismatch fires). A missing side, missing events row, or unknown CIK
    contributes nothing (other gates already review absent values; this gate
    never fabricates a verdict from absence). Non-financials families PASS."""

    name = "finance_issuer_continuity"
    version = "finance_issuer_continuity:v1"

    FAMILIES = (QUARTERLY_REVENUE, MARKET_CAP)

    @staticmethod
    def _sides(record):
        """{'before': filing-summary|None, 'after': filing-summary|None} from
        the record's provenance (quarter summaries for revenue, the chosen
        shares filings for market_cap)."""
        provenance = getattr(record, "provenance", None)
        provenance = provenance if isinstance(provenance, dict) else {}
        family = provenance.get("family")
        if family == QUARTERLY_REVENUE:
            return {"before": provenance.get("before_quarter"),
                    "after": provenance.get("after_quarter")}
        shares = provenance.get("shares_filings")
        shares = shares if isinstance(shares, dict) else {}
        return {"before": shares.get("before"), "after": shares.get("after")}

    def evaluate(self, record, ctx: dict) -> GateResult:
        if getattr(record, "property", None) not in self.FAMILIES:
            return GateResult(
                name=self.name, version=self.version, verdict="pass",
                evidence={"note": "issuer-continuity guard applies to "
                                  "quarterly_revenue/market_cap only",
                          "not_applicable": True},
            )

        entity = getattr(record, "entity", None)
        ids = entity.get("ids") if isinstance(entity, dict) else None
        ticker = ids.get("ticker") if isinstance(ids, dict) else None
        entity_cik = _cik_int(ids.get("cik") if isinstance(ids, dict) else None)

        sides = self._sides(record)
        evidence: dict = {
            "ticker": ticker,
            "entity_cik": entity_cik,
            "sides": {
                name: (None if not isinstance(side, dict) else
                       {"cik": side.get("cik"), "end_date": side.get("end_date"),
                        "report_date": side.get("report_date")})
                for name, side in sides.items()
            },
        }
        problems: list = []

        # (1) issuer mismatch: a side's filing CIK != the entity's CIK.
        for side_name in ("before", "after"):
            side = sides.get(side_name)
            if not isinstance(side, dict):
                continue
            side_cik = _cik_int(side.get("cik"))
            if entity_cik is not None and side_cik is not None and side_cik != entity_cik:
                problems.append(
                    f"issuer_mismatch:{side_name}: filing CIK {side_cik} != entity CIK "
                    f"{entity_cik} — the {side_name} value belongs to a different issuer "
                    "that held this symbol"
                )

        # (2) symbol-history discontinuity after the BEFORE filing's report date.
        before = sides.get("before")
        before_ref = None
        if isinstance(before, dict):
            before_ref = (_iso_or_none(before.get("report_date"))
                          or _iso_or_none(before.get("filing_date"))
                          or _iso_or_none(before.get("end_date")))
        evidence["before_reference_date"] = before_ref
        if isinstance(ticker, str) and ticker and before_ref:
            events_store = ctx.get(EVENTS_CTX_KEY) if isinstance(ctx, dict) else None
            away, adoptions = _symbol_history_events(events_store, ticker)
            late_away = [(d, nt) for d, nt in away if d > before_ref]
            late_adoptions = [(d, prior) for d, prior in adoptions if d > before_ref]
            if late_away:
                evidence["symbol_abandoned_after_before_filing"] = [
                    {"date": d, "new_ticker": nt} for d, nt in late_away]
                dates = ", ".join(d for d, _ in late_away)
                problems.append(
                    f"symbol_abandoned_after_before_filing: the issuer moved away from "
                    f"{ticker!r} on {dates}, after the before filing was reported "
                    f"({before_ref}) — later pre-bound filings live under another symbol, "
                    "so the symbol-keyed history is provably truncated"
                )
            if late_adoptions:
                evidence["symbol_adopted_after_before_filing"] = [
                    {"date": d, "prior_ticker": prior} for d, prior in late_adoptions]
                dates = ", ".join(d for d, _ in late_adoptions)
                problems.append(
                    f"symbol_adopted_after_before_filing: {ticker!r} was adopted on "
                    f"{dates}, after the before filing was reported ({before_ref}) — "
                    "the pre-adoption history under this symbol may belong to a "
                    "different issuer"
                )

        if problems:
            evidence["problem"] = (
                "issuer_continuity: " + "; ".join(problems)
                + " — held for review (resolvable by refetching the financial "
                  "history by CIK / under the issuer's prior symbols), never failed"
            )
            return GateResult(name=self.name, version=self.version, verdict="review",
                              evidence=evidence)
        evidence["note"] = (
            "filing CIKs match the entity and no symbol adoption/abandonment "
            "postdates the before filing"
        )
        return GateResult(name=self.name, version=self.version, verdict="pass",
                          evidence=evidence)


def _evidence_ref(value_state) -> dict:
    """The evidence.ref dict of a ValueState (before/after side), or {} when
    absent/malformed. Pure and total."""
    ev = getattr(value_state, "evidence", None)
    ref = getattr(ev, "ref", None)
    return ref if isinstance(ref, dict) else {}


def _implied_shares(ref: dict):
    """Implied share count for one market_cap side: PREFER the recorded
    evidence.ref.shares.shares (the exact value the cap was computed from), else
    fall back to computed_market_cap / close. None when neither is derivable.
    Pure and total."""
    if not isinstance(ref, dict):
        return None
    shares = ref.get("shares")
    if isinstance(shares, dict) and _is_num(shares.get("shares")):
        return float(shares["shares"])
    cmc, close = ref.get("computed_market_cap"), ref.get("close")
    if _is_num(cmc) and _is_num(close) and close != 0:
        return cmc / close
    return None


def _share_price_move_ratio(record):
    """after_close / before_close for a share_price record, read from each side's
    ``evidence.ref.close`` (the grouped daily close; canonical is not yet parsed at
    gate time). None when either close is missing/non-numeric or before is zero.
    Pure and total."""
    before = _evidence_ref(getattr(record, "before", None)).get("close")
    after = _evidence_ref(getattr(record, "after", None)).get("close")
    if not (_is_num(before) and _is_num(after)) or before == 0:
        return None
    return float(after) / float(before)


class FinanceShareStabilityGate(Gate):
    """Per-record market_cap plausibility screen (market_cap ONLY).

    market_cap is a PERIOD-AVERAGE-shares APPROXIMATION: cap = avg_shares(t) x
    close(t), where avg_shares is the income-statement fiscal-period weighted
    average (Polygon exposes no point-in-time shares_outstanding; owner-accepted,
    labeled ``point_in_time=false``). That approximation fails two ways among
    INCLUDED rows:

    * a MALFORMED weighted-average share count — Waters Corporation's 59.5M ->
      82 BILLION shares (a ~1380x units bug) computing an impossible $22.07B ->
      $30.81T cap; also Netflix, ServiceNow, Texas Pacific Land, and Booking's
      cap side (bad shares);
    * a REAL corporate action the period-average cannot be trusted through —
      Amcor / Omnicom / Paramount Skydance M&A share-structure changes.

    Because cap = shares x price the implied share count each side is recoverable,
    and a real share count cannot move >1.5x or <0.67x across the ~5-month window
    absent a corporate action. So an implied ratio shares_after/shares_before
    outside [SHARE_STABILITY_MIN, SHARE_STABILITY_MAX] means the cap value is
    unreliable -> ``review`` (NEVER fail: resolvable with a genuine point-in-time
    share count). A FALLBACK absolute sanity catches any computed cap >
    CAP_ABSOLUTE_MAX ($5e12) regardless of the ratio (only a handful of firms
    approach ~$4T in 2026). The implied ratio is recorded in the gate evidence.

    Implied shares per side PREFER the recorded ``evidence.ref.shares.shares``
    (the exact value the cap was computed from), else ``computed_market_cap /
    close``. A side whose implied shares cannot be determined contributes no
    ratio (a missing side already degrades via value_parsed), yet the absolute
    cap sanity still runs on whatever caps ARE computable. Non-market_cap
    families are not applicable and PASS."""

    name = "finance_share_stability"
    version = "finance_share_stability:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        if getattr(record, "property", None) != MARKET_CAP:
            return GateResult(
                name=self.name, version=self.version, verdict="pass",
                evidence={"note": "share-stability screen applies to market_cap only",
                          "not_applicable": True},
            )
        before_ref = _evidence_ref(getattr(record, "before", None))
        after_ref = _evidence_ref(getattr(record, "after", None))
        shares_before = _implied_shares(before_ref)
        shares_after = _implied_shares(after_ref)
        cap_before = before_ref.get("computed_market_cap")
        cap_after = after_ref.get("computed_market_cap")

        ratio = None
        if (shares_before is not None and shares_after is not None
                and shares_before != 0):
            ratio = shares_after / shares_before

        evidence = {
            "shares_before": shares_before,
            "shares_after": shares_after,
            "implied_share_move_x": ratio,
            "computed_market_cap_before": cap_before if _is_num(cap_before) else None,
            "computed_market_cap_after": cap_after if _is_num(cap_after) else None,
            "share_move_band": [SHARE_STABILITY_MIN, SHARE_STABILITY_MAX],
            "cap_absolute_max": CAP_ABSOLUTE_MAX,
        }
        problems = []
        if ratio is not None and (ratio < SHARE_STABILITY_MIN or ratio > SHARE_STABILITY_MAX):
            problems.append(
                f"implied_share_move_x {ratio:.4g} outside [{SHARE_STABILITY_MIN}, "
                f"{SHARE_STABILITY_MAX}]: a real share count cannot move this far in the "
                "window absent a corporate action, so the period-average-shares cap is "
                "unreliable"
            )
        for side_name, cap in (("before", cap_before), ("after", cap_after)):
            if _is_num(cap) and cap > CAP_ABSOLUTE_MAX:
                problems.append(
                    f"implausible_market_cap: {side_name} cap {cap:.4g} > {CAP_ABSOLUTE_MAX:.3g} "
                    "(above any real 2026 market capitalization)"
                )
        if problems:
            evidence["problem"] = (
                "share_stability: " + "; ".join(problems)
                + " — held for review (resolvable with a genuine point-in-time share "
                  "count), never failed"
            )
            return GateResult(name=self.name, version=self.version, verdict="review",
                              evidence=evidence)
        evidence["note"] = (
            "implied share count stable within the band and computed cap below the "
            "absolute sanity ceiling"
        )
        return GateResult(name=self.name, version=self.version, verdict="pass",
                          evidence=evidence)


class FinancePriceVerifyGate(Gate):
    """Consult the committed price-verification sidecar for the share_price
    family (share_price ONLY).

    The release close is Polygon grouped-daily (UNADJUSTED). Most large post-
    cutoff moves are the REAL signal the benchmark wants and MUST be kept (the
    H1-2026 semiconductor rally: Micron 415->1154, Intel 46->139, Dell 114->431,
    Marvell 79->298, Sandisk 576->2273, ...). A handful, though, are split
    artifacts: an UNADJUSTED anchor close compared across an in-window stock
    split to an UNADJUSTED asof close manufactures a spurious move (Booking's
    ~25:1 split turned a real ~-11% move into a raw -96%). This is a raw sole-
    source contamination not internally detectable — so a one-time TARGETED live
    Polygon re-fetch of ONLY the ~10 suspicious movers (>70% down or >150% up)
    was frozen into ``price_verify.jsonl``: per (ticker, side) it records the
    release grouped (unadjusted) close and the split-normalized ADJUSTED per-
    ticker close, and a per-ticker verdict — ``bad_tick_review`` when the two
    diverge on either endpoint (a corporate action inside the window), else
    ``real_kept`` (the adjusted and unadjusted series agree; a genuine price
    move).

    This gate reads ONLY that committed sidecar (offline, deterministic — the
    verdict is computed once at sidecar-build time from the frozen refetch).
    Owner decision 2026-07-23 — ADJUST, do not exclude: a ticker in Polygon's
    AUTHORITATIVE ``/v3/reference/splits`` for the window is verdict
    ``split_adjusted``; ``_build_share_price`` has already put its before/after on
    the split-adjusted current basis (from the sidecar's ``refetched_close``), so
    the gate PASSes it — KEPT (value_changed + the material-change policy decide
    inclusion; the three previously-reviewed splits BKNG/CVNA/KLAC become
    adjusted-and-kept, and a split ticker independently held by another gate — DD's
    in-window ticker-rename event — still reviews on THAT reason with its value
    adjusted). A ``real_kept`` ticker -> ``pass`` (KEPT — the benchmark's wanted
    signal). A legacy ``bad_tick_review`` (an uncorrectable corporate action that
    cannot be put on a consistent basis) -> ``review`` (NEVER fail). A ticker
    ABSENT from a PRESENT sidecar is definitionally a non-split (the sidecar
    enumerates every split) and PASSes untouched on its grouped close.

    A wholly ABSENT sidecar (a broken/legacy derive — the sanctioned snapshot path
    can no longer reach here missing the sidecar, since it is a rule-5b required
    input) does NOT fail open: a share_price record whose own endpoint move is a
    SUSPECT (>=+150% or <=-70%, the band the re-fetch enumerated) is held for
    ``review`` — the move cannot be adjudicated real-vs-artifact without the
    sidecar, so it is never silently included (matching 'missing input -> review,
    never silent include'); a non-suspect (ordinary) move still PASSes. Thus the
    bad-tick movers can never revert to INCLUDED merely because the sidecar went
    missing. Non-share_price families are not applicable and PASS."""

    name = "finance_price_verify"
    version = "finance_price_verify:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        if getattr(record, "property", None) != SHARE_PRICE:
            return GateResult(
                name=self.name, version=self.version, verdict="pass",
                evidence={"note": "price-verify screen applies to share_price only",
                          "not_applicable": True},
            )
        entity = getattr(record, "entity", None)
        ids = entity.get("ids") if isinstance(entity, dict) else None
        ticker = ids.get("ticker") if isinstance(ids, dict) else None

        store = ctx.get(PRICE_VERIFY_CTX_KEY) if isinstance(ctx, dict) else None
        store = store if isinstance(store, dict) else {}
        present = bool((ctx.get(CACHE_INFO_CTX_KEY) or {}).get("price_verify", {}).get("present")) \
            if isinstance(ctx, dict) else False
        entry = store.get(ticker) if isinstance(ticker, str) else None

        evidence = {"ticker": ticker, "verify_present": present}
        if not isinstance(entry, dict):
            if present:
                # The sidecar is authoritative and lists every suspect, so a ticker
                # not in it is a non-suspect: leave the grouped close untouched.
                evidence["note"] = "ticker not in the verified-suspect set; grouped close untouched"
                return GateResult(name=self.name, version=self.version, verdict="pass",
                                  evidence=evidence)
            # Sidecar wholly absent -> fail CLOSED for a suspect move: without the
            # adjudication we cannot tell a real move from a split artifact/bad tick,
            # so a >=+150% / <=-70% mover is held for review, never silently included.
            ratio = _share_price_move_ratio(record)
            evidence["implied_move_x"] = ratio
            if ratio is not None and (ratio >= PRICE_SUSPICIOUS_UP or ratio <= PRICE_SUSPICIOUS_DOWN):
                evidence["problem"] = (
                    f"price_verify: the committed price-verification sidecar is ABSENT, so this "
                    f"suspicious endpoint move (x{ratio:.4g}, at/beyond the "
                    f"[{PRICE_SUSPICIOUS_DOWN}, {PRICE_SUSPICIOUS_UP}] band) cannot be adjudicated "
                    "as a genuine move vs a split artifact / bad tick — held for review (resolvable "
                    "by restoring the sidecar), never silently included and never failed"
                )
                return GateResult(name=self.name, version=self.version, verdict="review",
                                  evidence=evidence)
            evidence["note"] = (
                "price-verify sidecar absent; no computable endpoint move to screen, left untouched"
                if ratio is None else
                "price-verify sidecar absent; endpoint move within the non-suspect band, "
                "left untouched"
            )
            return GateResult(name=self.name, version=self.version, verdict="pass",
                              evidence=evidence)

        evidence["verdict"] = entry.get("verdict")
        evidence["sides"] = entry.get("sides")
        evidence["source"] = entry.get("source")
        if entry.get("verdict") == PRICE_VERIFY_SPLIT:
            # ADJUST, don't exclude: _build_share_price has already put the
            # before/after on the split-adjusted current basis, so the emitted move
            # is the real economics. KEEP it (value_changed + the material-change
            # policy decide inclusion). Never review on the split alone.
            evidence["split"] = entry.get("split")
            evidence["note"] = (
                "in-window stock split (authoritative /v3/reference/splits): the before/after "
                "are on the split-adjusted current basis, so the move is the REAL economic drift, "
                "not a split artifact — kept (subject to value_changed + the material-change policy)"
            )
            return GateResult(name=self.name, version=self.version, verdict="pass",
                              evidence=evidence)
        if entry.get("verdict") == PRICE_VERIFY_BAD:
            evidence["problem"] = (
                "price_verify: the release grouped (unadjusted) close diverges from the "
                "split-adjusted per-ticker re-fetch inside the window (a corporate action / "
                "stock split), so the raw endpoint-to-endpoint move is a split artifact, not "
                "a real price drift — held for review (resolvable by re-anchoring the "
                "split-adjusted close), never failed"
            )
            return GateResult(name=self.name, version=self.version, verdict="review",
                              evidence=evidence)
        evidence["note"] = (
            "targeted live re-fetch confirms a genuine price move (adjusted close matches the "
            "grouped close on both endpoints); kept included"
        )
        return GateResult(name=self.name, version=self.version, verdict="pass",
                          evidence=evidence)


class FinanceCrossSourceGate(Gate):
    """Independent asof-close cross-check for the continuous families (price /
    market_cap), reading the Stooq result stamped into
    ``provenance['cross_check']`` by build_record.

    Agreement can PASS; NOTHING here can FAIL. Owner decision (2026-07-20):
    Polygon (SIP consolidated tape) is the declared SOLE ground truth and the
    Stooq cross-check is dropped (JS-challenge blocked), so an unavailable /
    absent independent close PASSES with an ``uncorroborated=True`` flag (not
    review) — single-source is acceptable for large-cap official closes. The
    only ``review`` outcomes are a real disagreement between two PRESENT
    sources (> ~1%) or a missing Polygon close (a genuine data gap).
    Non-price/cap families are not applicable and PASS."""

    name = "cross_source_agreement"
    version = "cross_source_agreement:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        if getattr(record, "property", None) not in CONTINUOUS_FAMILIES:
            return GateResult(
                name=self.name, version=self.version, verdict="pass",
                evidence={"note": "cross-check applies to price/market_cap only", "not_applicable": True},
            )
        provenance = getattr(record, "provenance", None)
        cc = provenance.get("cross_check") if isinstance(provenance, dict) else None
        cc = cc if isinstance(cc, dict) else {}
        status = cc.get("cross_check_status")
        polygon_close = cc.get("polygon_asof_close")
        stooq_close = cc.get("stooq_close")
        tolerance = cc.get("tolerance", CROSS_CHECK_TOLERANCE)
        evidence = {
            "cross_check_status": status,
            "reason": cc.get("reason"),
            "source": cc.get("source"),
            "polygon_asof_close": polygon_close if _is_num(polygon_close) else None,
            "stooq_close": stooq_close if _is_num(stooq_close) else None,
            "tolerance": tolerance,
        }

        def result(verdict: str, problem: str = None, **extra) -> GateResult:
            ev = dict(evidence)
            if problem:
                ev["problem"] = problem
            ev.update(extra)
            return GateResult(name=self.name, version=self.version, verdict=verdict, evidence=ev)

        # Owner decision (2026-07-20): Polygon (SIP consolidated tape) is the
        # declared SOLE ground truth; the Stooq cross-check is dropped (blocked
        # by a JS challenge). An absent/unavailable independent close therefore
        # PASSES with an ``uncorroborated`` flag rather than resting in review —
        # single-source is acceptable because large-cap official closes are
        # penny-identical across vendors. The ONLY review outcome is a genuine
        # disagreement between two present sources (kept live for when an
        # independent feed is wired). A missing Polygon close is a real data
        # gap and still reviews.
        if not _is_num(polygon_close):
            return result("review", "no Polygon asof close (missing/unpriced ticker)")
        if status != "ok" or not _is_num(stooq_close):
            return result(
                "pass",
                uncorroborated=True,
                note=("single-source (Polygon SIP consolidated tape); no independent "
                      f"cross-check available ({cc.get('reason') or status!r})"),
            )
        denom = max(abs(polygon_close), abs(stooq_close))
        rel = 0.0 if denom == 0 else abs(polygon_close - stooq_close) / denom
        evidence["relative_difference"] = rel
        if rel <= tolerance:
            return result("pass", agreement=True)
        return result(
            "review",
            f"sources_disagree: Polygon vs Stooq asof close differ by {rel:.4f} "
            f"(> {tolerance}); held for review, never failed",
        )


class FinanceMaterialChangeGate(Gate):
    """POLICY gate for the continuous families (price / market_cap),
    manifest-recorded and one-flag revisable via ``cfg['finance_policy']``.

    Default ``price_min_drift = None`` (OFF): drift is a PURE covariate (kept
    in ``provenance['drift']`` for Stage-2 stratification), so this PASSes with
    a note — "stratify, don't filter". A configured float threshold reviews
    sub-threshold |drift| moves (a sensitivity-analysis knob), NEVER fails.
    Non-price/cap families are not applicable and PASS."""

    name = "finance_material_change"
    version = "finance_material_change:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        if getattr(record, "property", None) not in CONTINUOUS_FAMILIES:
            return GateResult(
                name=self.name, version=self.version, verdict="pass",
                evidence={"note": "material-change policy applies to price/market_cap only",
                          "not_applicable": True},
            )
        policy = ctx.get(POLICY_ACTIVE_CTX_KEY) if isinstance(ctx, dict) else None
        threshold = policy.get("price_min_drift") if isinstance(policy, dict) else None
        provenance = getattr(record, "provenance", None)
        drift = provenance.get("drift") if isinstance(provenance, dict) else None
        evidence = {"policy": "price_min_drift", "policy_setting": threshold,
                    "drift": drift if _is_num(drift) else None}
        if not (isinstance(threshold, (int, float)) and not isinstance(threshold, bool)):
            evidence["note"] = "no drift threshold configured (drift is a covariate, not a filter)"
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
        if not _is_num(drift):
            evidence["problem"] = "material-change policy is on but drift is unavailable"
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        if abs(drift) < threshold:
            evidence["problem"] = (
                f"sub_threshold_move: |drift| {abs(drift):.6f} < policy price_min_drift {threshold}"
            )
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        evidence["note"] = "drift meets the configured materiality threshold"
        return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)


class FinanceWindowBindingGate(Gate):
    """Bind every finance record to the (cutoff, asof) window the frozen cache
    was BUILT for (Finding 2, adapter side).

    The frozen caches are pinned to one window (recorded in each sidecar's
    ``cutoff``/``asof``). Running the offline adapter with different window
    flags than the cache was fetched under would silently pin values to the
    cache's window while the manifest reports the run's window — internally
    inconsistent provenance. This gate reads the price cache's sidecar window
    (surfaced into CACHE_INFO by ``_declare_cache``) and compares it to the
    run's ctx cutoff/asof:

    * windows AGREE (or the sidecar carries no window to check) -> ``pass``;
    * windows DIFFER -> ``review`` (never fail, mirroring 'missing cache ->
      review, never silent include'): the record is held for a human rather
      than emitted with window-mixed provenance.

    Applies to every finance family (all five depend on the window). In the
    sanctioned flow the fetch and the adapter share the same flags, so this
    passes; it only fires on a genuine window/cache mismatch."""

    name = "window_binding"
    version = "window_binding:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        cache_info = ctx.get(CACHE_INFO_CTX_KEY) if isinstance(ctx, dict) else None
        price = (cache_info or {}).get("price_grouped") or {}
        meta = price.get("meta") if isinstance(price, dict) else None
        cache_cutoff = meta.get("cutoff") if isinstance(meta, dict) else None
        cache_asof = meta.get("asof") if isinstance(meta, dict) else None

        run_cutoff = _ctx_date_iso(ctx, "cutoff")
        run_asof = _ctx_date_iso(ctx, "asof")

        evidence = {
            "cache_cutoff": cache_cutoff, "cache_asof": cache_asof,
            "run_cutoff": run_cutoff, "run_asof": run_asof,
            "price_cache_present": bool(price.get("present")),
        }

        mismatches = []
        if isinstance(cache_cutoff, str) and run_cutoff is not None and cache_cutoff != run_cutoff:
            mismatches.append(f"cutoff (cache {cache_cutoff!r} != run {run_cutoff!r})")
        if isinstance(cache_asof, str) and run_asof is not None and cache_asof != run_asof:
            mismatches.append(f"asof (cache {cache_asof!r} != run {run_asof!r})")

        if mismatches:
            evidence["problem"] = (
                "window_mismatch: the frozen cache was built for a different "
                "(cutoff, asof) window than this run — " + "; ".join(mismatches)
                + "; held for review to avoid window-mixed provenance"
            )
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        if not (isinstance(cache_cutoff, str) or isinstance(cache_asof, str)):
            evidence["note"] = "no sidecar window recorded to bind against (nothing to check)"
        else:
            evidence["note"] = "cache window matches the run window"
        return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)


def _ctx_date_iso(ctx: dict, key: str):
    """The run's cutoff/asof from ctx as an ISO string (date, datetime, or
    'YYYY-MM-DD' string all accepted), or None."""
    value = ctx.get(key) if isinstance(ctx, dict) else None
    if isinstance(value, _date):
        return value.isoformat()
    return _iso_or_none(value)


# --------------------------------------------------------------------------- #
# The adapter
# --------------------------------------------------------------------------- #
class FinanceAdapter(Adapter):
    """Adapter for source 'finance'. See the module docstring for the design.

    Stateless: the universe and the five frozen caches are loaded once per run
    into cfg (the runner's sanctioned ctx channel), never onto the instance."""

    source = SOURCE

    # -- enumeration --------------------------------------------------------
    def enumerate_candidates(self, cfg: dict):
        """Yield EVERY candidate in EVERY family, deterministically ordered.
        No inclusion decision here — a missing-cache ticker is emitted as a
        degraded candidate (its record degrades to review), never dropped."""
        self._require_offline(cfg)
        self._ensure_loaded(cfg)
        universe = cfg[UNIVERSE_MAP_CTX_KEY]
        events = cfg[EVENTS_CTX_KEY]

        # Four S&P families: one candidate per universe ticker (sorted).
        for ticker in sorted(universe):
            entity = universe[ticker]
            base = {"_ticker": ticker, "_name": entity.get("name") or ticker,
                    "_cik": entity.get("cik") or ""}
            yield {**base, "_family": SHARE_PRICE}
            yield {**base, "_family": MARKET_CAP}
            yield {**base, "_family": QUARTERLY_REVENUE}
            # ticker_change: one candidate per ticker_change event; a ticker
            # ABSENT from the events cache is unknown (was it fetched?) and
            # degrades to a review candidate; a cached ticker with zero events
            # genuinely has no ticker-change fact (no candidate).
            row = events.get(ticker)
            if not isinstance(row, dict):
                yield {**base, "_family": TICKER_CHANGE, "_event_index": None,
                       "_cache_missing": True}
                continue
            evs = [e for e in (row.get("events") or [])
                   if isinstance(e, dict) and e.get("type") == "ticker_change"]
            for index in range(len(evs)):
                yield {**base, "_family": TICKER_CHANGE, "_event_index": index,
                       "_events": evs, "_figi": row.get("composite_figi"),
                       "_events_name": row.get("name")}

        # IPO family: EVERY market-wide IPO row (its own universe; scope gate
        # judges). Sorted by (listing_date, ticker) for determinism.
        ipo_rows = cfg[IPO_CTX_KEY]
        for pos, row in enumerate(
            sorted(ipo_rows, key=lambda r: (str(r.get("listing_date") or ""),
                                            str(r.get("ticker") or ""), )),
        ):
            yield {"_family": IPO_LISTING, "_ipo": row, "_pos": pos}

    # -- record building ----------------------------------------------------
    def build_record(self, candidate: dict, cfg: dict) -> FactChangeRecord:
        self._ensure_loaded(cfg)
        family = candidate.get("_family")
        if family == SHARE_PRICE:
            record = self._build_share_price(candidate, cfg)
        elif family == MARKET_CAP:
            record = self._build_market_cap(candidate, cfg)
        elif family == QUARTERLY_REVENUE:
            record = self._build_revenue(candidate, cfg)
        elif family == TICKER_CHANGE:
            record = self._build_ticker_change(candidate, cfg)
        elif family == IPO_LISTING:
            record = self._build_ipo(candidate, cfg)
        else:
            raise ValueError(f"unknown finance family: {family!r}")
        # Owner decision B: stamp the predictability tag on EVERY record (all
        # dispositions) as Stage-2 metadata. Purely additive to provenance and
        # read by no gate, so it cannot change any disposition/value/change_date.
        record.provenance["predictability"] = check_predictability(
            PREDICTABILITY_BY_FAMILY[family]
        )
        return record

    # -- per-family builders ------------------------------------------------
    def _entity(self, candidate: dict) -> dict:
        ids = {"ticker": candidate["_ticker"]}
        cik = candidate.get("_cik")
        if isinstance(cik, str) and cik:
            ids["cik"] = cik
        return {"name": candidate.get("_name") or candidate["_ticker"], "ids": ids}

    def _asof_day(self, cfg: dict) -> str:
        resolved = cfg.get(PRICE_RESOLVED_CTX_KEY) or {}
        return resolved.get("asof") or cfg["asof"].isoformat()

    def _anchor_day(self, cfg: dict) -> str:
        resolved = cfg.get(PRICE_RESOLVED_CTX_KEY) or {}
        return resolved.get("anchor") or cfg["cutoff"].isoformat()

    def _price_rows(self, cfg: dict, ticker: str):
        pr = cfg[PRICE_CTX_KEY].get(ticker) or {}
        return pr.get("anchor"), pr.get("asof")

    @staticmethod
    def _close(row):
        """The close from a price row when present & numeric, else None."""
        if not isinstance(row, dict) or row.get("status") != "present":
            return None
        return row.get("c") if _is_num(row.get("c")) else None

    def _cache_info(self, cfg: dict, family: str) -> dict:
        info = (cfg.get(CACHE_INFO_CTX_KEY) or {}).get(family) or {}
        return {"file": info.get("file"), "sha1": info.get("sha1"),
                "present": bool(info.get("present"))}

    def _cross_check(self, cfg: dict, ticker: str, asof_day: str, polygon_close) -> dict:
        """The cross_check provenance block for a price/cap record: the Stooq
        row (or a cache_missing/no_row sentinel) plus the Polygon close the
        gate compares against. Never raises."""
        store = cfg.get(CROSS_CHECK_CTX_KEY) or {}
        present = self._cache_info(cfg, "cross_check")["present"]
        row = store.get(ticker)
        block = {
            "ticker": ticker,
            "asof_date": asof_day,
            "polygon_asof_close": polygon_close if _is_num(polygon_close) else None,
            "tolerance": CROSS_CHECK_TOLERANCE,
        }
        if isinstance(row, dict):
            block["cross_check_status"] = row.get("cross_check_status")
            block["stooq_close"] = row.get("close")
            block["reason"] = row.get("reason")
            block["source"] = row.get("source")
            block["stooq_asof_date"] = row.get("asof_date")
        elif not present:
            block["cross_check_status"] = "cache_missing"
            block["stooq_close"] = None
            block["reason"] = "cross_check cache not found"
            block["source"] = None
        else:
            block["cross_check_status"] = "no_row"
            block["stooq_close"] = None
            block["reason"] = "ticker absent from the cross_check cache"
            block["source"] = None
        return block

    def _split_adjustment(self, cfg: dict, ticker: str):
        """The split-adjusted (anchor, asof) closes for a share_price ticker that
        is a ``split_adjusted`` entry in the committed sidecar, else None. The
        adjusted close is the sidecar's frozen per-endpoint ``refetched_close``
        (== grouped x split_from/split_to, on the current/post-split basis). A
        side whose adjusted close is missing stays None (the grouped close then
        stands — never fabricated). ONLY share_price consults this; market_cap is
        split-invariant (cap = shares x close). Pure and total."""
        entry = (cfg.get(PRICE_VERIFY_CTX_KEY) or {}).get(ticker)
        if not isinstance(entry, dict) or entry.get("verdict") != PRICE_VERIFY_SPLIT:
            return None
        by_role = entry.get("by_role") or {}

        def adj(role):
            value = (by_role.get(role) or {}).get("refetched_close")
            return float(value) if _is_num(value) else None

        return {"anchor": adj("anchor"), "asof": adj("asof"),
                "split": entry.get("split"), "source": entry.get("source")}

    def _build_share_price(self, candidate: dict, cfg: dict) -> FactChangeRecord:
        ticker = candidate["_ticker"]
        anchor_row, asof_row = self._price_rows(cfg, ticker)
        grouped_anchor, grouped_asof = self._close(anchor_row), self._close(asof_row)
        anchor_day, asof_day = self._anchor_day(cfg), self._asof_day(cfg)

        # SPLIT ADJUSTMENT (owner decision 2026-07-23): an authoritative in-window
        # split puts the emitted before/after on the split-adjusted current basis
        # so the move is the REAL economics, not a split artifact. A non-split
        # ticker returns None here and is byte-identical to the grouped derive.
        split_adj = self._split_adjustment(cfg, ticker)
        anchor_close, asof_close = grouped_anchor, grouped_asof
        if split_adj is not None:
            if split_adj["anchor"] is not None:
                anchor_close = split_adj["anchor"]
            if split_adj["asof"] is not None:
                asof_close = split_adj["asof"]

        drift = None
        if _is_num(anchor_close) and _is_num(asof_close) and anchor_close != 0:
            drift = (asof_close - anchor_close) / anchor_close

        anchor_ref = {"role": "anchor", "ticker": ticker, "close": anchor_close,
                      "requested_date": (anchor_row or {}).get("requested_date"),
                      "resolved_trading_day": (anchor_row or {}).get("resolved_trading_day") or anchor_day,
                      "step_back_days": (anchor_row or {}).get("step_back_days"),
                      "status": (anchor_row or {}).get("status", "absent"),
                      "basis": "continuous_endpoint_sample",
                      "source": (anchor_row or {}).get("source")}
        asof_ref = {"role": "asof", "ticker": ticker, "close": asof_close,
                    "requested_date": (asof_row or {}).get("requested_date"),
                    "resolved_trading_day": (asof_row or {}).get("resolved_trading_day") or asof_day,
                    "step_back_days": (asof_row or {}).get("step_back_days"),
                    "status": (asof_row or {}).get("status", "absent"),
                    "basis": "continuous_endpoint_sample",
                    "source": (asof_row or {}).get("source")}
        if split_adj is not None:
            # split rows disclose the adjustment; ``close`` is the ADJUSTED value,
            # the raw grouped close is preserved for audit.
            anchor_ref["grouped_close"] = grouped_anchor
            anchor_ref["split_adjusted"] = anchor_close != grouped_anchor
            asof_ref["grouped_close"] = grouped_asof
            asof_ref["split_adjusted"] = asof_close != grouped_asof

        before_ev = Evidence(kind="price_snapshot", url=None, as_of=anchor_day, ref=anchor_ref)
        after_ev = Evidence(kind="price_snapshot", url=None, as_of=asof_day, ref=asof_ref)
        provenance = {
            "family": SHARE_PRICE,
            "drift": drift,
            "polygon_cache": self._cache_info(cfg, "price_grouped"),
            "cross_check": self._cross_check(cfg, ticker, asof_day, asof_close),
            "anchor_day": anchor_day,
            "asof_day": asof_day,
        }
        if split_adj is not None:
            provenance["split_adjustment"] = {
                "split": split_adj["split"],
                "source": split_adj["source"],
                "grouped": {"anchor": grouped_anchor, "asof": grouped_asof},
                "adjusted": {"anchor": anchor_close, "asof": asof_close},
                "note": ("in-window stock split (authoritative /v3/reference/splits); the "
                         "anchor/asof closes are put on the current/post-split basis so "
                         "before->after is the REAL economic move, not a split artifact — for a "
                         "split ticker the model's parametric prior is the PRE-split price"),
            }
        return self._continuous_record(
            candidate, cfg, SHARE_PRICE, _num_str(anchor_close), _num_str(asof_close),
            before_ev, after_ev, asof_day, provenance,
        )

    def _build_market_cap(self, candidate: dict, cfg: dict) -> FactChangeRecord:
        ticker = candidate["_ticker"]
        anchor_row, asof_row = self._price_rows(cfg, ticker)
        anchor_close, asof_close = self._close(anchor_row), self._close(asof_row)
        anchor_day, asof_day = self._anchor_day(cfg), self._asof_day(cfg)

        fin_rows = cfg[FINANCIALS_CTX_KEY].get(ticker) or []
        # KNOWABILITY bounds (the market-cap side of the last-FILED rule): the
        # anchor/before shares filing must have been REPORTED on/before the
        # cutoff (the value was computable at the cutoff), the after side
        # on/before the asof. Mirrors select_quarters for quarterly_revenue.
        cutoff_iso = cfg["cutoff"].isoformat()
        asof_iso = cfg["asof"].isoformat()
        shares_anchor = select_shares(fin_rows, anchor_day, cutoff_iso)
        shares_asof = select_shares(fin_rows, asof_day, asof_iso)

        def cap(close, shares_pick):
            if not _is_num(close) or shares_pick is None or not _is_num(shares_pick.get("shares")):
                return None
            return shares_pick["shares"] * close

        cap_anchor, cap_asof = cap(anchor_close, shares_anchor), cap(asof_close, shares_asof)
        drift = None
        if _is_num(cap_anchor) and _is_num(cap_asof) and cap_anchor != 0:
            drift = (cap_asof - cap_anchor) / cap_anchor

        def shares_ref(shares_pick):
            if shares_pick is None:
                return {"shares": None, "field": None, "point_in_time": False,
                        "shares_basis": SHARES_BASIS, "filing": None,
                        "selection": "no_filing_with_period_end_on_or_before_pin",
                        "filed_by": None}
            row = shares_pick.get("row") or {}
            filing = None
            if shares_pick.get("row") is not None:
                filing = {"end_date": row.get("end_date"), "filing_date": row.get("filing_date"),
                          "report_date": _report_date(row), "cik": row.get("cik"),
                          "fiscal_period": row.get("fiscal_period"), "fiscal_year": row.get("fiscal_year"),
                          "source_filing_url": row.get("source_filing_url")}
            return {
                "shares": shares_pick.get("shares"),
                "field": shares_pick.get("field"),
                # HONEST LABEL (Finding 3): the share count is a fiscal-period
                # weighted AVERAGE (income-statement basic/diluted average
                # shares), NOT a point-in-time shares_outstanding. Polygon's
                # financials payload carries no balance-sheet shares line and a
                # per-date ticker-details call is excluded by owner decision.
                "point_in_time": False,
                "shares_basis": SHARES_BASIS,
                # KNOWABILITY disclosure: which bound governed the selection and
                # how it resolved (see select_shares) — auditable per side.
                "selection": shares_pick.get("selection"),
                "filed_by": shares_pick.get("filed_by"),
                "later_filings_excluded_by_bound": shares_pick.get(
                    "later_filings_excluded_by_bound"),
                "filing": filing,
            }

        before_ev = Evidence(
            kind="market_cap_snapshot", url=None, as_of=anchor_day,
            ref={"role": "anchor", "ticker": ticker, "close": anchor_close,
                 "resolved_trading_day": (anchor_row or {}).get("resolved_trading_day") or anchor_day,
                 "shares": shares_ref(shares_anchor), "computed_market_cap": cap_anchor,
                 "formula": MARKET_CAP_FORMULA, "point_in_time": False,
                 "shares_basis": SHARES_BASIS, "pin_date": anchor_day,
                 "basis": "continuous_endpoint_sample",
                 "approximation_note": MARKET_CAP_APPROX_NOTE},
        )
        after_ev = Evidence(
            kind="market_cap_snapshot", url=None, as_of=asof_day,
            ref={"role": "asof", "ticker": ticker, "close": asof_close,
                 "resolved_trading_day": (asof_row or {}).get("resolved_trading_day") or asof_day,
                 "shares": shares_ref(shares_asof), "computed_market_cap": cap_asof,
                 "formula": MARKET_CAP_FORMULA, "point_in_time": False,
                 "shares_basis": SHARES_BASIS, "pin_date": asof_day,
                 "basis": "continuous_endpoint_sample",
                 "approximation_note": MARKET_CAP_APPROX_NOTE},
        )
        def shares_filing_summary(shares_pick):
            """Compact issuer-continuity view of a side's chosen shares filing
            (read by finance_issuer_continuity): the filing's CIK plus the
            dates the continuity checks compare. None when no filing."""
            if shares_pick is None or shares_pick.get("row") is None:
                return None
            row = shares_pick["row"]
            return {"cik": row.get("cik"), "end_date": row.get("end_date"),
                    "filing_date": row.get("filing_date"),
                    "report_date": _report_date(row),
                    "fiscal_period": row.get("fiscal_period"),
                    "fiscal_year": row.get("fiscal_year"),
                    "selection": shares_pick.get("selection")}

        provenance = {
            "family": MARKET_CAP,
            "drift": drift,
            "polygon_cache": self._cache_info(cfg, "price_grouped"),
            "financials_cache": self._cache_info(cfg, "financials"),
            "cross_check": self._cross_check(cfg, ticker, asof_day, asof_close),
            "anchor_day": anchor_day,
            "asof_day": asof_day,
            "computed": {"before": cap_anchor, "after": cap_asof},
            "point_in_time": False,
            "shares_basis": SHARES_BASIS,
            "approximation_note": MARKET_CAP_APPROX_NOTE,
            # the per-side shares filings (issuer + dates), consumed by the
            # finance_issuer_continuity gate (Qnity/Fiserv class defects).
            "shares_filings": {"before": shares_filing_summary(shares_anchor),
                               "after": shares_filing_summary(shares_asof)},
        }
        return self._continuous_record(
            candidate, cfg, MARKET_CAP, _num_str(cap_anchor), _num_str(cap_asof),
            before_ev, after_ev, asof_day, provenance,
        )

    def _continuous_record(self, candidate, cfg, family, before_raw, after_raw,
                           before_ev, after_ev, asof_day, provenance):
        entity = self._entity(candidate)
        change_date = ChangeDate(
            value=asof_day, precision="day",
            evidence=Evidence(kind=f"{family}_snapshot", url=None, as_of=asof_day,
                              ref={"basis": "continuous_endpoint_sample",
                                   "note": "endpoint-sampled snapshot; change_date is the resolved "
                                           "asof trading day (asof in [cutoff, asof])"}),
        )
        fact_id = compute_fact_id(self.source, entity["name"], family, change_date.value)
        record_id = compute_record_id(fact_id, f"{family}|{candidate['_ticker']}|{asof_day}")
        return FactChangeRecord(
            fact_id=fact_id, record_id=record_id, source=self.source, entity=entity,
            property=family, value_type=QUANTITY,
            before=ValueState(raw=before_raw, canonical=None, evidence=before_ev),
            after=ValueState(raw=after_raw, canonical=None, evidence=after_ev),
            change_date=change_date, provenance=provenance,
        )

    def _build_revenue(self, candidate: dict, cfg: dict) -> FactChangeRecord:
        ticker = candidate["_ticker"]
        entity = self._entity(candidate)
        cutoff_iso = cfg["cutoff"].isoformat()
        asof_iso = cfg["asof"].isoformat()
        fin_rows = cfg[FINANCIALS_CTX_KEY].get(ticker) or []
        before_q, after_q = select_quarters(fin_rows, cutoff_iso, asof_iso)

        def quarter_ev(q, role):
            if not isinstance(q, dict):
                return "", Evidence(kind="financial_filing", url=None, as_of=None,
                                    ref={"role": role, "ticker": ticker, "status": "absent",
                                         "note": f"no {role} quarter in the financials cache"})
            ev = Evidence(
                kind="financial_filing", url=q.get("source_filing_url"), as_of=q.get("filing_date"),
                ref={"role": role, "ticker": ticker, "fiscal_period": q.get("fiscal_period"),
                     "fiscal_year": q.get("fiscal_year"), "end_date": q.get("end_date"),
                     "start_date": q.get("start_date"), "filing_date": q.get("filing_date"),
                     "revenues": q.get("revenues"), "revenues_unit": q.get("revenues_unit"),
                     "metric": "quarterly total revenues", "source": q.get("source")},
            )
            return _num_str(q.get("revenues")), ev

        before_raw, before_ev = quarter_ev(before_q, "last_pre_cutoff_quarter")
        after_raw, after_ev = quarter_ev(after_q, "first_post_cutoff_quarter")

        # change_date = the after quarter's period-of-report end (discrete);
        # when the after quarter is absent the record degrades to review via
        # value_parsed and the change_date is a placeholder inside the window.
        change_value = (after_q or {}).get("end_date") if isinstance(after_q, dict) else None
        change_value = _iso_or_none(change_value) or self._asof_day(cfg)
        change_basis = "period_of_report_end" if isinstance(after_q, dict) and _iso_or_none(after_q.get("end_date")) \
            else "no_post_cutoff_quarter_placeholder"
        change_date = ChangeDate(
            value=change_value, precision="day",
            evidence=Evidence(kind="financial_filing",
                              url=(after_q or {}).get("source_filing_url") if isinstance(after_q, dict) else None,
                              as_of=(after_q or {}).get("filing_date") if isinstance(after_q, dict) else None,
                              ref={"basis": change_basis, "ticker": ticker,
                                   "end_date": (after_q or {}).get("end_date") if isinstance(after_q, dict) else None}),
        )
        before_reported = _report_date(before_q) if isinstance(before_q, dict) else None
        provenance = {
            "family": QUARTERLY_REVENUE,
            "financials_cache": self._cache_info(cfg, "financials"),
            "before_quarter": self._quarter_summary(before_q),
            "after_quarter": self._quarter_summary(after_q),
            # the before side is the last quarter REPORTED on/before cutoff (a
            # pre-cutoff-knowable baseline); its reporting date is recorded so
            # the knowability guarantee is auditable (Finding 1).
            "before_reported_on": before_reported,
            "before_reported_on_or_before_cutoff": (
                before_reported is not None and before_reported <= cutoff_iso),
            # adjacency disclosure: whether before/after are consecutive
            # quarters or straddle a gap (folded fiscal-Q4 or a skipped
            # post-cutoff-filed quarter) (Finding 4).
            "adjacency": quarter_adjacency(before_q, after_q, fin_rows),
            "cutoff": cutoff_iso,
            "asof": asof_iso,
        }
        fact_id = compute_fact_id(self.source, entity["name"], QUARTERLY_REVENUE, change_date.value)
        record_id = compute_record_id(fact_id, f"{QUARTERLY_REVENUE}|{ticker}|{self._asof_day(cfg)}")
        return FactChangeRecord(
            fact_id=fact_id, record_id=record_id, source=self.source, entity=entity,
            property=QUARTERLY_REVENUE, value_type=QUANTITY,
            before=ValueState(raw=before_raw, canonical=None, evidence=before_ev),
            after=ValueState(raw=after_raw, canonical=None, evidence=after_ev),
            change_date=change_date, provenance=provenance,
        )

    @staticmethod
    def _quarter_summary(q):
        if not isinstance(q, dict):
            return None
        return {"fiscal_period": q.get("fiscal_period"), "fiscal_year": q.get("fiscal_year"),
                "end_date": q.get("end_date"), "filing_date": q.get("filing_date"),
                # issuer + report date, consumed by finance_issuer_continuity
                # (a before-quarter filed by a DIFFERENT issuer, or under a
                # symbol later adopted/abandoned, must never pass silently).
                "report_date": _report_date(q), "cik": q.get("cik"),
                "revenues": q.get("revenues")}

    def _build_ticker_change(self, candidate: dict, cfg: dict) -> FactChangeRecord:
        ticker = candidate["_ticker"]
        entity = self._entity(candidate)
        asof_day = self._asof_day(cfg)

        # Missing from the events cache -> degraded review candidate (unknown
        # whether the ticker had events; never a silent drop).
        if candidate.get("_cache_missing"):
            change_date = ChangeDate(
                value=asof_day, precision="day",
                evidence=Evidence(kind="ticker_event", url=None, as_of=None,
                                  ref={"basis": "cache_missing", "ticker": ticker,
                                       "note": "ticker absent from the ticker_events cache"}),
            )
            provenance = {"family": TICKER_CHANGE, "events_cache": self._cache_info(cfg, "ticker_events"),
                          "cache_missing": True}
            fact_id = compute_fact_id(self.source, entity["name"], TICKER_CHANGE, change_date.value)
            record_id = compute_record_id(fact_id, f"{TICKER_CHANGE}|{ticker}|cache_missing")
            miss_ev = Evidence(kind="ticker_event", url=None, as_of=None,
                               ref={"ticker": ticker, "status": "cache_missing"})
            return FactChangeRecord(
                fact_id=fact_id, record_id=record_id, source=self.source, entity=entity,
                property=TICKER_CHANGE, value_type=TEXT_SPAN,
                before=ValueState(raw="", canonical=None, evidence=miss_ev),
                after=ValueState(raw="", canonical=None, evidence=miss_ev),
                change_date=change_date, provenance=provenance,
            )

        evs = candidate["_events"]
        index = candidate["_event_index"]
        event = evs[index]
        # events are sorted newest-first; the previous ticker is the NEXT
        # (older) event's adopted symbol, unknown for the earliest event.
        new_ticker = event.get("new_ticker")
        old_ticker = evs[index + 1].get("new_ticker") if index + 1 < len(evs) else None
        after_raw = new_ticker if isinstance(new_ticker, str) else ""
        before_raw = old_ticker if isinstance(old_ticker, str) else ""
        event_date = _iso_or_none(event.get("date"))

        after_ev = Evidence(
            kind="ticker_event", url=None, as_of=event_date,
            ref={"ticker": ticker, "new_ticker": new_ticker, "event_date": event.get("date"),
                 "type": event.get("type"), "composite_figi": candidate.get("_figi")},
        )
        if before_raw:
            before_ev = Evidence(kind="ticker_event", url=None, as_of=None,
                                 ref={"ticker": ticker, "prior_ticker": old_ticker,
                                      "note": "the symbol in effect before this ticker_change event"})
        else:
            before_ev = Evidence(kind="ticker_history_start", url=None, as_of=None,
                                 ref={"ticker": ticker,
                                      "note": "no prior ticker_change event recorded; the pre-event "
                                              "symbol is unknown"})

        if event_date is not None:
            change_value, change_basis = event_date, "ticker_event_date"
        else:
            # Unparseable event date: route to review by dropping the values
            # (value_parsed reviews the empty sides) with an in-window
            # placeholder so the schema stays valid — never a fabricated
            # include on an unknown date.
            change_value, change_basis = asof_day, "event_date_unparseable"
            before_raw = after_raw = ""
        change_date = ChangeDate(
            value=change_value, precision="day",
            evidence=Evidence(kind="ticker_event", url=None, as_of=event_date,
                              ref={"basis": change_basis, "ticker": ticker,
                                   "event_date_raw": event.get("date")}),
        )
        provenance = {
            "family": TICKER_CHANGE,
            "events_cache": self._cache_info(cfg, "ticker_events"),
            "event": {"date": event.get("date"), "new_ticker": new_ticker, "type": event.get("type")},
            "prior_ticker": old_ticker,
            "event_index": index,
            "events_total": len(evs),
            "composite_figi": candidate.get("_figi"),
        }
        fact_id = compute_fact_id(self.source, entity["name"], TICKER_CHANGE, change_date.value)
        record_id = compute_record_id(
            fact_id, f"{TICKER_CHANGE}|{ticker}|{event.get('date')}|{new_ticker}")
        return FactChangeRecord(
            fact_id=fact_id, record_id=record_id, source=self.source, entity=entity,
            property=TICKER_CHANGE, value_type=TEXT_SPAN,
            before=ValueState(raw=before_raw, canonical=None, evidence=before_ev),
            after=ValueState(raw=after_raw, canonical=None, evidence=after_ev),
            change_date=change_date, provenance=provenance,
        )

    def _build_ipo(self, candidate: dict, cfg: dict) -> FactChangeRecord:
        row = candidate["_ipo"]
        row = row if isinstance(row, dict) else {}
        ticker = row.get("ticker")
        ticker = ticker if isinstance(ticker, str) and ticker else ""
        issuer = row.get("issuer_name")
        name = issuer if isinstance(issuer, str) and issuer else (ticker or "(unknown issuer)")
        ids = {}
        if ticker:
            ids["ticker"] = ticker
        isin = row.get("isin")
        if isinstance(isin, str) and isin:
            ids["isin"] = isin
        entity = {"name": name, "ids": ids}

        listing_date = _iso_or_none(row.get("listing_date"))
        change_value = listing_date or self._asof_day(cfg)
        change_basis = "ipo_listing_date" if listing_date else "listing_date_unparseable"

        before_ev = Evidence(
            kind="pre_ipo_absence", url=None, as_of=None,
            ref={"issuer_name": name, "ticker": ticker,
                 "note": "synthetic before-state: no prior public listing (negative evidence)"},
        )
        after_ev = Evidence(
            kind="ipo_record", url=None, as_of=listing_date,
            ref={f: row.get(f) for f in (
                "ticker", "issuer_name", "listing_date", "announced_date", "primary_exchange",
                "security_type", "ipo_status", "isin", "us_code", "final_issue_price",
                "total_offer_size", "shares_outstanding", "currency_code",
                "security_description", "last_updated")},
        )
        after_raw = f"listed:{ticker}" if ticker else "listed"
        change_date = ChangeDate(
            value=change_value, precision="day",
            evidence=Evidence(kind="ipo_record", url=None, as_of=listing_date,
                              ref={"basis": change_basis, "listing_date_raw": row.get("listing_date"),
                                   "ticker": ticker}),
        )
        provenance = {
            "family": IPO_LISTING,
            "ipo_cache": self._cache_info(cfg, "ipo"),
            "ipo": {f: row.get(f) for f in (
                "ticker", "issuer_name", "listing_date", "primary_exchange", "security_type",
                "ipo_status", "isin", "us_code", "final_issue_price", "total_offer_size",
                "shares_outstanding")},
        }
        fact_id = compute_fact_id(self.source, entity["name"], IPO_LISTING, change_date.value)
        record_id = compute_record_id(
            fact_id, f"{IPO_LISTING}|{ticker}|{row.get('listing_date')}")
        return FactChangeRecord(
            fact_id=fact_id, record_id=record_id, source=self.source, entity=entity,
            property=IPO_LISTING, value_type=TEXT_SPAN,
            before=ValueState(raw="unlisted", canonical=None, evidence=before_ev),
            after=ValueState(raw=after_raw, canonical=None, evidence=after_ev),
            change_date=change_date, provenance=provenance,
        )

    # -- gates --------------------------------------------------------------
    def gate_list(self, cfg: dict):
        """Ordered, family-aware gates (rationale in the module docstring).
        Loads the universe and caches into cfg and records the active owner
        policy for the manifest."""
        self._require_offline(cfg)
        self._ensure_loaded(cfg)
        cfg[GARBAGE_RULES_CTX_KEY] = {}  # no rules for quantity/text_span (authoritative feed)
        cfg[POLICY_ACTIVE_CTX_KEY] = resolve_policy(cfg)
        return [
            FinanceUniverseGate(),
            IpoScopeGate(),
            FinanceWindowBindingGate(),
            TemporalWindowGate(),
            ValueParsedGate(),
            GarbageValueGate(rules_ctx_key=GARBAGE_RULES_CTX_KEY),
            ValueActuallyChangedGate(),
            FinanceTickerRenamePriceGate(),
            FinanceIssuerContinuityGate(),
            FinanceShareStabilityGate(),
            FinancePriceVerifyGate(),
            FinanceCrossSourceGate(),
            FinanceMaterialChangeGate(),
            EvidenceResolvableGate(),
            DedupGate(),
        ]

    def snapshot_inputs(self, cfg: dict):
        """The fixed data files a finance snapshot must pin (see the coverage
        check): the five per-family caches, the price-verification sidecar, and
        the universe csv. Consulted ONLY when deriving from a harvested snapshot
        (a dir carrying snapshot_manifest.json); a legacy --data-dir / the package
        cache has no manifest, so this never affects those derives. The caches'
        .meta.json sidecars are optional provenance (a missing sidecar degrades
        window_binding to a pass), so they are not required here — though when the
        finance harvester writes them they are sha1-pinned in the manifest and
        verified like any other listed file.

        price_verify.jsonl is REQUIRED (rule 5b): it is the frozen split adjustment
        for every authoritative in-window split, read by BOTH _build_share_price
        (which puts BKNG/CVNA/KLAC/DD on the split-adjusted basis) and
        FinancePriceVerifyGate. Were it omitted here, a snapshot whose files block
        dropped the sidecar would still pass coverage and derive at exit 0 — the
        build would fall back to the RAW grouped closes (re-manufacturing the split
        artifacts) and the gate's fail-closed suspect fallback would review the
        extreme movers. Listing it makes a snapshot missing the sidecar raise
        CoverageError instead, closing that silent-artifact path (in-band with
        'missing input -> review/refuse, never silent include'). Never changes
        derive output."""
        return [PRICE_FILENAME, FINANCIALS_FILENAME, EVENTS_FILENAME, IPO_FILENAME,
                CROSS_CHECK_FILENAME, PRICE_VERIFY_FILENAME, UNIVERSE_FILENAME]

    # -- loading ------------------------------------------------------------
    @staticmethod
    def _require_offline(cfg: dict) -> None:
        if not cfg.get("offline", True):
            raise NotImplementedError(
                "the finance adapter is offline-only: the Polygon caches are built "
                "once by stage1.tools.fetch_polygon (the API key never enters the pipeline)"
            )

    def _ensure_loaded(self, cfg: dict) -> None:
        """Load the universe and the five frozen caches into cfg exactly once
        per run. Each present cache is declared to the manifest's input
        fingerprint (extra_input_files + extra_input_meta). Malformed lines
        become input_load_errors, never silent skips."""
        if UNIVERSE_MAP_CTX_KEY not in cfg:
            self._load_universe(cfg)
        if CACHE_INFO_CTX_KEY not in cfg:
            cfg[CACHE_INFO_CTX_KEY] = {}
            self._load_price(cfg)
            self._load_by_ticker(cfg, FINANCIALS_CTX_KEY, FINANCIALS_FILENAME, "financials",
                                 key="ticker", multi=True)
            self._load_by_ticker(cfg, EVENTS_CTX_KEY, EVENTS_FILENAME, "ticker_events",
                                 key="ticker", multi=False)
            self._load_by_ticker(cfg, CROSS_CHECK_CTX_KEY, CROSS_CHECK_FILENAME, "cross_check",
                                 key="ticker", multi=False)
            self._load_price_verify(cfg)
            self._load_ipo(cfg)

    # ---- universe ---------------------------------------------------------
    @classmethod
    def _resolve_universe_path(cls, data_dir):
        if data_dir is not None:
            local = Path(data_dir) / UNIVERSE_FILENAME
            if local.is_file():
                return local, UNIVERSE_FILENAME
        if DEFAULT_UNIVERSE_PATH.is_file():
            return DEFAULT_UNIVERSE_PATH, PACKAGE_UNIVERSE_ID
        return None, None

    def _load_universe(self, cfg: dict) -> None:
        data_dir = cfg.get("data_dir")
        path, ident = self._resolve_universe_path(data_dir)
        universe: dict = {}
        if path is None:
            cfg[UNIVERSE_MAP_CTX_KEY] = universe
            cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).append(
                {"file": UNIVERSE_FILENAME, "line": 0,
                 "error": "universe file not found (no S&P family will find its universe)"})
            return
        errors = []
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for line_no, r in enumerate(reader, 2):
                ticker = (r.get("ticker") or "").strip()
                if not ticker:
                    errors.append({"file": ident, "line": line_no, "error": "missing ticker"})
                    continue
                if ticker in universe:
                    errors.append({"file": ident, "line": line_no,
                                   "error": f"duplicate ticker {ticker!r} (first kept)"})
                    continue
                universe[ticker] = {"name": (r.get("name") or "").strip(),
                                    "cik": (r.get("cik") or "").strip()}
        cfg[UNIVERSE_MAP_CTX_KEY] = universe
        if errors:
            cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(errors)
        # Fingerprint the universe file (lives outside data_dir when default).
        digest = _sha1_file(path)
        cfg.setdefault(EXTRA_INPUTS_CTX_KEY, {})[ident] = digest
        cfg.setdefault(EXTRA_INPUT_META_CTX_KEY, {})[ident] = {
            "sha1": digest, "role": "universe", "tickers": len(universe)}

    # ---- caches -----------------------------------------------------------
    @classmethod
    def _resolve_cache_path(cls, data_dir, filename: str):
        """(path, location-independent identifier) for a cache file: the
        vendored copy under data_dir when present, else the package default
        under stage1/cache/; (None, None) when neither exists."""
        if data_dir is not None:
            local = Path(data_dir) / filename
            if local.is_file():
                return local, filename
        default = PACKAGE_CACHE_DIR / filename
        if default.is_file():
            return default, f"stage1/cache/{filename}"
        return None, None

    def _declare_cache(self, cfg: dict, family: str, path, ident: str) -> None:
        """Record a present cache's (id, sha1) and sidecar retrieval metadata
        in the manifest channels, and remember it in CACHE_INFO for provenance."""
        info = {"file": None, "sha1": None, "present": False, "meta": None}
        if path is not None:
            digest = _sha1_file(path)
            meta = self._load_sidecar(path)
            info = {"file": ident, "sha1": digest, "present": True, "meta": meta}
            cfg.setdefault(EXTRA_INPUTS_CTX_KEY, {})[ident] = digest
            meta_entry = {"sha1": digest, "retrieval": meta}
            if isinstance(meta, dict) and "cache_sha1" in meta:
                meta_entry["sidecar_matches_cache"] = meta.get("cache_sha1") == digest
            cfg.setdefault(EXTRA_INPUT_META_CTX_KEY, {})[ident] = meta_entry
        cfg[CACHE_INFO_CTX_KEY][family] = info

    @staticmethod
    def _load_sidecar(path: Path):
        sidecar = path.with_name(path.name[: -len(".jsonl")] + ".meta.json")
        if not sidecar.is_file():
            return None
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"sidecar": sidecar.name, "error": f"unreadable sidecar: {exc}"}
        if not isinstance(meta, dict):
            return {"sidecar": sidecar.name, "error": "sidecar is not a JSON object"}
        entry = {"sidecar": sidecar.name}
        for key in _SIDECAR_META_KEYS:
            if key in meta:
                entry[key] = meta[key]
        return entry

    def _load_price(self, cfg: dict) -> None:
        data_dir = cfg.get("data_dir")
        path, ident = self._resolve_cache_path(data_dir, PRICE_FILENAME)
        by_ticker: dict = {}
        resolved: dict = {}
        errors = []
        if path is not None:
            for line_no, row in _iter_jsonl(path, PRICE_FILENAME, errors):
                ticker, role = row.get("ticker"), row.get("role")
                if not isinstance(ticker, str) or role not in ("anchor", "asof"):
                    errors.append({"file": ident, "line": line_no,
                                   "error": "price row missing a string ticker or valid role"})
                    continue
                by_ticker.setdefault(ticker, {})[role] = row
                rtd = row.get("resolved_trading_day")
                if isinstance(rtd, str) and role not in resolved:
                    resolved[role] = rtd
        cfg[PRICE_CTX_KEY] = by_ticker
        cfg[PRICE_RESOLVED_CTX_KEY] = resolved
        if errors:
            cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(errors)
        self._declare_cache(cfg, "price_grouped", path, ident)

    def _load_by_ticker(self, cfg: dict, ctx_key: str, filename: str, family: str,
                        key: str, multi: bool) -> None:
        data_dir = cfg.get("data_dir")
        path, ident = self._resolve_cache_path(data_dir, filename)
        store: dict = {}
        errors = []
        if path is not None:
            for line_no, row in _iter_jsonl(path, filename, errors):
                k = row.get(key)
                if not isinstance(k, str) or not k:
                    errors.append({"file": ident, "line": line_no,
                                   "error": f"row missing a string {key!r}"})
                    continue
                if multi:
                    store.setdefault(k, []).append(row)
                elif k in store:
                    errors.append({"file": ident, "line": line_no,
                                   "error": f"duplicate {key} {k!r} (first kept)"})
                else:
                    store[k] = row
        cfg[ctx_key] = store
        if errors:
            cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(errors)
        self._declare_cache(cfg, family, path, ident)

    def _load_ipo(self, cfg: dict) -> None:
        data_dir = cfg.get("data_dir")
        path, ident = self._resolve_cache_path(data_dir, IPO_FILENAME)
        rows = []
        errors = []
        if path is not None:
            for _line_no, row in _iter_jsonl(path, IPO_FILENAME, errors):
                rows.append(row)
        cfg[IPO_CTX_KEY] = rows
        if errors:
            cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(errors)
        self._declare_cache(cfg, "ipo", path, ident)

    def _load_price_verify(self, cfg: dict) -> None:
        """Load the committed price-verification sidecar into
        ``ticker -> {verdict, sides, by_role, split, source}`` for
        FinancePriceVerifyGate and the split adjustment. Each row is one
        (ticker, side): ``grouped_close`` is the release grouped (unadjusted)
        close and ``refetched_close`` the SPLIT-ADJUSTED close on the current
        basis. ``by_role`` (role -> the side dict) lets ``_build_share_price``
        read the adjusted close for a ``split_adjusted`` ticker; ``split`` carries
        the authoritative /v3/reference/splits ref for provenance. The per-ticker
        verdict is the strongest side verdict (precedence split_adjusted >
        bad_tick_review > real_kept). A missing sidecar leaves an empty store
        (no ticker adjusted, guard silent). Declared to the manifest fingerprint
        like the other caches."""
        data_dir = cfg.get("data_dir")
        path, ident = self._resolve_cache_path(data_dir, PRICE_VERIFY_FILENAME)
        store: dict = {}
        errors = []
        rank = {PRICE_VERIFY_SPLIT: 2, PRICE_VERIFY_BAD: 1, PRICE_VERIFY_REAL: 0}
        if path is not None:
            for line_no, row in _iter_jsonl(path, PRICE_VERIFY_FILENAME, errors):
                ticker = row.get("ticker")
                if not isinstance(ticker, str) or not ticker:
                    errors.append({"file": ident, "line": line_no,
                                   "error": "price_verify row missing a string ticker"})
                    continue
                entry = store.setdefault(
                    ticker, {"verdict": None, "sides": [], "by_role": {},
                             "split": None, "source": None})
                # NOTE: the serialized side dict is byte-stable (FinancePriceVerifyGate
                # writes it into evidence) — do not add keys here.
                side = {
                    "role": row.get("role"), "date": row.get("date"),
                    "grouped_close": row.get("grouped_close"),
                    "refetched_close": row.get("refetched_close"),
                    "divergence": row.get("divergence"),
                    "reliable": row.get("reliable"),
                }
                entry["sides"].append(side)
                role = row.get("role")
                if isinstance(role, str) and role in ("anchor", "asof"):
                    entry["by_role"][role] = side
                if entry["source"] is None:
                    entry["source"] = row.get("source")
                if entry["split"] is None and row.get("split_execution_date"):
                    entry["split"] = {"split_from": row.get("split_from"),
                                      "split_to": row.get("split_to"),
                                      "execution_date": row.get("split_execution_date")}
                # per-ticker verdict: the strongest side verdict wins (a split
                # side makes the whole record split_adjusted; a bad-tick side
                # makes it review; else real_kept).
                v = row.get("verdict")
                if v in rank:
                    if entry["verdict"] is None or rank[v] > rank.get(entry["verdict"], -1):
                        entry["verdict"] = v
                elif entry["verdict"] is None:
                    entry["verdict"] = v
        # sort each ticker's sides for deterministic evidence ordering.
        for entry in store.values():
            entry["sides"].sort(key=lambda s: (str(s.get("date") or ""), str(s.get("role") or "")))
        cfg[PRICE_VERIFY_CTX_KEY] = store
        if errors:
            cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(errors)
        self._declare_cache(cfg, "price_verify", path, ident)


def _sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_jsonl(path: Path, filename: str, errors: list):
    """Yield (line_no, row_dict) from a jsonl file; malformed / non-object
    lines append an error entry (never a silent skip) and are omitted."""
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                errors.append({"file": filename, "line": line_no, "error": str(exc)})
                continue
            if not isinstance(row, dict):
                errors.append({"file": filename, "line": line_no,
                               "error": f"row is {type(row).__name__}, expected object"})
                continue
            yield line_no, row


ADAPTER = FinanceAdapter()
