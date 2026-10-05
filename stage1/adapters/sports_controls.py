"""Sports CONTROL adapter, V2 DISCOVERY design (source 'sports_controls').

PRINCIPAL SPEC (2026-08-05): "Sport. Player who has been at the same club
continuously across the cutoff and still is. Question unchanged. Draw from the
same league tiers and prominence deciles as the transfer set — don't let
controls skew to long-tenured stars."

V1 (ruling A7: the treatment release's excluded:value_changed rows) is retired
for sports controls: under the 2024-03-31 KNOWABILITY ANCHOR it yielded 2/165
survivors, because that pool descends from a discovery that selects in-window
P54 starts — recent joiners by construction. Its release
(stage1/releases/dev-sports-controls) stays as the audit trail; the two v1
anchored survivors are carried into the v2 pool (deduped by QID).

V2 DISCOVERY (stage1/harvest/sports_controls.py): the universe is the
TREATMENT transfer set's own clubs — per club, a bounded Wikidata query finds
humans with an OPEN P54 membership of that club whose start (P580) is provably
at/before the anchor. Candidates are stratified by THREE margins (league tier
x tenure band x sitelink-prominence decile — the tenure margin per the
2026-08-05 addendum: tenure is a training-data confound), with decile
boundaries, tenure bands, and target quotas computed from the TREATMENT
release's included players (tenure-before-transfer from the treatment's own
archived P54 cache), and a seeded deterministic verify-selection per stratum
(recorded in sports_controls_matching.json).

Inputs (all read-only, under --data-dir — a v2 harvest snapshot):

* sports_controls_candidates.jsonl — every discovered candidate (plus the v1
  survivors, origin 'v1_pool'), with club/league/tier/sitelinks/decile/P580,
  seeded_rank, and verify_selected. Only verify-selected rows carry pinned
  evidence and are enumerated; the full frame stays frozen for audit.
* sports_controls_revisions.jsonl  — per verify-selected title: the pinned
  CUTOFF-era revision (newest <= cutoff) AND the pinned CURRENT revision
  (newest <= pull asof), full wikitext.
* sports_controls_wd_p54.jsonl    — pull-date full P54 statements for the
  verify-selected players (treatment cache row shape).
* sports_controls_matching.json   — seed, decile boundaries, treatment margins
  (tier / decile / joint), per-cell quotas, treatment-release binding sha1s.

Record shape: source 'sports_controls', property 'current_club', value_type
'org'. ``before`` = the cutoff-era revision's infobox club; ``after`` = the
pull-date revision's infobox club (the SAME authority as treatment — the
Wikipedia infobox, read by the treatment extractor). ``change_date`` = the P54
membership start (basis 'p54_membership_start'): when the control fact became
true. ``provenance``: population='control', anchor fields, league/tier,
sitelinks, decile, tenure_start, cell, seeded_rank, origin.

Gate order (first fail names the disposition; every gate always runs):

1. ``sports_loan``       (sports_loan:controls_v2, policy loans='exclude') —
   a loan flag on the cutoff-era OR pull-date extraction fails the record.
2. ``control_unchanged`` (control_unchanged:v2) — CONTINUITY: the infobox
   club at the cutoff revision AND at the pull-date revision must both
   org-equal the discovered P54 club. Provably at a different club at either
   endpoint (or explicitly unattached) -> FAIL with the endpoint named;
   unreadable/blank/missing -> review, never included, never counted changed.
3. ``corroboration``     (review-only, unchanged from v1) — current P54 must
   carry an OPEN membership of the club with no later open membership
   elsewhere.
4. ``control_anchor``    (control_anchor:v1, unchanged) — the membership's
   P580 must prove the fact held by 2024-03-31; precision-aware.
5. ``evidence_resolvable`` (shared).
6. ``control_quota``     (control_quota:v1) — the principal's three-margin
   matching via the addendum's priority ladder (preserve tier first, then
   tenure band, then decile): a fully-passing record claims, in seeded
   enumeration order, its exact (tier x band x decile) cell, else same-
   (tier, band) room with decile relaxed, else same-tier room with band
   relaxed; otherwise excluded:control_quota (auditable surplus). The v1
   survivors are quota-exempt (coordinator: keep them in the pool).
   Target-vs-achieved per cell/tier-band/tier, claim levels, AND the
   per-tenure-band floor-geometry table (treatment count vs achieved,
   structurally empty low bands included) land in the manifest policy block.
7. ``dedup`` (shared, last).

Offline-only (--online raises). Deterministic: same snapshot, byte-identical
facts.jsonl. NO LLM anywhere.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from datetime import date as _date
from pathlib import Path

from stage1.adapters import Adapter
from stage1.adapters.sports import (
    POLICY_ACTIVE_CTX_KEY,
    POLICY_MODES,
    _policy_mode,
    _statement_match,
    change_date_interval,
    wd_time_to_change_date,
)
from stage1.adapters.sports_infobox import EXTRACTOR_VERSION, extract_current_club
from stage1.gates import Gate
from stage1.gates.standard import DedupGate, EvidenceResolvableGate
from stage1.normalize import Comparison, get_comparator
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

SOURCE = "sports_controls"
PROPERTY = "current_club"
VALUE_TYPE = "org"
ENTITY_ID_KEY = "wikidata_qid"
POPULATION = "control"

# KNOWABILITY ANCHOR (principal ruling, 2026-08-05): a control must already
# have HELD by this date — the end of Q1 2024, before the earliest roster model
# cutoff (~June 2024). V2 discovery queries filter on it; the control_anchor
# gate re-verifies it precision-aware from the frozen P54 cache.
ANCHOR_DATE = _date(2024, 3, 31)
ANCHOR_BASIS = "p54_start_time"

TREATMENT_SOURCE = "sports"

# The fixed snapshot filenames this adapter reads by name (written by
# stage1/harvest/sports_controls.py).
CANDIDATES_FILENAME = "sports_controls_candidates.jsonl"
REVISIONS_FILENAME = "sports_controls_revisions.jsonl"
WD_CACHE_FILENAME = "sports_controls_wd_p54.jsonl"
MATCHING_FILENAME = "sports_controls_matching.json"
REVISIONS_SIDECAR_FILENAME = "sports_controls_revisions.meta.json"
WD_SIDECAR_FILENAME = "sports_controls_wd_p54.meta.json"

MATCHING_CTX_KEY = "sports_controls_matching"
REVISIONS_CTX_KEY = "sports_controls_revisions_by_title"
REVISIONS_INFO_CTX_KEY = "sports_controls_revisions_info"
WD_CACHE_CTX_KEY = "sports_controls_wd_cache"
WD_CACHE_INFO_CTX_KEY = "sports_controls_wd_cache_info"
LOAD_ERRORS_CTX_KEY = "input_load_errors"
EXTRA_INPUT_META_CTX_KEY = "extra_input_meta"

DEFAULT_POLICY = {"loans": "exclude"}
POLICY_OVERRIDE_CTX_KEY = "sports_controls_policy"

UNKNOWN_TIER = "unknown"


def resolve_policy(cfg) -> dict:
    """The active policy: DEFAULT_POLICY overlaid by cfg['sports_controls_policy'].
    Only recognized flags with a valid mode override; pure and total."""
    active = dict(DEFAULT_POLICY)
    override = cfg.get(POLICY_OVERRIDE_CTX_KEY) if isinstance(cfg, dict) else None
    if isinstance(override, dict):
        for flag in DEFAULT_POLICY:
            mode = override.get(flag)
            if mode in POLICY_MODES:
                active[flag] = mode
    return active


def infobox_club(result) -> str:
    """The DISPLAYED infobox club string of one extract_current_club result,
    or '' when the side displays no value (same policy as the treatment
    harvest's club reading; an explicit free-agency marker IS a displayed
    value and is preserved)."""
    if not isinstance(result, dict):
        return ""
    if result.get("status") == "club" and isinstance(result.get("club"), str):
        return result["club"]
    if result.get("status") == "unattached" and result.get("reason") == "explicit_marker":
        marker = (result.get("detail") or {}).get("marker")
        if isinstance(marker, str) and marker:
            return marker
    return ""


# ---------------------------------------------------------------------------
# Prominence deciles + strata + quotas (pure; shared with the harvester)
# ---------------------------------------------------------------------------

def decile_boundaries(counts) -> list:
    """The 9 internal decile boundaries of a sitelink-count sample (the
    TREATMENT included players), index-based on the sorted sample.
    Deterministic; a degenerate sample yields repeated boundaries."""
    values = sorted(int(c) for c in counts if isinstance(c, (int, float))
                    and not isinstance(c, bool))
    if not values:
        return []
    n = len(values)
    return [values[min(n - 1, (k * n) // 10)] for k in range(1, 10)]


def decile_of(count, boundaries) -> int:
    """0-based decile of one sitelink count under the treatment boundaries:
    the number of boundaries strictly below it, capped at 9. Unknown counts
    land in decile 0 (least prominent — the conservative bin)."""
    if not isinstance(count, (int, float)) or isinstance(count, bool):
        return 0
    d = 0
    for b in boundaries or []:
        if count > b:
            d += 1
    return min(d, 9)


# TENURE BANDS (coordinator addendum, 2026-08-05): tenure is a CONFOUND —
# long-tenured players are better represented in training data, so their club
# is easier to recall, deflating the floor. Controls are quota-matched to the
# treatment's tenure-before-transfer distribution in these bands (boundaries
# in years; chosen to bracket the addendum's 2-3/3-5/5-10/10+ suggestion and
# additionally resolve the short-tenure mass the anchor makes UNREACHABLE for
# controls: the 2024-03-31 anchor floor means no control can have tenure under
# ~2.4y at pull, so the '<1' and '1-2' bands are structurally empty on the
# control side and the manifest reports them anyway — honest floor geometry).
TENURE_BAND_BOUNDARIES = (1.0, 2.0, 3.0, 5.0, 10.0)
TENURE_BANDS = ("<1", "1-2", "2-3", "3-5", "5-10", "10+")
TENURE_BAND_UNKNOWN = "unknown"


def tenure_years(start_iso, end_date) -> float | None:
    """Tenure in years between a pinned ISO start date and an end date
    (change date for treatment, pull asof for controls), days/365.25 rounded
    to 2 decimals. None when either side is unusable. Pure and total."""
    if not isinstance(start_iso, str):
        return None
    try:
        start = _date.fromisoformat(start_iso)
    except ValueError:
        return None
    end = end_date
    if isinstance(end, str):
        try:
            end = _date.fromisoformat(end)
        except ValueError:
            return None
    if not isinstance(end, _date):
        return None
    days = (end - start).days
    if days < 0:
        return None
    return round(days / 365.25, 2)


def band_of(years) -> str:
    """The tenure band of a tenure-in-years value; 'unknown' for None."""
    if not isinstance(years, (int, float)) or isinstance(years, bool):
        return TENURE_BAND_UNKNOWN
    for boundary, band in zip(TENURE_BAND_BOUNDARIES, TENURE_BANDS):
        if years < boundary:
            return band
    return TENURE_BANDS[-1]


def cell_key(tier, band, decile) -> str:
    """The JSON-safe 3-margin stratum key: 'tier=<t>|band=<b>|decile=<d>'."""
    t = tier if tier is not None else UNKNOWN_TIER
    b = band if isinstance(band, str) and band else TENURE_BAND_UNKNOWN
    return f"tier={t}|band={b}|decile={int(decile)}"


def tier_band_key(tier, band) -> str:
    t = tier if tier is not None else UNKNOWN_TIER
    b = band if isinstance(band, str) and band else TENURE_BAND_UNKNOWN
    return f"tier={t}|band={b}"


def tier_key(tier) -> str:
    t = tier if tier is not None else UNKNOWN_TIER
    return f"tier={t}"


def seeded_rank(seed: str, qid: str) -> str:
    """Deterministic per-candidate priority: sha1('<seed>|<qid>') hex; lower
    sorts first (the draw-fairness frozen-prefix style — the seed is fixed in
    the matching file and the manifest)."""
    return hashlib.sha1(f"{seed}|{qid}".encode("utf-8")).hexdigest()


def allocate_quotas(joint_counts: dict, target: int) -> dict:
    """Per-cell quotas matching the treatment JOINT (tier x decile)
    distribution — which matches BOTH margins by construction — via the
    largest-remainder method (deterministic ties by cell key).
    joint_counts: {cell_key: treatment count}. Returns {cell_key: quota}
    summing to ``target`` (all-zero when the joint is empty)."""
    total = sum(v for v in joint_counts.values()
                if isinstance(v, int) and not isinstance(v, bool) and v > 0)
    if total <= 0 or target <= 0:
        return {k: 0 for k in joint_counts}
    exact = {k: (target * v) / total for k, v in joint_counts.items()
             if isinstance(v, int) and v > 0}
    quotas = {k: int(x) for k, x in exact.items()}
    remainder = target - sum(quotas.values())
    order = sorted(exact, key=lambda k: (-(exact[k] - int(exact[k])), k))
    for k in order[:remainder]:
        quotas[k] += 1
    for k in joint_counts:
        quotas.setdefault(k, 0)
    return quotas


# ---------------------------------------------------------------------------
# Wikidata P54 re-verification + anchor (unchanged from v1)
# ---------------------------------------------------------------------------

def _statement_brief(statement: dict) -> dict:
    return {
        "team_qid": statement.get("team_qid"),
        "team_enwiki_title": statement.get("team_enwiki_title"),
        "team_label_en": statement.get("team_label_en"),
        "p580": statement.get("p580"),
        "p582": statement.get("p582"),
        "rank": statement.get("rank"),
    }


def _p580_time(statement: dict) -> str:
    p580 = statement.get("p580")
    time = p580.get("time") if isinstance(p580, dict) else None
    return time if isinstance(time, str) else ""


def reverify_membership(attested_raw, statements, comparator) -> dict:
    """Does the CURRENT P54 state support the attested club as the player's
    team? Squad-aware/sitelink-primary via the treatment's _statement_match.
    Pure; deterministic; JSON-safe. Outcomes: 'membership_open' (the
    corroborating state), 'newer_open_membership_elsewhere',
    'membership_ended', 'attested_club_not_matched', 'no_statements',
    'attested_value_unparseable'."""
    result = {
        "statements_total": len(statements),
        "matched": [],
        "demoted": [],
        "open_match": None,
        "later_open_other": [],
        "outcome": None,
    }
    parsed = comparator.parse(attested_raw) if isinstance(attested_raw, str) else None
    if parsed is None or not parsed.ok:
        result["outcome"] = "attested_value_unparseable"
        return result
    if not statements:
        result["outcome"] = "no_statements"
        return result

    matched = []
    unmatched_open = []
    for statement in statements:
        if not isinstance(statement, dict):
            continue
        outcome, name, detail = _statement_match(
            parsed.canonical, attested_raw, statement, comparator
        )
        brief = _statement_brief(statement)
        brief["outcome"] = outcome
        if outcome == "equal":
            brief["matched_name"] = name
            brief["matched_via"] = detail.get("matched_via")
            matched.append((statement, brief))
            result["matched"].append(brief)
        elif outcome == "demoted":
            brief["demote_reason"] = detail.get("demote_reason")
            result["demoted"].append(brief)
            if not statement.get("p582"):
                unmatched_open.append((statement, brief))
        else:
            if not statement.get("p582"):
                unmatched_open.append((statement, brief))

    if not matched:
        result["outcome"] = "attested_club_not_matched"
        return result

    open_matches = [(s, b) for s, b in matched if not s.get("p582")]
    if not open_matches:
        result["outcome"] = "membership_ended"
        return result
    # The newest open membership of the attested club (P580 time strings are
    # the cache's own sort key — lexicographic IS chronological here).
    open_matches.sort(key=lambda m: _p580_time(m[0]))
    newest = open_matches[-1]
    result["open_match"] = newest[1]
    newest_time = _p580_time(newest[0])
    later_other = [
        b for s, b in unmatched_open
        if s.get("snaktype", "value") == "value" and _p580_time(s) > newest_time
    ]
    if later_other:
        result["later_open_other"] = later_other
        result["outcome"] = "newer_open_membership_elsewhere"
        return result
    result["outcome"] = "membership_open"
    return result


def anchor_from_reverify(reverify) -> dict:
    """The KNOWABILITY ANCHOR from the pull-date P54 re-verification: the
    P580 start of the attested club's open membership (the very statement
    corroboration certifies). Precision-aware interval rule against
    ANCHOR_DATE. Statuses: 'anchored' | 'too_recent' | 'straddles' |
    'start_missing' | 'no_open_membership' | 'no_reverify'."""
    empty = {"basis": None, "date": None, "precision": None,
             "wd_precision": None, "status": None}
    if not isinstance(reverify, dict):
        return {**empty, "status": "no_reverify"}
    open_match = reverify.get("open_match")
    if not isinstance(open_match, dict):
        return {**empty, "status": "no_open_membership"}
    cd = wd_time_to_change_date(open_match.get("p580"))
    if cd is None:
        return {**empty, "status": "start_missing"}
    value, precision, wd_precision = cd
    interval = change_date_interval(value, precision)
    if interval is None:
        return {**empty, "status": "start_missing"}
    lo, hi = interval
    if hi <= ANCHOR_DATE:
        status = "anchored"
    elif lo > ANCHOR_DATE:
        status = "too_recent"
    else:
        status = "straddles"
    return {"basis": ANCHOR_BASIS, "date": value, "precision": precision,
            "wd_precision": wd_precision, "status": status}


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def _side_extraction_ref(record, side: str):
    state = getattr(record, side, None)
    evidence = getattr(state, "evidence", None)
    ref = getattr(evidence, "ref", None)
    if isinstance(ref, dict):
        ext = ref.get("extraction")
        return ext if isinstance(ext, dict) else None
    return None


class SportsControlsLoanGate(Gate):
    """Loan screen (standing policy loans='exclude', 2026-07-19): a loan flag
    on the CUTOFF-era or PULL-DATE extraction fails the record — a
    loan-annotated value is not a clean senior-club control. Runs first so a
    loan display is never misread as (dis)continuity. A side without
    extraction metadata passes here with a note — its absence is
    control_unchanged's review, not a loan verdict."""

    name = "sports_loan"
    version = "sports_loan:controls_v2"

    def evaluate(self, record, ctx: dict) -> GateResult:
        mode = _policy_mode(ctx, "loans")
        evidence: dict = {
            "extractor": EXTRACTOR_VERSION,
            "policy": "loans",
            "policy_setting": mode,
        }
        flagged = []
        for side, label in (("before", "cutoff"), ("after", "current")):
            ext = _side_extraction_ref(record, side)
            if not isinstance(ext, dict):
                evidence[label] = {"checked": False, "basis": "no_extraction_metadata"}
                continue
            loan = bool(ext.get("loan"))
            evidence[label] = {"checked": True, "loan": loan}
            if loan:
                detail = ext.get("detail") if isinstance(ext.get("detail"), dict) else {}
                evidence[label]["club"] = ext.get("club")
                evidence[label]["loan_marker"] = detail.get("loan_marker")
                flagged.append(label)
        if not flagged:
            return GateResult(name=self.name, version=self.version,
                              verdict="pass", evidence=evidence)
        evidence["flagged_sides"] = flagged
        evidence["reason"] = "loan_annotation"
        base = (
            "loan_annotation: the pinned infobox annotates the "
            + " and ".join(flagged)
            + " value as a loan destination; a loan spell is not provably the "
            "senior-club fact a control must attest"
        )
        if mode == "exclude":
            evidence["problem"] = (
                base + " — owner policy loans='exclude' (2026-07-19) drops it "
                "from the control set"
            )
            return GateResult(name=self.name, version=self.version,
                              verdict="fail", evidence=evidence)
        evidence["problem"] = base + " (policy loans='review': held for a human)"
        return GateResult(name=self.name, version=self.version,
                          verdict="review", evidence=evidence)


class ControlUnchangedGate(Gate):
    """CONTINUITY re-verification (principal spec: "at the same club
    continuously across the cutoff and still is"): the infobox club at the
    pinned CUTOFF-era revision AND at the pinned PULL-DATE revision must both
    org-equal the discovered P54 club (the record's target_club) — the
    endpoints in Wikipedia's own voice; the open P54 statement is the
    in-between continuity evidence (corroboration + anchor gates).

    * both endpoints equal -> pass ('continuous');
    * cutoff endpoint provably a different club or explicitly unattached ->
      FAIL 'not_at_club_at_cutoff';
    * current endpoint provably different -> FAIL 'changed'; explicitly
      unattached now -> FAIL 'changed_to_unattached';
    * anything unverifiable (missing revision, unreadable value, blank
      field, comparator undecided) -> review with the reason — never
      included, never counted changed. FAIL evidence outranks a review on
      the other side (positive discontinuity evidence decides).
    """

    name = "control_unchanged"
    version = "control_unchanged:v2"

    def evaluate(self, record, ctx: dict) -> GateResult:
        after_ref = record.after.evidence.ref if isinstance(record.after.evidence.ref, dict) else {}
        target = after_ref.get("target_club") if isinstance(after_ref.get("target_club"), dict) else {}
        target_name = target.get("enwiki_title") or target.get("label")
        evidence: dict = {
            "extractor": EXTRACTOR_VERSION,
            "target_club": target_name,
        }

        def verdict(v: str, reason: str, problem=None) -> GateResult:
            evidence["reason"] = reason
            if problem:
                evidence["problem"] = problem
            return GateResult(name=self.name, version=self.version,
                              verdict=v, evidence=evidence)

        if not isinstance(target_name, str) or not target_name:
            return verdict("review", "target_club_missing",
                           "the candidate carries no club title to verify against")
        try:
            comparator = get_comparator(record.value_type)
        except LookupError:
            return verdict("review", "no_comparator",
                           f"no comparator registered for {record.value_type!r}")
        evidence["comparator_version"] = comparator.VERSION
        parsed_target = comparator.parse(target_name)
        if not parsed_target.ok:
            return verdict("review", "target_club_unparseable",
                           f"the club title {target_name!r} has no org canonical")

        outcomes = {}
        for side, label, fail_reason in (
            ("before", "cutoff", "not_at_club_at_cutoff"),
            ("after", "current", "changed"),
        ):
            state = getattr(record, side)
            ext = _side_extraction_ref(record, side)
            side_ev: dict = {"value": state.raw}
            evidence[label] = side_ev
            if not isinstance(ext, dict):
                outcomes[label] = ("review", f"{label}_revision_unavailable")
                continue
            status = ext.get("status")
            side_ev["status"] = status
            if status == "unattached":
                if ext.get("reason") == "explicit_marker":
                    outcomes[label] = (
                        "fail",
                        "changed_to_unattached" if label == "current"
                        else "not_at_club_at_cutoff",
                    )
                else:
                    outcomes[label] = ("review", f"{label}_club_not_displayed")
                continue
            if status != "club":
                outcomes[label] = ("review", str(ext.get("reason") or "no_value"))
                continue
            canonical = state.canonical
            if canonical is None:
                parsed = comparator.parse(state.raw) if isinstance(state.raw, str) else None
                canonical = parsed.canonical if parsed is not None and parsed.ok else None
            if canonical is None:
                outcomes[label] = ("review", f"{label}_value_unparseable")
                continue
            comparison = comparator.compare(canonical, parsed_target.canonical)
            if not isinstance(comparison, Comparison):
                outcomes[label] = ("review", "comparison_undecided")
                continue
            side_ev["comparison"] = {"verdict": comparison.verdict,
                                     "reason": comparison.reason}
            if comparison.verdict == "equal":
                outcomes[label] = ("pass", "equal")
            elif comparison.verdict == "different":
                outcomes[label] = ("fail", fail_reason)
            else:
                outcomes[label] = ("review", "comparison_undecided")

        for label in ("cutoff", "current"):
            v, reason = outcomes[label]
            if v == "fail":
                return verdict(
                    "fail", reason,
                    f"the {label} revision's infobox does not place the player at "
                    f"{target_name!r} (reads {evidence[label].get('value')!r}): not "
                    "continuously at the club — a changed/at-odds fact, not a control",
                )
        for label in ("cutoff", "current"):
            v, reason = outcomes[label]
            if v == "review":
                return verdict(
                    "review", reason,
                    f"the {label} side cannot be verified ({reason}): continuity is "
                    "unprovable, and unverifiable is never included",
                )
        evidence["reason"] = "continuous"
        return GateResult(name=self.name, version=self.version,
                          verdict="pass", evidence=evidence)


class ControlAnchorGate(Gate):
    """THE KNOWABILITY ANCHOR (principal ruling, 2026-08-05) — unchanged from
    v1: the attested club's open P54 membership must have started provably
    at/before ANCHOR_DATE. Precision-aware; a provably later start FAILS
    ('anchor_too_recent'); straddling/missing/unverifiable starts are review
    — unverifiable is not knowable."""

    name = "control_anchor"
    version = "control_anchor:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        after_ref = record.after.evidence.ref if isinstance(record.after.evidence.ref, dict) else {}
        anchor = anchor_from_reverify(after_ref.get("wikidata_p54_reverify"))
        evidence: dict = {
            "anchor_date_max": ANCHOR_DATE.isoformat(),
            "basis": anchor.get("basis"),
            "start_date": anchor.get("date"),
            "start_precision": anchor.get("precision"),
            "start_wd_precision": anchor.get("wd_precision"),
            "extraction_status": anchor.get("status"),
        }
        status = anchor.get("status")
        if status == "anchored":
            evidence["reason"] = "anchored"
            return GateResult(name=self.name, version=self.version,
                              verdict="pass", evidence=evidence)
        if status == "too_recent":
            evidence["reason"] = "anchor_too_recent"
            evidence["problem"] = (
                f"the attested membership's P54 start {anchor.get('date')} "
                f"(precision {anchor.get('precision')}) is provably AFTER the "
                f"knowability anchor {ANCHOR_DATE.isoformat()}: an early-cutoff "
                "model cannot know this fact, so it fails as a control floor"
            )
            return GateResult(name=self.name, version=self.version,
                              verdict="fail", evidence=evidence)
        if status == "straddles":
            evidence["reason"] = "anchor_straddles_boundary"
            evidence["problem"] = (
                f"the P54 start {anchor.get('date')} has precision "
                f"{anchor.get('precision')} and its interval straddles the anchor "
                f"{ANCHOR_DATE.isoformat()}: neither 'held by the anchor' nor "
                "'too recent' is provable — a human must date it"
            )
        elif status == "start_missing":
            evidence["reason"] = "anchor_start_missing"
            evidence["problem"] = (
                "the attested club's open P54 membership carries no usable "
                "start-time qualifier: knowability by the anchor cannot be "
                "verified, and unverifiable is not knowable"
            )
        else:
            evidence["reason"] = "anchor_unverifiable"
            evidence["problem"] = (
                "no open corroborated P54 membership of the attested club to "
                "anchor (the row is already held by corroboration review)"
            )
        return GateResult(name=self.name, version=self.version,
                          verdict="review", evidence=evidence)


class ControlsCorroborationGate(Gate):
    """Review-only pull-date Wikidata P54 corroboration (unchanged from v1):
    pass only on outcome 'membership_open'; every other outcome is a named
    review — never a fail (second-source disagreement is a human call)."""

    name = "corroboration"
    version = "corroboration:sports_controls_v1"

    SOURCE_LABEL = "wikidata_p54"

    def evaluate(self, record, ctx: dict) -> GateResult:
        evidence: dict = {"source_label": self.SOURCE_LABEL}

        def review(problem: str) -> GateResult:
            evidence["problem"] = problem
            return GateResult(name=self.name, version=self.version,
                              verdict="review", evidence=evidence)

        after_ref = record.after.evidence.ref if isinstance(record.after.evidence.ref, dict) else {}
        reverify = after_ref.get("wikidata_p54_reverify")
        if not isinstance(reverify, dict):
            entity_ids = record.entity.get("ids") if isinstance(record.entity, dict) else None
            qid = entity_ids.get(ENTITY_ID_KEY) if isinstance(entity_ids, dict) else None
            if not qid:
                return review("no_player_qid: the candidate carries no Wikidata QID, "
                              "so the P54 state cannot be re-checked")
            return review("player_not_in_cache: the pull-date P54 cache has no row "
                          f"for {qid}")
        evidence["reverify"] = reverify
        outcome = reverify.get("outcome")
        if outcome == "membership_open":
            return GateResult(name=self.name, version=self.version,
                              verdict="pass", evidence=evidence)
        return review(f"{outcome}: the pull-date Wikidata P54 state does not "
                      "cleanly corroborate the attested club (second-source "
                      "disagreement is a human call, never a fail)")


class ControlQuotaGate(Gate):
    """The principal's THREE-MARGIN MATCHING (tier x tenure band x prominence
    decile), applied deterministically at derive time in seeded enumeration
    order. Quotas are computed from the TREATMENT joint distribution at three
    nested levels (exact 3-margin cell; tier x band; tier), and a fully
    passing record claims through the addendum's PRIORITY LADDER — preserve
    tier first, then tenure band, then decile:

    * level 'exact'          — its exact 3-margin cell has quota AND its
      (tier, band) and tier envelopes are not exhausted;
    * level 'decile_relaxed' — the exact cell is exhausted/empty but the
      (tier, band) envelope has room: the decile margin is sacrificed first;
    * level 'band_relaxed'   — the (tier, band) envelope is exhausted but the
      tier envelope has room: the band margin is sacrificed next (this is how
      the anchor's structurally-empty low-tenure bands are visibly, not
      silently, backfilled by longer tenures);
    * otherwise FAIL 'quota_exhausted' (excluded:control_quota — a verified
      control over quota; auditable surplus).

    The claim rule mirrors dedup: only a record whose prior gate results ALL
    pass claims (a review/excluded row never starves a verified control). A
    v1 survivor (origin 'v1_pool') is quota-exempt (coordinator ruling). No
    computable cell / no quota table -> review, never a silent claim.

    Achieved counts (per 3-margin cell, per tier x band, per tier, AND per
    tenure band — the honest floor-geometry table, empty low bands included)
    are incremented IN PLACE in the shared policy quota table: the runner's
    ctx is a shallow copy of cfg, so these are the very dicts the manifest
    serializes after the loop — every release records target-vs-achieved and
    the claim levels (margin deviations) per stratum."""

    name = "control_quota"
    version = "control_quota:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        policy = ctx.get(POLICY_ACTIVE_CTX_KEY) if isinstance(ctx, dict) else None
        quota = policy.get("quota") if isinstance(policy, dict) else None
        evidence: dict = {}
        prov = record.provenance if isinstance(record.provenance, dict) else {}
        origin = prov.get("origin")
        evidence["origin"] = origin
        if origin == "v1_pool":
            evidence["note"] = "v1 anchored survivor: quota-exempt by coordinator ruling"
            return GateResult(name=self.name, version=self.version,
                              verdict="pass", evidence=evidence)
        tables = {level: (quota.get(level) if isinstance(quota, dict) else None)
                  for level in ("cells", "tier_bands", "tiers", "bands")}
        if not all(isinstance(t, dict) for t in tables.values()):
            evidence["problem"] = ("no quota tables in ctx policy: the matching "
                                   "file was missing or unreadable")
            return GateResult(name=self.name, version=self.version,
                              verdict="review", evidence=evidence)
        tier = prov.get("tier")
        band = prov.get("tenure_band")
        decile = prov.get("decile")
        if decile is None:
            evidence["problem"] = "record has no stratum (decile missing)"
            return GateResult(name=self.name, version=self.version,
                              verdict="review", evidence=evidence)
        k3 = cell_key(tier, band, decile)
        k2 = tier_band_key(tier, band)
        k1 = tier_key(tier)
        evidence.update({"cell": k3, "tier_band": k2, "tier": k1})
        if not all(g.verdict == "pass" for g in record.gates):
            evidence["note"] = ("not fully valid at this gate; no slot claimed "
                                "(a review/excluded row must never starve a "
                                "verified control)")
            return GateResult(name=self.name, version=self.version,
                              verdict="pass", evidence=evidence)

        def entry(table: dict, key: str) -> dict:
            e = table.get(key)
            if not isinstance(e, dict):
                e = {"target": 0, "achieved": 0}
                table[key] = e
            return e

        e3 = entry(tables["cells"], k3)
        e2 = entry(tables["tier_bands"], k2)
        e1 = entry(tables["tiers"], k1)
        room3 = int(e3.get("achieved") or 0) < int(e3.get("target") or 0)
        room2 = int(e2.get("achieved") or 0) < int(e2.get("target") or 0)
        room1 = int(e1.get("achieved") or 0) < int(e1.get("target") or 0)
        evidence["targets"] = {"cell": int(e3.get("target") or 0),
                               "tier_band": int(e2.get("target") or 0),
                               "tier": int(e1.get("target") or 0)}
        level = None
        if room3 and room2 and room1:
            level = "exact"
        elif room2 and room1:
            level = "decile_relaxed"
        elif room1:
            level = "band_relaxed"
        if level is None:
            evidence["reason"] = "quota_exhausted"
            evidence["problem"] = (
                f"verified control, but every quota envelope for {k3} is "
                "exhausted (tier margin preserved first, then tenure band, then "
                "decile): excluded to keep the control set's margins aligned "
                "with the transfer set"
            )
            return GateResult(name=self.name, version=self.version,
                              verdict="fail", evidence=evidence)
        # Claim: every level consumes the tier and tier-band envelopes; only
        # an exact claim consumes the 3-margin cell. The per-band floor table
        # counts every claim by the record's OWN band (honest geometry).
        e1["achieved"] = int(e1.get("achieved") or 0) + 1
        if level in ("exact", "decile_relaxed"):
            e2["achieved"] = int(e2.get("achieved") or 0) + 1
        if level == "exact":
            e3["achieved"] = int(e3.get("achieved") or 0) + 1
        band_entry = tables["bands"].get(str(band))
        if not isinstance(band_entry, dict):
            band_entry = {"treatment_count": 0, "achieved": 0}
            tables["bands"][str(band)] = band_entry
        band_entry["achieved"] = int(band_entry.get("achieved") or 0) + 1
        evidence["claim_level"] = level
        return GateResult(name=self.name, version=self.version,
                          verdict="pass", evidence=evidence)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class SportsControlsAdapter(Adapter):
    """Adapter for source 'sports_controls' (v2 discovery). Stateless; caches
    load once per run into cfg (the sanctioned runner channel)."""

    source = SOURCE

    # -- enumeration --------------------------------------------------------

    def enumerate_candidates(self, cfg: dict):
        """Yield the VERIFY-SELECTED rows of sports_controls_candidates.jsonl
        in (seeded_rank, qid, line) order — so the seeded draw priority IS
        the quota gate's first-come order. Unselected discovery rows are the
        frozen sampling frame (counted in the snapshot manifest), not
        candidates; unparseable lines are yielded as error candidates so
        build_record raises them into build_errors."""
        self._require_offline(cfg)
        path = self._data_path(cfg, CANDIDATES_FILENAME)
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
                if not row.get("verify_selected"):
                    continue
                candidates.append({"_line": line_no, "candidate": row})
        candidates.sort(key=lambda c: (
            str((c.get("candidate") or {}).get("seeded_rank") or ""),
            str((c.get("candidate") or {}).get("qid") or ""),
            c["_line"],
        ))
        yield from candidates

    # -- record building ----------------------------------------------------

    def build_record(self, candidate: dict, cfg: dict) -> FactChangeRecord:
        if "_parse_error" in candidate:
            raise ValueError(
                f"{CANDIDATES_FILENAME} line {candidate.get('_line')}: unparseable "
                f"JSON ({candidate['_parse_error']})"
            )
        cand = candidate.get("candidate") or {}
        title = cand.get("title")
        if not isinstance(title, str) or not title:
            raise ValueError(
                f"{CANDIDATES_FILENAME} line {candidate.get('_line')}: missing title"
            )
        self._ensure_loaded(cfg)

        qid = cand.get("qid") if isinstance(cand.get("qid"), str) else None
        club_qid = cand.get("club_qid")
        club_title = cand.get("club_title") or ""
        club_label = cand.get("club_label") or ""
        target_name = club_title or club_label
        matching = cfg.get(MATCHING_CTX_KEY) or {}

        page_url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(
            title.replace(" ", "_")
        )

        # ---- the two pinned revisions (cutoff-era + pull-date) -------------
        rev_row = (cfg.get(REVISIONS_CTX_KEY) or {}).get(title)
        fetch_errors = []
        sides: dict = {}
        if isinstance(rev_row, dict):
            fetch_errors = [e for e in (rev_row.get("fetch_errors") or [])
                            if isinstance(e, str)]
            for side_key in ("cutoff", "current"):
                side = rev_row.get(side_key)
                if isinstance(side, dict):
                    ext = dict(extract_current_club(side.get("content")))
                    ext["revision"] = {
                        "pinned_ts": side.get("pinned_ts"),
                        "ts_source": side.get("ts_source"),
                        "ts": side.get("ts"),
                        "revid": side.get("revid"),
                        "sha1": side.get("sha1"),
                    }
                    ext["side"] = side_key
                    sides[side_key] = (side, ext)

        def side_state(side_key: str, extra_ref=None) -> ValueState:
            side, ext = sides.get(side_key, (None, None))
            ts = side.get("ts") if isinstance(side, dict) else None
            ref = {
                "page_title": title,
                "revision_timestamp": ts,
                "extraction": ext,
                "role": "cutoff_state" if side_key == "cutoff" else "reverify_current",
            }
            if extra_ref:
                ref.update(extra_ref)
            if fetch_errors and ext is None:
                ref["fetch_errors"] = fetch_errors
            return ValueState(
                raw=infobox_club(ext),
                canonical=None,
                evidence=Evidence(kind="wikipedia_infobox", url=page_url,
                                  ref=ref, as_of=ts),
            )

        # ---- pull-date P54 re-verification + anchor ------------------------
        cache_row = (cfg.get(WD_CACHE_CTX_KEY) or {}).get(qid) if qid else None
        statements = (
            cache_row.get("statements") if isinstance(cache_row, dict) else None
        )
        statements = statements if isinstance(statements, list) else []
        reverify = None
        if isinstance(cache_row, dict):
            comparator = get_comparator(VALUE_TYPE)
            reverify = reverify_membership(target_name, statements, comparator)
        anchor = anchor_from_reverify(reverify)

        before = side_state("cutoff")
        after = side_state("current", extra_ref={
            "target_club": {"qid": club_qid, "enwiki_title": club_title,
                            "label": club_label},
            "wikidata_p54_reverify": reverify,
        })

        # ---- change_date: the P54 membership start (tenure start) ----------
        cd = wd_time_to_change_date(cand.get("p580"))
        if cd is not None:
            cd_value, cd_precision, cd_wd_precision = cd
            cd_basis = "p54_membership_start"
        else:
            cd_value, cd_precision, cd_wd_precision = (
                ANCHOR_DATE.isoformat(), "day", None)
            cd_basis = "anchor_fallback_start_missing"
        change_date = ChangeDate(
            value=cd_value,
            precision=cd_precision,
            evidence=Evidence(
                kind="wikidata_p54",
                url=f"https://www.wikidata.org/wiki/{qid}" if qid else None,
                ref={
                    "basis": cd_basis,
                    "note": ("a control is an UNCHANGED fact; this date is when it "
                             "became true — the open P54 membership's start"),
                    "club_qid": club_qid,
                    "p580": cand.get("p580"),
                    "wd_precision": cd_wd_precision,
                },
                as_of=None,
            ),
        )

        sport = None
        for key in ("current", "cutoff"):
            pair = sides.get(key)
            if pair and isinstance(pair[1], dict) and pair[1].get("sport"):
                sport = pair[1]["sport"]
                break

        rev_info = cfg.get(REVISIONS_INFO_CTX_KEY) or {"file": None, "sha1": None}
        wd_info = cfg.get(WD_CACHE_INFO_CTX_KEY) or {"file": None, "sha1": None}
        tier = cand.get("tier")
        decile = cand.get("decile")
        provenance = {
            "predictability": ANNOUNCED,
            "population": POPULATION,
            "origin": cand.get("origin") or "discovery",
            # KNOWABILITY ANCHOR + tenure (the principal's visible-tension ask)
            "anchor_date": anchor.get("date"),
            "anchor_precision": anchor.get("precision"),
            "anchor_basis": anchor.get("basis"),
            "tenure_start": cd_value if cd is not None else None,
            "tenure_start_precision": cd_precision if cd is not None else None,
            # TENURE at pull date (addendum: stamped so the eval never
            # recomputes with different boundaries) — frozen by the harvester
            # from the pinned start vs the pull asof.
            "tenure_years": cand.get("tenure_years"),
            "tenure_band": cand.get("tenure_band") or TENURE_BAND_UNKNOWN,
            # MATCHING strata
            "club": {"qid": club_qid, "enwiki_title": club_title,
                     "label": club_label},
            "league": {"qid": cand.get("league_qid"),
                       "label": cand.get("league_label")},
            "tier": tier if tier is not None else UNKNOWN_TIER,
            "sitelinks": cand.get("sitelinks"),
            "decile": decile,
            "cell": cell_key(tier, cand.get("tenure_band"),
                             decile if decile is not None else 0),
            "seeded_rank": cand.get("seeded_rank"),
            "line": candidate.get("_line"),
            "page": {
                "title": title,
                "sport": sport,
                "cutoff_rev_ts": before.evidence.ref.get("revision_timestamp"),
                "reverify_rev_ts": after.evidence.ref.get("revision_timestamp"),
            },
            "matching": {
                "seed": matching.get("seed"),
                "treatment_release": matching.get("treatment_release"),
                "treatment_facts_sha1": matching.get("treatment_facts_sha1"),
            },
            "revisions_cache": {"file": rev_info.get("file"),
                                "sha1": rev_info.get("sha1"),
                                "row_present": isinstance(rev_row, dict)},
            "wd_cache": {"file": wd_info.get("file"), "sha1": wd_info.get("sha1"),
                         "player_in_cache": isinstance(cache_row, dict),
                         "statements": len(statements)},
        }

        entity_ids = {ENTITY_ID_KEY: qid} if qid else {}
        fact_id = compute_fact_id(SOURCE, title, PROPERTY, cd_value)
        cutoff_ts = before.evidence.ref.get("revision_timestamp")
        current_ts = after.evidence.ref.get("revision_timestamp")
        return FactChangeRecord(
            fact_id=fact_id,
            record_id=compute_record_id(
                fact_id, f"{qid or ''}|{cutoff_ts or ''}|{current_ts or ''}"
            ),
            source=SOURCE,
            entity={"name": title, "ids": entity_ids},
            property=PROPERTY,
            value_type=VALUE_TYPE,
            before=before,
            after=after,
            change_date=change_date,
            provenance=provenance,
        )

    # -- gates --------------------------------------------------------------

    def gate_list(self, cfg: dict):
        self._require_offline(cfg)
        self._ensure_loaded(cfg)
        matching = cfg.get(MATCHING_CTX_KEY) or {}
        quotas = matching.get("quotas") if isinstance(matching.get("quotas"), dict) else {}
        margins = (matching.get("treatment_margins")
                   if isinstance(matching.get("treatment_margins"), dict) else {})

        def quota_table(level: str) -> dict:
            table = quotas.get(level)
            if not isinstance(table, dict):
                return {}
            return {str(k): {"target": int(v), "achieved": 0}
                    for k, v in sorted(table.items())
                    if isinstance(v, int) and not isinstance(v, bool)}

        band_counts = margins.get("bands") if isinstance(margins.get("bands"), dict) else {}
        # The manifest-recorded policy: loans + anchor + the SHARED quota
        # tables (three nested levels + the per-tenure-band floor-geometry
        # table, treatment count vs achieved, empty low bands included). The
        # runner's ctx is a shallow copy of cfg, so the quota gate's in-place
        # 'achieved' increments land in the very dicts the manifest
        # serializes after the loop: every release records target-vs-achieved
        # per stratum and the priority-ladder deviations.
        cfg[POLICY_ACTIVE_CTX_KEY] = {
            **resolve_policy(cfg),
            "anchor_date": ANCHOR_DATE.isoformat(),
            "quota": {
                "seed": matching.get("seed"),
                "target_total": matching.get("target_total"),
                "priority": "tier>tenure_band>decile",
                "cells": quota_table("cells"),
                "tier_bands": quota_table("tier_bands"),
                "tiers": quota_table("tiers"),
                "bands": {
                    str(b): {"treatment_count": int(band_counts.get(b) or 0),
                             "achieved": 0}
                    for b in sorted(set(list(band_counts) + list(TENURE_BANDS)
                                        + [TENURE_BAND_UNKNOWN]))
                },
            },
        }
        return [
            SportsControlsLoanGate(),
            ControlUnchangedGate(),
            ControlsCorroborationGate(),
            ControlAnchorGate(),
            EvidenceResolvableGate(),
            ControlQuotaGate(),
            DedupGate(),
        ]

    def snapshot_inputs(self, cfg: dict):
        return [
            CANDIDATES_FILENAME, REVISIONS_FILENAME, WD_CACHE_FILENAME,
            MATCHING_FILENAME, REVISIONS_SIDECAR_FILENAME, WD_SIDECAR_FILENAME,
        ]

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _require_offline(cfg: dict) -> None:
        if not cfg.get("offline", True):
            raise NotImplementedError(
                "the sports_controls adapter is offline-only: the pull-date "
                "evidence is fetched once by `python3 -m stage1.harvest "
                "--source sports_controls`"
            )

    @staticmethod
    def _data_path(cfg: dict, filename: str) -> Path:
        data_dir = cfg.get("data_dir")
        if data_dir is None:
            raise LookupError(
                f"the sports_controls adapter requires --data-dir (a sports_controls "
                f"harvest snapshot containing {filename})"
            )
        path = Path(data_dir) / filename
        if not path.is_file():
            raise LookupError(f"sports_controls input file not found: {path}")
        return path

    def _ensure_loaded(self, cfg: dict) -> None:
        """Load the matching file, the pinned-revisions cache, and the P54
        cache into cfg exactly once per run. Malformed content becomes
        input_load_errors; a missing cache degrades to review via the gates,
        never a crash."""
        if MATCHING_CTX_KEY not in cfg:
            cfg[MATCHING_CTX_KEY] = self._load_matching(cfg)
        if REVISIONS_CTX_KEY not in cfg:
            store, info, errors = self._load_cache(
                cfg.get("data_dir"), REVISIONS_FILENAME, REVISIONS_SIDECAR_FILENAME,
                key="title",
            )
            cfg[REVISIONS_CTX_KEY] = store
            cfg[REVISIONS_INFO_CTX_KEY] = info
            self._surface_cache_meta(cfg, info)
            if errors:
                cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(errors)
        if WD_CACHE_CTX_KEY not in cfg:
            store, info, errors = self._load_cache(
                cfg.get("data_dir"), WD_CACHE_FILENAME, WD_SIDECAR_FILENAME,
                key="player_qid",
            )
            cfg[WD_CACHE_CTX_KEY] = store
            cfg[WD_CACHE_INFO_CTX_KEY] = info
            self._surface_cache_meta(cfg, info)
            if errors:
                cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(errors)

    @staticmethod
    def _surface_cache_meta(cfg: dict, info: dict) -> None:
        """Bind a cache's sha1 to its sidecar retrieval metadata in the
        manifest (the extra_input_meta channel), mirroring the treatment
        adapter's audit-chain practice."""
        if not (info.get("file") and info.get("sha1")):
            return
        entry = {"sha1": info["sha1"], "retrieval": info.get("meta")}
        meta = info.get("meta")
        if isinstance(meta, dict) and "cache_sha1" in meta:
            entry["sidecar_matches_cache"] = meta.get("cache_sha1") == info["sha1"]
        cfg.setdefault(EXTRA_INPUT_META_CTX_KEY, {})[info["file"]] = entry

    def _load_matching(self, cfg: dict) -> dict:
        """The matching/quota file written by the harvester. Missing or
        unreadable degrades to an empty dict (the quota gate then reviews
        every record — visible, never a silently unmatched release), with the
        problem in input_load_errors."""
        data_dir = cfg.get("data_dir")
        if data_dir is None:
            return {}
        path = Path(data_dir) / MATCHING_FILENAME
        if not path.is_file():
            cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).append(
                {"file": MATCHING_FILENAME, "line": 0,
                 "error": "matching file missing: quotas/margins unavailable"}
            )
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                meta = json.load(fh)
        except ValueError as exc:
            cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).append(
                {"file": MATCHING_FILENAME, "line": 0,
                 "error": f"unparseable matching file: {exc}"}
            )
            return {}
        return meta if isinstance(meta, dict) else {}

    @staticmethod
    def _load_cache(data_dir, filename: str, sidecar_name: str, key: str):
        """({row[key]: row}, {'file','sha1','meta'}, load_errors). A missing
        cache file yields an empty store (gates then review); malformed lines
        and duplicate keys become load errors."""
        store: dict = {}
        errors: list = []
        info: dict = {"file": None, "sha1": None, "meta": None}
        if data_dir is None:
            return store, info, errors
        path = Path(data_dir) / filename
        if not path.is_file():
            return store, info, errors
        digest = hashlib.sha1()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                digest.update(chunk)
        info["file"] = filename
        info["sha1"] = digest.hexdigest()
        with open(path, encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError(f"row is {type(row).__name__}, expected object")
                except ValueError as exc:
                    errors.append({"file": filename, "line": line_no,
                                   "error": f"unparseable JSON: {exc}"})
                    continue
                row_key = row.get(key)
                if not isinstance(row_key, str) or not row_key:
                    errors.append({"file": filename, "line": line_no,
                                   "error": f"missing/malformed {key!r}: {row_key!r}"})
                    continue
                if row_key in store:
                    errors.append({"file": filename, "line": line_no,
                                   "error": f"duplicate {key!r}: {row_key!r} (first wins)"})
                    continue
                store[row_key] = row
        sidecar_path = Path(data_dir) / sidecar_name
        if sidecar_path.is_file():
            try:
                with open(sidecar_path, encoding="utf-8") as fh:
                    meta = json.load(fh)
                info["meta"] = meta if isinstance(meta, dict) else None
            except ValueError as exc:
                errors.append({"file": sidecar_name, "line": 0,
                               "error": f"unparseable sidecar: {exc}"})
        return store, info, errors


ADAPTER = SportsControlsAdapter()
