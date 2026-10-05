"""Wikipedia deaths adapter (source 'wiki_people') — the DEATH-CANDIDATE redesign.

REDESIGN (owner-approved, 2026-07-22). The people benchmark is DEATHS ONLY
(owner decision A: a death is the one fact class both genuinely unpredictable
at the training cutoff AND deterministically identifiable). The legacy pipeline
reached deaths through a wasteful funnel — diff EVERY infobox field across
~29,570 people/org pages, then scope-gate away everything that was not a death
(19 survivors). This adapter replaces that funnel with a targeted read of the
DEATH-CANDIDATE SNAPSHOT written by ``stage1/harvest/wiki_people.py``
(``harvest_people:v1``): Wikidata P570 discovery finds who died in-window; the
harvester pins each person's Wikipedia revisions at the cutoff and asof and
freezes them; this adapter derives, offline and byte-deterministically, one
record per (person, death-property).

PRIMARY-SOURCE INVARIANT (mirrors sports): the recorded before/after VALUE is
the WIKIPEDIA INFOBOX reading (``evidence.kind='wikipedia_infobox'``),
re-extracted from the pinned revisions in ``people_wikitext.jsonl`` via the
shared, versioned ``stage1.adapters.people_death`` extractor — never a Wikidata
value. That is what makes "according to Wikipedia, is X alive / when did X
die" defensible. Wikidata (P570/P20/P509) CORROBORATES and dates; it is never
the recorded answer.

Inputs (the EXACT fixed snapshot filenames, all under ``--data-dir``):

* ``people_death_verified.jsonl`` — enumeration: one row per in-window death
  candidate with the harvest-time infobox readings, pinned revision ids/
  timestamps, field-presence flags, and prose length. Per row this adapter
  emits up to THREE FactChangeRecords: property ``deathdate`` (headline;
  always), ``deathplace`` and ``deathcause`` (secondary; only when the pinned
  infobox carries them on either side — structural presence, frozen in the
  row's ``fields_present``).
* ``people_wikitext.jsonl`` (+ ``.meta.json``) — the FULL wikitext of the two
  pinned revisions per title; the byte-anchor for this adapter's versioned
  re-extraction AND for the deterministic body-prose measure.
* ``people_wd_p570.jsonl`` (+ ``.meta.json``) — Wikidata cutoff/current claim
  state for the DEATH properties only (P570 date, P20 place, P509 cause, P119
  resting place; the cache_format contract, keyed by title). Corroboration +
  vandalism guard: a fake death vandalised onto Wikipedia will not match P570
  and rests in review, never included.
* ``people_death_candidates.jsonl`` — discovery provenance (P570 date/
  precision from the finder, sitelink count).
* ``people_pageviews.jsonl`` — the PRE-DEATH baseline-month pageview per
  title (fetched once by the harvester so the recognisability floor is
  evaluated offline).

A death is a SINGLE-SIDED change: no death date at the cutoff, a death date
now (``''`` -> value). The ``death_date`` / ``death_place`` / ``death_cause``
value_types give the empty side a first-class ABSENT canonical, and the
``death_change`` gate applies the manifest-recorded ``single_sided_death``
policy (default ``admit``): an addition is a valid post-cutoff change; a
removal is vandalism/error and is held for review.

Gate order (first FAIL names the disposition):

  1. ``no_infobox_death_date`` — the primary value IS the infobox death date,
     so a pinned current revision that provably displays none (no infobox, or
     no death-date field) FAILS the headline record ->
     ``excluded:no_infobox_death_date`` (the ~1-in-5 coverage limit, named and
     auditable). A present-but-unreadable date, or a missing wikitext side, is
     review — refuse-to-guess. Secondary properties pass on honest statuses
     (their presence is structural at enumeration) but an after-side
     ``unreadable``/cache-gap secondary routes to review too (people_v2), so a
     displayed-but-unparseable value never mislabels as
     ``excluded:value_changed``.
  2. ``value_parsed`` / 3. ``garbage_value`` — shared; the absent-able
     comparators make a single-sided addition compare 'different' and a
     both-absent pair 'equal'.
  4. ``value_changed`` (value_changed:people_v1) — the shared comparator gate
     wrapped with the honest-exclusion rule: an ARTIFACT-empty side (an
     ``unreadable`` displayed value, a missing cache row, an unexplained
     missing pinned revision) is reviewed, never compared — only honest
     absents (page created post-cutoff, field genuinely absent/blank) reach
     the comparator, so ``excluded:value_changed`` always means 'the infobox
     provably did not change'.
  5. ``death_change`` — the single-sided POLICY gate (admit addition / review
     removal / two-sided defers to the comparator).
  6. ``temporal_window`` (people_v1) — the change date is the INFOBOX death
     date with its own precision: in-window passes, a provably pre-cutoff
     death (a backfilled old death) FAILS ``pre_cutoff``, a coarse date
     straddling an edge follows the ``window_edge_ambiguous`` policy
     (default ``exclude``), a dateless record (revision bracket) is review.
  7. ``corroboration`` (people_v3, death-scoped, RANK-AWARE) — the VANDALISM
     GUARD: the infobox death date must AGREE with a NON-DEPRECATED current
     P570 claim (ANY-match over the rank-ordered claims; a deprecated claim —
     Wikidata's this-value-is-wrong marker — can neither corroborate nor
     veto). Disagreement with every credible claim, or no credible P570 ->
     review, never included. For deathplace/deathcause the mapped P20/P509
     claim is additionally compared when present (deprecated statements
     skipped; disagreement -> review); an ABSENT secondary claim does not
     review — the death itself is P570-corroborated, and Wikidata is
     routinely sparse on circumstances.
  8. ``stub_no_body`` (real_article_body:people_v1) — REQUIRED body-prose
     floor for the downstream poisoned-paragraph bio study: the pinned
     current revision's body prose (people_prose:v2, recomputed offline from
     the wikitext cache) must reach ``body_prose_min_chars`` ->
     ``excluded:stub_no_body`` otherwise; an unavailable revision is review.
  9. ``below_recognisability_floor`` (recognisability:people_v1) — MODEST
     noise floor (owner: abstention is a VALID outcome, so the floor drops
     stubs/noise, not non-celebrities): pass when pre-death baseline-month
     views >= ``pre_death_monthly_views_min`` OR sitelinks >=
     ``sitelinks_min``; both known-below -> ``excluded:
     below_recognisability_floor``; an unknown signal degrades to review
     (``recognisability_unknown``), never a silent pass/fail.
 10. ``evidence_resolvable`` / 11. ``dedup`` — shared; dedup key
     (entity.name, property, change_date).

POLICY + THRESHOLDS (manifest-recorded via cfg['policy']; all tunable through
``cfg['people_policy_override']``): ``window_edge_ambiguous='exclude'``,
``single_sided_death='admit'``, ``body_prose_min_chars=600`` (just above the
observed p25=508 of real 2026 deaths; drops one-to-two-sentence stubs),
``pre_death_monthly_views_min=50`` and ``sitelinks_min=3`` combined as a
lenient OR (data-grounded from the observed distributions: views p25=60,
sitelinks p50=4; real bios exist with 1 sitelink or 0 pre-death views, so
neither is a hard solo gate).

PREDICTABILITY (owner decision B): every record of this source is a death
event -> the constant tag ``unpredictable`` (the per-field-class map of the
legacy funnel collapses, like SEC's single-valued tag).

RETIRED with the funnel: the changes.csv/changes_tail.csv enumeration, the
gold xlsx join (the LLM-baseline regression provenance), the
``out_of_scope_unpredictable`` scope gate and the MONEY/COUNT/LEAD/POS/PLACE/
STAT field taxonomy, the NumberedSlotGate, and the quantity/currency recovery
path (only the date/place/cause cleaners are exercised).

Offline-only: ``--online`` raises. Every input is a frozen snapshot artifact.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from datetime import date as _date, timedelta
from pathlib import Path

import stage1.normalize.date  # noqa: F401  (registers the date comparator)
import stage1.normalize.death_date  # noqa: F401  (registers death_date)
import stage1.normalize.death_event  # noqa: F401  (registers death_place/death_cause)
import stage1.normalize.org  # noqa: F401  (org: the death_place delegate)
import stage1.normalize.text_span  # noqa: F401  (text_span: the death_cause delegate)
from stage1.adapters import Adapter
from stage1.adapters.people_death import (
    EXTRACTOR_VERSION,
    PROSE_VERSION,
    clean_death_value,
    extract_death_fields,
    prose_chars,
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
from stage1.predictability import UNPREDICTABLE, check_predictability
from stage1.schema import (
    ChangeDate,
    Evidence,
    FactChangeRecord,
    GateResult,
    ValueState,
    compute_fact_id,
    compute_record_id,
)

SOURCE = "wiki_people"

# --------------------------------------------------------------------------
# Snapshot filenames (the EXACT names the harvester writes) / ctx keys
# --------------------------------------------------------------------------

VERIFIED_FILENAME = "people_death_verified.jsonl"
CANDIDATES_FILENAME = "people_death_candidates.jsonl"
WIKITEXT_CACHE_FILENAME = "people_wikitext.jsonl"
WD_CACHE_FILENAME = "people_wd_p570.jsonl"
PAGEVIEWS_FILENAME = "people_pageviews.jsonl"

CANDIDATES_CTX_KEY = "people_candidates_by_title"
WD_CACHE_CTX_KEY = "people_wd_cache"
WD_CACHE_INFO_CTX_KEY = "people_wd_cache_info"
WIKITEXT_CACHE_CTX_KEY = "people_wikitext_cache"
WIKITEXT_CACHE_INFO_CTX_KEY = "people_wikitext_cache_info"
PAGEVIEWS_CTX_KEY = "people_pageviews_by_title"
GARBAGE_RULES_CTX_KEY = "garbage_rules"
POLICY_CTX_KEY = "policy"  # the runner's manifest channel (adapter_policy)
LOAD_ERRORS_CTX_KEY = "input_load_errors"
EXTRA_INPUTS_CTX_KEY = "extra_input_files"
EXTRA_INPUT_META_CTX_KEY = "extra_input_meta"

# Location-independent identifiers for package-default caches (a release may
# vendor the caches next to its inputs — a harvest snapshot always does).
PACKAGE_WD_CACHE_ID = f"stage1/cache/{WD_CACHE_FILENAME}"
PACKAGE_WIKITEXT_CACHE_ID = f"stage1/cache/{WIKITEXT_CACHE_FILENAME}"
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"

# --------------------------------------------------------------------------
# The death-property taxonomy (the WHOLE taxonomy of the redesigned source)
# --------------------------------------------------------------------------


class DeathPropertySpec:
    __slots__ = ("value_type", "wd_property")

    def __init__(self, value_type, wd_property):
        self.value_type = value_type
        self.wd_property = wd_property


DEATH_PROPERTIES = {
    # The headline fact: the infobox date of death, corroborated by P570.
    "deathdate": DeathPropertySpec("death_date", "P570"),
    # Secondary circumstances, emitted only when the infobox carries them.
    "deathplace": DeathPropertySpec("death_place", "P20"),
    "deathcause": DeathPropertySpec("death_cause", "P509"),
}
PROPERTY_ORDER = ("deathdate", "deathplace", "deathcause")

# The death-scoped Wikidata property whitelist the snapshot cache must cover
# (P119 resting place is fetched for provenance/audit though no record maps it).
DEATH_PROPERTY_WHITELIST = ("P119", "P20", "P509", "P570")

# --------------------------------------------------------------------------
# POLICY + TUNABLE THRESHOLDS (manifest-recorded; every knob in one dict)
# --------------------------------------------------------------------------
#
# window_edge_ambiguous: what the temporal gate does with a coarse (month/
#   year) change date straddling the cutoff/asof edge — 'exclude' (owner
#   default, stricter than sports' review) or 'review'.
# single_sided_death: what the death_change gate does with a single-sided
#   ADDITION ('' -> value) — 'admit' (owner default) or 'review'. A REMOVAL is
#   always review regardless.
# body_prose_min_chars: the REQUIRED real-article-body floor (people_prose:v2
#   chars). Default 600 ~ 100 words: just above the observed p25 (508) on real
#   2026 deaths; drops the one-to-two-sentence stubs (<=~350) while keeping
#   the bulk of genuine bios (observed pass rate ~70%).
# pre_death_monthly_views_min / sitelinks_min: the MODEST recognisability
#   floor, combined as a lenient OR (views>=50 OR sitelinks>=3). Observed
#   distributions (n=120 real deaths): views p10/p25/p50 = 0/60/162, sitelinks
#   p10/p25/p50 = 1/2/4 — the OR drops only articles that are BOTH near-zero
#   traffic AND barely cross-wiki; abstention on obscure-but-real people is a
#   valid benchmark outcome, so the floor exists to drop noise, not to keep
#   only celebrities.
#
# GROUNDING PROVENANCE: the percentiles above come from the 2026-07-22 live
#   design-session study (n=120 in-window deaths, measured at people_prose:v1;
#   the raw table was live-only and is NOT committed). The AUDITABLE grounding
#   is regenerable instead of asserted: every harvested snapshot freezes the
#   observed distribution of all three signals into its manifest
#   (``params.signal_distribution``), and
#   ``python3 -m stage1.tools.people_threshold_study SNAPSHOT_DIR`` recomputes
#   the full per-title table + percentiles + default-threshold pass rates
#   offline from any snapshot at the CURRENT prose version. Retune floors
#   against those, not against this comment.
DEFAULT_POLICY = {
    "window_edge_ambiguous": "exclude",
    "single_sided_death": "admit",
    "body_prose_min_chars": 600,
    "pre_death_monthly_views_min": 50,
    "sitelinks_min": 3,
}

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WD_TIME_RE = re.compile(r"^([+-]?)(\d{1,})-(\d{2})-(\d{2})T")

# Wikidata properties whose value is a time datavalue.
WD_DATE_PROPERTIES = frozenset({"P569", "P570", "P571", "P580", "P582", "P1619"})


# --------------------------------------------------------------------------
# Wikidata time / interval helpers (pure, total — kept from the v1 adapter)
# --------------------------------------------------------------------------


def wd_time_to_iso(time_str, precision):
    """Map a Wikidata (time string, wikibase precision int) to
    (iso_value, precision_str, wd_precision_int) or None.

    precision 11=day, 10=month, 9=year, coarser collapses to 'year'. The
    sub-precision components of the raw string are UNTRUSTED below day
    precision (known-inconsistent Wikibase garbage): the value is derived from
    (time, precision), month/year pin unknown components to 01."""
    if not isinstance(time_str, str) or not isinstance(precision, int) or isinstance(precision, bool):
        return None
    match = _WD_TIME_RE.match(time_str)
    if not match:
        return None
    sign, year_s, month_s, day_s = match.groups()
    if sign == "-":
        return None
    year, month, day = int(year_s), int(month_s), int(day_s)
    if year < 1 or year > 9999:
        return None
    if precision >= 11 and 1 <= month <= 12 and 1 <= day <= 31:
        try:
            return _date(year, month, day).isoformat(), "day", precision
        except ValueError:
            return None
    if precision >= 10 and 1 <= month <= 12:
        return f"{year:04d}-{month:02d}-01", "month", precision
    return f"{year:04d}-01-01", "year", precision


def change_date_interval(value: str, precision: str):
    """Closed [lo, hi] date interval a (value, precision) pair denotes, or
    None when the value is not a real ISO date / precision is unknown."""
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


def prev_month_start(d: _date) -> _date:
    return (d.replace(day=1) - timedelta(days=1)).replace(day=1)


def _date_canon_to_iso(canonical: dict):
    """(iso_value, precision_str) for a date comparator canonical, or None."""
    if not isinstance(canonical, dict) or canonical.get("kind") != "date":
        return None
    y = canonical.get("y")
    if not isinstance(y, int) or isinstance(y, bool) or not 1 <= y <= 9999:
        return None
    precision = canonical.get("precision")
    m = canonical.get("m") if isinstance(canonical.get("m"), int) else 1
    d = canonical.get("d") if isinstance(canonical.get("d"), int) else 1
    if precision not in ("day", "month", "year"):
        return None
    try:
        iso = _date(y, m if precision != "year" else 1, d if precision == "day" else 1).isoformat()
    except ValueError:
        return None
    return iso, precision


def _dates_agree(iso_a, prec_a, iso_b, prec_b) -> bool:
    """True when two (iso, precision) dates agree at the coarser common
    precision (the infobox-vs-P570 agreement test)."""
    ia = change_date_interval(iso_a, prec_a)
    ib = change_date_interval(iso_b, prec_b)
    if ia is None or ib is None:
        return False
    rank = {"day": 2, "month": 1, "year": 0}
    common = min(rank.get(prec_a, 0), rank.get(prec_b, 0))
    da = _date.fromisoformat(iso_a)
    db = _date.fromisoformat(iso_b)
    if common == 0:
        return da.year == db.year
    if common == 1:
        return (da.year, da.month) == (db.year, db.month)
    return da == db


# --------------------------------------------------------------------------
# Wikidata cache decode helpers (the cache_format contract — kept from v1)
# --------------------------------------------------------------------------


def wd_state_statements(state, prop):
    """The list of STATEMENTs for property ``prop`` in a STATE_BLOCK, or []."""
    if not isinstance(state, dict):
        return []
    claims = state.get("claims")
    if not isinstance(claims, dict):
        return []
    stmts = claims.get(prop)
    return stmts if isinstance(stmts, list) else []


def wd_value_candidates(decoded):
    """Comparison-candidate raw strings for one DECODED_VALUE, primary key
    first. entityid -> [sitelink, label, *aliases] (sitelink-primary, the
    same namespace as the infobox wikilink target); time -> [precision-honest
    iso]; monolingualtext/string -> [text]. Unknown/unresolved -> []."""
    if not isinstance(decoded, dict):
        return []
    dtype = decoded.get("type")
    if dtype == "entityid":
        names = [decoded.get("sitelink"), decoded.get("label")]
        aliases = decoded.get("aliases")
        if isinstance(aliases, list):
            names.extend(aliases)
        out = []
        for n in names:
            if isinstance(n, str) and n and n not in out:
                out.append(n)
        return out
    if dtype == "time":
        iso = wd_time_to_iso(decoded.get("time"), decoded.get("precision"))
        return [iso[0]] if iso is not None else []
    if dtype == "monolingualtext":
        text = decoded.get("text")
        return [text] if isinstance(text, str) and text else []
    if dtype == "string":
        value = decoded.get("value")
        return [value] if isinstance(value, str) and value else []
    return []


def _stmt_rank(stmt) -> str:
    """A statement's Wikidata rank, defaulting a missing/malformed rank to
    'normal' (the Wikibase default rank)."""
    rank = stmt.get("rank") if isinstance(stmt, dict) else None
    return rank if isinstance(rank, str) else "normal"


def wd_dates_from_state(state, prop):
    """EVERY decodable, NON-DEPRECATED value-typed time statement of ``prop``
    in a state, as (iso, precision_str, wd_precision, statement_summary)
    tuples — preferred-rank statements first, statement order otherwise.

    Wikidata RANK semantics are honored (the deprecated-rank fix):

    * ``deprecated`` is the community's explicit this-value-is-WRONG marker
      (the classic case: a death date from an initial media report, kept for
      provenance after correction). A deprecated claim must neither
      CORROBORATE an inclusion nor VETO an otherwise-corroborated death, so
      it is skipped entirely here (the corroboration gate's outcome log still
      shows it via _compare_side's audit entries).
    * ``preferred`` is the community's best current value and is consulted
      first (mirroring Wikibase truthy semantics)."""
    preferred: list = []
    normal: list = []
    for stmt in wd_state_statements(state, prop):
        if not isinstance(stmt, dict) or stmt.get("snaktype") != "value":
            continue
        rank = _stmt_rank(stmt)
        if rank == "deprecated":
            continue
        value = stmt.get("value")
        if not isinstance(value, dict) or value.get("type") != "time":
            continue
        iso = wd_time_to_iso(value.get("time"), value.get("precision"))
        if iso is None:
            continue
        entry = (iso[0], iso[1], iso[2], {
            "property": prop,
            "wd_time": value.get("time"),
            "wd_precision": iso[2],
            "rank": rank,
        })
        (preferred if rank == "preferred" else normal).append(entry)
    return preferred + normal


def wd_date_from_state(state, prop):
    """The single BEST (iso, precision_str, wd_precision, statement_summary)
    date claim of ``prop`` in a state, or None: the first NON-DEPRECATED
    statement in rank order (preferred before normal/unknown). A
    deprecated-rank claim — Wikidata's this-value-is-wrong marker — can never
    be returned (see :func:`wd_dates_from_state`)."""
    dates = wd_dates_from_state(state, prop)
    return dates[0] if dates else None


# --------------------------------------------------------------------------
# Record-reading helpers (pure)
# --------------------------------------------------------------------------


def _prov(record):
    prov = getattr(record, "provenance", None)
    return prov if isinstance(prov, dict) else {}


def _prop_of(record) -> str:
    prop = getattr(record, "property", None)
    return prop if isinstance(prop, str) else ""


def _title_of(record) -> str:
    entity = getattr(record, "entity", None)
    name = entity.get("name") if isinstance(entity, dict) else None
    return name if isinstance(name, str) else ""


def _side_present(record, side: str) -> bool:
    """True when a before/after side carries a non-empty raw value (the
    deterministic 'this side has a value' test the single-sided policy keys
    on — an absent side is empty by construction)."""
    raw = getattr(getattr(record, side, None), "raw", None)
    return isinstance(raw, str) and bool(raw.strip())


def _policy_int(policy, key: str):
    value = policy.get(key) if isinstance(policy, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_POLICY[key]
    return value


# --------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------


class PeopleInfoboxDeathDateGate(Gate):
    """The primary value IS the Wikipedia infobox death date, so a headline
    (``deathdate``) record whose pinned CURRENT revision provably displays no
    infobox death date FAILS here -> ``excluded:no_infobox_death_date`` — the
    named, auditable coverage limit (~1 in 5 in-window P570 deaths carry no
    infobox death field). Verdicts, per the after-side extraction status:

    * ``extracted``                      -> pass (a readable infobox death date).
    * ``no_infobox`` / ``field_absent`` / ``field_blank`` -> FAIL (the pinned
      revision provably displays no death date; P570 is recorded in evidence
      for the audit trail).
    * ``unreadable``                     -> review (the field IS displayed but
      could not be safely read — refuse-to-guess, a human looks).
    * ``no_wikitext`` / ``side_missing`` -> review (cache gap: cannot decide).

    Secondary records (deathplace/deathcause) mostly pass — their presence is
    structural at enumeration and their value quality is judged by the shared
    value gates — EXCEPT (people_v2, the honest-exclusion fix) when their own
    after-side extraction is ``unreadable`` (the field IS displayed but the
    value was refused) or a cache gap (``no_wikitext``/``side_missing``): the
    empty raw would otherwise compare 'no change' against an empty before side
    and land in ``excluded:value_changed``, silently mislabelling a
    displayed-but-unparseable value as 'the infobox did not change'. Those
    route to review, mirroring the headline's refuse-to-guess verdicts."""

    name = "no_infobox_death_date"
    version = "infobox_death_date:people_v2"

    def evaluate(self, record, ctx: dict) -> GateResult:
        prop = _prop_of(record)
        evidence: dict = {"property": prop}
        extraction = (_prov(record).get("extraction") or {}).get("after") or {}
        status = extraction.get("status")
        evidence["after_extraction_status"] = status
        if prop != "deathdate":
            if status == "unreadable":
                evidence["problem"] = (
                    "the infobox DOES display a value for this secondary death field but "
                    f"it could not be safely read ({extraction.get('reason')!r}): the empty "
                    "raw would otherwise compare as 'no change' — refuse-to-guess, held for "
                    "review rather than excluded:value_changed"
                )
                return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
            if status in ("no_wikitext", "side_missing"):
                evidence["problem"] = (
                    f"the pinned current revision's wikitext is unavailable (status {status!r}): "
                    "whether this secondary death field still displays a value cannot be "
                    "decided — review"
                )
                return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
            evidence["note"] = (
                "secondary death property: enumerated only when the infobox carries it, "
                "so the headline infobox-death-date requirement does not apply"
            )
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
        evidence["infobox_found"] = extraction.get("infobox_found")
        if status == "extracted":
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
        if status in ("no_infobox", "field_absent", "field_blank"):
            evidence["reason"] = "no_infobox_death_date"
            evidence["problem"] = (
                "the pinned current revision displays NO infobox death date "
                f"(extraction status {status!r}): the wikipedia_infobox primary value cannot "
                "be produced for this death — the documented ~1-in-5 coverage limit of the "
                "infobox-primary design, excluded by name rather than silently dropped"
            )
            return GateResult(name=self.name, version=self.version, verdict="fail", evidence=evidence)
        if status == "unreadable":
            evidence["problem"] = (
                "the infobox DOES display a death date but it could not be safely read "
                f"({extraction.get('reason')!r}): refuse-to-guess, held for review"
            )
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        evidence["problem"] = (
            f"the pinned current revision's wikitext is unavailable (status {status!r}): "
            "whether an infobox death date is displayed cannot be decided — review"
        )
        return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)


class PeopleValueChangedGate(ValueActuallyChangedGate):
    """The shared value_changed comparator gate, wrapped with the people
    honest-exclusion rule (value_changed:people_v1): a side whose emptiness is
    an ARTIFACT of a refused or impossible read must never feed the
    comparator — the '' would compare as an honest ABSENT and a genuinely
    displayed (but unparseable) value, or an unknowable side, would land in
    ``excluded:value_changed`` ('the infobox did not change'), silently
    mislabelling a refuse-to-guess situation as a non-change. Reviewed
    instead, for BOTH the headline and the secondaries:

    * ``unreadable`` on EITHER side — the field IS displayed but the value
      was refused;
    * ``no_wikitext`` on either side — the whole pinned-wikitext cache row is
      missing (cache gap);
    * ``side_missing`` on the AFTER side — the current pinned revision is
      unavailable (the page must exist to be discovered, so this is always a
      fetch/cache gap);
    * ``side_missing`` on the BEFORE side — review UNLESS the recorded fetch
      error says the page had no revision at/before the cutoff (created
      later): THAT absence is an honest 'displayed nothing at the cutoff'
      and defers to the comparator (the routine single-sided-death shape for
      post-cutoff articles).

    Honest statuses (``extracted``/``no_infobox``/``field_absent``/
    ``field_blank``) defer to the shared comparator logic unchanged."""

    version = "value_changed:people_v1"

    # The stable prefix fetch_wiki_revisions.fetch_side emits for a page whose
    # first revision postdates the pin — the one HONEST reason a before side
    # can be missing.
    _CREATED_LATER_MARK = "no revision at or before"

    def evaluate(self, record, ctx: dict) -> GateResult:
        extraction = _prov(record).get("extraction") or {}
        for side_name in ("before", "after"):
            detail = extraction.get(side_name) or {}
            problem = self._artifact_problem(side_name, detail if isinstance(detail, dict) else {})
            if problem:
                evidence = {
                    "value_type": getattr(record, "value_type", None),
                    "side": side_name,
                    "extraction_status": (detail or {}).get("status"),
                    "problem": problem,
                }
                return GateResult(name=self.name, version=self.version,
                                  verdict="review", evidence=evidence)
        return super().evaluate(record, ctx)

    @classmethod
    def _artifact_problem(cls, side_name: str, detail: dict):
        status = detail.get("status")
        if status == "unreadable":
            return (
                f"the {side_name} side DISPLAYS a value that could not be safely read "
                f"({detail.get('reason')!r}): its empty raw is an artifact, not an honest "
                "absent — comparing it would mislabel a refuse-to-guess as a non-change"
            )
        if status == "no_wikitext":
            return (
                f"the {side_name} side's pinned wikitext cache row is missing: what the "
                "revision displays cannot be known — review, never excluded:value_changed"
            )
        if status == "side_missing":
            if side_name == "before":
                errors = [e for e in (detail.get("fetch_errors") or [])
                          if isinstance(e, str) and e.startswith("cutoff:")]
                if errors and all(cls._CREATED_LATER_MARK in e for e in errors):
                    return None  # honest absent: the page did not exist at the cutoff
            return (
                f"the pinned {side_name} revision is unavailable "
                f"(fetch_errors {detail.get('fetch_errors')!r}) and its absence is not "
                "explained by the page being created after the cutoff — review"
            )
        return None


class PeopleDeathChangeGate(Gate):
    """Single-sided death POLICY gate (review-only, NEVER fails). Every record
    of this source is a death property, so the manifest-recorded
    ``single_sided_death`` policy applies to all of them:

    * before empty, after present -> single-sided ADDITION: policy 'admit'
      (default) PASSES it; policy 'review' holds it for a human.
    * before present, after empty -> single-sided REMOVAL: always review
      (``death_removal`` — deleting a death value is vandalism/error, never an
      in-window fact change).
    * both present                -> two-sided correction: pass, deferring to
      the ``value_changed`` comparator verdict.
    * neither present             -> pass (``value_changed`` resolves the
      both-absent pair as a non-change)."""

    name = "death_change"
    version = "death_change:people_v2"

    def evaluate(self, record, ctx: dict) -> GateResult:
        evidence: dict = {"property": _prop_of(record)}
        policy = ctx.get(POLICY_CTX_KEY)
        action = policy.get("single_sided_death") if isinstance(policy, dict) else None
        if action not in ("admit", "review"):
            action = DEFAULT_POLICY["single_sided_death"]
        evidence["single_sided_death_policy"] = action

        before_present = _side_present(record, "before")
        after_present = _side_present(record, "after")
        evidence["before_present"] = before_present
        evidence["after_present"] = after_present

        if before_present and after_present:
            evidence["case"] = "two_sided_change"
            evidence["note"] = "both sides present: a correction handled by the value_changed comparator"
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
        if not before_present and after_present:
            evidence["case"] = "single_sided_addition"
            if action == "admit":
                evidence["note"] = (
                    "single-sided death addition ('' -> a value): ADMITTED as a valid "
                    "post-cutoff change (owner policy single_sided_death='admit')"
                )
                return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
            evidence["reason"] = "single_sided_death_review"
            evidence["problem"] = (
                "single-sided death addition held for review (policy single_sided_death='review')"
            )
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        if before_present and not after_present:
            evidence["case"] = "single_sided_removal"
            evidence["reason"] = "death_removal"
            evidence["problem"] = (
                "single-sided death REMOVAL (a value -> ''): deleting a death value is "
                "vandalism/error, never an in-window fact change — held for review"
            )
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        evidence["case"] = "both_absent"
        evidence["note"] = "both sides empty; the value_changed comparator resolves this as a non-change"
        return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)


class PeopleTemporalWindowGate(Gate):
    """Precision-aware temporal window for people change dates (people_v1 —
    unchanged from the funnel adapter; the change date is now always the
    infobox DEATH date).

    Let [lo, hi] be the closed interval the (value, precision) pair denotes:

    * basis 'revision_bracket' (no usable death date) -> review
      'unresolved_change_date'.
    * lo > asof -> FAIL 'post_window'.
    * hi < cutoff: day precision -> FAIL 'pre_cutoff' (a backfilled OLD death,
      not in-window drift); coarse with hi in the month just before the cutoff
      -> 'window_edge' -> POLICY; coarse older -> FAIL 'pre_cutoff'.
    * cutoff <= lo and hi <= asof -> pass 'in_window'.
    * a coarse interval straddling cutoff or asof -> 'window_edge' -> POLICY
      (window_edge_ambiguous: 'exclude' -> FAIL, 'review' -> review).

    Anything unparseable -> review."""

    name = "temporal_window"
    version = "temporal_window:people_v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        policy = ctx.get(POLICY_CTX_KEY)
        edge_action = "exclude"
        if isinstance(policy, dict) and policy.get("window_edge_ambiguous") in ("exclude", "review"):
            edge_action = policy["window_edge_ambiguous"]
        edge_verdict = "fail" if edge_action == "exclude" else "review"

        evidence: dict = {"window_edge_policy": edge_action}
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
                "unresolved_change_date: no usable infobox death date; the infobox edit is "
                "in-window by construction but the real death date is unknown"
            )
            evidence["reason"] = "unresolved_change_date"
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
                    "window_edge: a coarse-precision date in the month immediately before "
                    "the cutoff cannot resolve which side of the cutoff the death falls on"
                )
                evidence["reason"] = "window_edge"
                return GateResult(name=self.name, version=self.version, verdict=edge_verdict, evidence=evidence)
            evidence["problem"] = "entire interval is before the cutoff (a backfilled old death, not in-window drift)"
            evidence["reason"] = "pre_cutoff"
            return GateResult(name=self.name, version=self.version, verdict="fail", evidence=evidence)
        if lo >= cutoff and hi <= asof:
            evidence["reason"] = "in_window"
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
        evidence["problem"] = "window_edge: the date interval straddles the cutoff or asof bound"
        evidence["reason"] = "window_edge"
        return GateResult(name=self.name, version=self.version, verdict=edge_verdict, evidence=evidence)


class PeopleCorroborationGate(Gate):
    """Death-scoped Wikidata corroboration (corroboration:people_v3) — the
    VANDALISM GUARD. Nothing here can FAIL (the infobox stays the declared
    ground truth; a mismatch may be vandalism on either side or a timing lag,
    so only a human calls it) — but an UNCORROBORATED death never reaches
    ``included``:

    (1) THE DEATH ITSELF, for every record: the infobox death date (carried in
        provenance['death_date']) must AGREE with a NON-DEPRECATED current
        P570 claim at the coarser common precision (ANY-match over the
        rank-ordered claims, mirroring the secondary check — a deprecated
        claim, Wikidata's explicit this-value-is-wrong marker, can neither
        corroborate nor veto; see :func:`wd_dates_from_state`). Discovery IS
        P570, so a claim is present for every discovered candidate; no
        credible (non-deprecated) P570 (``wikidata_has_no_statement`` /
        ``wikidata_only_deprecated_statements``), a disagreement with every
        credible claim (``sources_disagree``) — the fake-death signature — or
        a missing infobox death date all -> review.
    (2) The record's own mapped property, for the secondary records:
        deathplace vs P20 / deathcause vs P509, compared with the record's own
        comparator over the decoded candidate strings (sitelink-primary);
        deprecated-rank statements are skipped (logged in the outcomes for
        audit, never compared). Agree -> recorded; disagree -> review; ABSENT
        (incl. deprecated-only) -> pass with ``secondary_property_absent``
        (the death is P570-corroborated and Wikidata is routinely sparse on
        circumstances — an absent secondary claim is not a disagreement).

    Cache / row / qid / state missing, or a property outside the cache's
    stored whitelist -> review with the specific reason (never a crash,
    never a silent include)."""

    name = "corroboration"
    version = "corroboration:people_v3"

    SOURCE_LABEL = "wikidata_p570"

    def evaluate(self, record, ctx: dict) -> GateResult:
        evidence: dict = {"source_label": self.SOURCE_LABEL}

        def review(problem: str) -> GateResult:
            evidence["problem"] = problem
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)

        prop = _prop_of(record)
        spec = DEATH_PROPERTIES.get(prop)
        evidence["property"] = prop
        if spec is None:
            return review(f"unknown death property {prop!r}")
        wd_prop = spec.wd_property
        evidence["wd_property"] = wd_prop

        title = _title_of(record)
        evidence["entity_title"] = title
        store = ctx.get(WD_CACHE_CTX_KEY)
        info = ctx.get(WD_CACHE_INFO_CTX_KEY)
        if not isinstance(store, dict):
            return review(
                f"cache_missing: the Wikidata death cache was not loaded "
                f"(ctx[{WD_CACHE_CTX_KEY!r}] absent)"
            )
        if not info or not info.get("file"):
            return review("cache_missing: no Wikidata death cache file resolved")

        whitelist = info.get("property_whitelist")
        if isinstance(whitelist, (list, tuple, set, frozenset)):
            for needed in ("P570", wd_prop):
                if needed not in whitelist:
                    return review(
                        f"property_not_in_cache_whitelist: {needed!r} is outside the cache's "
                        "stored property_whitelist; the value was never fetched"
                    )

        row = store.get(title)
        evidence["entity_in_cache"] = row is not None
        if not isinstance(row, dict):
            return review(f"{self.SOURCE_LABEL} cache has no row for this entity title")
        qid = row.get("qid")
        evidence["qid"] = qid if isinstance(qid, str) else None
        if not isinstance(qid, str) or not qid:
            return review(
                f"no_qid: cache row has no Wikidata QID (title_status {row.get('title_status')!r})"
            )
        current = row.get("current")
        if not isinstance(current, dict) or not current.get("exists"):
            return review("current_state_missing: no current Wikidata state for this entity")
        if current.get("is_redirect"):
            return review(
                f"current_state_is_redirect:{current.get('redirect_to')} — a merge/redirect "
                "stub has no usable claims"
            )

        # ---- (1) the death itself: infobox death date vs P570 -------------
        dd = _prov(record).get("death_date") or {}
        info_iso = dd.get("value")
        info_prec = dd.get("precision")
        evidence["infobox_death_date"] = {"value": info_iso, "precision": info_prec}
        if not isinstance(info_iso, str) or not info_iso:
            return review(
                "infobox_death_date_missing: the death itself cannot be corroborated "
                "without an infobox death date"
            )
        wd_dates = wd_dates_from_state(current, "P570")
        if not wd_dates:
            all_stmts = wd_state_statements(current, "P570")
            deprecated_only = any(
                isinstance(s, dict) and s.get("snaktype") == "value"
                and _stmt_rank(s) == "deprecated" for s in all_stmts
            )
            if deprecated_only:
                return review(
                    "wikidata_only_deprecated_statements: every value-typed P570 (date of "
                    "death) claim on the entity's current state is DEPRECATED-rank — "
                    "Wikidata's explicit this-value-is-wrong marker cannot corroborate a "
                    "death, so the death is uncorroborated"
                )
            return review(
                "wikidata_has_no_statement: no value-typed P570 (date of death) claim on "
                "the entity's current state — the death is uncorroborated"
            )
        evidence["p570_candidates"] = [
            {"value": d[0], "precision": d[1], "statement": d[3]} for d in wd_dates
        ]
        agreeing = next(
            (d for d in wd_dates if _dates_agree(info_iso, info_prec, d[0], d[1])), None
        )
        evidence["death_corroborated"] = agreeing is not None
        if agreeing is None:
            return review(
                "sources_disagree: the infobox death date does not agree with ANY "
                "non-deprecated current P570 claim at the coarser common precision — the "
                "fake-death/vandalism signature; a human must call it"
            )
        evidence["p570"] = {
            "value": agreeing[0], "precision": agreeing[1], "wd_precision": agreeing[2],
            "statement": agreeing[3],
        }

        if prop == "deathdate":
            evidence["corroboration"] = "corroborated"
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)

        # ---- (2) the secondary property: P20 / P509 -----------------------
        after_canonical = getattr(getattr(record, "after", None), "canonical", None)
        if not isinstance(after_canonical, dict):
            return review("after.canonical is missing (raw value did not parse)")
        try:
            comparator = get_comparator(getattr(record, "value_type", None))
        except (LookupError, TypeError) as exc:
            return review(str(exc))
        evidence["comparator_version"] = comparator.VERSION

        statements = wd_state_statements(current, wd_prop)
        outcomes, matched, undecided, saw_value = self._compare_side(
            after_canonical, statements, comparator
        )
        evidence["secondary_candidates"] = outcomes
        if matched is not None:
            evidence["matched_value"] = matched["value"]
            evidence["corroboration"] = "corroborated"
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
        if not saw_value:
            evidence["corroboration"] = "secondary_property_absent"
            evidence["note"] = (
                f"no value-typed {wd_prop} claim on the current state: the death itself is "
                "P570-corroborated and Wikidata is routinely sparse on death circumstances, "
                "so an absent secondary claim is recorded, not reviewed"
            )
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
        if undecided:
            return review(
                f"undecidable: comparator could not decide agreement with the {wd_prop} claim"
            )
        return review(
            f"sources_disagree: no {wd_prop} value matches after.canonical "
            "(the death is P570-corroborated but its circumstance disagrees)"
        )

    @staticmethod
    def _compare_side(canonical, statements, comparator):
        """Compare one canonical against a property's NON-DEPRECATED statement
        values. Returns (outcomes, matched, undecided, saw_value). A
        deprecated-rank statement (Wikidata's this-value-is-wrong marker) is
        logged in the outcomes for audit but never compared: it can neither
        corroborate the record nor count as a present-but-disagreeing claim
        (``saw_value`` stays False for deprecated-only properties, which then
        resolve as ``secondary_property_absent``)."""
        outcomes: list = []
        matched = None
        undecided = False
        saw_value = False
        for index, stmt in enumerate(statements):
            if not isinstance(stmt, dict):
                continue
            if stmt.get("snaktype") != "value":
                outcomes.append({"index": index, "snaktype": stmt.get("snaktype"), "outcome": "no_value"})
                continue
            if _stmt_rank(stmt) == "deprecated":
                outcomes.append({"index": index, "rank": "deprecated",
                                 "outcome": "deprecated_rank_skipped"})
                continue
            saw_value = True
            for candidate in wd_value_candidates(stmt.get("value")):
                outcome = {"index": index, "value": candidate, "rank": stmt.get("rank")}
                parsed = comparator.parse(candidate)
                if not getattr(parsed, "ok", False) or not isinstance(getattr(parsed, "canonical", None), dict):
                    outcome["outcome"] = f"unparseable: {getattr(parsed, 'failure_reason', 'contract violation')}"
                    outcomes.append(outcome)
                    continue
                comparison = comparator.compare(canonical, parsed.canonical)
                if not isinstance(comparison, Comparison) or comparison.verdict not in (
                    "equal", "different", "review", "incomparable"
                ):
                    outcome["outcome"] = "comparator_contract_violation"
                    outcomes.append(outcome)
                    undecided = True
                    continue
                outcome["outcome"] = comparison.verdict
                if comparison.reason:
                    outcome["reason"] = comparison.reason
                outcomes.append(outcome)
                if comparison.verdict == "equal" and matched is None:
                    matched = outcome
                elif comparison.verdict in ("review", "incomparable"):
                    undecided = True
        return outcomes, matched, undecided, saw_value


class PeopleBodyProseGate(Gate):
    """REQUIRED real-article-body floor (the poisoned-paragraph bio study
    needs substantive prose, not a birth/death stub). The measure is
    provenance['article']['prose_chars'] — recomputed OFFLINE by build_record
    from the pinned CURRENT revision in the wikitext cache via the versioned
    people_prose:v2 function, never a live fetch and never the harvest row's
    unverified number. Below the manifest-recorded ``body_prose_min_chars``
    floor -> FAIL (``excluded:stub_no_body``); an unavailable pinned revision
    -> review (``prose_unknown``), never a silent pass/fail."""

    name = "stub_no_body"
    version = "real_article_body:people_v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        policy = ctx.get(POLICY_CTX_KEY)
        floor = _policy_int(policy if isinstance(policy, dict) else {}, "body_prose_min_chars")
        article = _prov(record).get("article") or {}
        measured = article.get("prose_chars")
        evidence: dict = {
            "body_prose_min_chars": floor,
            "prose_chars": measured if isinstance(measured, int) else None,
            "prose_version": article.get("prose_version"),
        }
        if not isinstance(measured, int) or isinstance(measured, bool):
            evidence["problem"] = (
                "prose_unknown: the pinned current revision's wikitext is unavailable, so "
                "the body-prose measure cannot be computed — review, never a silent pass/fail"
            )
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        if measured >= floor:
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
        evidence["reason"] = "stub_no_body"
        evidence["problem"] = (
            f"the article body measures {measured} prose chars, below the required "
            f"real-article-body floor of {floor} (a birth/death stub cannot support the "
            "downstream bio study)"
        )
        return GateResult(name=self.name, version=self.version, verdict="fail", evidence=evidence)


class PeopleRecognisabilityGate(Gate):
    """MODEST recognisability floor (owner: abstention is a VALID outcome — the
    floor drops stubs/noise, not non-celebrities). Signals live in
    provenance['recognisability']: the PRE-DEATH baseline-month pageviews
    (from the snapshot's people_pageviews.jsonl — the month before the cutoff,
    so the death spike never inflates the signal) and the Wikidata sitelink
    count (from the discovery candidates). Lenient OR:

    * views >= pre_death_monthly_views_min OR sitelinks >= sitelinks_min
      -> pass.
    * BOTH known and BOTH below -> FAIL
      (``excluded:below_recognisability_floor``).
    * a signal unknown (a pageviews fetch miss, a candidates gap) and the
      known one below -> review ``recognisability_unknown`` — a fetch miss
      degrades to review, never a silent pass or fail."""

    name = "below_recognisability_floor"
    version = "recognisability:people_v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        policy = ctx.get(POLICY_CTX_KEY)
        policy = policy if isinstance(policy, dict) else {}
        views_min = _policy_int(policy, "pre_death_monthly_views_min")
        sitelinks_min = _policy_int(policy, "sitelinks_min")
        rec = _prov(record).get("recognisability") or {}
        views = rec.get("pre_death_monthly_views")
        sitelinks = rec.get("sitelinks")
        views_known = isinstance(views, int) and not isinstance(views, bool)
        sitelinks_known = isinstance(sitelinks, int) and not isinstance(sitelinks, bool)
        evidence: dict = {
            "pre_death_monthly_views": views if views_known else None,
            "pre_death_monthly_views_min": views_min,
            "sitelinks": sitelinks if sitelinks_known else None,
            "sitelinks_min": sitelinks_min,
            "baseline_month": rec.get("baseline_month"),
            "rule": "views >= floor OR sitelinks >= floor (lenient OR; modest by design)",
        }
        if (views_known and views >= views_min) or (sitelinks_known and sitelinks >= sitelinks_min):
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
        if views_known and sitelinks_known:
            evidence["reason"] = "below_recognisability_floor"
            evidence["problem"] = (
                f"both signals known and below the modest floor (views {views} < {views_min} "
                f"AND sitelinks {sitelinks} < {sitelinks_min}): near-zero traffic and barely "
                "cross-wiki — dropped as noise, by name"
            )
            return GateResult(name=self.name, version=self.version, verdict="fail", evidence=evidence)
        missing = []
        if not views_known:
            missing.append("pre_death_monthly_views (pageviews fetch miss or cache gap)")
        if not sitelinks_known:
            missing.append("sitelinks (candidates row missing or count unfetched)")
        evidence["reason"] = "recognisability_unknown"
        evidence["problem"] = (
            "recognisability_unknown: " + "; ".join(missing) + " — an unknown signal degrades "
            "to review, never a silent pass or fail"
        )
        return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)


# --------------------------------------------------------------------------
# The adapter
# --------------------------------------------------------------------------


class WikiPeopleAdapter(Adapter):
    """Adapter for source 'wiki_people' (deaths only). See the module
    docstring. Stateless: joins and caches load once per run into cfg (the
    sanctioned runner channel), never onto the instance."""

    source = SOURCE

    # -- snapshot contract --------------------------------------------------

    def snapshot_inputs(self, cfg: dict):
        """EVERY snapshot input this adapter reads by name — declared so the
        coverage check (rule 5b) refuses a trimmed snapshot whose files block
        dropped one while its on-disk bytes were swapped. The candidates and
        pageviews caches are declared too (the trimmed-files-block fix): they
        feed the recognisability GATE, so a snapshot whose manifest was
        trimmed of them while their bytes were edited could otherwise flip
        inclusions/exclusions silently. Absence-tolerance is a separate,
        legacy-only concern: rule 5 runs only when a snapshot manifest exists,
        and a harvested snapshot always writes all five files — a manifest-less
        legacy data dir (where a missing cache degrades to review
        ``recognisability_unknown``) never reaches this check."""
        return [VERIFIED_FILENAME, WIKITEXT_CACHE_FILENAME, WD_CACHE_FILENAME,
                CANDIDATES_FILENAME, PAGEVIEWS_FILENAME]

    # -- enumeration --------------------------------------------------------

    def enumerate_candidates(self, cfg: dict):
        """Yield up to three property-candidates per people_death_verified.jsonl
        row, sorted by (title, property-order, line). ``deathdate`` is always
        emitted (its absence from the infobox is the named exclusion);
        ``deathplace``/``deathcause`` only when the row's frozen
        ``fields_present`` flags say the pinned infobox displays them on
        either side (structural presence — there is no candidate value at all
        otherwise). Unparseable lines are yielded as error candidates so
        build_record raises into manifest build_errors — enumeration never
        decides inclusion."""
        self._require_offline(cfg)
        path = self._data_path(cfg, VERIFIED_FILENAME)
        rows = []
        with open(path, encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError(f"row is {type(row).__name__}, expected object")
                except ValueError as exc:
                    rows.append({"_line": line_no, "_parse_error": str(exc)})
                    continue
                row["_line"] = line_no
                rows.append(row)
        prop_rank = {p: i for i, p in enumerate(PROPERTY_ORDER)}
        candidates = []
        for row in rows:
            if "_parse_error" in row:
                candidates.append(row)
                continue
            present = row.get("fields_present")
            present = present if isinstance(present, dict) else {}
            before_p = present.get("before") if isinstance(present.get("before"), dict) else {}
            after_p = present.get("after") if isinstance(present.get("after"), dict) else {}
            for prop in PROPERTY_ORDER:
                if prop != "deathdate" and not (before_p.get(prop) or after_p.get(prop)):
                    continue  # structurally not a candidate: the infobox never displays it
                candidate = dict(row)
                candidate["_prop"] = prop
                candidates.append(candidate)
        candidates.sort(
            key=lambda c: (
                str(c.get("title") or ""),
                prop_rank.get(c.get("_prop"), len(PROPERTY_ORDER)),
                c.get("_line") or 0,
            )
        )
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
            raise ValueError(f"{VERIFIED_FILENAME} line {candidate.get('_line')}: missing title")
        prop = candidate.get("_prop")
        spec = DEATH_PROPERTIES.get(prop)
        if spec is None:
            raise ValueError(
                f"{VERIFIED_FILENAME} line {candidate.get('_line')}: unknown death property {prop!r}"
            )
        self._ensure_loaded(cfg)

        qid = candidate.get("qid") if isinstance(candidate.get("qid"), str) and candidate.get("qid") else None
        cutoff_rev_ts = candidate.get("cutoff_rev_ts") or None
        cutoff_revid = candidate.get("cutoff_revid")
        cur_rev_ts = candidate.get("cur_rev_ts") or None
        cur_revid = candidate.get("cur_revid")

        wt_store = cfg.get(WIKITEXT_CACHE_CTX_KEY) or {}
        wt_row = wt_store.get(title) if isinstance(wt_store, dict) else None
        wt_info = cfg.get(WIKITEXT_CACHE_INFO_CTX_KEY) or {"file": None, "sha1": None}

        # Versioned re-extraction from the pinned revisions (the byte-anchor):
        # the recorded before/after VALUE is the infobox reading of THIS record's
        # property; the after-side death DATE is additionally extracted for the
        # change date + P570 corroboration of every record of this person.
        before_value, before_detail = self._extract_side(wt_row, "cutoff", prop)
        after_value, after_detail = self._extract_side(wt_row, "current", prop)
        if prop == "deathdate":
            dd_value, dd_detail = after_value, after_detail
        else:
            dd_value, dd_detail = self._extract_side(wt_row, "current", "deathdate")
        death_date_iso = None
        if isinstance(dd_value, str) and dd_value.strip():
            parsed = get_comparator("date").parse(dd_value)
            if getattr(parsed, "ok", False):
                death_date_iso = _date_canon_to_iso(parsed.canonical)
        death_date_info = {
            "cleaned": dd_value if isinstance(dd_value, str) and dd_value else None,
            "value": death_date_iso[0] if death_date_iso else None,
            "precision": death_date_iso[1] if death_date_iso else None,
            "extraction_status": dd_detail.get("status"),
        }

        # Deterministic offline prose measure from the pinned CURRENT revision.
        measured_prose = None
        if isinstance(wt_row, dict):
            side = wt_row.get("current")
            if isinstance(side, dict) and isinstance(side.get("content"), str):
                measured_prose = prose_chars(side["content"])

        # Discovery + recognisability provenance.
        cand_store = cfg.get(CANDIDATES_CTX_KEY) or {}
        cand_row = cand_store.get(title) if isinstance(cand_store, dict) else None
        sitelinks = None
        if isinstance(cand_row, dict):
            raw_sl = cand_row.get("sitelinks")
            if isinstance(raw_sl, int) and not isinstance(raw_sl, bool):
                sitelinks = raw_sl
        pv_store = cfg.get(PAGEVIEWS_CTX_KEY) or {}
        pv_row = pv_store.get(title) if isinstance(pv_store, dict) else None
        views = None
        baseline_month = None
        views_note = None
        if isinstance(pv_row, dict):
            baseline_month = pv_row.get("month")
            raw_views = pv_row.get("views")
            if isinstance(raw_views, int) and not isinstance(raw_views, bool):
                views = raw_views
            views_note = pv_row.get("note") or pv_row.get("error")

        wd_store = cfg.get(WD_CACHE_CTX_KEY) or {}
        wd_row = wd_store.get(title) if isinstance(wd_store, dict) else None
        wd_info = cfg.get(WD_CACHE_INFO_CTX_KEY) or {"file": None, "sha1": None}
        if qid is None and isinstance(wd_row, dict) and isinstance(wd_row.get("qid"), str) and wd_row["qid"]:
            qid = wd_row["qid"]

        page_url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
        wd_url = f"https://www.wikidata.org/wiki/{qid}" if qid else None
        change_date = self._resolve_change_date(
            death_date_info["value"], death_date_info["precision"], wd_row,
            qid, wd_url, page_url, cutoff_rev_ts, cur_rev_ts,
        )

        def infobox_evidence(detail, rev_ts, revid, side_name) -> Evidence:
            ref = {
                "title": title,
                "property": prop,
                "side": side_name,
                "revision_id": revid,
                "revision_timestamp": rev_ts,
                "extraction": detail,
            }
            return Evidence(kind="wikipedia_infobox", url=page_url, ref=ref, as_of=rev_ts)

        provenance = {
            # Owner decision B: every record of this source is a death event —
            # the one genuinely unforecastable class — so the tag is the
            # constant 'unpredictable' (metadata only; never a gate).
            "predictability": check_predictability(UNPREDICTABLE),
            "line": candidate.get("_line"),
            "property": prop,
            "value_type": spec.value_type,
            "page": {
                "title": title,
                "qid": qid,
                "cutoff_rev_ts": cutoff_rev_ts,
                "cutoff_revid": cutoff_revid,
                "cur_rev_ts": cur_rev_ts,
                "cur_revid": cur_revid,
                "infobox_found": candidate.get("infobox_found"),
                "template": candidate.get("template"),
                "fetch_errors": candidate.get("fetch_errors") or [],
            },
            "finder": {
                "in_candidates": cand_row is not None,
                "p570_date": cand_row.get("p570_date") if isinstance(cand_row, dict) else None,
                "p570_precision": cand_row.get("p570_precision") if isinstance(cand_row, dict) else None,
                "sitelinks": sitelinks,
            },
            "death_date": death_date_info,
            "extraction": {"before": before_detail, "after": after_detail},
            "article": {
                "prose_chars": measured_prose,
                "prose_version": PROSE_VERSION,
                "harvest_prose_chars": candidate.get("prose_chars"),
            },
            "recognisability": {
                "pre_death_monthly_views": views,
                "baseline_month": baseline_month,
                "views_note": views_note,
                "sitelinks": sitelinks,
            },
            "wd_cache": {
                "file": wd_info.get("file"),
                "sha1": wd_info.get("sha1"),
                "row_present": wd_row is not None,
                "mapped_property": spec.wd_property,
            },
            "wikitext_cache": {
                "file": wt_info.get("file"),
                "sha1": wt_info.get("sha1"),
                "row_present": isinstance(wt_row, dict),
            },
            "policy": dict(self._policy(cfg)),
        }

        entity_ids = {"wikidata_qid": qid} if qid else {}
        fact_id = compute_fact_id(self.source, title, prop, change_date.value)
        record_id = compute_record_id(
            fact_id, f"{cutoff_rev_ts or ''}|{cur_rev_ts or ''}|{prop}"
        )
        return FactChangeRecord(
            fact_id=fact_id,
            record_id=record_id,
            source=self.source,
            entity={"name": title, "ids": entity_ids},
            property=prop,
            value_type=spec.value_type,
            before=ValueState(
                raw=before_value,
                canonical=None,
                evidence=infobox_evidence(before_detail, cutoff_rev_ts, cutoff_revid, "cutoff"),
            ),
            after=ValueState(
                raw=after_value,
                canonical=None,
                evidence=infobox_evidence(after_detail, cur_rev_ts, cur_revid, "current"),
            ),
            change_date=change_date,
            provenance=provenance,
        )

    # -- side extraction (versioned, pure given the caches) ------------------

    @staticmethod
    def _extract_side(wt_row, side_key: str, prop: str):
        """(value, extraction_detail) for one pinned-revision side of one
        death property. The value is the comparator-ready infobox reading
        ('' when the side displays nothing readable — an honest ABSENT);
        the detail names exactly what happened (refuse-to-guess)."""
        detail: dict = {"extractor": EXTRACTOR_VERSION, "side": side_key}
        if not isinstance(wt_row, dict):
            detail["status"] = "no_wikitext"
            return "", detail
        side = wt_row.get(side_key)
        if not isinstance(side, dict) or not isinstance(side.get("content"), str):
            detail["status"] = "side_missing"
            errors = wt_row.get("fetch_errors")
            if isinstance(errors, list) and errors:
                detail["fetch_errors"] = list(errors)
            return "", detail
        detail["revid"] = side.get("revid")
        detail["rev_ts"] = side.get("ts")
        detail["sha1"] = side.get("sha1")
        ext = extract_death_fields(side["content"])
        detail["infobox_found"] = ext["infobox_found"]
        detail["template"] = ext["template_name"]
        if not ext["infobox_found"]:
            detail["status"] = "no_infobox"
            return "", detail
        field = ext["fields"].get(prop) or {}
        if not field.get("present"):
            detail["status"] = "field_absent"
            return "", detail
        detail["field"] = field.get("field")
        detail["raw_wikitext"] = field.get("raw")
        if field.get("blank"):
            detail["status"] = "field_blank"
            return "", detail
        cleaned = clean_death_value(prop, field.get("raw"))
        if cleaned["notes"]:
            detail["notes"] = list(cleaned["notes"])
        if not cleaned["ok"]:
            detail["status"] = "unreadable"
            detail["reason"] = cleaned["reason"]
            return "", detail
        detail["status"] = "extracted"
        if cleaned.get("precision"):
            detail["precision"] = cleaned["precision"]
        return cleaned["value"], detail

    # -- change-date resolution ---------------------------------------------

    @staticmethod
    def _resolve_change_date(info_iso, info_prec, wd_row, qid, wd_url, page_url,
                             cutoff_rev_ts, cur_rev_ts) -> ChangeDate:
        """The change date of every record of a person is the person's INFOBOX
        death date with its OWN precision (the ground truth for the value AND
        its dating — a corroborating P570 is recorded as evidence but NEVER
        overrides the infobox precision, so a second-source precision can
        never silently flip a temporal disposition):

        * infobox date present + ANY non-deprecated current P570 claim agrees
          at the coarser common precision (the SAME rank-aware ANY-match the
          corroboration gate applies — a deprecated claim can neither supply
          nor block corroboration) -> basis 'wd_event_corroborated' (kind
          wikidata_history, the agreeing claim recorded).
        * infobox date present, no credible P570 agreeing -> basis
          'infobox_value_date' (kind wikipedia_infobox; the corroboration gate
          separately reviews the disagreement).
        * no infobox death date -> basis 'revision_bracket': the change is
          provably inside (cutoff_rev_ts, cur_rev_ts]; the recorded value is a
          placeholder the temporal gate refuses (review)."""
        wd_dates = []
        if isinstance(wd_row, dict):
            current = wd_row.get("current")
            if isinstance(current, dict) and current.get("exists") and not current.get("is_redirect"):
                wd_dates = wd_dates_from_state(current, "P570")
        if isinstance(info_iso, str) and info_iso and info_prec in ("day", "month", "year"):
            agreeing = next(
                (d for d in wd_dates if _dates_agree(info_iso, info_prec, d[0], d[1])),
                None,
            )
            if agreeing is not None:
                return ChangeDate(
                    value=info_iso,
                    precision=info_prec,
                    evidence=Evidence(
                        kind="wikidata_history",
                        url=wd_url,
                        ref={
                            "basis": "wd_event_corroborated",
                            "property": "P570",
                            "change_date_source": "infobox_value",
                            "infobox_value": info_iso,
                            "infobox_precision": info_prec,
                            "wd_value": agreeing[0],
                            "wd_value_precision": agreeing[1],
                            "wd_precision": agreeing[2],
                            "wd_rank": agreeing[3]["rank"],
                        },
                        as_of=None,
                    ),
                )
            return ChangeDate(
                value=info_iso,
                precision=info_prec,
                evidence=Evidence(
                    kind="wikipedia_infobox",
                    url=page_url,
                    ref={
                        "basis": "infobox_value_date",
                        "source_field": "deathdate",
                        "wd_corroborated": False,
                    },
                    as_of=None,
                ),
            )
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
                        "no usable infobox death date; the change is provably inside "
                        "(cutoff_rev_ts, cur_rev_ts] — a placeholder the temporal gate refuses"
                    ),
                },
                as_of=None,
            ),
        )

    # -- gates ---------------------------------------------------------------

    def gate_list(self, cfg: dict):
        """Ordered gates (rationale in the module docstring). Loads the
        caches and the POLICY into cfg (the runner copies cfg into ctx and
        fingerprints cfg['policy'] into the manifest)."""
        self._require_offline(cfg)
        self._ensure_loaded(cfg)
        cfg[GARBAGE_RULES_CTX_KEY] = {}
        cfg[POLICY_CTX_KEY] = dict(self._policy(cfg))
        return [
            PeopleInfoboxDeathDateGate(),
            ValueParsedGate(),
            GarbageValueGate(rules_ctx_key=GARBAGE_RULES_CTX_KEY),
            PeopleValueChangedGate(),
            PeopleDeathChangeGate(),
            PeopleTemporalWindowGate(),
            PeopleCorroborationGate(),
            PeopleBodyProseGate(),
            PeopleRecognisabilityGate(),
            EvidenceResolvableGate(),
            DedupGate(),
        ]

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _require_offline(cfg: dict) -> None:
        if not cfg.get("offline", True):
            raise NotImplementedError(
                "the wiki_people adapter is offline-only: every input is a frozen "
                "death-candidate snapshot written by stage1.harvest --source wiki_people"
            )

    @staticmethod
    def _policy(cfg: dict) -> dict:
        """The active POLICY + thresholds (DEFAULT_POLICY, per-key overridable
        via cfg['people_policy_override']). Each flag validates against its
        own closed value set and each threshold must be a non-negative int —
        a typo never silently changes behavior."""
        policy = dict(DEFAULT_POLICY)
        override = cfg.get("people_policy_override")
        if isinstance(override, dict):
            if override.get("window_edge_ambiguous") in ("exclude", "review"):
                policy["window_edge_ambiguous"] = override["window_edge_ambiguous"]
            if override.get("single_sided_death") in ("admit", "review"):
                policy["single_sided_death"] = override["single_sided_death"]
            for key in ("body_prose_min_chars", "pre_death_monthly_views_min", "sitelinks_min"):
                value = override.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    policy[key] = value
        return policy

    @staticmethod
    def _data_path(cfg: dict, filename: str) -> Path:
        data_dir = cfg.get("data_dir")
        if data_dir is None:
            raise LookupError(
                f"the wiki_people adapter requires --data-dir (a death-candidate snapshot "
                f"containing {filename})"
            )
        path = Path(data_dir) / filename
        if not path.is_file():
            raise LookupError(f"wiki_people input file not found: {path}")
        return path

    def _ensure_loaded(self, cfg: dict) -> None:
        """Load the candidates/pageviews join tables and the two caches into
        cfg exactly once per run. Malformed content becomes input_load_errors
        (the manifest channel), never a silent skip and never a crash."""
        if CANDIDATES_CTX_KEY not in cfg:
            table, errors = self._load_keyed_jsonl(cfg.get("data_dir"), CANDIDATES_FILENAME)
            cfg[CANDIDATES_CTX_KEY] = table
            if errors:
                cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(errors)
        if PAGEVIEWS_CTX_KEY not in cfg:
            table, errors = self._load_keyed_jsonl(cfg.get("data_dir"), PAGEVIEWS_FILENAME)
            cfg[PAGEVIEWS_CTX_KEY] = table
            if errors:
                cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(errors)
        if WD_CACHE_CTX_KEY not in cfg:
            store, info, errors = self._load_cache(
                cfg.get("data_dir"), WD_CACHE_FILENAME, PACKAGE_WD_CACHE_ID
            )
            cfg[WD_CACHE_CTX_KEY] = store
            cfg[WD_CACHE_INFO_CTX_KEY] = info
            self._declare_extra_input(cfg, info)
            if errors:
                cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(errors)
            cfg.setdefault(EXTRA_INPUT_META_CTX_KEY, {})["wiki_people_policy"] = dict(self._policy(cfg))
        if WIKITEXT_CACHE_CTX_KEY not in cfg:
            store, info, errors = self._load_cache(
                cfg.get("data_dir"), WIKITEXT_CACHE_FILENAME, PACKAGE_WIKITEXT_CACHE_ID
            )
            cfg[WIKITEXT_CACHE_CTX_KEY] = store
            cfg[WIKITEXT_CACHE_INFO_CTX_KEY] = info
            self._declare_extra_input(cfg, info)
            if errors:
                cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(errors)

    @staticmethod
    def _declare_extra_input(cfg: dict, info: dict) -> None:
        """Declare a cache (location-independent id, sha1) to the runner's
        manifest fingerprint plus its sidecar retrieval metadata — the cache
        may live outside data_dir where the manifest walk cannot see it."""
        if not (info.get("file") and info.get("sha1")):
            return
        cfg.setdefault(EXTRA_INPUTS_CTX_KEY, {})[info["file"]] = info["sha1"]
        meta_entry = {"sha1": info["sha1"], "retrieval": info.get("meta")}
        meta = info.get("meta")
        if isinstance(meta, dict) and "cache_sha1" in meta:
            meta_entry["sidecar_matches_cache"] = meta.get("cache_sha1") == info["sha1"]
        cfg.setdefault(EXTRA_INPUT_META_CTX_KEY, {})[info["file"]] = meta_entry

    @staticmethod
    def _load_keyed_jsonl(data_dir, filename: str):
        """{row['title']: row} from a snapshot jsonl. Missing file -> empty
        table (downstream gates then review). Unreadable/duplicate lines are
        error entries; first row wins per title."""
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
                if not isinstance(row, dict) or not isinstance(row.get("title"), str) or not row["title"]:
                    errors.append({
                        "file": filename, "line": line_no,
                        "error": "row is not an object with a string title",
                    })
                    continue
                if row["title"] in table:
                    errors.append({
                        "file": filename, "line": line_no,
                        "error": f"duplicate title {row['title']!r} (first occurrence kept)",
                    })
                    continue
                table[row["title"]] = row
        return table, errors

    _SIDECAR_META_KEYS = (
        "tool_version",
        "decoder_version",
        "retrieved_at",
        "endpoint",
        "endpoints",
        "cutoff_ts",
        "property_whitelist",
        "qualifier_whitelist",
        "verified_file",
        "verified_sha1",
        "candidates_file",
        "candidates_sha1",
        "cache_sha1",
    )

    @classmethod
    def _load_cache_meta(cls, path: Path):
        """Retrieval metadata from a cache's .meta.json sidecar, or None. A
        malformed sidecar returns an error entry, never a raise — the sidecar
        is audit metadata, not a load-bearing input."""
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
    def _load_cache(cls, data_dir, filename: str, package_id: str):
        """(store, info, errors) for a title-keyed cache: {data_dir}/<file>
        when the snapshot vendors it (always, for a harvest snapshot), else
        the package default under stage1/cache/. info carries the
        location-independent identifier, sha1, sidecar metadata, and — for
        the WD cache — the stored property_whitelist (so the corroboration
        gate can assert the cache covers the death properties)."""
        store: dict = {}
        errors: list = []
        path, ident = None, None
        if data_dir is not None:
            local = Path(data_dir) / filename
            if local.is_file():
                path, ident = local, filename
        if path is None:
            default = DEFAULT_CACHE_DIR / filename
            if default.is_file():
                path, ident = default, package_id
        if path is None:
            return store, {"file": None, "sha1": None, "meta": None, "property_whitelist": None}, errors
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
                    errors.append({"file": filename, "line": line_no, "error": str(exc)})
                    continue
                if not isinstance(row, dict) or not isinstance(row.get("title"), str) or not row["title"]:
                    errors.append({
                        "file": filename, "line": line_no,
                        "error": "row is not an object with a string title",
                    })
                    continue
                if row["title"] in store:
                    errors.append({
                        "file": filename, "line": line_no,
                        "error": f"duplicate title {row['title']!r} (first occurrence kept)",
                    })
                    continue
                store[row["title"]] = row
        meta = cls._load_cache_meta(path)
        property_whitelist = None
        if isinstance(meta, dict) and isinstance(meta.get("property_whitelist"), list):
            property_whitelist = meta["property_whitelist"]
        info = {
            "file": ident,
            "sha1": digest.hexdigest(),
            "meta": meta,
            "property_whitelist": property_whitelist,
        }
        return store, info, errors


ADAPTER = WikiPeopleAdapter()
