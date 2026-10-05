"""People CONTROL adapter (source 'people_controls') — the second control lane.

RULING A7 (2026-07-31, verbatim): "people = Wikidata **sitelink-decile**
matched living persons". RULING A3 (2026-07-30): the block cells run on the
~50 LIVING control persons "whose current leads are present-tense AND true" —
the population floor that licenses interpreting the treatment-arm rates.
PRINCIPAL RULING (2026-08-05): the matching basis is the DRAWN 50 treatment
people (eval/data/draws/draws.json -> domains.people.taken); their sitelink
deciles are the target distribution. Claims invert for free ("is still alive"
TRUE for a living control; "died in {asof year}" FALSE) — claim building is
EVAL-SIDE ingest, so this release carries exactly what ingest needs: entity
display name, QID, sitelink count, the pinned current revision, and NO death
fields.

The pull job's derive half. The POOL is the harvester's seeded, decile-quota
selection over a bounded Category:Living people scan (see
stage1/harvest/people_controls.py for the discovery strategy and its
documented alphabetical-prefix bias). Every selected person is RE-VERIFIED
LIVING here, offline, from evidence the harvester froze at pull time:

* the CURRENT article revision (newest at/before the pull asof), re-read with
  the shared people_death extractor — an infobox death field or a death
  category is an article-side death signal; the article is the PRIMARY source
  (the recorded value is the ARTICLE reading, never a Wikidata value);
* the pull-date Wikidata state, death-scoped (DEATH_PROPERTY_WHITELIST) — a
  P570 (date of death) claim excludes: a person Wikidata attests dead is not
  verifiably living, whatever the article shows (principal ruling 2026-08-05:
  "P570 present -> excluded; ambiguous -> review");
* the CUTOFF-pinned revision — the knowability floor: a control must be a
  fact the model CAN know from training, so the article must predate the
  treatment cutoff (a page created after the cutoff fails, positively).

Inputs (all read-only, under --data-dir — a people_controls harvest snapshot):

* people_controls_selected.jsonl   — the selected pool: one row per person
  (title, qid, sitelinks, decile, selection_hash). Enumerated in full;
  enumeration never decides inclusion.
* people_controls_selection.meta.json — deterministic sidecar binding the
  selection to exact bytes: the eval draws.json sha1, the treatment release
  facts/manifest sha1s, seed, decile boundaries, target histogram, quotas,
  scan depth, and the treatment QID list (the overlap screen's basis).
* people_controls_wikitext.jsonl   — per selected title: the cutoff and
  current pinned revisions (full wikitext + revid/sha1/ts), the SAME row
  shape as the death snapshot's people_wikitext.jsonl ('cutoff'/'current'
  side dicts) so eval's zero-network held_sources jobs can read it verbatim.
* people_controls_wd.jsonl         — per selected person: the pull-date
  death-scoped Wikidata state (fetch_wikidata_people decode machinery).

Record shape: source 'people_controls', property 'living_status', value_type
'text_span'. ``before.raw`` is 'alive' (the person is a Category:Living people
member whose article existed at the cutoff; alive at pull implies alive at
cutoff). ``after.raw`` is the pull-date ARTICLE reading: 'alive' when the
pinned revision shows no death signal, 'deceased' when its infobox carries a
death field, '' when unverifiable (no revision / category-only ambiguity).
``change_date`` is the pull asof (basis 'control_no_change': a control is an
UNCHANGED fact; the date pins WHEN liveness was verified, and makes fact_id
stable per person and pull window). Every record carries
``provenance['population'] = 'control'`` (the sports_controls precedent).

Gate order (first fail names the disposition; every gate always runs):

1. ``control_pool``          (review-only) — the row is structurally a
   selected pool member with its selection binding. Held for a human
   otherwise, never silently included.
2. ``underdetermined_entity`` — the mononym screen, mirroring eval's ruling
   2026-08-02 (eval/src/orchestrate.py underdetermined_entity_excluded):
   a display name of <= 1 word or <= 4 chars once the parenthetical is
   stripped cannot carry an unambiguous entity; applied at pull time so we
   never ship an undrawable control. Fails (a property of the fact, true
   under any window). The harvester also screens at scan; this is defense
   in depth.
3. ``treatment_overlap``      — the QID appears in the treatment people
   release: fails (a treatment person can never be their own control; the
   treatment set is all deaths, so overlap would also mean 'dead').
4. ``control_anchor``         — the KNOWABILITY ANCHOR (principal ruling
   addendum 2026-08-05): the article's FIRST revision must be at/before the
   anchor date (default 2024-03-31, before the earliest roster model cutoff
   ~June 2024). Created after -> fail 'anchor_too_recent'; unfetchable ->
   review. Also applied at selection time with quota backfill, so anchored
   candidates fill the deciles.
5. ``precutoff_presence``     — the article must have a revision at/before
   the treatment cutoff. 'No revision at or before' is positive evidence
   (page created later) -> fail; a fetch error is review. (Nearly implied by
   the anchor, but on a DIFFERENT evidence basis — the pinned cutoff
   revision — so both are checked.)
6. ``living_verified``        — THE verification, run against BOTH pinned
   revisions and never shortcut by the role gate (third addendum
   2026-08-05: a person dead BEFORE the cutoff has a frozen, 'role-stable'
   article): P570 present -> FAIL 'p570_present'; a non-blank infobox death
   field on the current revision -> FAIL 'infobox_death_field'; on the
   cutoff-era revision -> FAIL 'infobox_death_field_at_cutoff'; a death
   category or a died/passed-away mention in the lead (either revision) ->
   review (weak signals — vandalism, a category-ahead-of-infobox edit, a
   relative's death — a human call); unfetchable revision / Wikidata state
   -> review. Only the all-clear passes, with the three-basis ledger
   recorded per record (provenance.living_verification).
7. ``control_role``           — role stability (principal's fuller spec,
   2026-08-05): the role predicate read from the cutoff-pinned lead and the
   current lead (same extractor both sides) must be substantively the same.
   A substitution is a role CHANGE -> fail (a treatment-class fact, not a
   control; both roles recorded); additive/undecidable edits and
   unextractable roles -> review ('What is X's role?' needs an answerable,
   stable role).
8. ``evidence_resolvable`` / 9. ``dedup`` (shared, last).

Offline-only (--online raises). Deterministic: same snapshot, byte-identical
facts.jsonl. The FULL verified pool ships in the release (no adapter-side
cap): the eval's seeded draw selects its ~50. NO LLM anywhere.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from pathlib import Path

from stage1.adapters import Adapter
from stage1.adapters.people_death import (
    _EXTLINK_BARE_RE,
    _EXTLINK_LABELED_RE,
    _QUOTES_RE,
    _TABLE_RE,
    _TAG_RE,
    _WIKILINK_REDUCE_RE,
    _strip_nonprose_links,
    _strip_top_level_templates,
    DEATH_PROPERTIES,
    EXTRACTOR_VERSION,
    extract_death_fields,
)
from stage1.adapters.wiki_people import wd_date_from_state
from stage1.gates import Gate
from stage1.gates.standard import DedupGate, EvidenceResolvableGate
from stage1.normalize import Comparison, get_comparator
from stage1.predictability import UNPREDICTABLE
from stage1.schema import (
    ChangeDate,
    Evidence,
    FactChangeRecord,
    GateResult,
    ValueState,
    compute_fact_id,
    compute_record_id,
)
from stage1.wikitext import strip_comments, strip_refs

SOURCE = "people_controls"
PROPERTY = "living_status"
VALUE_TYPE = "text_span"
ENTITY_ID_KEY = "wikidata_qid"
POPULATION = "control"

ALIVE_VALUE = "alive"
DECEASED_VALUE = "deceased"

# THE KNOWABILITY ANCHOR (principal ruling addendum, 2026-08-05): every
# control fact must already have held BEFORE the earliest roster model cutoff
# (~June 2024). For a living control the anchored requirement is that the
# model can know the PERSON at all, evidenced by the article's FIRST enwiki
# revision: created at/before this date passes; created after is excluded
# (a person whose article only appeared later cannot be "still alive" to a
# model that has never heard of them). Overridable via --opt anchor_date=…,
# recorded in the selection sidecar and every record's evidence.
DEFAULT_ANCHOR_DATE = "2024-03-31"
ANCHOR_BASIS = "enwiki_first_revision"

# The fixed snapshot filenames this adapter reads by name (written by
# stage1/harvest/people_controls.py).
SELECTED_FILENAME = "people_controls_selected.jsonl"
SELECTION_SIDECAR_FILENAME = "people_controls_selection.meta.json"
WIKITEXT_CACHE_FILENAME = "people_controls_wikitext.jsonl"
WIKITEXT_SIDECAR_FILENAME = "people_controls_wikitext.meta.json"
WD_CACHE_FILENAME = "people_controls_wd.jsonl"
WD_SIDECAR_FILENAME = "people_controls_wd.meta.json"
# Written by the harvester as the scan audit trail; sha1-pinned in the
# snapshot manifest but never read by this adapter.
SCAN_FILENAME = "people_controls_scan.jsonl"

SELECTION_CTX_KEY = "people_controls_selection_meta"
WIKITEXT_CTX_KEY = "people_controls_wikitext_by_title"
WIKITEXT_INFO_CTX_KEY = "people_controls_wikitext_info"
WD_CACHE_CTX_KEY = "people_controls_wd_by_qid"
WD_CACHE_INFO_CTX_KEY = "people_controls_wd_info"
LOAD_ERRORS_CTX_KEY = "input_load_errors"

# ---------------------------------------------------------------------------
# The mononym screen — a MIRROR of eval/src/orchestrate.py's
# underdetermined_entity_excluded (human ruling 2026-08-02). stage1 must not
# import eval code, so the criterion is restated here with its source named;
# if eval's rule ever changes, this mirror must change with it.
# ---------------------------------------------------------------------------
MONONYM_MAX_CHARS = 4  # eval/src/orchestrate.py MONONYM_MAX_CHARS

_PARENTHETICAL_RE = re.compile(r"\s*\([^()]*\)\s*$")


def display_name(title) -> str:
    """The natural display name eval's ingest will build: the enwiki title
    with the trailing parenthetical disambiguator dropped
    ('PSD (rapper)' -> 'PSD'). Pure and total ('' for non-strings)."""
    if not isinstance(title, str):
        return ""
    return _PARENTHETICAL_RE.sub("", title).strip()


def underdetermined_entity(title) -> bool:
    """True when the display name is a mononym / too short to carry an
    unambiguous entity — eval's exact criterion (<= 1 word or <= 4 chars),
    applied at pull time so no undrawable control is ever shipped."""
    name = display_name(title)
    return len(name.split()) <= 1 or len(name) <= MONONYM_MAX_CHARS


# ---------------------------------------------------------------------------
# Sitelink-decile machinery (pure; shared with the harvester — the single
# source of truth for boundary computation, assignment, and seeded selection)
# ---------------------------------------------------------------------------

def decile_boundaries(counts) -> list:
    """The 9 decile upper bounds of the drawn-50's sitelink counts, by
    nearest-rank (bound_k = sorted[ceil(k/10 * n) - 1], k = 1..9 — the same
    deterministic no-interpolation rule as the people harvester's
    _percentiles). Ties may collapse deciles (two equal bounds leave the
    later decile empty); that is recorded honestly, never smoothed.
    Raises ValueError on an empty/non-integer basis (the harvester turns it
    into a loud LookupError)."""
    import math

    known = sorted(v for v in counts
                   if isinstance(v, int) and not isinstance(v, bool))
    if not known:
        raise ValueError("decile_boundaries: no integer sitelink counts")
    n = len(known)
    return [known[max(1, math.ceil(k / 10 * n)) - 1] for k in range(1, 10)]


def decile_of(value, boundaries) -> int:
    """The decile (1..10) of one sitelink count: the smallest k with
    value <= boundaries[k-1], else 10. With tied boundaries the FIRST decile
    of a tie run takes every member (later tied deciles stay empty) — the
    same rule for the target histogram and for candidate assignment, so the
    two can never disagree."""
    for k, bound in enumerate(boundaries, 1):
        if value <= bound:
            return k
    return 10


def decile_histogram(counts, boundaries) -> list:
    """[count_decile_1, ..., count_decile_10] of the given sitelink counts
    under the given boundaries."""
    hist = [0] * 10
    for value in counts:
        hist[decile_of(value, boundaries) - 1] += 1
    return hist


def decile_quotas(target_hist, headroom) -> list:
    """The per-decile selection quotas: ceil(target * headroom), so the
    selected pool overshoots the 50-person target histogram and the eval draw
    has headroom for verification losses. A zero target stays zero."""
    import math

    return [int(math.ceil(t * headroom)) if t > 0 else 0 for t in target_hist]


def selection_key(seed, title) -> str:
    """The deterministic selection hash — sha256(f'{seed}:{title}'), the
    eval draw's exact seeded-key construction (eval/src/orchestrate.py
    seeded_key), lowest-first at selection."""
    return hashlib.sha256(f"{seed}:{title}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Liveness signals (pure)
# ---------------------------------------------------------------------------

# Category links whose name contains 'deaths' ('2026 deaths', 'Deaths from
# cancer in ...'). A SCREEN, not a classifier: a hit is an article-side death
# SIGNAL that (without an infobox death field) routes to review, never to a
# silent include or exclude.
_DEATH_CATEGORY_RE = re.compile(
    r"\[\[\s*category\s*:\s*([^\]|#]*deaths[^\]|#]*)", re.IGNORECASE)
_LIVING_CATEGORY_RE = re.compile(
    r"\[\[\s*category\s*:\s*living\s+people\s*[\]|]", re.IGNORECASE)
# Death statements in the LEAD's first paragraph ('died', 'passed away'). A
# deliberately WEAK-tier signal (third addendum, 2026-08-05: 'death-in-lead'):
# the lead can mention a relative's death, so a hit routes to review — never a
# silent include, never an automatic exclude.
_LEAD_DEATH_RE = re.compile(r"\b(died|passed away)\b", re.IGNORECASE)


def article_death_signals(content) -> dict:
    """Every article-side death signal of one pinned revision's wikitext,
    read with the SAME versioned people_death extractor the treatment uses.
    Pure and total; a non-string yields the all-absent shape.

    Returns {'infobox_found', 'template', 'death_fields': {prop: bool
    (present AND non-blank)}, 'infobox_death': bool, 'death_categories':
    [names], 'lead_death_mentions': [terms], 'has_living_category': bool,
    'extractor': version}."""
    ext = extract_death_fields(content)
    fields = {}
    for prop in DEATH_PROPERTIES:
        field = ext["fields"][prop]
        fields[prop] = bool(field["present"] and not field["blank"])
    text = strip_comments(content) if isinstance(content, str) else ""
    categories = sorted({" ".join(m.split()) for m in _DEATH_CATEGORY_RE.findall(text)})
    lead = lead_prose(content)
    first_paragraph = lead.split("\n\n")[0] if lead else ""
    mentions = sorted({m.lower() for m in _LEAD_DEATH_RE.findall(first_paragraph)})
    return {
        "extractor": EXTRACTOR_VERSION,
        "infobox_found": ext["infobox_found"],
        "template": ext["template_name"],
        "death_fields": fields,
        "infobox_death": any(fields.values()),
        "death_categories": categories,
        "lead_death_mentions": mentions,
        "has_living_category": bool(_LIVING_CATEGORY_RE.search(text)),
    }


def wd_p570_signal(state) -> dict:
    """The pull-date Wikidata death signal of one death-scoped STATE_BLOCK:
    {'available': bool, 'reason': str | None, 'p570': {'iso', 'precision',
    'rank'} | None, 'statements': int}. Deprecated-rank P570 claims are
    ignored (wd_date_from_state — Wikidata's this-value-is-wrong marker must
    not kill a control). Pure and total."""
    if not isinstance(state, dict):
        return {"available": False, "reason": "state_missing",
                "p570": None, "statements": 0}
    if state.get("is_redirect"):
        return {"available": False,
                "reason": f"entity_redirect:{state.get('redirect_to')}",
                "p570": None, "statements": 0}
    if state.get("exists") is False:
        return {"available": False, "reason": "entity_missing",
                "p570": None, "statements": 0}
    claims = state.get("claims") if isinstance(state.get("claims"), dict) else {}
    statements = claims.get("P570")
    n = len(statements) if isinstance(statements, list) else 0
    best = wd_date_from_state(state, "P570")
    p570 = None
    if best is not None:
        p570 = {"iso": best[0], "precision": best[1], "rank": best[3].get("rank")}
    return {"available": True, "reason": None, "p570": p570, "statements": n}


# ---------------------------------------------------------------------------
# Role stability (principal's fuller people-control spec, 2026-08-05):
# "the control is a living person of matched prominence whose ROLE is
# unchanged across the window: 'What is X's role?' is answerable and stable."
# The lead prose extraction below is the people_lead:v1 COMPOSITION from
# eval/src/held_sources.py, built from the SAME imported stage1 primitives
# (people_death's stripping regexes + stage1.wikitext) — stage1 must not
# import eval code (the dependency runs eval -> stage1), so the composition
# is mirrored here with its source named; if held_sources' pipeline changes,
# this mirror must change with it.
# ---------------------------------------------------------------------------
ROLE_EXTRACTOR_VERSION = "people_controls_role:v1"

# The copular role predicate of the lead's opening: "X … is a Kenyan
# long-distance runner", "X remains the bishop of Y". A SCREEN, not a parser:
# the same crude rule reads BOTH pinned revisions, so what the control_role
# gate compares is like-for-like; anything it cannot read is review, never a
# silent include.
_ROLE_RE = re.compile(
    r"\b(?:is|has been|remains|serves as|continues to serve as)\s+"
    r"(?:an?\s+|the\s+)?([^.;\n]{2,160}?)(?=\s*(?:[.;\n]|$))"
)


def lead_prose(content) -> str:
    """The article LEAD as plain prose: strip comments/refs/templates/tables/
    nonprose links, cut at the first section heading, reduce links to labels,
    drop markup residue, collapse whitespace (paragraph breaks preserved).
    Pure and total ('' for non-strings)."""
    if not isinstance(content, str) or not content.strip():
        return ""
    text = strip_refs(strip_comments(content))
    text = _strip_top_level_templates(text)
    for _ in range(4):
        stripped = _TABLE_RE.sub(" ", text)
        if stripped == text:
            break
        text = stripped
    text = _strip_nonprose_links(text)
    cut = text.find("\n==")
    if cut != -1:
        text = text[:cut]
    text = _WIKILINK_REDUCE_RE.sub(r"\1", text)
    text = _EXTLINK_LABELED_RE.sub(r"\1", text)
    text = _EXTLINK_BARE_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = _QUOTES_RE.sub("", text)
    paragraphs = []
    for block in text.split("\n\n"):
        clean = " ".join(re.sub(r"[\[\]{}|=]", " ", block).split())
        if clean:
            paragraphs.append(clean)
    return "\n\n".join(paragraphs)


def extract_role(content) -> dict:
    """The role predicate of one pinned revision's lead. Returns
    {'ok', 'role', 'reason', 'extractor'} — ok=True with the cleaned role
    phrase, or ok=False with a machine reason ('no_revision_content',
    'no_lead_prose', 'no_role_predicate'). Pure and total."""
    out = {"ok": False, "role": None, "reason": None,
           "extractor": ROLE_EXTRACTOR_VERSION}
    if not isinstance(content, str) or not content.strip():
        out["reason"] = "no_revision_content"
        return out
    lead = lead_prose(content)
    if not lead:
        out["reason"] = "no_lead_prose"
        return out
    first_paragraph = lead.split("\n\n")[0]
    match = _ROLE_RE.search(first_paragraph)
    if not match:
        out["reason"] = "no_role_predicate"
        return out
    role = " ".join(match.group(1).split()).strip(" ,")
    if not role:
        out["reason"] = "no_role_predicate"
        return out
    out["ok"] = True
    out["role"] = role
    return out


def compare_roles(role_at_cutoff, role_current) -> dict:
    """'same' | 'changed' | 'undecided' for two extracted role phrases, via
    the shared text_span comparator: folded-equal -> same (stable role);
    substitution/reordering -> changed (a role change is a treatment-class
    fact, not a control); additive edits or unparseable -> undecided (a
    copyedit and a promotion can look alike — a human call). Pure, total,
    JSON-safe."""
    comparator = get_comparator(VALUE_TYPE)
    out = {"verdict": "undecided", "reason": None,
           "comparator_version": comparator.VERSION}
    a = comparator.parse(role_at_cutoff) if isinstance(role_at_cutoff, str) else None
    b = comparator.parse(role_current) if isinstance(role_current, str) else None
    if a is None or not a.ok or b is None or not b.ok:
        out["reason"] = "role_unparseable"
        return out
    comparison = comparator.compare(a.canonical, b.canonical)
    if not isinstance(comparison, Comparison):
        out["reason"] = "comparator_contract_violation"
        return out
    if comparison.verdict == "equal":
        out["verdict"] = "same"
    elif comparison.verdict == "different":
        out["verdict"] = "changed"
        out["reason"] = comparison.reason
    else:
        out["reason"] = comparison.reason or comparison.verdict
    return out


# ---------------------------------------------------------------------------
# Selected-row readers (pure, total)
# ---------------------------------------------------------------------------

def selected_title(row):
    title = row.get("title") if isinstance(row, dict) else None
    return title if isinstance(title, str) and title else None


def selected_qid(row):
    qid = row.get("qid") if isinstance(row, dict) else None
    return qid if isinstance(qid, str) and qid else None


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

class PeopleControlPoolGate(Gate):
    """Review-only structural check that the enumerated row IS a selected
    pool member: a title, a QID, an integer sitelink count, a decile in
    1..10, and the selection binding (draws + treatment release sha1s) in the
    before-side evidence. Anything else is held for a human — it can never be
    included, and never silently dropped."""

    name = "control_pool"
    version = "control_pool:people_controls_v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        ref = record.before.evidence.ref if isinstance(record.before.evidence.ref, dict) else {}
        selection = ref.get("selection") if isinstance(ref.get("selection"), dict) else {}
        entity_ids = record.entity.get("ids") if isinstance(record.entity, dict) else None
        qid = entity_ids.get(ENTITY_ID_KEY) if isinstance(entity_ids, dict) else None
        sitelinks = selection.get("sitelinks")
        decile = selection.get("decile")
        problems = []
        if not qid:
            problems.append("selected row has no Wikidata QID")
        if not (isinstance(sitelinks, int) and not isinstance(sitelinks, bool)):
            problems.append(f"sitelink count is {sitelinks!r}, expected an int")
        if not (isinstance(decile, int) and 1 <= decile <= 10):
            problems.append(f"decile is {decile!r}, expected 1..10")
        if not selection.get("draws_sha1"):
            problems.append("no selection binding: eval draws.json sha1 is missing")
        if not selection.get("treatment_facts_sha1"):
            problems.append("no selection binding: treatment release facts sha1 is missing")
        evidence = {
            "qid": qid,
            "sitelinks": sitelinks,
            "decile": decile,
            "seed": selection.get("seed"),
        }
        if problems:
            evidence["problem"] = "; ".join(problems)
            return GateResult(name=self.name, version=self.version,
                              verdict="review", evidence=evidence)
        return GateResult(name=self.name, version=self.version,
                          verdict="pass", evidence=evidence)


class UnderdeterminedEntityGate(Gate):
    """The mononym screen, mirroring eval's underdetermined_entity exclusion
    (ruled 2026-08-02): a display name of <= 1 word or <= MONONYM_MAX_CHARS
    chars once the parenthetical is stripped cannot carry an unambiguous
    entity in a rendered question, so the control would be undrawable. FAIL —
    a property of the fact, true under any window. The harvester applies the
    same screen at scan time; this gate is defense in depth."""

    name = "underdetermined_entity"
    version = "underdetermined_entity:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        title = record.entity.get("name") if isinstance(record.entity, dict) else None
        name = display_name(title)
        evidence = {
            "title": title,
            "display_name": name,
            "criterion": f"<= 1 word or <= {MONONYM_MAX_CHARS} chars "
                         "(mirror of eval/src/orchestrate.py, ruled 2026-08-02)",
        }
        if underdetermined_entity(title):
            evidence["problem"] = (
                f"display name {name!r} is a mononym/too short: the entity is "
                "under-determined once the disambiguator is stripped — an "
                "undrawable control (eval would exclude it at draw time)"
            )
            return GateResult(name=self.name, version=self.version,
                              verdict="fail", evidence=evidence)
        return GateResult(name=self.name, version=self.version,
                          verdict="pass", evidence=evidence)


class TreatmentOverlapGate(Gate):
    """FAIL any person whose QID appears in the treatment people release: a
    treatment person can never be their own control (and the treatment set is
    deaths, so an overlap would also mean the person is dead). The QID list
    comes from the selection sidecar (frozen at harvest); an absent list or
    an absent QID is review — the overlap cannot be verified."""

    name = "treatment_overlap"
    version = "treatment_overlap:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        meta = ctx.get(SELECTION_CTX_KEY)
        qids = meta.get("treatment_qids") if isinstance(meta, dict) else None
        entity_ids = record.entity.get("ids") if isinstance(record.entity, dict) else None
        qid = entity_ids.get(ENTITY_ID_KEY) if isinstance(entity_ids, dict) else None
        evidence = {"qid": qid}
        if not isinstance(qids, list):
            evidence["problem"] = ("selection sidecar carries no treatment_qids "
                                   "list: the overlap screen cannot be verified")
            return GateResult(name=self.name, version=self.version,
                              verdict="review", evidence=evidence)
        evidence["treatment_qids_n"] = len(qids)
        if not qid:
            evidence["problem"] = ("record has no Wikidata QID, so treatment "
                                   "overlap cannot be checked")
            return GateResult(name=self.name, version=self.version,
                              verdict="review", evidence=evidence)
        if qid in set(qids):
            evidence["problem"] = (
                f"{qid} is IN the treatment people release: a treatment person "
                "cannot be a control (and the treatment set is deaths)"
            )
            return GateResult(name=self.name, version=self.version,
                              verdict="fail", evidence=evidence)
        return GateResult(name=self.name, version=self.version,
                          verdict="pass", evidence=evidence)


class ControlAnchorGate(Gate):
    """The KNOWABILITY ANCHOR (principal ruling addendum 2026-08-05): the
    article's FIRST enwiki revision must be at/before the anchor date
    (default 2024-03-31 — before the earliest roster model cutoff ~June
    2024). Created after -> FAIL, reason 'anchor_too_recent' (the framework
    derives the disposition as 'excluded:<gate name>', so the row lands as
    'excluded:control_anchor' with this reason in its evidence); an
    unfetchable first revision -> review. The anchor date comes from the
    selection sidecar (frozen at harvest), falling back to the ruling
    default."""

    name = "control_anchor"
    version = "control_anchor:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        ref = record.before.evidence.ref if isinstance(record.before.evidence.ref, dict) else {}
        anchor = ref.get("anchor") if isinstance(ref.get("anchor"), dict) else {}
        meta = ctx.get(SELECTION_CTX_KEY)
        anchor_date = None
        if isinstance(meta, dict) and isinstance(meta.get("anchor_date"), str):
            anchor_date = meta["anchor_date"]
        if not anchor_date:
            anchor_date = anchor.get("anchor_date") if isinstance(anchor.get("anchor_date"), str) else None
        if not anchor_date:
            anchor_date = DEFAULT_ANCHOR_DATE
        first = anchor.get("first_rev_ts")
        evidence = {
            "basis": ANCHOR_BASIS,
            "anchor_date": anchor_date,
            "first_rev_ts": first,
            "first_revid": anchor.get("first_revid"),
        }
        if isinstance(first, str) and first:
            if first[:10] <= anchor_date:
                return GateResult(name=self.name, version=self.version,
                                  verdict="pass", evidence=evidence)
            evidence["reason"] = "anchor_too_recent"
            evidence["problem"] = (
                f"the article's first revision {first} is AFTER the knowability "
                f"anchor {anchor_date} (before the earliest roster model cutoff "
                "~June 2024): a model that has never seen the person cannot hold "
                "the control fact — not a usable control"
            )
            return GateResult(name=self.name, version=self.version,
                              verdict="fail", evidence=evidence)
        error = anchor.get("error")
        if error is not None:
            evidence["fetch_error"] = error
        evidence["reason"] = "anchor_unavailable"
        evidence["problem"] = (
            "the article's first-revision timestamp is unavailable: the "
            "knowability anchor cannot be verified"
        )
        return GateResult(name=self.name, version=self.version,
                          verdict="review", evidence=evidence)


class PrecutoffPresenceGate(Gate):
    """The knowability floor: a control must be a fact the model CAN know
    from training (A3 — controls are the difficulty floor), so the article
    must have a revision at/before the treatment cutoff. fetch_side's 'no
    revision at or before' outcome is POSITIVE evidence the page was created
    later -> fail; any other fetch problem is review."""

    name = "precutoff_presence"
    version = "precutoff_presence:v1"

    _CREATED_LATER = "no revision at or before"

    def evaluate(self, record, ctx: dict) -> GateResult:
        ref = record.before.evidence.ref if isinstance(record.before.evidence.ref, dict) else {}
        rev_ts = ref.get("revision_timestamp")
        error = ref.get("fetch_error")
        evidence = {"cutoff_revision_timestamp": rev_ts}
        if isinstance(rev_ts, str) and rev_ts:
            return GateResult(name=self.name, version=self.version,
                              verdict="pass", evidence=evidence)
        if isinstance(error, str) and self._CREATED_LATER in error:
            evidence["fetch_error"] = error
            evidence["problem"] = (
                "the article has NO revision at/before the treatment cutoff "
                "(created later): the person was not knowable from training "
                "data, so the control fails the difficulty floor"
            )
            return GateResult(name=self.name, version=self.version,
                              verdict="fail", evidence=evidence)
        if error is not None:
            evidence["fetch_error"] = error
        evidence["problem"] = (
            "the cutoff-pinned revision is unavailable (fetch problem): "
            "pre-cutoff presence cannot be verified"
        )
        return GateResult(name=self.name, version=self.version,
                          verdict="review", evidence=evidence)


class LivingVerifiedGate(Gate):
    """THE control verification (principal rulings 2026-08-05: 'P570 present
    -> excluded; ambiguous -> review', and the third addendum: the checks run
    against BOTH pinned revisions, independent of — never shortcut by — the
    role gate, because a person who died BEFORE the cutoff has a perfectly
    'stable role' across a frozen article pair). Three bases, all from
    already-frozen artifacts:

      (1) the pull-date Wikidata state — P570 present -> FAIL 'p570_present';
      (2) the CURRENT revision — non-blank infobox death field -> FAIL
          'infobox_death_field';
      (3) the CUTOFF-ERA revision — same article checks; a non-blank infobox
          death field there -> FAIL 'infobox_death_field_at_cutoff' (the
          frozen-article edge: dead before the cutoff).

    WEAK signals on either revision (a death category, a died/passed-away
    mention in the lead's first paragraph) are ambiguous — vandalism, a
    category-ahead-of-infobox edit, a relative's death — and route to review
    ('article_death_category' / 'lead_death_mention', the side named), never
    included. Unverifiable states are review: 'current_revision_unavailable',
    'wikidata_state_unavailable', 'cutoff_revision_unavailable'. Only the
    all-clear passes ('verified_living'), with the three-basis ledger in its
    evidence. (Framework note: the disposition derives as
    'excluded:living_verified' — the fired signal is in evidence.reason.)
    """

    name = "living_verified"
    version = "living_verified:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        after_ref = record.after.evidence.ref if isinstance(record.after.evidence.ref, dict) else {}
        before_ref = record.before.evidence.ref if isinstance(record.before.evidence.ref, dict) else {}
        current = after_ref.get("death_signals")
        cutoff = before_ref.get("death_signals")
        p570 = after_ref.get("wikidata_p570")
        evidence: dict = {
            "basis": ["wikidata_p570", "article_current_revision",
                      "article_cutoff_revision"],
        }

        def verdict(v: str, reason: str, problem=None) -> GateResult:
            evidence["reason"] = reason
            if problem:
                evidence["problem"] = problem
            return GateResult(name=self.name, version=self.version,
                              verdict=v, evidence=evidence)

        p570 = p570 if isinstance(p570, dict) else {"available": False,
                                                    "reason": "signal_missing",
                                                    "p570": None, "statements": 0}
        evidence["wikidata_p570"] = p570
        current = current if isinstance(current, dict) else None
        cutoff = cutoff if isinstance(cutoff, dict) else None
        if current is not None:
            evidence["death_signals"] = current
        if cutoff is not None:
            evidence["death_signals_at_cutoff"] = cutoff

        # Positive death attestations first: any source attesting a death on
        # EITHER revision is disqualifying, whatever the others show.
        if p570.get("available") and p570.get("p570"):
            return verdict(
                "fail", "p570_present",
                f"Wikidata carries a P570 date of death ({p570['p570'].get('iso')}): "
                "the person is not verifiably living — excluded (ruling 2026-08-05)",
            )
        if current is None:
            fetch_errors = after_ref.get("fetch_errors")
            if fetch_errors:
                evidence["fetch_errors"] = fetch_errors
            return verdict(
                "review", "current_revision_unavailable",
                "no pull-date revision: liveness cannot be verified",
            )
        if current.get("infobox_death"):
            present = sorted(k for k, v in (current.get("death_fields") or {}).items() if v)
            return verdict(
                "fail", "infobox_death_field",
                f"the pull-date infobox carries death field(s) {present}: the "
                "article attests a death — not a living control",
            )
        if cutoff is not None and cutoff.get("infobox_death"):
            present = sorted(k for k, v in (cutoff.get("death_fields") or {}).items() if v)
            return verdict(
                "fail", "infobox_death_field_at_cutoff",
                f"the CUTOFF-era infobox carries death field(s) {present}: the "
                "person was already dead before the cutoff (a frozen article "
                "has a 'stable role' too) — not a living control",
            )

        weak = []
        for side_name, side in (("current", current), ("cutoff", cutoff)):
            if side is None:
                continue
            if side.get("death_categories"):
                weak.append(("article_death_category", side_name,
                             side["death_categories"]))
            if side.get("lead_death_mentions"):
                weak.append(("lead_death_mention", side_name,
                             side["lead_death_mentions"]))
        if weak:
            evidence["weak_signals"] = [
                {"signal": s, "revision": side_name, "detail": detail}
                for s, side_name, detail in weak]
            reason = weak[0][0]
            return verdict(
                "review", reason,
                "ambiguous article death signal(s) "
                + "; ".join(f"{s} on the {side_name} revision ({detail})"
                            for s, side_name, detail in weak)
                + ": vandalism, a category-ahead-of-infobox edit, or a "
                "relative's death — a human call, never included",
            )
        if not p570.get("available"):
            return verdict(
                "review", "wikidata_state_unavailable",
                f"the pull-date Wikidata state is unavailable "
                f"({p570.get('reason')}): the no-P570 condition cannot be verified",
            )
        if cutoff is None:
            return verdict(
                "review", "cutoff_revision_unavailable",
                "no cutoff-era revision: liveness at the cutoff cannot be "
                "verified against the article pair",
            )
        return verdict("pass", "verified_living")


class ControlRoleGate(Gate):
    """Role stability (principal's fuller spec, 2026-08-05): the person's
    role predicate, read from the CUTOFF-pinned lead and the CURRENT lead
    with the same extractor, must be substantively the same —

    * both extracted, folded-equal            -> pass ('What is X's role?'
      is answerable and stable; 'X continues to serve as {role}' is a true
      block line for free);
    * both extracted, substitution/reordering -> FAIL 'role_changed' (a
      professor who became a vice-chancellor is a treatment-class fact, not
      a control — both roles recorded);
    * additive edits / comparator undecided   -> review (a copyedit and a
      promotion can look alike — a human call);
    * either side unextractable               -> review 'role_unextractable'
      (the recall cell needs an answerable role; unverifiable is never
      included).
    """

    name = "control_role"
    version = "control_role:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        before_ref = record.before.evidence.ref if isinstance(record.before.evidence.ref, dict) else {}
        after_ref = record.after.evidence.ref if isinstance(record.after.evidence.ref, dict) else {}
        cutoff_role = before_ref.get("role_extraction")
        current_role = after_ref.get("role_extraction")
        cutoff_role = cutoff_role if isinstance(cutoff_role, dict) else {"ok": False, "reason": "missing"}
        current_role = current_role if isinstance(current_role, dict) else {"ok": False, "reason": "missing"}
        evidence: dict = {
            "extractor": ROLE_EXTRACTOR_VERSION,
            "role_at_cutoff": cutoff_role.get("role"),
            "role_current": current_role.get("role"),
        }
        problems = []
        if not cutoff_role.get("ok"):
            problems.append(f"cutoff lead: {cutoff_role.get('reason')}")
        if not current_role.get("ok"):
            problems.append(f"current lead: {current_role.get('reason')}")
        if problems:
            evidence["reason"] = "role_unextractable"
            evidence["problem"] = (
                "no role predicate could be read from "
                + " and ".join(problems)
                + ": 'What is X's role?' needs an answerable, stable role — "
                "unverifiable is never included"
            )
            return GateResult(name=self.name, version=self.version,
                              verdict="review", evidence=evidence)
        comparison = compare_roles(cutoff_role.get("role"), current_role.get("role"))
        evidence["comparison"] = comparison
        if comparison["verdict"] == "same":
            return GateResult(name=self.name, version=self.version,
                              verdict="pass", evidence=evidence)
        if comparison["verdict"] == "changed":
            evidence["reason"] = "role_changed"
            evidence["problem"] = (
                f"the role changed across the window ({cutoff_role.get('role')!r} "
                f"-> {current_role.get('role')!r}): a role change is a "
                "treatment-class fact, not a control"
            )
            return GateResult(name=self.name, version=self.version,
                              verdict="fail", evidence=evidence)
        evidence["reason"] = "role_comparison_undecided"
        evidence["problem"] = (
            f"role comparison of {cutoff_role.get('role')!r} vs "
            f"{current_role.get('role')!r} is undecided "
            f"({comparison.get('reason')}): only a human can call it"
        )
        return GateResult(name=self.name, version=self.version,
                          verdict="review", evidence=evidence)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class PeopleControlsAdapter(Adapter):
    """Adapter for source 'people_controls'. Stateless; caches load once per
    run into cfg (the sanctioned runner channel)."""

    source = SOURCE

    # -- enumeration --------------------------------------------------------

    def enumerate_candidates(self, cfg: dict):
        """Yield every row of {data_dir}/people_controls_selected.jsonl
        sorted by (title, line). The file IS the pool; a structurally invalid
        row still becomes a record held by the control_pool gate —
        enumeration never decides inclusion. Unparseable lines are yielded as
        error candidates so build_record raises them into build_errors."""
        self._require_offline(cfg)
        path = self._data_path(cfg, SELECTED_FILENAME)
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
                candidates.append({"_line": line_no, "selected": row})
        candidates.sort(key=lambda c: (selected_title(c.get("selected")) or "", c["_line"]))
        yield from candidates

    # -- record building ----------------------------------------------------

    def build_record(self, candidate: dict, cfg: dict) -> FactChangeRecord:
        if "_parse_error" in candidate:
            raise ValueError(
                f"{SELECTED_FILENAME} line {candidate.get('_line')}: unparseable JSON "
                f"({candidate['_parse_error']})"
            )
        row = candidate.get("selected")
        title = selected_title(row)
        if title is None:
            raise ValueError(
                f"{SELECTED_FILENAME} line {candidate.get('_line')}: missing title"
            )
        self._ensure_loaded(cfg)

        qid = selected_qid(row)
        sitelinks = row.get("sitelinks")
        decile = row.get("decile")
        meta = cfg.get(SELECTION_CTX_KEY) or {}
        asof_iso = self._asof_iso(cfg)

        page_url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(
            title.replace(" ", "_")
        )

        # ---- pinned revisions (cutoff presence + pull-date verification) ---
        wt_row = (cfg.get(WIKITEXT_CTX_KEY) or {}).get(title)
        cutoff_side = None
        current_side = None
        fetch_errors = []
        if isinstance(wt_row, dict):
            fetch_errors = [e for e in (wt_row.get("fetch_errors") or [])
                            if isinstance(e, str)]
            if isinstance(wt_row.get("cutoff"), dict):
                cutoff_side = wt_row["cutoff"]
            if isinstance(wt_row.get("current"), dict):
                current_side = wt_row["current"]
        cutoff_error = next((e[len("cutoff: "):] for e in fetch_errors
                             if e.startswith("cutoff: ")), None)
        current_error = next((e[len("current: "):] for e in fetch_errors
                              if e.startswith("current: ")), None)
        if wt_row is None:
            cutoff_error = cutoff_error or "title not in wikitext cache"
            current_error = current_error or "title not in wikitext cache"

        signals = None
        if current_side is not None and isinstance(current_side.get("content"), str):
            signals = article_death_signals(current_side["content"])
        # The cutoff-era revision runs the SAME article checks (third
        # addendum 2026-08-05: a person dead before the cutoff has a frozen,
        # 'role-stable' article — death evidence in EITHER revision
        # disqualifies).
        signals_cutoff = None
        if cutoff_side is not None and isinstance(cutoff_side.get("content"), str):
            signals_cutoff = article_death_signals(cutoff_side["content"])
        current_ts = current_side.get("ts") if isinstance(current_side, dict) else None
        cutoff_ts = cutoff_side.get("ts") if isinstance(cutoff_side, dict) else None

        # ---- role stability: the SAME extractor over both pinned leads -----
        role_current = extract_role(
            current_side.get("content") if isinstance(current_side, dict) else None)
        role_cutoff = extract_role(
            cutoff_side.get("content") if isinstance(cutoff_side, dict) else None)

        # ---- pull-date Wikidata death-scoped state -------------------------
        wd_row = (cfg.get(WD_CACHE_CTX_KEY) or {}).get(qid) if qid else None
        state = wd_row.get("current") if isinstance(wd_row, dict) else None
        p570 = wd_p570_signal(state)
        if not isinstance(wd_row, dict) and p570.get("reason") == "state_missing":
            p570 = dict(p570)
            p570["reason"] = ("no_qid" if not qid else "qid_not_in_cache")

        # ---- the after-side ARTICLE reading (primary-source invariant) -----
        if signals is None:
            after_raw = ""
        elif signals["infobox_death"]:
            after_raw = DECEASED_VALUE
        elif signals["death_categories"]:
            after_raw = ""  # ambiguous article signal: no provable reading
        else:
            after_raw = ALIVE_VALUE

        selection_binding = {
            "seed": meta.get("seed"),
            "decile": decile if isinstance(decile, int) else None,
            "sitelinks": sitelinks if isinstance(sitelinks, int) else None,
            "selection_hash": row.get("selection_hash"),
            "decile_boundaries": meta.get("decile_boundaries"),
            "draws_path": meta.get("draws_path"),
            "draws_sha1": meta.get("draws_sha1"),
            "treatment_release": meta.get("treatment_release"),
            "treatment_facts_sha1": meta.get("treatment_facts_sha1"),
            "treatment_manifest_sha1": meta.get("treatment_manifest_sha1"),
        }

        anchor_date_max = meta.get("anchor_date")
        if not isinstance(anchor_date_max, str) or not anchor_date_max:
            anchor_date_max = DEFAULT_ANCHOR_DATE
        anchor = {
            "basis": ANCHOR_BASIS,
            "anchor_date": anchor_date_max,
            "first_rev_ts": row.get("first_rev_ts"),
            "first_revid": row.get("first_revid"),
        }
        if row.get("anchor_error"):
            anchor["error"] = row.get("anchor_error")

        before_ref = {
            "page_title": title,
            "role": "cutoff_presence_and_selection",
            "revision_timestamp": cutoff_ts,
            "revid": cutoff_side.get("revid") if isinstance(cutoff_side, dict) else None,
            "note": (
                "before = 'alive' at the treatment cutoff: the person is a "
                "Category:Living people member verified living at pull time "
                "(alive now implies alive then), whose article existed at the "
                "cutoff (this pinned revision) and already at the knowability "
                "anchor (the 'anchor' block)"
            ),
            "anchor": anchor,
            "role_extraction": role_cutoff,
            "death_signals": signals_cutoff,
            "selection": selection_binding,
        }
        if cutoff_error:
            before_ref["fetch_error"] = cutoff_error
        before_evidence = Evidence(
            kind="wikipedia_revision",
            url=page_url,
            ref=before_ref,
            as_of=cutoff_ts,
        )

        after_ref = {
            "page_title": title,
            "role": "liveness_verification",
            "revision_timestamp": current_ts,
            "revid": current_side.get("revid") if isinstance(current_side, dict) else None,
            "death_signals": signals,
            "wikidata_p570": p570,
            "role_extraction": role_current,
        }
        if current_error:
            after_ref["fetch_errors"] = [f"current: {current_error}"]
        after_evidence = Evidence(
            kind="wikipedia_revision",
            url=page_url,
            ref=after_ref,
            as_of=current_ts,
        )

        change_date = ChangeDate(
            value=asof_iso,
            precision="day",
            evidence=Evidence(
                kind="control_verification",
                url=None,
                ref={
                    "basis": "control_no_change",
                    "note": (
                        "a control is an UNCHANGED fact; this date pins WHEN "
                        "liveness was verified (the pull asof), giving the "
                        "fact_id a stable identity per person and pull window "
                        "— it never claims anything changed"
                    ),
                },
                as_of=None,
            ),
        )

        wt_info = cfg.get(WIKITEXT_INFO_CTX_KEY) or {"file": None, "sha1": None}
        wd_info = cfg.get(WD_CACHE_INFO_CTX_KEY) or {"file": None, "sha1": None}
        provenance = {
            # The property class mirrors the treatment people domain's
            # death-event tag: whether a given person dies is genuinely
            # unforecastable at the cutoff (metadata only, never a gate).
            "predictability": UNPREDICTABLE,
            "population": POPULATION,
            "line": candidate.get("_line"),
            "display_name": display_name(title),
            # The creation timestamp itself (coordinator-requested key), plus
            # the full anchor block with its basis and the ruling's bound.
            "anchor_date": row.get("first_rev_ts"),
            "anchor": anchor,
            # The role predicate at both pins (principal's fuller spec,
            # 2026-08-05) — what the recall cell asks about and the block
            # cells' alive-twin line asserts, with the revision ids it was
            # read from.
            "role": {
                "current": role_current.get("role"),
                "at_cutoff": role_cutoff.get("role"),
                "cur_revid": after_ref["revid"],
                "cutoff_revid": before_ref["revid"],
                "extractor": ROLE_EXTRACTOR_VERSION,
            },
            # The living-verification basis (third addendum 2026-08-05):
            # three checks over already-frozen artifacts, per record.
            "living_verification": {
                "basis": ["wikidata_p570", "article_current_revision",
                          "article_cutoff_revision"],
                "wikidata_p570_present": (bool(p570.get("p570"))
                                          if p570.get("available") else None),
                "article_current": ("unavailable" if signals is None
                                    else "death_signals"
                                    if (signals["infobox_death"]
                                        or signals["death_categories"]
                                        or signals["lead_death_mentions"])
                                    else "clean"),
                "article_cutoff": ("unavailable" if signals_cutoff is None
                                   else "death_signals"
                                   if (signals_cutoff["infobox_death"]
                                       or signals_cutoff["death_categories"]
                                       or signals_cutoff["lead_death_mentions"])
                                   else "clean"),
            },
            "page": {
                "title": title,
                "qid": qid,
                "cutoff_rev_ts": cutoff_ts,
                "cutoff_revid": before_ref["revid"],
                "cur_rev_ts": current_ts,
                "cur_revid": after_ref["revid"],
                "fetch_errors": fetch_errors,
                "infobox_found": bool(signals and signals.get("infobox_found")),
                "template": signals.get("template") if signals else None,
            },
            "matching": {
                "basis": "drawn_50_treatment_people_sitelink_deciles",
                "sitelinks": sitelinks if isinstance(sitelinks, int) else None,
                "decile": decile if isinstance(decile, int) else None,
                "seed": meta.get("seed"),
                "selection_hash": row.get("selection_hash"),
            },
            "selection": {
                "draws_path": meta.get("draws_path"),
                "draws_sha1": meta.get("draws_sha1"),
                "treatment_release": meta.get("treatment_release"),
                "treatment_facts_sha1": meta.get("treatment_facts_sha1"),
                "treatment_manifest_sha1": meta.get("treatment_manifest_sha1"),
                "treatment_release_cutoff": meta.get("treatment_release_cutoff"),
                "treatment_release_asof": meta.get("treatment_release_asof"),
            },
            "wikitext_cache": {
                "file": wt_info.get("file"),
                "sha1": wt_info.get("sha1"),
                "row_present": isinstance(wt_row, dict),
            },
            "wd_cache": {
                "file": wd_info.get("file"),
                "sha1": wd_info.get("sha1"),
                "row_present": isinstance(wd_row, dict),
            },
        }

        entity_ids = {ENTITY_ID_KEY: qid} if qid else {}
        fact_id = compute_fact_id(SOURCE, title, PROPERTY, asof_iso)
        return FactChangeRecord(
            fact_id=fact_id,
            record_id=compute_record_id(fact_id, f"{qid or ''}|{current_ts or ''}"),
            source=SOURCE,
            entity={"name": title, "ids": entity_ids},
            property=PROPERTY,
            value_type=VALUE_TYPE,
            before=ValueState(raw=ALIVE_VALUE, canonical=None, evidence=before_evidence),
            after=ValueState(raw=after_raw, canonical=None, evidence=after_evidence),
            change_date=change_date,
            provenance=provenance,
        )

    # -- gates --------------------------------------------------------------

    def gate_list(self, cfg: dict):
        self._require_offline(cfg)
        self._ensure_loaded(cfg)
        # Manifest-recorded curation policy: the two named human screens this
        # lane applies, so every release states them plainly.
        meta = cfg.get(SELECTION_CTX_KEY) or {}
        anchor_date = meta.get("anchor_date")
        cfg["policy"] = {
            "mononym_max_chars": MONONYM_MAX_CHARS,
            "ambiguous_death_signal": "review",
            "anchor_date": anchor_date if isinstance(anchor_date, str) and anchor_date
            else DEFAULT_ANCHOR_DATE,
        }
        return [
            PeopleControlPoolGate(),
            UnderdeterminedEntityGate(),
            TreatmentOverlapGate(),
            ControlAnchorGate(),
            PrecutoffPresenceGate(),
            LivingVerifiedGate(),
            ControlRoleGate(),
            EvidenceResolvableGate(),
            DedupGate(),
        ]

    def snapshot_inputs(self, cfg: dict):
        return [
            SELECTED_FILENAME, SELECTION_SIDECAR_FILENAME,
            WIKITEXT_CACHE_FILENAME, WIKITEXT_SIDECAR_FILENAME,
            WD_CACHE_FILENAME, WD_SIDECAR_FILENAME,
        ]

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _asof_iso(cfg: dict) -> str:
        asof = cfg.get("asof")
        return asof.isoformat() if hasattr(asof, "isoformat") else str(asof)

    @staticmethod
    def _require_offline(cfg: dict) -> None:
        if not cfg.get("offline", True):
            raise NotImplementedError(
                "the people_controls adapter is offline-only: the pull-date "
                "evidence is fetched once by `python3 -m stage1.harvest "
                "--source people_controls`"
            )

    @staticmethod
    def _data_path(cfg: dict, filename: str) -> Path:
        data_dir = cfg.get("data_dir")
        if data_dir is None:
            raise LookupError(
                f"the people_controls adapter requires --data-dir (a people_controls "
                f"harvest snapshot containing {filename})"
            )
        path = Path(data_dir) / filename
        if not path.is_file():
            raise LookupError(f"people_controls input file not found: {path}")
        return path

    def _ensure_loaded(self, cfg: dict) -> None:
        """Load the selection sidecar, the wikitext cache, and the Wikidata
        cache into cfg exactly once per run — reusing the sports_controls
        loader (malformed content becomes input_load_errors, never a silent
        skip; a missing optional cache degrades to review via the gates)."""
        from stage1.adapters.sports_controls import SportsControlsAdapter

        if SELECTION_CTX_KEY not in cfg:
            cfg[SELECTION_CTX_KEY] = self._load_selection_sidecar(cfg)
        if WIKITEXT_CTX_KEY not in cfg:
            store, info, errors = SportsControlsAdapter._load_cache(
                cfg.get("data_dir"), WIKITEXT_CACHE_FILENAME,
                WIKITEXT_SIDECAR_FILENAME, key="title",
            )
            cfg[WIKITEXT_CTX_KEY] = store
            cfg[WIKITEXT_INFO_CTX_KEY] = info
            SportsControlsAdapter._surface_cache_meta(cfg, info)
            if errors:
                cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(errors)
        if WD_CACHE_CTX_KEY not in cfg:
            store, info, errors = SportsControlsAdapter._load_cache(
                cfg.get("data_dir"), WD_CACHE_FILENAME, WD_SIDECAR_FILENAME,
                key="qid",
            )
            cfg[WD_CACHE_CTX_KEY] = store
            cfg[WD_CACHE_INFO_CTX_KEY] = info
            SportsControlsAdapter._surface_cache_meta(cfg, info)
            if errors:
                cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(errors)

    def _load_selection_sidecar(self, cfg: dict) -> dict:
        """The selection binding written by the harvester. A missing or
        unreadable sidecar degrades to an empty binding (control_pool then
        reviews every record for the missing sha1s and treatment_overlap
        reviews for the missing QID list — visible, not fatal) with the
        problem recorded in input_load_errors."""
        data_dir = cfg.get("data_dir")
        if data_dir is None:
            return {}
        path = Path(data_dir) / SELECTION_SIDECAR_FILENAME
        if not path.is_file():
            cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).append(
                {"file": SELECTION_SIDECAR_FILENAME, "line": 0,
                 "error": "selection sidecar missing: selection binding unavailable"}
            )
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                meta = json.load(fh)
        except ValueError as exc:
            cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).append(
                {"file": SELECTION_SIDECAR_FILENAME, "line": 0,
                 "error": f"unparseable selection sidecar: {exc}"}
            )
            return {}
        return meta if isinstance(meta, dict) else {}


ADAPTER = PeopleControlsAdapter()
