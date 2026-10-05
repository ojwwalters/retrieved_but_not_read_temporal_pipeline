"""Wikipedia sports-transfer adapter (source 'sports').

Ports the legacy wikipedia/sports pipeline (sports_harvest.py phase-2 ground
truth + sports_curate.py tiers) onto the shared Stage-1 machinery, replacing
the legacy LLM tier-3 web-verify gate with deterministic Wikidata-vs-infobox
corroboration.

METHODOLOGICAL CHANGE (owner decision, 22 Jul 2026): the same-source tier-2
stability gate (sports_stability) has been REMOVED. It replayed a same-source
Wikipedia re-fetch OFFLINE from the legacy now_club primitive, which is
redundant with the corroboration gate (name 'corroboration', version
'corroboration:sports_v1'): corroboration already requires the infobox
after-value to org-match a Wikidata P54 claim, and two independently-edited
sources agreeing is a STRONGER vandalism guard than a same-source re-fetch,
with no window-size edge cases. Removing it also COMPLETES the harvest port —
the harvester (stage1/harvest/sports.py) never produced the now_club
primitive, so with the gate present a fresh harvest snapshot (which lacks
sports_curated.jsonl) degraded every transfer to a 'stability_unknown'
review. The old stability signals stay covered: a genuine revert leaves
after == before and is caught by value_changed (excluded), loans by the
sports_loan gate, and recency by corroboration availability. Sports included
rose 185 -> 210 as a result; sports_curated.jsonl is now purely optional
regression provenance (no gate reads it, and the now_club primitive is no
longer consulted at all).

Inputs (all read-only, under --data-dir):

* sports_verified.jsonl  — the enumeration source: one row per player found
  by the Wikidata P54 finder, with the enwiki infobox club at the cutoff
  snapshot (old_club, cutoff_rev_ts) and at harvest time (new_club,
  cur_rev_ts). ALL 959 rows are enumerated, including changed=false and
  unrecognized-infobox rows — enumeration never decides inclusion; the
  value_changed gate excludes provable non-changes and everything
  undecidable lands in review (the legacy pipeline saw only the 436
  changed=true rows and dropped the rest silently).
* sports_candidates.jsonl — joined by title for the player QID (the only
  file that has it) and the finder's sport hint.
* sports_curated.jsonl    — OPTIONAL provenance-only regression data,
  joined by title for the legacy tier-1/2/3 VERDICT columns (tier1_ok,
  tier2_state, keep, web_verified, ...). NO gate reads this file: the port
  recomputes all dispositions from primitives, because the stored verdicts
  are known-inconsistent (stale tier2_ok booleans, spurious
  tier2_state='reverted' on tier1-fail rows, keep=None on 13 rows). The old
  tier-2 re-fetch primitive (now_club) that fed the removed stability gate
  is no longer consulted (see the METHODOLOGICAL CHANGE note above), so a
  harvest snapshot WITHOUT sports_curated.jsonl derives fully — the adapter
  degrades to cache_hit=false legacy provenance, never a crash and never a
  forced review. The LLM tier-3 verdict is NOT an input to any gate.
* sports_wd_p54.jsonl     — the Wikidata P54 enrichment cache built once by
  stage1.tools.fetch_wikidata_p54 (checked in {data_dir} first, then the
  package default stage1/cache/). Read-only; a missing cache or cache entry
  degrades to 'review' via the corroboration gate, never a crash. This is
  the sanctioned exception to the data-dir-only rule: the cache is a
  stage1-owned frozen artifact, and a LOCATION-INDEPENDENT identifier
  (the bare filename when vendored in data_dir, else the repo-relative
  'stage1/cache/sports_wd_p54.jsonl') plus its sha1 are recorded in every
  record's provenance AND in the manifest's input_files (via the runner's
  cfg['extra_input_files'] channel) so releases stay auditable and
  facts.jsonl is byte-identical across checkouts (an absolute path here
  used to leak the machine's directory layout into every record). The
  .meta.json sidecar's retrieval metadata (tool_version, retrieved_at,
  endpoint, candidates sha1) is surfaced into the manifest via
  cfg['extra_input_meta'], binding the pinned cache sha1 to when/how it was
  fetched even if a later refetch overwrites the sidecar in place.
* sports_wikitext.jsonl   — the pinned enwiki wikitext cache built once by
  stage1.tools.fetch_wiki_revisions (same {data_dir}-then-package-default
  resolution, same provenance/manifest treatment as the P54 cache). One
  row per title with the FULL wikitext of the revision at each side's pin
  (cutoff_rev_ts / cur_rev_ts, or the deterministic window-fallback pins
  when the legacy harvest recorded none — ts_source distinguishes them).
  Used by the versioned wiki_infobox re-extraction (stage1.wikitext +
  stage1.adapters.sports_infobox) under STRICT precedence rules:

    (a) legacy harvest extraction non-empty -> the legacy value stays
        PRIMARY; the re-extraction runs as a consistency check and any
        disagreement routes to review via the 'extraction_conflict' gate —
        never a silent repoint of a validated value;
    (b) legacy extraction empty (the 258 empty-club review rows) -> the
        re-extraction fills the gap from the pinned revision, with
        extractor version + revid + revision sha1 recorded in the side's
        evidence so the value is evidence-anchored; sides the extractor
        cannot READ safely stay empty with an explicit machine-readable
        reason (redirect_at_snapshot, no_article_at_snapshot,
        template_not_recognized, value_unreadable, career_loan_row,
        team_field_loan_annotation — a loan-annotated team field is never
        filled as the senior club), and a present-but-blank current-team
        field is recorded as the canonically DISTINCT state 'unattached'
        (never conflated with 'page absent' or 'parse failure');
    (c) wikitext cache/title missing -> unchanged behavior (review, with
        reason cache_missing / title_not_in_cache in the evidence); a
        cached side whose pin does not match the row's recorded revision
        timestamp (a cache built from a different verified file) is
        unusable with reason cache_pin_mismatch — never a value anchored
        to the wrong revision.

Value semantics: value_type='org', property='current_club'. before.raw /
after.raw are the infobox club extractions verbatim (first wikilink target,
per the legacy harvest); Wikipedia is the declared ground truth and Wikidata
is the corroborating second source — hence a WD disagreement is 'review'
(loan/second-move/timing-lag/vandalism, only a human can say), never a fail.

Change date policy (the legacy wd_date is untrustworthy: the SPARQL harvest
truncated xsd:dateTime and discarded the wikibase precision qualifier —
195/436 changed rows read '2026-01-01', i.e. month- or year-precision
rendered as a day):

* basis 'wd_p54_corroborated'  — exactly one cached P54 statement in the
  study window org-matches the infobox club: its P580 becomes the change
  date with the TRUE Wikidata precision (11->day, 10->month, <=9->year).
* basis 'wd_p54_ambiguous'     — several in-window statements match with
  differing dates/teams (loan + permanent, second move): the earliest
  matched date is recorded but the temporal gate routes the record to
  review, never picking a side by default.
* basis 'wd_date_uncorroborated' — no corroborated statement: the legacy
  wd_date pinned to its month start with explicit precision 'month'
  (conservative: the harvested day component cannot be trusted).
* basis 'revision_bracket'     — no usable date at all (defensive): the
  change is provably inside (cutoff_rev_ts, cur_rev_ts]; the record carries
  the bracket and the temporal gate sends it to review.

Gate order (first FAIL names the disposition; every gate always runs):

 1. sports_schema      — required fields present and the sport infobox
                         recognized (by the legacy harvest OR the wiki_
                         infobox re-extraction); a side's revision evidence
                         may come from the legacy timestamps or the pinned
                         wikitext cache; anything missing -> review
                         (absence is ambiguity, not proof of scope).
 1b. extraction_conflict— review-only consistency screen: where the legacy
                         value is primary, the wiki_infobox re-extraction
                         from the pinned revision must agree (org-equal) or
                         the record goes to review with both values in
                         evidence. Unchecked sides (missing cache) pass
                         with a note — the check is an audit, not an input.
                         PRECEDENCE CAVEAT (derive_disposition): a review
                         verdict here is outranked by ANY later fail; the
                         sports value_changed gate is therefore conflict-
                         aware (an 'equal' built from disputed values
                         demotes to review, see 8), so a disputed value can
                         never prove a non-change — but other hard fails
                         (temporal, free-agent, ...) still name the
                         disposition with the conflict evidence in the
                         ledger.
 2. sports_free_agent  — legacy tier-1 'free/retired' port: new value
                         matches free agent|retired|unattached|without club
                         -> FAIL (positively not a club-to-club change).
 3. sports_reserve_team— legacy tier-1 'reserve/youth/national' port,
                         split: unambiguous marker words (reserves, academy,
                         youth, national team, U-nn) -> FAIL; the legacy
                         regex's bare 'B'/'II' tokens -> REVIEW (they
                         false-positive on legitimate club names). v2 adds a
                         Wikidata feedback branch: when the corroborating
                         P54 team's sitelink/label carries a reserve/youth
                         marker the infobox value lacks (e.g. infobox
                         'Bilbao Athletic', WD label 'Athletic Bilbao B'),
                         the record is REVIEW 'reserve_side_per_wikidata' —
                         an unmarked reserve-club name must not pass the
                         scope screen on pattern absence alone.
 4. sports_fragment    — legacy tier-1 'fragment' port: a non-empty new
                         value under 3 chars cannot name a club -> FAIL;
                         empty -> review (extraction absence).
                         (The legacy tier-2 same-source stability gate that
                         once sat here was REMOVED 22 Jul 2026 — see the
                         METHODOLOGICAL CHANGE note above; corroboration is
                         the vandalism guard now.)
 5. temporal_window    — precision-aware window check (see the gate
                         docstring): provably-pre-cutoff day-precision
                         dates fail; month/year-precision dates straddling
                         the cutoff (esp. the 2026-01 harvest-edge month)
                         -> review 'window_edge', never a default.
 6. value_parsed       — both sides org-parseable (one-sided extractions
                         -> review).
 7. garbage_value      — shared screen; no org rules configured (the named
                         sports gates above ARE the org screen), so it
                         passes with an explicit note.
 8. value_changed      — sports-aware org comparison (version
                         value_changed:sports_v1): equal -> FAIL (covers
                         legacy tier-1 'same_after_norm' AND the 523
                         changed=false rows), EXCEPT when either side's
                         extraction consistency verdict is 'conflict' or
                         'undecided' — an equal built from values the
                         pinned-revision re-extraction disputes demotes to
                         REVIEW (a disputed value cannot prove a
                         non-change; without this, the first-fail rule
                         would let the exclusion silently outrank the
                         extraction_conflict review). Suffix/diacritic
                         variants canonicalize equal, so 'CD Extremadura'
                         vs 'Club Deportivo Extremadura'-class renames land
                         here as review via token-extension rather than
                         being kept as changes.
 8a. sports_loan       — POLICY loan screen (see DEFAULT_POLICY), placed here,
                         AFTER value_changed, so a provable non-change wins.
                         A before/after value the sports_infobox:v2 extractor
                         flagged as a LOAN destination ('[[X]]<br>(on loan
                         from [[Y]])') is not provably a senior-club transfer
                         (Wikidata registers loan spells as ordinary P54
                         statements, so corroboration alone cannot keep a loan
                         out). Under policy loans='exclude' (owner, 19 Jul
                         2026) -> FAIL, disposition excluded:sports_loan, with
                         the active policy in evidence; under 'review' ->
                         review, the pre-policy behavior. A loan row whose
                         values are equal keeps excluded:value_changed.
 8b. temporal_window_ambiguous — POLICY window-edge screen (see DEFAULT_POLICY),
                         also AFTER value_changed. It mirrors the
                         temporal_window verdict from the ledger: a REVIEW with
                         reason 'window_edge' (a coarse-precision date at the
                         cutoff/asof edge) under policy window_edge_ambiguous=
                         'exclude' (owner, 19 Jul 2026) -> FAIL, disposition
                         excluded:temporal_window_ambiguous; under 'review' it
                         passes and the temporal_window review stands. Provable
                         non-changes still win (excluded:value_changed).
 9. corroboration      — REPLACES the LLM tier-3 gate (sports-specific
                         implementation, name 'corroboration', version
                         'corroboration:sports_v1'): after-value must
                         org-match a cached in-window Wikidata P54 team.
                         Matching is SQUAD-AWARE and sitelink-primary: the
                         enwiki sitelink title (same namespace as the
                         infobox wikilink target) decides when it compares
                         'equal'; a bare label/alias 'equal' is DEMOTED —
                         never a match — when the sitelink disagrees
                         outright or carries a squad/era marker (women's/
                         youth/reserve 'B'/'(1958)') the infobox value
                         lacks, so a same-named sibling item (the Ebnoutalib
                         women's-team class) can never corroborate the
                         senior-club fact. Match -> pass; everything else ->
                         review with a specific reason: 'sources_disagree'
                         is reserved for rows where NO statement at any date
                         org-matches the club; 'club_present_out_of_window'
                         / 'club_present_undated' name the rows where
                         Wikidata agrees on the club but cannot date the
                         change; 'label_only_no_sitelink' flags mismatches
                         that may be romanization artifacts; 'sitelink_
                         mismatch' records demoted label matches. (The 101
                         rows the legacy tier-3 excluded with
                         web_searched=false get a real adjudication.)
10. evidence_resolvable— both sides' evidence points somewhere.
11. dedup              — last; key (entity.name, property, change_date);
                         verified titles are unique so this should be a
                         no-op check (player QID would need the candidates
                         join, which not every row has — title is the
                         stable key of this source).
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from datetime import date as _date, timedelta
from pathlib import Path

import stage1.normalize.org  # noqa: F401  (registers the org comparator)
from stage1.adapters import Adapter
from stage1.adapters.sports_infobox import (
    EXTRACTOR_VERSION,
    extract_current_club,
)
from stage1.gates import Gate
from stage1.gates.standard import (
    DedupGate,
    EvidenceResolvableGate,
    GarbageValueGate,
    ValueActuallyChangedGate,
    ValueParsedGate,
    _coerce_date,
)
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

VERIFIED_FILENAME = "sports_verified.jsonl"
CANDIDATES_FILENAME = "sports_candidates.jsonl"
CURATED_FILENAME = "sports_curated.jsonl"
WD_CACHE_FILENAME = "sports_wd_p54.jsonl"
WIKITEXT_CACHE_FILENAME = "sports_wikitext.jsonl"

CANDIDATES_CTX_KEY = "sports_candidates_by_title"
CURATED_CTX_KEY = "sports_curated_by_title"
WD_CACHE_CTX_KEY = "sports_wd_cache"
WD_CACHE_INFO_CTX_KEY = "sports_wd_cache_info"
WIKITEXT_CACHE_CTX_KEY = "sports_wikitext_cache"
WIKITEXT_CACHE_INFO_CTX_KEY = "sports_wikitext_cache_info"
GARBAGE_RULES_CTX_KEY = "garbage_rules"
LOAD_ERRORS_CTX_KEY = "input_load_errors"
EXTRA_INPUTS_CTX_KEY = "extra_input_files"
EXTRA_INPUT_META_CTX_KEY = "extra_input_meta"

# ---------------------------------------------------------------------------
# Owner curation policy (decided 19 Jul 2026), manifest-recorded and
# one-flag revisable.
# ---------------------------------------------------------------------------
# Each flag is 'exclude' (the policy turns a would-be review into a benchmark
# FAIL) or 'review' (hold the row for a human — the pre-policy behavior):
#
#   loans                 — a loan-flagged record (an infobox side annotated
#                           '(on loan from ...)', caught by the sports_loan
#                           gate) is not provably a senior-club transfer; the
#                           owner excludes loans for a clean current_club
#                           ground truth (report them as a tagged subset).
#   window_edge_ambiguous — a temporal_window 'window_edge' review (a
#                           month/year-precision change date at the cutoff/asof
#                           edge that cannot be placed relative to the cutoff);
#                           the owner excludes them because pre-cutoff news
#                           contamination cannot be ruled out.
#
# Both policy exclusions are enforced by gates that run AFTER value_changed in
# gate_list, so a PROVABLE non-change (value_changed FAIL, first in ledger
# order) always names the disposition first: a loan or window-edge row whose
# before == after stays excluded:value_changed, never excluded:sports_loan /
# excluded:temporal_window_ambiguous. Flip a flag to 'review' to restore the
# hold-for-human behavior without touching any gate; the active policy is
# written into the manifest (via cfg[POLICY_ACTIVE_CTX_KEY]) so every release
# records which rule produced it.
DEFAULT_POLICY = {
    "loans": "exclude",
    "window_edge_ambiguous": "exclude",
}
POLICY_MODES = ("exclude", "review")
# cfg channel for an explicit override (a {flag: mode} dict); the resolved
# active policy is written to cfg[POLICY_ACTIVE_CTX_KEY] for the gates to read
# and for the runner to fingerprint in the manifest.
POLICY_OVERRIDE_CTX_KEY = "sports_policy"
POLICY_ACTIVE_CTX_KEY = "policy"


def resolve_policy(cfg) -> dict:
    """The active policy: DEFAULT_POLICY overlaid by cfg['sports_policy'].

    Only recognized flags with a valid mode ('exclude'/'review') override a
    default; anything else is ignored, so a malformed override can never
    silently disable a gate or invent a new flag. Pure and total."""
    active = dict(DEFAULT_POLICY)
    override = cfg.get(POLICY_OVERRIDE_CTX_KEY) if isinstance(cfg, dict) else None
    if isinstance(override, dict):
        for flag in DEFAULT_POLICY:
            mode = override.get(flag)
            if mode in POLICY_MODES:
                active[flag] = mode
    return active


def _policy_mode(ctx, flag: str) -> str:
    """The active mode for one policy flag, read from
    ctx[POLICY_ACTIVE_CTX_KEY]. Defaults to 'review' (NEVER exclude without an
    explicit active policy) so a gate invoked with no policy wired stays on the
    safe side of the 'nothing silently dropped' rule."""
    policy = ctx.get(POLICY_ACTIVE_CTX_KEY) if isinstance(ctx, dict) else None
    mode = policy.get(flag) if isinstance(policy, dict) else None
    return mode if mode in POLICY_MODES else "review"

# Location-independent identifiers for the package-default caches, recorded
# in provenance and the manifest instead of a machine-absolute path so that
# facts.jsonl is byte-identical across checkouts (the path is only resolved
# absolutely for READING).
PACKAGE_WD_CACHE_ID = "stage1/cache/sports_wd_p54.jsonl"
PACKAGE_WIKITEXT_CACHE_ID = "stage1/cache/sports_wikitext.jsonl"

PROPERTY = "current_club"
VALUE_TYPE = "org"
ENTITY_ID_KEY = "wikidata_qid"

# PREDICTABILITY tag (owner decision B, 2026-07-21): a sports transfer (the
# current_club property) is rumoured/announced ahead of completion, so every
# sports record carries "announced". Stage-2 stratification METADATA written to
# provenance['predictability']; never a gate, never affecting a disposition.
PREDICTABILITY = ANNOUNCED

# Package-default cache location (written by stage1.tools.fetch_wikidata_p54).
DEFAULT_WD_CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"

# Legacy tier-1 patterns, ported. The free-agent pattern is the legacy one
# verbatim (case-insensitive, new value only). The reserve pattern is SPLIT:
# unambiguous marker words fail (now case-insensitive — the legacy regex was
# accidentally case-sensitive), while the legacy bare 'B'/'II' alternatives
# are kept case-sensitive and demoted to review because they false-positive
# on legitimate club names.
FREE_AGENT_RE = re.compile(r"free agent|retired|unattached|without club", re.I)
RESERVE_STRONG_RE = re.compile(r"\b(?:U-?\d+|reserves?|academy|youth|national team)\b", re.I)
RESERVE_WEAK_RE = re.compile(r"\b(?:II|B)\b")

# Squad/era markers on a WIKIDATA-side team name (sitelink title or label)
# that identify a same-named sibling item of a senior men's club: gendered
# sides ('Eintracht Frankfurt (women)' labeled 'Eintracht Frankfurt'),
# college men's/women's program items, and year-disambiguated refounds
# ('FC Bihor Oradea (1958)' vs '(2022)'). Used with RESERVE_STRONG_RE /
# RESERVE_WEAK_RE by the squad-aware corroboration match: a label/alias
# 'equal' cannot corroborate when one of these appears on the Wikidata side
# but not in the infobox value.
WD_SQUAD_MARKER_RE = re.compile(
    r"\bwomen'?s?\b|\bladies\b|\bmen's\b|\bfrauen\b|\bfemenin[oa]\b|\bfeminino\b"
    r"|\(\d{4}\)",
    re.I,
)

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WD_TIME_RE = re.compile(r"^\+(\d{4})-(\d{2})-(\d{2})T")


# ---------------------------------------------------------------------------
# Wikidata time / interval helpers (pure, total)
# ---------------------------------------------------------------------------

def wd_time_to_change_date(p580):
    """Map a cached P580 {'time','precision'} to (iso_value, precision_str,
    wd_precision_int), or None when unusable.

    Wikibase precision 11 = day, 10 = month, 9 = year; coarser precisions
    collapse to 'year' (the schema's coarsest) with the raw integer kept so
    evidence stays honest. Month/year-precision times pin unknown components
    to '01' per the ChangeDate convention. A '00' day/month component (old
    Wikibase data) demotes the precision accordingly. Negative years and
    nonsense dates return None."""
    if not isinstance(p580, dict):
        return None
    match = _WD_TIME_RE.match(p580.get("time") or "")
    precision = p580.get("precision")
    if not match or not isinstance(precision, int) or isinstance(precision, bool):
        return None
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if year < 1 or year > 9999:
        return None
    if precision >= 11 and month >= 1 and day >= 1:
        try:
            return _date(year, month, day).isoformat(), "day", precision
        except ValueError:
            return None
    if precision >= 10 and month >= 1 and month <= 12:
        return f"{year:04d}-{month:02d}-01", "month", precision
    return f"{year:04d}-01-01", "year", precision


def change_date_interval(value: str, precision: str):
    """The closed [lo, hi] date interval a (value, precision) pair denotes,
    or None when the value is not a real ISO date / precision is unknown."""
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return None
    try:
        day = _date.fromisoformat(value)
    except ValueError:
        return None
    if precision == "day":
        return day, day
    if precision == "month":
        lo = day.replace(day=1)
        nxt = (lo + timedelta(days=31)).replace(day=1)
        return lo, nxt - timedelta(days=1)
    if precision == "year":
        return day.replace(month=1, day=1), day.replace(month=12, day=31)
    return None


def month_start(d: _date) -> _date:
    return d.replace(day=1)


def prev_month_start(d: _date) -> _date:
    return (d.replace(day=1) - timedelta(days=1)).replace(day=1)


def statement_names(statement: dict) -> list:
    """Comparison candidates of one cached statement, primary key first:
    enwiki sitelink title (same namespace as the infobox wikilink target),
    then the English label, then aliases. Deduplicated, order-preserving."""
    names = []
    for name in (
        [statement.get("team_enwiki_title"), statement.get("team_label_en")]
        + list(statement.get("team_aliases_en") or [])
    ):
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def _statement_summary(statement: dict, cd=None) -> dict:
    p580 = statement.get("p580") or {}
    summary = {
        "team_qid": statement.get("team_qid"),
        "team_enwiki_title": statement.get("team_enwiki_title"),
        "team_label_en": statement.get("team_label_en"),
        "p580_time": p580.get("time"),
        "wd_precision": p580.get("precision"),
        "rank": statement.get("rank"),
    }
    if cd is not None:
        summary["change_date"] = cd[0]
        summary["precision"] = cd[1]
    return summary


def _squad_markers(name) -> list:
    """Reserve/youth/gender/era markers found in one team name (see
    WD_SQUAD_MARKER_RE); empty when none. Deterministic order."""
    if not isinstance(name, str) or not name:
        return []
    markers = []
    for regex in (RESERVE_STRONG_RE, RESERVE_WEAK_RE, WD_SQUAD_MARKER_RE):
        match = regex.search(name)
        if match and match.group(0) not in markers:
            markers.append(match.group(0))
    return markers


def _statement_match(new_canonical, new_club_raw, statement, comparator):
    """Squad-aware match of the infobox after-value against ONE statement.

    The enwiki sitelink title is PRIMARY: it lives in the same namespace as
    the infobox wikilink target, so when it compares 'equal' the statement
    corroborates. A label/alias 'equal' is a fallback and is DEMOTED — not a
    match — when (a) the sitelink title compares outright 'different', or
    (b) the sitelink title or label carries a squad/era marker
    (women's/youth/reserve-'B'/'(1958)') that the infobox value lacks: the
    statement then very likely denotes a same-named sibling item (the
    'Eintracht Frankfurt (women)' labeled 'Eintracht Frankfurt' class) and
    must never corroborate the senior-club fact, nor pin its change date.

    Returns (outcome, name, detail): outcome is 'equal' (match; name is the
    matched name), 'demoted' (label/alias equal rejected; detail has
    demote_reason and markers), 'review' (some comparison undecided),
    'different' (every parseable name disagreed), or 'no_name' (nothing
    parseable to compare). detail always records sitelink_verdict (None when
    the statement has no usable sitelink)."""
    title = statement.get("team_enwiki_title")
    title = title if isinstance(title, str) and title else None
    sitelink_verdict = None
    if title is not None:
        parsed_title = comparator.parse(title)
        if parsed_title.ok:
            comparison = comparator.compare(new_canonical, parsed_title.canonical)
            if isinstance(comparison, Comparison):
                sitelink_verdict = comparison.verdict
                if sitelink_verdict == "equal":
                    return "equal", title, {
                        "matched_via": "sitelink",
                        "sitelink_verdict": "equal",
                    }

    label_equal_name = None
    saw_review = sitelink_verdict in ("review", "incomparable")
    saw_different = sitelink_verdict == "different"
    compared_any = sitelink_verdict is not None
    for name in statement_names(statement):
        if name == title:
            continue  # the sitelink was already compared above
        parsed_name = comparator.parse(name)
        if not parsed_name.ok:
            continue
        comparison = comparator.compare(new_canonical, parsed_name.canonical)
        if not isinstance(comparison, Comparison):
            saw_review = True
            continue
        compared_any = True
        if comparison.verdict == "equal":
            if label_equal_name is None:
                label_equal_name = name
        elif comparison.verdict in ("review", "incomparable"):
            saw_review = True
        elif comparison.verdict == "different":
            saw_different = True

    if label_equal_name is not None:
        detail = {
            "matched_via": "label_alias",
            "sitelink_verdict": sitelink_verdict,
            "label_only": title is None,
        }
        if sitelink_verdict == "different":
            detail["demote_reason"] = "sitelink_disagrees"
            return "demoted", label_equal_name, detail
        side_markers = sorted(
            {m for n in (title, statement.get("team_label_en")) for m in _squad_markers(n)}
        )
        if side_markers and not _squad_markers(new_club_raw):
            detail["demote_reason"] = "wd_side_squad_marker"
            detail["markers"] = side_markers
            return "demoted", label_equal_name, detail
        return "equal", label_equal_name, detail

    if saw_review:
        return "review", None, {"sitelink_verdict": sitelink_verdict}
    if saw_different:
        return "different", None, {"sitelink_verdict": sitelink_verdict}
    if compared_any:
        return "different", None, {"sitelink_verdict": sitelink_verdict}
    return "no_name", None, {"sitelink_verdict": sitelink_verdict}


def corroborate(new_club_raw, statements, window_lo, window_hi, comparator) -> dict:
    """Deterministically match the infobox after-value against the cached P54
    statements whose P580 interval overlaps [window_lo, window_hi], using the
    squad-aware per-statement rule (_statement_match).

    Returns {'statements_total', 'statements_in_window', 'matched' (summary
    list), 'match' (summary or None — set only when every matched statement
    agrees on one (team, date, precision)), 'ambiguous', 'date'
    ((value, precision, wd_precision) or None; on ambiguity the earliest
    matched date, which the temporal gate then refuses to trust), 'demoted'
    (summaries of in-window statements whose label/alias compared equal but
    were rejected by the squad-aware rule — recorded so the review is
    self-contained and the reserve gate can read them), 'in_window' (one
    outcome summary per in-window statement)}."""
    result = {
        "statements_total": len(statements),
        "statements_in_window": 0,
        "matched": [],
        "match": None,
        "ambiguous": False,
        "date": None,
        "demoted": [],
        "in_window": [],
    }
    parsed_new = comparator.parse(new_club_raw) if isinstance(new_club_raw, str) else None
    new_canonical = parsed_new.canonical if parsed_new is not None and parsed_new.ok else None

    matched = []
    for statement in statements:
        if not isinstance(statement, dict):
            continue
        cd = wd_time_to_change_date(statement.get("p580"))
        if cd is None:
            continue
        interval = change_date_interval(cd[0], cd[1])
        if interval is None or interval[1] < window_lo or interval[0] > window_hi:
            continue
        result["statements_in_window"] += 1
        if new_canonical is None:
            continue
        outcome, name, detail = _statement_match(
            new_canonical, new_club_raw, statement, comparator
        )
        summary = _statement_summary(statement, cd)
        summary["outcome"] = outcome
        summary["sitelink_verdict"] = detail.get("sitelink_verdict")
        if outcome == "equal":
            summary["matched_name"] = name
            summary["matched_via"] = detail.get("matched_via")
            matched.append((cd, summary))
        elif outcome == "demoted":
            summary["label_equal_name"] = name
            summary["demote_reason"] = detail.get("demote_reason")
            if "markers" in detail:
                summary["markers"] = detail["markers"]
            result["demoted"].append(summary)
        result["in_window"].append(summary)

    matched.sort(key=lambda m: (m[0][0], m[0][1], m[1].get("team_qid") or ""))
    result["matched"] = [m[1] for m in matched]
    if matched:
        distinct = {
            (m[1].get("team_qid"), m[0][0], m[0][1]) for m in matched
        }
        result["date"] = matched[0][0]
        if len(distinct) == 1:
            result["match"] = matched[0][1]
        else:
            result["ambiguous"] = True
    return result


# ---------------------------------------------------------------------------
# Sports-specific gates
# ---------------------------------------------------------------------------

class SportsSchemaGate(Gate):
    """Review (never fail) when the row is structurally incomplete:
    unrecognized sport infobox, a missing club value on either side, or no
    revision evidence for a side. Absence is ambiguity — the transfer may
    be perfectly real (e.g. the 95 unrecognized-infobox rows the legacy
    pipeline silently treated as unchanged) — so a human must look.

    v2 (wikitext-cache aware): the sport counts as recognized when EITHER
    the legacy harvest identified it OR the versioned wiki_infobox
    re-extraction found a registered player template on a side (recorded
    as sport_source). A side's revision evidence is its evidence.as_of —
    the legacy revision timestamp when the harvest recorded one, else the
    pinned cache revision's timestamp when the re-extraction supplied the
    value (the per-side extraction summary, including ts_source =
    'row'|'window_fallback', is surfaced so fallback-pinned values stay
    auditable)."""

    name = "sports_schema"
    version = "sports_schema:v2"

    def evaluate(self, record, ctx: dict) -> GateResult:
        provenance = getattr(record, "provenance", None)
        page = provenance.get("page") if isinstance(provenance, dict) else None
        page = page if isinstance(page, dict) else {}
        before_ext = _extraction_ref(record, "before")
        after_ext = _extraction_ref(record, "after")
        legacy_sport = bool(page.get("sport"))
        extractor_sport = any(
            isinstance(ext, dict) and ext.get("template_registered")
            for ext in (before_ext, after_ext)
        )
        checks = {
            "sport_recognized": legacy_sport or extractor_sport,
            "before_value_present": bool(_raw_side(record, "before")),
            "after_value_present": bool(_raw_side(record, "after")),
            "before_revision_present": bool(_side_as_of(record, "before")),
            "after_revision_present": bool(_side_as_of(record, "after")),
        }
        problems = sorted(name for name, ok in checks.items() if not ok)
        evidence: dict = {"checks": checks}
        evidence["sport_source"] = (
            "legacy_harvest" if legacy_sport
            else "wiki_infobox" if extractor_sport
            else None
        )
        for side, ext in (("before", before_ext), ("after", after_ext)):
            if isinstance(ext, dict):
                revision = ext.get("revision")
                evidence[f"{side}_extraction"] = {
                    "status": ext.get("status"),
                    "reason": ext.get("reason"),
                    "ts_source": revision.get("ts_source") if isinstance(revision, dict) else None,
                }
        if problems:
            evidence["problems"] = problems
        return GateResult(
            name=self.name,
            version=self.version,
            verdict="review" if problems else "pass",
            evidence=evidence,
        )


class SportsExtractionConflictGate(Gate):
    """Review-only consistency screen between the legacy harvest extraction
    (PRIMARY wherever it is non-empty) and the versioned wiki_infobox
    re-extraction from the pinned wikitext cache.

    Verdicts:

    * every checked side 'consistent' (raw-identical, org-equal, or a
      legacy free-agent string matching the extractor's 'unattached'
      state) -> pass;
    * any side 'conflict' (org-different value, or the extractor says
      'unattached' where legacy has a club) or 'undecided' (comparator
      review/incomparable, extractor refused to read a value legacy
      claims) -> REVIEW with both values in the evidence — the legacy
      value is never silently repointed and the record is never excluded
      on this gate;
    * sides that could not be checked (cache/title/revision missing, or a
      cache_pin_mismatch) or where the check does not apply
      (extraction-filled or empty sides) pass with an explanatory note:
      the check is an audit of validated values, so a missing audit input
      must not degrade them.

    Precedence (derive_disposition): this gate's review is outranked by any
    LATER fail — the first fail in ledger order names the disposition. The
    sports value_changed gate (value_changed:sports_v1) therefore demotes
    an 'equal' FAIL to review whenever a side here is 'conflict'/'undecided'
    (a disputed value cannot prove a non-change); other hard fails
    (temporal_window, free_agent, ...) legitimately name the disposition
    while the conflict evidence stays visible in the ledger."""

    name = "extraction_conflict"
    version = "extraction_conflict:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        evidence: dict = {"extractor": EXTRACTOR_VERSION}
        problems = []
        for side in ("before", "after"):
            ext = _extraction_ref(record, side)
            if not isinstance(ext, dict):
                evidence[side] = {"verdict": "not_checked", "basis": "no_extraction_metadata"}
                continue
            consistency = ext.get("consistency")
            if not isinstance(consistency, dict):
                evidence[side] = {"verdict": "not_applicable", "role": ext.get("role")}
                continue
            entry = {
                "verdict": consistency.get("verdict"),
                "basis": consistency.get("basis"),
                "legacy_value": ext.get("legacy_value"),
                "extracted_value": consistency.get("extracted_value"),
            }
            if consistency.get("comparison_reason"):
                entry["comparison_reason"] = consistency.get("comparison_reason")
            evidence[side] = entry
            verdict = consistency.get("verdict")
            if verdict in ("conflict", "undecided"):
                problems.append(
                    f"{side}: {consistency.get('basis')} "
                    f"(legacy {ext.get('legacy_value')!r} vs extractor "
                    f"{consistency.get('extracted_value')!r})"
                )
            elif verdict not in ("consistent", "not_checked"):
                problems.append(f"{side}: unrecognized consistency verdict {verdict!r}")
        if problems:
            evidence["problem"] = (
                "extraction_conflict: the versioned re-extraction from the pinned "
                "wikitext disagrees with (or cannot confirm) the legacy harvest "
                "value; the legacy value remains primary and a human adjudicates: "
                + "; ".join(problems)
            )
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)


class SportsLoanGate(Gate):
    """Loan screen: a record whose before/after value is annotated as a LOAN
    destination in the pinned revision's infobox ('[[X]]<br>(on loan from
    [[Y]])', flagged loan=True by the sports_infobox:v2 extractor) is not
    provably a senior-club transfer — Wikidata registers loan spells as
    ordinary P54 statements, so corroboration can 'confirm' the loan
    destination and even pin the change date to the loan start. Whether the
    row is in scope (a permanent move reported alongside a loan, a loan
    return, a parent-club change) is exactly the call the design reserves for
    a human or a curation policy.

    POLICY (manifest-recorded, one-flag revisable — see DEFAULT_POLICY):

    * loans == 'exclude' (owner decision, 19 Jul 2026: loans are dropped for a
      clean senior-club ground truth) -> a loan-flagged side FAILS, disposition
      excluded:sports_loan, with the active policy named in evidence['policy'];
    * loans == 'review' (the pre-policy behavior) -> the record routes to
      review with the destination and parent clubs in evidence.

    This gate runs AFTER value_changed in gate_list precisely so a PROVABLE
    non-change wins: a loan row whose values are equal keeps
    excluded:value_changed (value_changed is the first fail in ledger order),
    never excluded:sports_loan. Under 'exclude' the fail is real, so the earlier
    review-only precedence caveat no longer applies — the ordering does the work.

    Both sides are screened: a loan-annotated BEFORE value distorts the
    transfer claim symmetrically (a loan return would otherwise present as a
    fabricated destination->parent transfer). Sides without extraction
    metadata (no wikitext cache) pass with a note — absence of the audit
    signal is handled by the schema/conflict gates, not here."""

    name = "sports_loan"
    version = "sports_loan:v2"

    def evaluate(self, record, ctx: dict) -> GateResult:
        mode = _policy_mode(ctx, "loans")
        evidence: dict = {
            "extractor": EXTRACTOR_VERSION,
            "policy": "loans",
            "policy_setting": mode,
        }
        flagged = []
        for side in ("before", "after"):
            ext = _extraction_ref(record, side)
            if not isinstance(ext, dict):
                evidence[side] = {"checked": False, "basis": "no_extraction_metadata"}
                continue
            loan = bool(ext.get("loan"))
            detail = ext.get("detail") if isinstance(ext.get("detail"), dict) else {}
            entry = {"checked": True, "loan": loan, "role": ext.get("role")}
            if loan:
                entry["club"] = ext.get("club")
                entry["loan_marker"] = detail.get("loan_marker")
                if detail.get("values"):
                    entry["values"] = detail.get("values")
                flagged.append(side)
            evidence[side] = entry
        if flagged:
            evidence["flagged_sides"] = flagged
            evidence["reason"] = "loan_annotation"
            base = (
                "loan_annotation: the pinned infobox annotates the "
                + " and ".join(flagged)
                + " value as a loan destination ('on loan from ...'); a loan "
                "spell is not provably the senior-club fact this dataset defines"
            )
            if mode == "exclude":
                evidence["problem"] = (
                    base
                    + " — owner policy loans='exclude' drops it from the benchmark "
                    "for a clean senior-club ground truth"
                )
                return GateResult(name=self.name, version=self.version, verdict="fail", evidence=evidence)
            evidence["problem"] = (
                base
                + ", and only a human can call whether the row is a genuine "
                "transfer, a loan, or a loan return (policy loans='review')"
            )
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)


class SportsValueChangedGate(ValueActuallyChangedGate):
    """Sports-aware value_changed: identical contract to the shared gate,
    EXCEPT that an 'equal' comparison cannot FAIL the record when either
    side's extraction consistency verdict is 'conflict' or 'undecided' —
    i.e. when the wiki_infobox re-extraction of the pinned revision
    disputes (or cannot confirm) the very legacy value the comparison used.
    A disposition is named by the FIRST fail in ledger order, so a plain
    value_changed fail would otherwise silently outrank the review-only
    extraction_conflict gate and exclude the record as a 'provable
    non-change' built from disputed values (the Billy Gowers class). A
    disputed value cannot prove a non-change: the equal verdict is demoted
    to review and the record joins the extraction-conflict human queue.

    Sides whose consistency is 'consistent', 'not_checked' (audit input
    missing — a missing audit must not degrade validated values), or absent
    (no wikitext cache) keep the shared gate's semantics unchanged."""

    version = "value_changed:sports_v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        result = super().evaluate(record, ctx)
        if result.verdict != "fail":
            return result
        disputed = {}
        for side in ("before", "after"):
            ext = _extraction_ref(record, side)
            consistency = ext.get("consistency") if isinstance(ext, dict) else None
            verdict = consistency.get("verdict") if isinstance(consistency, dict) else None
            if verdict in ("conflict", "undecided"):
                disputed[side] = {
                    "verdict": verdict,
                    "basis": consistency.get("basis"),
                    "legacy_value": ext.get("legacy_value"),
                    "extracted_value": consistency.get("extracted_value"),
                }
        if not disputed:
            return result
        evidence = dict(result.evidence)
        evidence["disputed_sides"] = disputed
        evidence["problem"] = (
            "before and after canonicals compare equal, but the pinned-revision "
            "re-extraction disputes (or cannot confirm) the legacy value on: "
            + ", ".join(sorted(disputed))
            + " — a disputed value cannot prove a non-change, so the equal "
            "verdict is demoted from fail to review for human adjudication"
        )
        return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)


class SportsFreeAgentGate(Gate):
    """Legacy tier-1 'free/retired' port, kept a FAIL: an after-value of
    'Free agent'/'Retired'/... is positive evidence the row is a status
    change, not a club-to-club transfer — out of scope for current_club, the
    same determination the legacy rules made (3 rows). Only the after side
    is screened (signing FROM free agency to a club IS a genuine change).
    A missing after value is review (screen could not run)."""

    name = "sports_free_agent"
    version = "sports_free_agent:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        raw = _raw_side(record, "after")
        evidence: dict = {"pattern": FREE_AGENT_RE.pattern}
        if raw is None:
            evidence["problem"] = "after.raw is missing or not a string"
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        match = FREE_AGENT_RE.search(raw)
        if match:
            evidence["matched"] = match.group(0)
            evidence["value"] = raw
            return GateResult(name=self.name, version=self.version, verdict="fail", evidence=evidence)
        return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)


class SportsReserveTeamGate(Gate):
    """Legacy tier-1 'reserve/youth/national' port, split by confidence:

    * unambiguous marker words (reserves?, academy, youth, national team,
      U-nn) -> FAIL — a reserve/youth/national side is not the senior-club
      fact this dataset defines. Case-insensitive (the legacy regex was
      accidentally case-sensitive).
    * the legacy pattern's bare 'B'/'II' tokens (case-sensitive) -> REVIEW,
      not fail: they also match legitimate club names ('Real Sociedad B' is
      a genuine reserve club, but the token alone cannot prove that), so
      the one legacy drop of this class now costs a human look instead of
      trusting a false-positive-prone regex.
    * v2 Wikidata feedback branch: a reserve club with an UNMARKED name
      ('Bilbao Athletic', 'Castilla') evades both patterns, but the
      pipeline's own corroboration evidence may identify it — when the
      matched (or demoted label-equal) P54 team's sitelink title or English
      label carries a reserve/youth marker (strong pattern, or a bare
      'B'/'II' token) that the infobox value lacks, the record is REVIEW
      'reserve_side_per_wikidata', never a pattern-blind pass. (A reserve
      side whose name is unmarked in BOTH the infobox and Wikidata remains
      undetectable offline — a documented limit, not a silent one: the
      corroboration evidence records the team names consulted.)

    A missing after value is review."""

    name = "sports_reserve_team"
    version = "sports_reserve_team:v2"

    def evaluate(self, record, ctx: dict) -> GateResult:
        raw = _raw_side(record, "after")
        evidence: dict = {
            "strong_pattern": RESERVE_STRONG_RE.pattern,
            "weak_pattern": RESERVE_WEAK_RE.pattern,
        }
        if raw is None:
            evidence["problem"] = "after.raw is missing or not a string"
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        strong = RESERVE_STRONG_RE.search(raw)
        if strong:
            evidence["matched"] = strong.group(0)
            evidence["value"] = raw
            return GateResult(name=self.name, version=self.version, verdict="fail", evidence=evidence)
        weak = RESERVE_WEAK_RE.search(raw)
        if weak:
            evidence["matched"] = weak.group(0)
            evidence["value"] = raw
            evidence["problem"] = (
                "bare 'B'/'II' token: possibly a reserve side, possibly part of a "
                "legitimate club name — cannot be decided by pattern alone"
            )
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)

        # Wikidata feedback: the infobox value carries no marker, but the
        # corroborating/demoted P54 team names might (unmarked reserve-club
        # names like 'Bilbao Athletic' whose WD label is 'Athletic Bilbao B').
        wd_team, wd_marker = self._wikidata_reserve_signal(record)
        if wd_team is not None:
            evidence["problem"] = (
                "reserve_side_per_wikidata: the Wikidata P54 team this value "
                f"matches is named {wd_team!r} (reserve/youth marker {wd_marker!r}) "
                "while the infobox value carries no marker — the after side may be "
                "a reserve team with an unmarked name"
            )
            evidence["wikidata_team"] = wd_team
            evidence["wikidata_marker"] = wd_marker
            evidence["value"] = raw
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)

    @staticmethod
    def _wikidata_reserve_signal(record):
        """(team_name, marker) when the corroboration evidence embedded in
        after.evidence.ref names a matched or demoted-label-equal P54 team
        whose sitelink/label hits a reserve/youth pattern; (None, None)
        otherwise. Reads only the record — pure and deterministic."""
        ref = getattr(getattr(getattr(record, "after", None), "evidence", None), "ref", None)
        wd = ref.get("wikidata_p54") if isinstance(ref, dict) else None
        if not isinstance(wd, dict):
            return None, None
        entries = []
        match = wd.get("match")
        if isinstance(match, dict):
            entries.append(match)
        demoted = wd.get("demoted")
        if isinstance(demoted, list):
            entries.extend(e for e in demoted if isinstance(e, dict))
        for entry in entries:
            for name in (entry.get("team_enwiki_title"), entry.get("team_label_en")):
                if not isinstance(name, str) or not name:
                    continue
                hit = RESERVE_STRONG_RE.search(name) or RESERVE_WEAK_RE.search(name)
                if hit:
                    return name, hit.group(0)
        return None, None


class SportsFragmentGate(Gate):
    """Legacy tier-1 'fragment' port: a NON-EMPTY after value shorter than 3
    characters cannot name a real club -> FAIL (positive determination, same
    as legacy). An EMPTY value is review, not fail — emptiness is extraction
    absence (the legacy 'empty_new' drop), and the schema/value_parsed gates
    carry the same signal."""

    name = "sports_fragment"
    version = "sports_fragment:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        raw = _raw_side(record, "after")
        evidence: dict = {"min_chars": 3}
        if raw is None:
            evidence["problem"] = "after.raw is missing or not a string"
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        stripped = raw.strip()
        evidence["length"] = len(stripped)
        if not stripped:
            evidence["problem"] = "after value is empty (extraction absence, not provable garbage)"
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        if len(stripped) < 3:
            evidence["value"] = raw
            return GateResult(name=self.name, version=self.version, verdict="fail", evidence=evidence)
        return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)


# NOTE: SportsStabilityGate (name 'sports_stability', version
# 'sports_stability:v1') was REMOVED on 22 Jul 2026 (owner decision — see the
# module docstring's METHODOLOGICAL CHANGE note). It replayed the legacy
# tier-2 same-source Wikipedia re-fetch offline from the now_club primitive,
# a check the corroboration gate (Wikidata P54 vs infobox) subsumes with a
# stronger, independently-edited second source and no window-size edge cases.
# The now_club primitive is no longer read anywhere in this module.


class SportsTemporalWindowGate(Gate):
    """Precision-aware temporal window for sports change dates.

    The change date carries an explicit precision (day/month/year) and a
    basis (see the module docstring). Bounds come from ctx cutoff/asof.
    Let [lo, hi] be the closed interval the (value, precision) pair denotes:

    * basis 'revision_bracket' or 'wd_p54_ambiguous' -> review (the date is
      not resolved; picking a side would be a default).
    * lo > asof                        -> FAIL 'post_window'.
    * hi <  cutoff (entirely before):
        - precision 'day'              -> FAIL 'pre_cutoff' (provable).
        - month/year, hi inside the month immediately before the cutoff
          month (the harvest edge month, Jan 2026 for the 2026-02-01
          cutoff)                      -> review 'window_edge': the true day
          is unknown and the model's training horizon is itself fuzzy, so
          which side of the cutoff the change falls on cannot be resolved.
        - month/year, older            -> FAIL 'pre_cutoff'.
    * cutoff <= lo and hi <= asof      -> pass.
    * interval straddles cutoff or asof-> review 'window_edge' (a
      month/year-precision date cannot resolve which side it falls on).

    Anything unparseable (missing bounds, bad value/precision) -> review."""

    name = "temporal_window"
    version = "temporal_window:sports_v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        evidence: dict = {}
        problems: list = []
        bounds: dict = {}
        for key in ("cutoff", "asof"):
            if key not in ctx:
                problems.append(f"ctx is missing {key!r}")
                continue
            parsed, why = _coerce_date(ctx[key])
            if parsed is None:
                problems.append(f"ctx[{key!r}] {why}")
            else:
                bounds[key] = parsed
                evidence[key] = parsed.isoformat()

        change_date = getattr(record, "change_date", None)
        value = getattr(change_date, "value", None)
        precision = getattr(change_date, "precision", None)
        evidence["change_date"] = value if isinstance(value, str) else repr(value)
        evidence["precision"] = precision if isinstance(precision, str) else repr(precision)

        basis = None
        ref = getattr(getattr(change_date, "evidence", None), "ref", None)
        if isinstance(ref, dict):
            basis = ref.get("basis")
        evidence["basis"] = basis

        if basis == "revision_bracket":
            evidence["problem"] = (
                "change date unresolved: only the revision-timestamp bracket is known"
            )
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        if basis == "wd_p54_ambiguous":
            evidence["problem"] = (
                "multiple corroborating P54 statements with differing teams/dates "
                "(loan or second move): the change date cannot be picked by default"
            )
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)

        interval = change_date_interval(value, precision) if isinstance(value, str) else None
        if interval is None:
            problems.append(
                f"change date {value!r} with precision {precision!r} does not denote a date interval"
            )
        if problems:
            evidence["problems"] = problems
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)

        lo, hi = interval
        cutoff, asof = bounds["cutoff"], bounds["asof"]
        edge_start = prev_month_start(cutoff)
        evidence["interval"] = [lo.isoformat(), hi.isoformat()]
        evidence["edge_start"] = edge_start.isoformat()

        if lo > asof:
            evidence["problem"] = "entire interval is after asof"
            evidence["reason"] = "post_window"
            return GateResult(name=self.name, version=self.version, verdict="fail", evidence=evidence)
        if hi < cutoff:
            if precision != "day" and hi >= edge_start:
                evidence["problem"] = (
                    "window_edge: a coarse-precision date in the month immediately "
                    "before the cutoff cannot resolve which side of the cutoff the "
                    "change falls on"
                )
                evidence["reason"] = "window_edge"
                return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
            evidence["problem"] = "entire interval is before the cutoff"
            evidence["reason"] = "pre_cutoff"
            return GateResult(name=self.name, version=self.version, verdict="fail", evidence=evidence)
        if lo >= cutoff and hi <= asof:
            evidence["reason"] = "in_window"
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
        evidence["problem"] = (
            "window_edge: the date interval straddles the cutoff or asof bound"
        )
        evidence["reason"] = "window_edge"
        return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)


class SportsWindowEdgePolicyGate(Gate):
    """Owner policy gate for coarse window-edge change dates (name
    'temporal_window_ambiguous', so the disposition reads
    excluded:temporal_window_ambiguous — distinct from the provable
    excluded:temporal_window pre-cutoff/post-window fails).

    It NEVER re-derives the window-edge decision; it MIRRORS the already-
    computed temporal_window verdict from the record's own gate ledger, so the
    two can never disagree. When temporal_window returned REVIEW with reason
    'window_edge' (a month/year-precision date at the cutoff/asof edge that
    cannot be placed relative to the cutoff — including the suspiciously exact
    '2026-01-01' month-precision harvest-edge dates) and the active policy
    window_edge_ambiguous == 'exclude' (owner decision, 19 Jul 2026: excluded
    because pre-cutoff news contamination cannot be ruled out), this gate FAILS
    with the policy named in evidence['policy']. Under 'review' it passes and
    the temporal_window review stands.

    Runs AFTER value_changed in gate_list, so a PROVABLE non-change wins: a
    coarse-dated row whose before == after keeps excluded:value_changed. If
    temporal_window did not run, or was a pass / a fail / a non-window_edge
    review (revision_bracket, wd_p54_ambiguous), this gate passes with a note —
    it only ever converts the specific window_edge review class."""

    name = "temporal_window_ambiguous"
    version = "temporal_window_ambiguous:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        mode = _policy_mode(ctx, "window_edge_ambiguous")
        evidence: dict = {"policy": "window_edge_ambiguous", "policy_setting": mode}
        temporal = None
        for gate_result in getattr(record, "gates", None) or []:
            if getattr(gate_result, "name", None) == SportsTemporalWindowGate.name:
                temporal = gate_result
                break
        if temporal is None:
            evidence["note"] = (
                "temporal_window gate result not present in the ledger; nothing to "
                "enforce (this gate mirrors that verdict, it never re-derives it)"
            )
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
        temporal_evidence = temporal.evidence if isinstance(temporal.evidence, dict) else {}
        temporal_reason = temporal_evidence.get("reason")
        evidence["temporal_window_verdict"] = temporal.verdict
        evidence["temporal_window_reason"] = temporal_reason
        is_window_edge = temporal.verdict == "review" and temporal_reason == "window_edge"
        if is_window_edge and mode == "exclude":
            evidence["reason"] = "window_edge"
            evidence["problem"] = (
                "temporal_window_ambiguous: the change date is coarse-precision at "
                "the cutoff/asof edge (temporal_window review reason 'window_edge') "
                "and cannot be placed relative to the cutoff; owner policy "
                "window_edge_ambiguous='exclude' drops it from the benchmark because "
                "pre-cutoff news contamination cannot be ruled out"
            )
            return GateResult(name=self.name, version=self.version, verdict="fail", evidence=evidence)
        return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)


class SportsCorroborationGate(Gate):
    """Sports-specific corroboration against the cached Wikidata P54
    statements (name 'corroboration' — it REPLACES the legacy LLM tier-3
    gate; version 'corroboration:sports_v1').

    Reads the full per-player statement list from ctx (the WD cache store)
    and applies the same squad-aware, sitelink-primary match rule as the
    change-date resolution (_statement_match via corroborate()), so the gate
    verdict and the recorded change-date basis can never disagree about what
    matched.

    Verdicts (agreement can pass; NOTHING here can fail — a second-source
    disagreement may be vandalism on either side, a timing lag, or a
    genuinely different event like a loan, and the infobox remains the
    declared ground truth):

    * >= 1 in-window statement matches -> 'pass' (ambiguity between several
      matching statements is recorded and adjudicated by the temporal gate
      via basis 'wd_p54_ambiguous').
    * otherwise 'review', with a SPECIFIC problem chosen in fixed priority
      order so a human adjudicating the queue is never misled:
        1. 'sitelink_mismatch'            — a label/alias compared equal but
           the squad-aware rule demoted it (sitelink disagrees or carries a
           women's/youth/reserve/era marker the infobox value lacks);
        2. 'club_present_out_of_window'   — the infobox club IS in the
           player's P54 statements, but every agreeing statement's P580 is
           outside the corroboration window: Wikidata agrees on the club yet
           cannot date the change (previously mis-reported as
           'sources_disagree');
        3. 'club_present_undated'         — as above but the agreeing
           statement has no usable P580 at all;
        4. comparator-undecided           — some in-window comparison was
           'review'/'incomparable';
        5. 'label_only_no_sitelink'       — every in-window statement lacks
           an enwiki sitelink and every label-only comparison was
           'different': the mismatch may be a romanization/styling artifact
           (Belarusian Łacinka labels vs Russian-romanized infobox values),
           not a real disagreement;
        6. 'sources_disagree'             — reserved for rows where NO
           statement at ANY date org-matches the infobox club;
        7. no data                        — player/cache/statements missing,
           or no statement dated inside the window (phrases contain
           'has no value for this entity' for report continuity).
      Malformed ctx, an absent entity id, or an unparsed after value are
      'review' with the specific reason. Per-statement outcomes are all
      recorded, so a review is self-contained."""

    name = "corroboration"
    version = "corroboration:sports_v1"

    SOURCE_LABEL = "wikidata_p54"

    def evaluate(self, record, ctx: dict) -> GateResult:
        evidence: dict = {"source_label": self.SOURCE_LABEL}

        def review(problem: str) -> GateResult:
            evidence["problem"] = problem
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)

        bounds = {}
        problems = []
        for key in ("cutoff", "asof"):
            if key not in ctx:
                problems.append(f"ctx is missing {key!r}")
                continue
            parsed, why = _coerce_date(ctx[key])
            if parsed is None:
                problems.append(f"ctx[{key!r}] {why}")
            else:
                bounds[key] = parsed
        if problems:
            return review("; ".join(problems))
        window_lo, window_hi = prev_month_start(bounds["cutoff"]), bounds["asof"]
        evidence["window"] = [window_lo.isoformat(), window_hi.isoformat()]

        entity = getattr(record, "entity", None)
        ids = entity.get("ids") if isinstance(entity, dict) else None
        if not isinstance(ids, dict) or ENTITY_ID_KEY not in ids:
            return review(f"entity has no {ENTITY_ID_KEY!r} id")
        qid = ids[ENTITY_ID_KEY]
        evidence["entity_id"] = qid if isinstance(qid, str) else repr(qid)

        store = ctx.get(WD_CACHE_CTX_KEY)
        if not isinstance(store, dict):
            return review(
                f"ctx[{WD_CACHE_CTX_KEY!r}] is missing or not a dict: the P54 cache was not loaded"
            )
        try:
            cache_row = store.get(qid)
        except TypeError:
            return review(f"entity id is not hashable: {type(qid).__name__}")
        evidence["player_in_cache"] = cache_row is not None
        if not isinstance(cache_row, dict):
            return review(
                f"{self.SOURCE_LABEL} has no value for this entity (player not in the cache)"
            )
        statements = cache_row.get("statements")
        statements = statements if isinstance(statements, list) else []
        evidence["statements_total"] = len(statements)

        after_canonical = getattr(getattr(record, "after", None), "canonical", None)
        if not isinstance(after_canonical, dict):
            return review("after.canonical is missing (raw value did not parse)")
        try:
            comparator = get_comparator(getattr(record, "value_type", None))
        except (LookupError, TypeError) as exc:
            return review(str(exc))
        evidence["comparator_version"] = comparator.VERSION

        after_raw = _raw_side(record, "after")
        corro = corroborate(after_raw, statements, window_lo, window_hi, comparator)
        evidence["statements_in_window"] = corro["statements_in_window"]
        candidates = []
        for summary in corro["in_window"]:
            entry = dict(summary)
            entry["value"] = (
                summary.get("team_enwiki_title") or summary.get("team_label_en") or ""
            )
            candidates.append(entry)
        evidence["candidates"] = candidates

        if corro["matched"]:
            first = corro["match"] or corro["matched"][0]
            evidence["matched_value"] = first.get("matched_name")
            evidence["matched_team_qid"] = first.get("team_qid")
            evidence["matched_via"] = first.get("matched_via")
            if corro["ambiguous"]:
                evidence["note"] = (
                    "several in-window statements match with differing teams/dates; "
                    "the temporal gate adjudicates via basis 'wd_p54_ambiguous'"
                )
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)

        # --- no in-window match: choose the most informative review reason ---
        if corro["demoted"]:
            evidence["demoted"] = corro["demoted"]
            return review(
                "sitelink_mismatch: the after value matches a Wikidata team "
                "label/alias, but the statement's enwiki sitelink title disagrees "
                "or is marked as a women's/youth/reserve/era sibling — a same-named "
                "sibling item must not corroborate the senior-club fact"
            )

        undated_agree, out_window_agree = self._full_history_agreement(
            after_canonical, after_raw, statements, window_lo, window_hi, comparator
        )
        if out_window_agree:
            evidence["club_out_of_window"] = out_window_agree
            return review(
                "club_present_out_of_window: the infobox club appears in this "
                "player's P54 statements, but only with a start date outside the "
                "corroboration window — Wikidata agrees on the club yet cannot "
                "date the change"
            )
        if undated_agree:
            evidence["club_undated"] = undated_agree
            return review(
                "club_present_undated: the infobox club appears in this player's "
                "P54 statements but with no usable start date — Wikidata agrees "
                "on the club yet cannot date the change"
            )

        outcomes = [s.get("outcome") for s in corro["in_window"]]
        if any(o == "review" for o in outcomes):
            return review(
                f"comparator could not decide agreement with the {self.SOURCE_LABEL}"
            )
        if outcomes and all(o == "no_name" for o in outcomes):
            return review(
                f"{self.SOURCE_LABEL} has no value for this entity (in-window "
                "statements carry no comparable team names)"
            )
        if corro["statements_in_window"] == 0:
            if not statements:
                return review(
                    f"{self.SOURCE_LABEL} has no value for this entity (no P54 "
                    "statements in the cache)"
                )
            return review(
                f"{self.SOURCE_LABEL} has no value for this entity inside the "
                "corroboration window (no in-window dated statement)"
            )
        if all(
            not s.get("team_enwiki_title") for s in corro["in_window"]
        ):
            return review(
                "label_only_no_sitelink: every in-window P54 team lacks an enwiki "
                "sitelink and its English label compares different — the mismatch "
                "may be a romanization/styling artifact rather than a real "
                "disagreement"
            )
        return review(
            f"sources_disagree: no {self.SOURCE_LABEL} value matches after.canonical"
        )

    @staticmethod
    def _full_history_agreement(
        after_canonical, after_raw, statements, window_lo, window_hi, comparator
    ):
        """(undated_matches, out_of_window_matches): squad-aware matches of
        the after value among statements NOT dated inside the window —
        the side channel that distinguishes 'Wikidata agrees but cannot date
        the change' from a genuine sources_disagree."""
        undated, out_of_window = [], []
        for statement in statements:
            if not isinstance(statement, dict):
                continue
            cd = wd_time_to_change_date(statement.get("p580"))
            if cd is not None:
                interval = change_date_interval(cd[0], cd[1])
                if interval is not None and not (
                    interval[1] < window_lo or interval[0] > window_hi
                ):
                    continue  # in-window: already adjudicated by corroborate()
            outcome, name, _detail = _statement_match(
                after_canonical, after_raw, statement, comparator
            )
            if outcome != "equal":
                continue
            summary = _statement_summary(statement, cd)
            summary["matched_name"] = name
            (out_of_window if cd is not None else undated).append(summary)
        return undated, out_of_window


def _raw_side(record, side: str):
    value = getattr(getattr(record, side, None), "raw", None)
    return value if isinstance(value, str) else None


def _extraction_ref(record, side: str):
    """The per-side wiki_infobox extraction summary written by build_record
    into <side>.evidence.ref['extraction'], or None."""
    ref = getattr(getattr(getattr(record, side, None), "evidence", None), "ref", None)
    ext = ref.get("extraction") if isinstance(ref, dict) else None
    return ext if isinstance(ext, dict) else None


def _side_as_of(record, side: str):
    as_of = getattr(getattr(getattr(record, side, None), "evidence", None), "as_of", None)
    return as_of if isinstance(as_of, str) and as_of else None


def _extraction_consistency(legacy_value: str, result: dict, comparator) -> dict:
    """Compare the PRIMARY legacy extraction with the wiki_infobox
    re-extraction of the same pinned side. Returns {'verdict', 'basis',
    'extracted_value'[, 'comparison_reason']} where verdict is:

    * 'consistent' — raw-identical, org-equal, or the legacy value is a
      free-agent string and the extractor found the 'unattached' state;
    * 'conflict'   — org-different clubs, or extractor 'unattached' vs a
      legacy club value;
    * 'undecided'  — the comparison could not be decided (comparator
      review/incomparable, a parse failure, or the extractor refused to
      read a value legacy claims to have seen).

    'conflict' and 'undecided' both route to review via the
    extraction_conflict gate — ambiguity costs a human look, never a
    silent pass."""
    status = result.get("status")
    if status == "club":
        club = result.get("club")
        if club == legacy_value:
            return {"verdict": "consistent", "basis": "exact_raw", "extracted_value": club}
        parsed_legacy = comparator.parse(legacy_value)
        parsed_club = comparator.parse(club) if isinstance(club, str) else None
        if parsed_club is None or not parsed_legacy.ok or not parsed_club.ok:
            reason = (
                parsed_legacy.failure_reason if not parsed_legacy.ok
                else parsed_club.failure_reason if parsed_club is not None
                else "extracted club is not a string"
            )
            return {
                "verdict": "undecided", "basis": "parse_failure",
                "extracted_value": club, "comparison_reason": reason,
            }
        comparison = comparator.compare(parsed_legacy.canonical, parsed_club.canonical)
        if not isinstance(comparison, Comparison):
            return {
                "verdict": "undecided", "basis": "comparator_error",
                "extracted_value": club,
                "comparison_reason": f"comparator {comparator.VERSION} returned a non-Comparison",
            }
        if comparison.verdict == "equal":
            return {"verdict": "consistent", "basis": "org_equal", "extracted_value": club}
        entry = {
            "verdict": "conflict" if comparison.verdict == "different" else "undecided",
            "basis": f"org_{comparison.verdict}",
            "extracted_value": club,
        }
        if comparison.reason:
            entry["comparison_reason"] = comparison.reason
        return entry
    if status == "unattached":
        if FREE_AGENT_RE.search(legacy_value):
            return {
                "verdict": "consistent", "basis": "unattached_marker_agrees",
                "extracted_value": None,
            }
        return {
            "verdict": "conflict", "basis": "extractor_unattached",
            "extracted_value": None, "comparison_reason": result.get("reason"),
        }
    return {
        "verdict": "undecided", "basis": "extractor_no_value",
        "extracted_value": None, "comparison_reason": result.get("reason"),
    }


def _side_extraction(wt_row, wt_info, side: str, legacy_value, comparator, expected_ts):
    """Run the wiki_infobox re-extraction for one record side and apply the
    precedence rules. Returns (extraction_ref, effective_raw_value):

    * legacy non-empty -> the legacy value stays the effective raw value
      (role 'consistency_check'; the comparison result rides in
      ['consistency'] for the extraction_conflict gate);
    * legacy empty, extractor reads a club -> the extracted club becomes
      the effective raw value (role 'primary', value_filled=True, with
      revid/sha1/ts/ts_source recorded so the value is evidence-anchored) —
      UNLESS the extraction carries the v2 loan flag ('[[X]]<br>(on loan
      from [[Y]])'): filling a loan destination as the senior club would
      fabricate a transfer, so the side stays empty with reason
      'team_field_loan_annotation' (role 'gap_unfilled'; the flagged
      extraction, destination and parent clubs stay in the ref for the
      sports_loan gate and the human queue);
    * legacy empty, extractor cannot SAFELY read a club (unattached state,
      redirect, absent page, unrecognized template, unreadable value, or a
      missing cache/title) -> effective value stays empty with the
      machine-readable reason (role 'gap_unfilled') — review is the
      correct downstream outcome for these.

    expected_ts is the candidate row's recorded revision timestamp for this
    side (cutoff_rev_ts / cur_rev_ts; None when the harvest recorded none).
    The cache row must have been pinned from exactly that expectation
    (ts_source 'row' with pinned_ts == expected_ts, or 'window_fallback'
    with no expected_ts); anything else means the cache was built from a
    DIFFERENT verified file and the side is unusable — reason
    'cache_pin_mismatch', never a value anchored to the wrong revision."""
    legacy_value = legacy_value if isinstance(legacy_value, str) else ""
    ext: dict = {
        "extractor": EXTRACTOR_VERSION,
        "side": side,
        "role": "not_run",
        "status": "not_run",
        "club": None,
        "template": None,
        "template_registered": False,
        "sport": None,
        "field": None,
        "via": None,
        "loan": False,
        "reason": None,
        "detail": None,
        "legacy_value": legacy_value,
        "value_filled": False,
        "revision": None,
        "consistency": None,
    }
    cache_present = isinstance(wt_info, dict) and bool(wt_info.get("file"))
    if not cache_present or not isinstance(wt_row, dict):
        ext["reason"] = "cache_missing" if not cache_present else "title_not_in_cache"
        if legacy_value:
            ext["role"] = "consistency_check"
            ext["consistency"] = {
                "verdict": "not_checked", "basis": ext["reason"], "extracted_value": None,
            }
        else:
            ext["role"] = "gap_unfilled"
        return ext, legacy_value

    side_obj = wt_row.get(side)
    if not isinstance(side_obj, dict):
        errors = [
            e for e in (wt_row.get("fetch_errors") or [])
            if isinstance(e, str) and e.startswith(side + ":")
        ]
        ext["status"] = "no_value"
        ext["reason"] = (
            "no_article_at_snapshot"
            if any("no revision at or before" in e for e in errors)
            else "snapshot_fetch_error"
        )
        ext["detail"] = {"fetch_errors": errors}
        if legacy_value:
            ext["role"] = "consistency_check"
            ext["consistency"] = {
                "verdict": "not_checked", "basis": "snapshot_unavailable",
                "extracted_value": None,
            }
        else:
            ext["role"] = "gap_unfilled"
        return ext, legacy_value

    # Cross-check the cache row's pin against the candidate's recorded
    # revision timestamp: a cache built from a different (or stale) verified
    # file must never silently anchor values or consistency checks to the
    # wrong revision.
    ts_source = side_obj.get("ts_source")
    pinned_ts = side_obj.get("pinned_ts")
    expected = expected_ts if isinstance(expected_ts, str) and expected_ts else None
    pin_ok = (
        (ts_source == "row" and pinned_ts == expected and expected is not None)
        or (ts_source == "window_fallback" and expected is None)
    )
    if not pin_ok:
        ext["reason"] = "cache_pin_mismatch"
        ext["detail"] = {
            "pinned_ts": pinned_ts,
            "ts_source": ts_source,
            "expected_ts": expected,
            "note": (
                "the cached revision was pinned from a timestamp that does not "
                "match this row's recorded revision timestamp: the wikitext "
                "cache was built from a different verified file — the side is "
                "unusable for gap-fill and consistency checks"
            ),
        }
        if legacy_value:
            ext["role"] = "consistency_check"
            ext["consistency"] = {
                "verdict": "not_checked", "basis": "cache_pin_mismatch",
                "extracted_value": None,
            }
        else:
            ext["role"] = "gap_unfilled"
        return ext, legacy_value

    result = extract_current_club(side_obj.get("content"))
    for key in ("status", "club", "template", "template_registered",
                "sport", "field", "via", "loan", "reason"):
        ext[key] = result.get(key)
    ext["detail"] = result.get("detail") or None
    ext["revision"] = {
        "revid": side_obj.get("revid"),
        "sha1": side_obj.get("sha1"),
        "ts": side_obj.get("ts"),
        "ts_source": ts_source,
        "pinned_ts": pinned_ts,
    }
    if legacy_value:
        ext["role"] = "consistency_check"
        ext["consistency"] = _extraction_consistency(legacy_value, result, comparator)
        return ext, legacy_value
    club = result.get("club")
    if result.get("status") == "club" and result.get("loan"):
        # Refuse to fill a loan destination as the senior club (the exact
        # fabrication the career-list guard exists to prevent): the side
        # stays empty and routes to review; the flagged extraction keeps the
        # destination/parent clubs visible for the human queue.
        ext["role"] = "gap_unfilled"
        ext["reason"] = "team_field_loan_annotation"
        return ext, ""
    if result.get("status") == "club" and isinstance(club, str) and club:
        ext["role"] = "primary"
        ext["value_filled"] = True
        return ext, club
    ext["role"] = "gap_unfilled"
    return ext, ""


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

class SportsAdapter(Adapter):
    """Adapter for source 'sports'. See the module docstring for the design.

    Stateless like the SEC reference adapter: joins and the WD cache are
    loaded once per run into cfg (the sanctioned runner channel), never onto
    the instance."""

    source = "sports"

    # -- enumeration --------------------------------------------------------

    def enumerate_candidates(self, cfg: dict):
        """Yield every row of {data_dir}/sports_verified.jsonl sorted by
        (title, line). Unparseable lines are yielded as error candidates so
        build_record raises a descriptive error into manifest build_errors —
        enumeration never decides inclusion."""
        self._require_offline(cfg)
        path = self._data_path(cfg, VERIFIED_FILENAME)
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
                row["_line"] = line_no
                candidates.append(row)
        candidates.sort(key=lambda c: (str(c.get("title") or ""), c["_line"]))
        yield from candidates

    # -- record building ----------------------------------------------------

    def build_record(self, candidate: dict, cfg: dict) -> FactChangeRecord:
        if "_parse_error" in candidate:
            raise ValueError(
                f"{VERIFIED_FILENAME} line {candidate.get('_line')}: unparseable JSON "
                f"({candidate['_parse_error']})"
            )
        title = candidate.get("title")
        if not isinstance(title, str) or not title:
            raise ValueError(
                f"{VERIFIED_FILENAME} line {candidate.get('_line')}: missing title"
            )
        self._ensure_loaded(cfg)

        old_club = candidate.get("old_club") if isinstance(candidate.get("old_club"), str) else ""
        new_club = candidate.get("new_club") if isinstance(candidate.get("new_club"), str) else ""
        cutoff_rev_ts = candidate.get("cutoff_rev_ts") or None
        cur_rev_ts = candidate.get("cur_rev_ts") or None
        gt_field = candidate.get("gt_field") or None
        wd_date = candidate.get("wd_date") if isinstance(candidate.get("wd_date"), str) else ""

        cand_row = cfg[CANDIDATES_CTX_KEY].get(title)
        qid = None
        if isinstance(cand_row, dict) and isinstance(cand_row.get("qid"), str) and cand_row["qid"]:
            qid = cand_row["qid"]

        cache_row = cfg[WD_CACHE_CTX_KEY].get(qid) if qid else None
        statements = (
            cache_row.get("statements") if isinstance(cache_row, dict) else None
        )
        statements = statements if isinstance(statements, list) else []

        comparator = get_comparator(VALUE_TYPE)

        # Wikitext-cache re-extraction with strict precedence (see the
        # module docstring): legacy values stay primary; empty sides are
        # filled from the pinned revision when a club is SAFELY readable.
        wt_store = cfg.get(WIKITEXT_CACHE_CTX_KEY) or {}
        wt_info = cfg.get(WIKITEXT_CACHE_INFO_CTX_KEY) or {"file": None, "sha1": None, "meta": None}
        wt_row = wt_store.get(title)
        before_ext, before_raw = _side_extraction(
            wt_row, wt_info, "cutoff", old_club, comparator, cutoff_rev_ts
        )
        after_ext, after_raw = _side_extraction(
            wt_row, wt_info, "current", new_club, comparator, cur_rev_ts
        )

        window_lo = prev_month_start(cfg["cutoff"])
        window_hi = cfg["asof"]
        corro = corroborate(after_raw, statements, window_lo, window_hi, comparator)

        page_url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(
            title.replace(" ", "_")
        )
        wd_url = f"https://www.wikidata.org/wiki/{qid}" if qid else None
        change_date = self._resolve_change_date(
            corro, wd_date, qid, wd_url, page_url, cutoff_rev_ts, cur_rev_ts
        )

        def infobox_evidence(rev_ts, ext, extra_ref=None) -> Evidence:
            ref = {
                "page_title": title,
                "gt_field": gt_field,
                "revision_timestamp": rev_ts,
                "extraction": ext,
            }
            has_revid = isinstance(ext, dict) and isinstance(ext.get("revision"), dict)
            if not has_revid:
                ref["revision_note"] = (
                    "revision id not recorded by the legacy harvest; recoverable "
                    "deterministically via the API from title+timestamp"
                )
            if extra_ref:
                ref.update(extra_ref)
            as_of = rev_ts
            if as_of is None and has_revid:
                as_of = ext["revision"].get("ts")
            return Evidence(kind="wikipedia_infobox", url=page_url, ref=ref, as_of=as_of)

        after_wd_ref = {
            "wikidata_p54": {
                "player_qid": qid,
                "player_in_cache": cache_row is not None,
                "statements_total": corro["statements_total"],
                "statements_in_window": corro["statements_in_window"],
                "match": corro["match"],
                "ambiguous": corro["ambiguous"],
                "demoted": corro["demoted"],
            }
        }

        # sports_curated.jsonl is OPTIONAL provenance-only regression data (no
        # gate reads it — cache_hit is false when absent, e.g. for a harvest
        # snapshot). The tier-2 re-fetch primitive now_club is deliberately NOT
        # copied: it fed the removed stability gate and is no longer consulted.
        legacy_row = cfg[CURATED_CTX_KEY].get(title)
        legacy = {"cache_hit": legacy_row is not None}
        if isinstance(legacy_row, dict):
            for key in (
                "tier1_ok", "tier1_reason", "tier2_ok", "tier2_state", "tier2_reason",
                "keep_free", "keep",
                "web_verified", "web_actual_club", "web_source", "web_why", "web_searched",
            ):
                legacy[key] = legacy_row.get(key)

        cache_info = cfg.get(WD_CACHE_INFO_CTX_KEY) or {"file": None, "sha1": None}
        provenance = {
            "predictability": PREDICTABILITY,
            "line": candidate.get("_line"),
            "page": {
                "title": title,
                "sport": candidate.get("sport"),
                "gt_field": gt_field,
                "changed_harvest_flag": candidate.get("changed"),
                "cutoff_rev_ts": cutoff_rev_ts,
                "cur_rev_ts": cur_rev_ts,
                "views": candidate.get("views"),
                "team_new_wd": candidate.get("team_new_wd"),
                "wd_date": wd_date,
            },
            "finder": {
                "qid": qid,
                "sport_hint": cand_row.get("sport_hint") if isinstance(cand_row, dict) else None,
                "wd_date": cand_row.get("date") if isinstance(cand_row, dict) else None,
            },
            "legacy": legacy,
            "wd_cache": {
                "file": cache_info.get("file"),
                "sha1": cache_info.get("sha1"),
                "player_in_cache": cache_row is not None,
                "statements": len(statements),
            },
            "wikitext_cache": {
                "file": wt_info.get("file"),
                "sha1": wt_info.get("sha1"),
                "row_present": isinstance(wt_row, dict),
            },
        }

        entity_ids = {ENTITY_ID_KEY: qid} if qid else {}
        fact_id = compute_fact_id(self.source, title, PROPERTY, change_date.value)
        return FactChangeRecord(
            fact_id=fact_id,
            record_id=compute_record_id(
                fact_id, f"{cutoff_rev_ts or ''}|{cur_rev_ts or ''}"
            ),
            source=self.source,
            entity={"name": title, "ids": entity_ids},
            property=PROPERTY,
            value_type=VALUE_TYPE,
            before=ValueState(
                raw=before_raw,
                canonical=None,
                evidence=infobox_evidence(cutoff_rev_ts, before_ext),
            ),
            after=ValueState(
                raw=after_raw,
                canonical=None,
                evidence=infobox_evidence(cur_rev_ts, after_ext, after_wd_ref),
            ),
            change_date=change_date,
            provenance=provenance,
        )

    # -- gates --------------------------------------------------------------

    def gate_list(self, cfg: dict):
        """Ordered gates (rationale in the module docstring). Loads the join
        tables, the WD P54 cache, and the corroboration lookup into cfg (the
        runner copies cfg into ctx — the sanctioned channel)."""
        self._require_offline(cfg)
        self._ensure_loaded(cfg)
        cfg[GARBAGE_RULES_CTX_KEY] = {}
        # Resolve and record the active owner policy (see DEFAULT_POLICY): the
        # runner copies cfg into ctx (the gates read it) AND fingerprints it in
        # the manifest, so every release states which rule produced it.
        cfg[POLICY_ACTIVE_CTX_KEY] = resolve_policy(cfg)
        # The two policy gates (sports_loan, temporal_window_ambiguous) run
        # AFTER value_changed so a PROVABLE non-change (value_changed FAIL,
        # first in ledger order) always names the disposition first.
        return [
            SportsSchemaGate(),
            SportsExtractionConflictGate(),
            SportsFreeAgentGate(),
            SportsReserveTeamGate(),
            SportsFragmentGate(),
            # (SportsStabilityGate removed 22 Jul 2026 — corroboration is the
            # vandalism guard; see the module docstring.)
            SportsTemporalWindowGate(),
            ValueParsedGate(),
            GarbageValueGate(rules_ctx_key=GARBAGE_RULES_CTX_KEY),
            SportsValueChangedGate(),
            SportsLoanGate(),
            SportsWindowEdgePolicyGate(),
            SportsCorroborationGate(),
            EvidenceResolvableGate(),
            DedupGate(),
        ]

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _require_offline(cfg: dict) -> None:
        if not cfg.get("offline", True):
            raise NotImplementedError(
                "the sports adapter is offline-only: the Wikidata P54 cache is "
                "built once by stage1.tools.fetch_wikidata_p54"
            )

    @staticmethod
    def _data_path(cfg: dict, filename: str) -> Path:
        data_dir = cfg.get("data_dir")
        if data_dir is None:
            raise LookupError(
                f"the sports adapter requires --data-dir (a directory containing {filename})"
            )
        path = Path(data_dir) / filename
        if not path.is_file():
            raise LookupError(f"sports input file not found: {path}")
        return path

    def _ensure_loaded(self, cfg: dict) -> None:
        """Load join tables, WD cache, and the corroboration lookup into cfg
        exactly once per run. Malformed lines become input_load_errors (the
        manifest channel), never silent skips."""
        if CANDIDATES_CTX_KEY not in cfg:
            table, errors = self._load_keyed_jsonl(
                cfg.get("data_dir"), CANDIDATES_FILENAME, key="title"
            )
            cfg[CANDIDATES_CTX_KEY] = table
            if errors:
                cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(errors)
        if CURATED_CTX_KEY not in cfg:
            table, errors = self._load_keyed_jsonl(
                cfg.get("data_dir"), CURATED_FILENAME, key="title"
            )
            cfg[CURATED_CTX_KEY] = table
            if errors:
                cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(errors)
        if WD_CACHE_CTX_KEY not in cfg:
            store, info, errors = self._load_wd_cache(cfg.get("data_dir"))
            cfg[WD_CACHE_CTX_KEY] = store
            cfg[WD_CACHE_INFO_CTX_KEY] = info
            if info.get("file") and info.get("sha1"):
                # Declare the cache to the runner's manifest fingerprint: it
                # may live outside data_dir (stage1/cache/), where the
                # manifest's data-dir walk would never see it. The key is the
                # LOCATION-INDEPENDENT identifier (never an absolute path).
                cfg.setdefault(EXTRA_INPUTS_CTX_KEY, {})[info["file"]] = info["sha1"]
                # Bind the cache version to its retrieval metadata in the
                # manifest: the .meta.json sidecar is overwritten in place by
                # any refetch, so the release itself must carry when/how this
                # cache version was retrieved for the audit chain to survive.
                meta_entry = {"sha1": info["sha1"], "retrieval": info.get("meta")}
                meta = info.get("meta")
                if isinstance(meta, dict) and "cache_sha1" in meta:
                    meta_entry["sidecar_matches_cache"] = meta.get("cache_sha1") == info["sha1"]
                cfg.setdefault(EXTRA_INPUT_META_CTX_KEY, {})[info["file"]] = meta_entry
            if errors:
                cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(errors)
        if WIKITEXT_CACHE_CTX_KEY not in cfg:
            store, info, errors = self._load_wikitext_cache(cfg.get("data_dir"))
            cfg[WIKITEXT_CACHE_CTX_KEY] = store
            cfg[WIKITEXT_CACHE_INFO_CTX_KEY] = info
            if info.get("file") and info.get("sha1"):
                # Same manifest treatment as the P54 cache: declare the
                # (location-independent id, sha1) pair and the sidecar's
                # retrieval metadata so the release fingerprints every byte
                # the re-extraction depends on.
                cfg.setdefault(EXTRA_INPUTS_CTX_KEY, {})[info["file"]] = info["sha1"]
                meta_entry = {"sha1": info["sha1"], "retrieval": info.get("meta")}
                meta = info.get("meta")
                if isinstance(meta, dict) and "cache_sha1" in meta:
                    meta_entry["sidecar_matches_cache"] = meta.get("cache_sha1") == info["sha1"]
                cfg.setdefault(EXTRA_INPUT_META_CTX_KEY, {})[info["file"]] = meta_entry
            if errors:
                cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(errors)

    @staticmethod
    def _load_keyed_jsonl(data_dir, filename: str, key: str):
        """{row[key]: row} from a jsonl file. Missing file -> empty table
        (downstream gates then review). Unreadable/duplicate lines are error
        entries. First row wins per key."""
        table: dict = {}
        errors: list = []
        if data_dir is None:
            return table, errors
        path = Path(data_dir) / filename
        if not path.is_file():
            return table, errors
        with open(path, encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    errors.append({"file": filename, "line": line_no, "error": str(exc)})
                    continue
                if not isinstance(row, dict) or not isinstance(row.get(key), str) or not row.get(key):
                    errors.append(
                        {
                            "file": filename,
                            "line": line_no,
                            "error": f"row is not an object with a string {key!r}",
                        }
                    )
                    continue
                if row[key] in table:
                    errors.append(
                        {
                            "file": filename,
                            "line": line_no,
                            "error": f"duplicate {key} {row[key]!r} (first occurrence kept)",
                        }
                    )
                    continue
                table[row[key]] = row
        return table, errors

    @classmethod
    def _resolve_wd_cache_path(cls, data_dir):
        """(path, identifier): {data_dir}/sports_wd_p54.jsonl when present (a
        release may vendor the cache next to its inputs), else the package
        default under stage1/cache/. (None, None) when neither exists — the
        pipeline then reviews every corroboration instead of crashing.

        The identifier is LOCATION-INDEPENDENT — the bare filename for a
        vendored cache (matching the manifest data-dir walk's relative key)
        or the repo-relative PACKAGE_WD_CACHE_ID for the package default —
        because it is recorded in every record's provenance and in the
        manifest's input_files: an absolute path there made facts.jsonl
        byte-DIFFERENT across checkouts, defeating hash-based release
        verification. The absolute path is used only for reading."""
        if data_dir is not None:
            local = Path(data_dir) / WD_CACHE_FILENAME
            if local.is_file():
                return local, WD_CACHE_FILENAME
        default = DEFAULT_WD_CACHE_DIR / WD_CACHE_FILENAME
        if default.is_file():
            return default, PACKAGE_WD_CACHE_ID
        return None, None

    # Sidecar keys surfaced into the manifest's extra_input_meta, PER KIND of
    # cache: only keys actually present in the sidecar are copied, so the P54
    # sidecar surfaces its candidates_file/candidates_sha1 input binding and
    # the wikitext sidecar surfaces verified_file/verified_sha1 (the hash of
    # the verified file whose timestamps pinned every revision) plus its
    # fallback pins — never a meaningless null field from the other kind.
    # cache_file is deliberately NOT surfaced (historically a machine-absolute
    # path; the manifest already keys the cache by its location-independent
    # identifier).
    _SIDECAR_META_KEYS = (
        "tool_version",
        "retrieved_at",
        "endpoint",
        "candidates_file",
        "candidates_sha1",
        "verified_file",
        "verified_sha1",
        "fallback_pins",
        "cache_sha1",
    )

    @classmethod
    def _load_wd_cache_meta(cls, path: Path):
        """Retrieval metadata from the cache's .meta.json sidecar (written by
        stage1.tools.fetch_wikidata_p54 / fetch_wiki_revisions), or None when
        absent. Copies the _SIDECAR_META_KEYS whitelist, keeping only keys
        the sidecar actually carries (the two cache kinds bind to different
        input files). Malformed sidecars return an error entry instead of
        raising — the sidecar is audit metadata, never a load-bearing
        input."""
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
        for key in cls._SIDECAR_META_KEYS:
            if key in meta:
                entry[key] = meta[key]
        return entry

    @classmethod
    def _load_wd_cache(cls, data_dir):
        """(store, info, errors): store is {player_qid: cache row}; info
        records the location-independent identifier, the sha1, and the
        sidecar retrieval metadata for provenance (the cache may live
        outside data_dir, so the manifest's input hashing would not
        otherwise cover it). A duplicate player_qid keeps the FIRST
        occurrence and records an input_load_errors entry — never a silent
        skip (mirroring _load_keyed_jsonl)."""
        store: dict = {}
        errors: list = []
        path, ident = cls._resolve_wd_cache_path(data_dir)
        if path is None:
            return store, {"file": None, "sha1": None, "meta": None}, errors
        digest = hashlib.sha1()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                digest.update(chunk)
        with open(path, encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    errors.append({"file": WD_CACHE_FILENAME, "line": line_no, "error": str(exc)})
                    continue
                if not isinstance(row, dict) or not isinstance(row.get("player_qid"), str):
                    errors.append(
                        {
                            "file": WD_CACHE_FILENAME,
                            "line": line_no,
                            "error": "row is not an object with a string player_qid",
                        }
                    )
                    continue
                if row["player_qid"] in store:
                    errors.append(
                        {
                            "file": WD_CACHE_FILENAME,
                            "line": line_no,
                            "error": (
                                f"duplicate player_qid {row['player_qid']!r} "
                                "(first occurrence kept)"
                            ),
                        }
                    )
                    continue
                store[row["player_qid"]] = row
        info = {
            "file": ident,
            "sha1": digest.hexdigest(),
            "meta": cls._load_wd_cache_meta(path),
        }
        return store, info, errors

    @classmethod
    def _resolve_wikitext_cache_path(cls, data_dir):
        """(path, identifier) for the pinned wikitext cache, mirroring
        _resolve_wd_cache_path: {data_dir}/sports_wikitext.jsonl when a
        release vendors it, else the package default under stage1/cache/;
        (None, None) when neither exists — every empty side then stays in
        review with reason cache_missing and every consistency check is
        'not_checked', never a crash. The identifier is LOCATION-
        INDEPENDENT (see _resolve_wd_cache_path for why)."""
        if data_dir is not None:
            local = Path(data_dir) / WIKITEXT_CACHE_FILENAME
            if local.is_file():
                return local, WIKITEXT_CACHE_FILENAME
        default = DEFAULT_WD_CACHE_DIR / WIKITEXT_CACHE_FILENAME
        if default.is_file():
            return default, PACKAGE_WIKITEXT_CACHE_ID
        return None, None

    @classmethod
    def _load_wikitext_cache(cls, data_dir):
        """(store, info, errors): store is {title: cache row} from the
        pinned wikitext cache written by stage1.tools.fetch_wiki_revisions;
        info records the location-independent identifier, sha1, and sidecar
        retrieval metadata. Duplicate titles keep the FIRST occurrence with
        an input_load_errors entry — never a silent skip."""
        store: dict = {}
        errors: list = []
        path, ident = cls._resolve_wikitext_cache_path(data_dir)
        if path is None:
            return store, {"file": None, "sha1": None, "meta": None}, errors
        digest = hashlib.sha1()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                digest.update(chunk)
        with open(path, encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    errors.append(
                        {"file": WIKITEXT_CACHE_FILENAME, "line": line_no, "error": str(exc)}
                    )
                    continue
                if not isinstance(row, dict) or not isinstance(row.get("title"), str) or not row["title"]:
                    errors.append(
                        {
                            "file": WIKITEXT_CACHE_FILENAME,
                            "line": line_no,
                            "error": "row is not an object with a string title",
                        }
                    )
                    continue
                if row["title"] in store:
                    errors.append(
                        {
                            "file": WIKITEXT_CACHE_FILENAME,
                            "line": line_no,
                            "error": (
                                f"duplicate title {row['title']!r} (first occurrence kept)"
                            ),
                        }
                    )
                    continue
                store[row["title"]] = row
        info = {
            "file": ident,
            "sha1": digest.hexdigest(),
            "meta": cls._load_wd_cache_meta(path),
        }
        return store, info, errors

    @staticmethod
    def _resolve_change_date(
        corro: dict, wd_date: str, qid, wd_url, page_url, cutoff_rev_ts, cur_rev_ts
    ) -> ChangeDate:
        """Implement the change-date policy from the module docstring."""
        if corro["date"] is not None and (corro["match"] is not None or corro["ambiguous"]):
            value, precision, wd_precision = corro["date"]
            basis = "wd_p54_ambiguous" if corro["ambiguous"] else "wd_p54_corroborated"
            ref = {
                "basis": basis,
                "player_qid": qid,
                "wd_precision": wd_precision,
                "matched_statements": corro["matched"],
            }
            if corro["match"] is not None:
                ref["statement"] = corro["match"]
            return ChangeDate(
                value=value,
                precision=precision,
                evidence=Evidence(kind="wikidata_p54", url=wd_url, ref=ref, as_of=None),
            )
        if isinstance(wd_date, str) and _ISO_DATE_RE.match(wd_date):
            try:
                parsed = _date.fromisoformat(wd_date)
            except ValueError:
                parsed = None
            if parsed is not None:
                return ChangeDate(
                    value=parsed.replace(day=1).isoformat(),
                    precision="month",
                    evidence=Evidence(
                        kind="wikidata_sparql_harvest",
                        url=wd_url,
                        ref={
                            "basis": "wd_date_uncorroborated",
                            "wd_date_raw": wd_date,
                            "note": (
                                "legacy SPARQL harvest truncated the P580 time and "
                                "discarded its precision; day component untrusted, "
                                "pinned to month start with explicit month precision"
                            ),
                        },
                        as_of=None,
                    ),
                )
        # Defensive last resort: no usable date evidence at all. The change is
        # provably inside (cutoff_rev_ts, cur_rev_ts]; the temporal gate sends
        # basis 'revision_bracket' to review, so this value is never trusted.
        bracket_value = "0001-01-01"
        precision = "year"
        if isinstance(cur_rev_ts, str) and _ISO_DATE_RE.match(cur_rev_ts[:10]):
            try:
                bracket_value = _date.fromisoformat(cur_rev_ts[:10]).replace(day=1).isoformat()
                precision = "month"
            except ValueError:
                pass
        return ChangeDate(
            value=bracket_value,
            precision=precision,
            evidence=Evidence(
                kind="wikipedia_revision_bracket",
                url=page_url,
                ref={
                    "basis": "revision_bracket",
                    "cutoff_rev_ts": cutoff_rev_ts,
                    "cur_rev_ts": cur_rev_ts,
                    "note": (
                        "no corroborated P54 statement and no parseable wd_date; the "
                        "change is provably inside (cutoff_rev_ts, cur_rev_ts] — the "
                        "recorded value is a placeholder the temporal gate refuses"
                    ),
                },
                as_of=None,
            ),
        )


ADAPTER = SportsAdapter()
