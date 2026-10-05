"""SEC Item 5.02 officer-change adapter — the Stage-1 reference adapter.

Ports the legacy sec/ pipeline (sec2_curate.py extraction + the
sec_verify.py ground-truth join) onto the shared Stage-1 record/gate
machinery. The port is deliberately verbatim where the legacy behavior IS
the dataset definition:

* ROLES priority regexes (CEO before CFO before COO/President/Chair, so
  "President and CEO" reads as CEO), the ACTION-verb proximity window
  (role phrase within +-100 chars of an action verb), the NM/INC_PATS/
  OUT_PATS name patterns, the effective-date regex, and the _STOP
  first-word filter are copied character-for-character from
  sec/sec2_curate.py. Notably the name patterns are compiled with re.I
  exactly as the legacy code was — that case-insensitivity is what let
  garbage like 'arro Viseras to serve' and 'that they' through, and those
  now surface as review via the garbage_value gate instead of silently
  polluting (or being silently dropped from) the dataset.
* legacy change_id = sha1(f"{cik}|{event_date}|{adsh}")[:10] with cik
  exactly as it appears in the raw jsonl (zero-padded). Verified to
  reproduce all 652 legacy sec_curated.jsonl ids, which is what makes the
  sec_verified.jsonl evidence join sound.

What is intentionally NOT verbatim:

* change_date is the 8-K's effective date when one is stated ('effective
  March 31, 2026' -> 2026-03-31), falling back to the event date for
  'effective immediately', no effective phrase, or an unconvertible
  phrase. The legacy pipeline windowed on event_date only; using the
  effective date means a change announced in-window but effective after
  --asof is excluded by temporal_window (at asof the incumbent has not
  changed yet), which matches what the SOX-cert ground truth would say.
* The legacy keep flag (role found AND >=1 name found) is decomposed into
  two logged gates: principal_role (fail when no principal office is
  named in an action context — the comp-committee/director-only filings
  the legacy pipeline dropped silently) and value_parsed (review when a
  side is missing or unparseable — the legacy keep=False false negatives,
  e.g. Uber/Baxter departures whose names the patterns missed, now
  surface as review instead of vanishing).
* The legacy surname-only genuine_drift check is replaced by the
  value_changed gate over person_name canonicals (equal -> excluded,
  conflicting given names with a shared surname -> review).

Evidence resolution is pluggable with two modes:

* offline (the default): {data_dir}/sec_verified.jsonl is treated as a
  cached evidence store keyed by legacy change_id. When a cached row's
  per-side source field says 'sox_cert', that side's raw value and
  evidence are upgraded to the cert values (kind='sox_cert',
  url=source_url, as_of=source_date). The cache stores only the
  latest-cert url/date; the before-side cert url was not recorded by the
  legacy verifier, so the before upgrade reuses the cache row's
  source_url/source_date with a ref note saying so. Candidates with no
  cached row keep 8-K-only evidence.
* online: raise NotImplementedError('EDGAR resolver: planned').

Evidence tiers (decided here, enforced by the evidence_tier gate):
'cert_confirmed' (both sides SOX-cert-backed — the legacy
verified='confirmed' class) passes; '8k_only' and 'partial_cert' are
review, never included — COO/President/Chair never certify and the
offline resolver cannot fetch certs, so those records wait for the online
EDGAR resolver or a human.

Gate order rationale (cheap/structural scope checks first, so
excluded:<gate> names the most fundamental reason; ALL gates still run on
every record):

1. universe_membership — is the company in the S&P 500 set at all;
2. temporal_window     — sec_v1: BOTH the change date (effective date) AND
                         the 8-K announcement event date must be inside
                         [cutoff, asof]. A pre-cutoff 8-K naming the
                         successor is training-window-public even when the
                         effective date is in-window, so it is excluded
                         (the documented January-event exclusion), never
                         retained as an included row;
3. principal_role      — did the filing concern a principal office
                         (legacy keep, part 1);
4. value_parsed        — both sides extracted and parseable (one-sided
                         records land review here, legacy keep part 2);
5. garbage_value       — raw-value plausibility screen (the
                         'arro Viseras to serve' class -> review);
6. value_changed       — before genuinely differs from after (replaces
                         the legacy surname-only genuine_drift check);
7. evidence_resolvable — both sides' evidence points somewhere;
8. evidence_tier       — cert-confirmed vs 8k_only/partial tiers;
9. dedup               — last, so a duplicate's exclusion reason prefers
                         any substantive defect; first record in the
                         adsh-sorted enumeration order wins (the legacy
                         verify-cleanup "earliest event" rule is
                         approximated by filing order; cross-event
                         duplicates of one change with different
                         effective dates are NOT collapsed in v1).
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import date as _date
from pathlib import Path

import stage1.normalize.person_name  # noqa: F401  (registers the person_name comparator)
from stage1.adapters import Adapter
from stage1.gates import Gate
from stage1.predictability import ANNOUNCED
from stage1.gates.standard import (
    DEFAULT_PERSON_NAME_FRAGMENT_PHRASES,
    DEFAULT_PERSON_NAME_STOPWORDS,
    DedupGate,
    EvidenceResolvableGate,
    GarbageValueGate,
    TemporalWindowGate,
    UniverseMembershipGate,
    ValueActuallyChangedGate,
    ValueParsedGate,
    _coerce_date,
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

RAW_FILENAME = "sec_5502_2026.jsonl"
VERIFIED_FILENAME = "sec_verified.jsonl"
UNIVERSE_FILENAME = "sp500_universe.csv"

UNIVERSE_CTX_KEY = "sec_universe"
GARBAGE_RULES_CTX_KEY = "garbage_rules"
VERIFIED_STORE_CTX_KEY = "sec_verified_store"
LOAD_ERRORS_CTX_KEY = "input_load_errors"

# ---------------------------------------------------------------------------
# Extraction rules, ported verbatim from sec/sec2_curate.py.
# ---------------------------------------------------------------------------

ROLES = [
    ("CEO",       r"chief\s+executive\s+officer|\bC\.?E\.?O\.?\b"),
    ("CFO",       r"chief\s+financial\s+officer|\bC\.?F\.?O\.?\b"),
    ("COO",       r"chief\s+operating\s+officer|\bC\.?O\.?O\.?\b"),
    ("President", r"\bpresident\b"),
    ("Chair",     r"chair(?:man|person|woman|)\b"),
]
ACTION = re.compile(
    r"appoint|elect|nam(?:e|ed|ing)|promot|hir(?:e|ed|ing)|resign|retir|"
    r"step(?:ping|s|ped)?\s+down|depart|termin|transition|succeed|separation|"
    r"will\s+become|assume|to\s+serve\s+as", re.I)

NM = r"(?:Mr\.|Ms\.|Mrs\.|Dr\.)?\s*([A-Z][A-Za-z'\-]+(?:\s+(?:[A-Z]\.|[A-Z][A-Za-z'\-]+)){1,3})"
INC_PATS = [re.compile(p, re.I | re.M) for p in [
    r"(?:appointed|named|elected|promoted|hired|designated)\s+" + NM + r"\s+(?:as|to)\b",
    NM + r"\s+(?:as|to\s+serve\s+as|will\s+serve\s+as|will\s+become|was\s+appointed|"
         r"has\s+been\s+appointed|will\s+be\s+appointed)\s+(?:the\s+)?(?:new\s+)?(?:company'?s\s+)?(?:interim\s+)?"
         r"(?:chief|president|chair)",
    NM + r"\s+will\s+succeed\b",
]]
OUT_PATS = [re.compile(p, re.I | re.M) for p in [
    r"(?:resignation|retirement|departure|separation)\s+of\s+" + NM,
    NM + r"\s+(?:notified|informed|has\s+notified|will\s+retire|is\s+retiring|will\s+resign|"
         r"is\s+resigning|resigned|retired|has\s+resigned|has\s+retired|will\s+step\s+down|"
         r"is\s+stepping\s+down|stepped\s+down|will\s+depart|is\s+departing|will\s+leave)",
    r"succeed(?:s|ing|ed)?\s+" + NM,
    r"replac(?:e|es|ing|ed)\s+" + NM,
]]
DATE = re.compile(r"effective\s+(?:as\s+of\s+)?"
                  r"((?:January|February|March|April|May|June|July|August|September|October|November|December)"
                  r"\s+\d{1,2},?\s+\d{4}|immediately)", re.I)
_STOP = {"the", "board", "company", "inc", "corporation", "directors", "director", "officer"}

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

PRINCIPAL_PROPERTIES = frozenset(role.lower() for role, _ in ROLES)
UNKNOWN_PROPERTY = "other"

# PREDICTABILITY tag (owner decision B, 2026-07-21): SEC officer changes are
# the officer-change property class — successions are typically disclosed ahead
# of the effective date — so every sec record carries "announced". This is
# Stage-2 stratification METADATA in provenance['predictability'], never a gate.
PREDICTABILITY = ANNOUNCED

# SEC-tuned garbage screening: the shared person_name defaults plus the
# pronoun/auxiliary tokens observed in real legacy extraction garbage
# ('that they', 'has served'). Verdict is always review, so a legitimate
# name that trips a stopword (e.g. a given name 'Will') costs a human
# look, never a wrong exclusion. The single-letter articles 'a'/'an' are
# REMOVED from the defaults: the stopword rule strips token punctuation
# before matching, so the middle initial 'A.' in cert names like
# 'Richard A. Hoker' would otherwise read as the article 'a' and send
# clean cert-confirmed records to review (5 of the 33 legacy gold rows).
SEC_PERSON_NAME_STOPWORDS = tuple(
    w for w in DEFAULT_PERSON_NAME_STOPWORDS if w not in ("a", "an")
) + (
    "that", "they", "he", "she", "him", "her", "his", "hers", "who", "whom",
    "will", "would", "has", "have", "had", "been", "be", "is", "are", "was",
    "were", "serve", "serves", "served", "serving",
)
SEC_PERSON_NAME_RULES = (
    ("min_tokens", {"min": 2}),
    ("no_stopwords", {"stopwords": list(SEC_PERSON_NAME_STOPWORDS)}),
    ("no_phrases", {"phrases": list(DEFAULT_PERSON_NAME_FRAGMENT_PHRASES)}),
)


def legacy_change_id(cik: str, event_date: str, adsh: str) -> str:
    """The legacy pipeline's row id: sha1('cik|event_date|adsh')[:10], with
    cik exactly as stored in the raw jsonl (zero-padded, per sec2_curate.py).
    This is the join key into the sec_verified.jsonl evidence cache."""
    return hashlib.sha1(f"{cik}|{event_date}|{adsh}".encode()).hexdigest()[:10]


def _first(pats, text):
    """First name captured by the first pattern whose match survives the
    legacy filters (first word not in _STOP, >=2 words). Returns
    (name, pattern_index) or ('', None). Ported from sec2_curate._first
    with the pattern index added for evidence."""
    for i, p in enumerate(pats):
        m = p.search(text)
        if m:
            nm = re.sub(r"\s+", " ", m.group(1)).strip(" ,.")
            if nm and nm.split()[0].lower() not in _STOP and len(nm.split()) >= 2:
                return nm, i
    return "", None


def detect_role(text: str) -> str:
    """Highest-priority principal role appearing within 100 chars of an
    action verb; 'other' when none does. Ported from sec2_curate.detect_role."""
    for role, pat in ROLES:
        for m in re.finditer(pat, text, re.I):
            w = text[max(0, m.start() - 100): m.end() + 100]
            if ACTION.search(w):
                return role
    return UNKNOWN_PROPERTY


def effective_change_date(body: str, event_date: str):
    """Resolve the record's change date from the 8-K narrative.

    Returns (iso_value, basis, phrase) where basis is one of:
      'effective_date'             — a convertible 'effective <Month D, YYYY>';
      'effective_immediately'      — 'effective immediately' -> event date;
      'effective_date_unparseable' — a phrase matched but did not convert to a
                                     real calendar date -> event date;
      'event_date'                 — no effective phrase -> event date.
    """
    m = DATE.search(body)
    if not m:
        return event_date, "event_date", None
    phrase = m.group(1)
    if phrase.strip().lower() == "immediately":
        return event_date, "effective_immediately", phrase
    dm = re.match(r"^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$", re.sub(r"\s+", " ", phrase).strip())
    month = _MONTHS.get(dm.group(1).lower()) if dm else None
    if month is not None:
        try:
            iso = _date(int(dm.group(3)), month, int(dm.group(2))).isoformat()
            return iso, "effective_date", phrase
        except ValueError:
            pass
    return event_date, "effective_date_unparseable", phrase


def extract(section_text: str, event_date: str) -> dict:
    """Deterministic extraction over one Item 5.02 narrative — the full
    sec2_curate.curate() logic, returning every intermediate for evidence."""
    body = re.sub(r"\s+", " ", section_text or "")
    role = detect_role(body)
    incoming, inc_idx = _first(INC_PATS, body)
    outgoing, out_idx = _first(OUT_PATS, body)
    change_value, basis, phrase = effective_change_date(body, event_date)
    return {
        "role": role,
        "incoming": incoming,
        "incoming_pattern": inc_idx,
        "outgoing": outgoing,
        "outgoing_pattern": out_idx,
        "change_value": change_value,
        "change_basis": basis,
        "effective_phrase": phrase,
        "legacy_keep": role != UNKNOWN_PROPERTY and (bool(incoming) or bool(outgoing)),
    }


def _pad_cik(cik: str) -> str:
    """Zero-pad a numeric CIK to the canonical 10 digits; non-numeric input
    is returned unchanged (membership then fails/reviews explicitly)."""
    try:
        return f"{int(cik):010d}"
    except (TypeError, ValueError):
        return cik


class SecTemporalWindowGate(TemporalWindowGate):
    """SEC window semantics (sec_v1 — owner window-alignment fix): the shared
    day-precision check on change_date PLUS the 8-K ANNOUNCEMENT must itself
    be post-cutoff.

    The benchmark cutoff exists because pre-cutoff public information is
    presumed memorised training data. A succession announced in a PRE-CUTOFF
    8-K but effective in-window (Walmart: 8-K event 2026-01-15 names John R.
    Furner as incoming CEO effective 2026-02-01) puts the 'after' answer
    verbatim inside the training window — a model can answer it from the
    January filing, defeating the contamination purpose of the window. So a
    record passes only when BOTH its change_date (the effective date) AND its
    provenance event_date (the 8-K event, the harvest window basis) fall
    inside [cutoff, asof]; a pre-cutoff event date is a provable
    out-of-window determination -> FAIL (excluded:temporal_window, the
    documented January-event exclusion), with the announcement lead time in
    evidence. A missing/unparseable event_date is review (cannot decide),
    never a silent include. The change_date half behaves exactly like the
    shared gate (fail out-of-window, review unparseable)."""

    version = "temporal_window:sec_v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        base = super().evaluate(record, ctx)
        evidence = dict(base.evidence)

        provenance = getattr(record, "provenance", None)
        raw_event = provenance.get("event_date") if isinstance(provenance, dict) else None
        event, why = _coerce_date(raw_event) if raw_event is not None else (
            None, "record has no provenance event_date")
        if event is not None:
            evidence["event_date"] = event.isoformat()

        # change_date already provably out of window -> the base fail stands
        # (the event check could only repeat the same determination).
        if base.verdict == "fail":
            return GateResult(name=self.name, version=self.version,
                              verdict="fail", evidence=evidence)

        if event is None:
            problems = list(evidence.get("problems") or [])
            problems.append(f"event_date {why}" if raw_event is not None else why)
            evidence["problems"] = problems
            return GateResult(name=self.name, version=self.version,
                              verdict="review", evidence=evidence)

        if base.verdict == "review":
            return GateResult(name=self.name, version=self.version,
                              verdict="review", evidence=evidence)

        cutoff, _ = _coerce_date(ctx.get("cutoff"))
        asof, _ = _coerce_date(ctx.get("asof"))
        # base passed, so both bounds parsed (they are in evidence already).
        if not (cutoff <= event <= asof):
            change, _ = _coerce_date(evidence.get("change_date"))
            if event < cutoff and change is not None:
                evidence["announcement_lead_days"] = (change - event).days
            evidence["in_window"] = False
            evidence["problem"] = (
                f"announced_pre_cutoff: the 8-K event date {event.isoformat()} is outside "
                f"[{cutoff.isoformat()}, {asof.isoformat()}] although the effective "
                "change_date is inside — the succession was publicly named in "
                "training-window data, so the row is excluded from the post-cutoff window"
                if event < cutoff else
                f"event_date {event.isoformat()} is outside [{cutoff.isoformat()}, "
                f"{asof.isoformat()}]"
            )
            return GateResult(name=self.name, version=self.version,
                              verdict="fail", evidence=evidence)
        return GateResult(name=self.name, version=self.version,
                          verdict="pass", evidence=evidence)


class SecPrincipalRoleGate(Gate):
    """Fail records whose filing named no principal office in an action
    context (property == 'other') — the comp-committee / director-only
    filings the legacy pipeline dropped silently via keep=False.

    This is a genuine 'fail', not review: role detection is a deterministic
    positive determination that the 5.02 narrative contains no principal-
    officer phrase within 100 chars of an action verb, i.e. the filing is
    out of scope, same class as an out-of-window date. An unrecognized or
    missing property is 'review' (that is a pipeline defect, not data)."""

    name = "principal_role"
    version = "principal_role:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        prop = getattr(record, "property", None)
        evidence: dict = {"property": prop if isinstance(prop, str) else repr(prop)}
        provenance = getattr(record, "provenance", None)
        if isinstance(provenance, dict) and isinstance(provenance.get("extraction"), dict):
            extraction = provenance["extraction"]
            evidence["harvest_flags"] = {
                key: extraction.get(key)
                for key in ("harvest_senior", "harvest_is_appt", "harvest_is_depart")
            }
        if not isinstance(prop, str) or not prop:
            evidence["problem"] = "record.property is missing or empty"
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        if prop == UNKNOWN_PROPERTY:
            evidence["problem"] = (
                "no principal-officer role phrase within 100 chars of an action verb"
            )
            return GateResult(name=self.name, version=self.version, verdict="fail", evidence=evidence)
        if prop in PRINCIPAL_PROPERTIES:
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
        evidence["problem"] = f"unrecognized property {prop!r} (known: {sorted(PRINCIPAL_PROPERTIES)})"
        return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)


class SecEvidenceTierGate(Gate):
    """Grade the record's evidence strength; only the cert tier passes.

    'cert_confirmed' (pass): both sides carry kind='sox_cert' — the value
    was read from an 'I, <name>, certify' SOX s302 certification, the legacy
    verified='confirmed' class.
    'partial_cert' / '8k_only' (review): one or no side is cert-backed.
    The 8-K narrative names people but is not the officer-of-record
    document, COO/President/Chair never certify, and the offline resolver
    can only replay the sec_verified.jsonl cache — so these tiers wait for
    the online EDGAR resolver (planned) or a human.

    The review note is honest about cache health: when the evidence-cache
    load reported unreadable lines (ctx['input_load_errors'] entries for
    sec_verified.jsonl), the note says a missing cert value may be cache
    damage rather than asserting no cached value exists."""

    name = "evidence_tier"
    version = "evidence_tier:v2"

    def evaluate(self, record, ctx: dict) -> GateResult:
        kinds: dict = {}
        problems: list = []
        for side in ("before", "after"):
            side_evidence = getattr(getattr(record, side, None), "evidence", None)
            if isinstance(side_evidence, Evidence) and isinstance(side_evidence.kind, str) and side_evidence.kind:
                kinds[side] = side_evidence.kind
            else:
                problems.append(f"{side}.evidence is missing or has an empty kind")
        evidence: dict = {"kinds": kinds}
        provenance = getattr(record, "provenance", None)
        if isinstance(provenance, dict) and isinstance(provenance.get("legacy"), dict):
            evidence["legacy_verified"] = provenance["legacy"].get("verified")
        if problems:
            evidence["problems"] = problems
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)

        cert_sides = sum(1 for kind in kinds.values() if kind == "sox_cert")
        if cert_sides == 2:
            evidence["tier"] = "cert_confirmed"
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
        evidence["tier"] = "partial_cert" if cert_sides == 1 else "8k_only"

        load_errors = ctx.get(LOAD_ERRORS_CTX_KEY)
        cache_errors = [
            e for e in load_errors
            if isinstance(e, dict) and e.get("file") == VERIFIED_FILENAME
        ] if isinstance(load_errors, list) else []
        sides_phrase = "one side" if cert_sides == 1 else "either side"
        if cache_errors:
            evidence["cache_load_errors"] = len(cache_errors)
            evidence["note"] = (
                f"not cert-confirmed offline: no readable cached SOX-cert value for {sides_phrase}, "
                f"but {VERIFIED_FILENAME} had {len(cache_errors)} unreadable line(s) — the missing "
                "value may be cache damage, not absence (online EDGAR cert resolver: planned)"
            )
        else:
            evidence["note"] = (
                f"not cert-confirmed offline: no cached SOX-cert value for {sides_phrase} "
                f"in {VERIFIED_FILENAME} (online EDGAR cert resolver: planned)"
            )
        return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)


class SecAdapter(Adapter):
    """Adapter for source 'sec'. See the module docstring for the design.

    Deliberately stateless: input files are re-read per run (gate_list loads
    the evidence store and universe once into cfg, the runner's sanctioned
    channel), never cached on the adapter instance — a module-level
    singleton with path-keyed caches would silently serve stale data when an
    input file changes between runs in one process, breaking the
    same-inputs-same-outputs audit chain."""

    source = "sec"

    # -- enumeration --------------------------------------------------------

    def enumerate_candidates(self, cfg: dict):
        """Yield every row of {data_dir}/sec_5502_2026.jsonl, sorted by
        (adsh, file line). Unparseable lines are yielded as error candidates
        so build_record can raise a descriptive error into the manifest's
        build_errors — enumeration never decides inclusion."""
        self._require_offline(cfg)
        raw_path = self._data_path(cfg, RAW_FILENAME)
        candidates = []
        with open(raw_path, encoding="utf-8") as fh:
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
        candidates.sort(key=lambda c: (str(c.get("adsh") or ""), c["_line"]))
        yield from candidates

    # -- record building ----------------------------------------------------

    def build_record(self, candidate: dict, cfg: dict) -> FactChangeRecord:
        if "_parse_error" in candidate:
            raise ValueError(
                f"{RAW_FILENAME} line {candidate.get('_line')}: unparseable JSON "
                f"({candidate['_parse_error']})"
            )
        missing = [key for key in ("adsh", "cik", "company", "event_date") if not candidate.get(key)]
        if missing:
            raise ValueError(
                f"{RAW_FILENAME} line {candidate.get('_line')} "
                f"(adsh={candidate.get('adsh')!r}): missing required field(s) {missing}"
            )
        event_date = candidate["event_date"]
        if not isinstance(event_date, str) or not _ISO_DATE_RE.match(event_date):
            raise ValueError(
                f"{RAW_FILENAME} line {candidate.get('_line')} "
                f"(adsh={candidate['adsh']!r}): event_date {event_date!r} is not 'YYYY-MM-DD'"
            )

        adsh = candidate["adsh"]
        raw_cik = str(candidate["cik"])
        cik = _pad_cik(raw_cik)
        company = candidate["company"]
        doc_url = candidate.get("doc_url") or None
        file_date = candidate.get("file_date") or None
        extraction = extract(candidate.get("section_text") or "", event_date)
        legacy_id = legacy_change_id(raw_cik, event_date, adsh)

        def filing_evidence(side: str) -> Evidence:
            index = extraction[f"{side}_pattern"]
            pattern_note = (
                f"{side}[{index}]" if index is not None else f"no_{side}_pattern_matched"
            )
            return Evidence(
                kind="8k_section",
                url=doc_url,
                ref={"adsh": adsh, "cik": cik, "extraction_pattern": pattern_note},
                as_of=file_date,
            )

        before_raw, before_evidence = extraction["outgoing"], filing_evidence("outgoing")
        after_raw, after_evidence = extraction["incoming"], filing_evidence("incoming")

        store = cfg.get(VERIFIED_STORE_CTX_KEY)
        if not isinstance(store, dict):
            store, load_errors = self._load_verified(cfg.get("data_dir"))
            cfg[VERIFIED_STORE_CTX_KEY] = store
            if load_errors:
                cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(load_errors)
        cached = store.get(legacy_id)
        if cached is not None:
            if cached.get("before_source") == "sox_cert" and cached.get("before"):
                before_raw = cached["before"]
                before_evidence = self._cert_evidence(cached, legacy_id, "before")
            if cached.get("gt_source") == "sox_cert" and cached.get("gt_answer"):
                after_raw = cached["gt_answer"]
                after_evidence = self._cert_evidence(cached, legacy_id, "gt_answer")

        prop = extraction["role"].lower()
        entity_ids = {"cik": cik}
        if candidate.get("ticker"):
            entity_ids["ticker"] = str(candidate["ticker"])

        change_date = ChangeDate(
            value=extraction["change_value"],
            precision="day",
            evidence=Evidence(
                kind="8k_section",
                url=doc_url,
                ref={
                    "adsh": adsh,
                    "basis": extraction["change_basis"],
                    "effective_phrase": extraction["effective_phrase"],
                    "event_date": event_date,
                },
                as_of=file_date,
            ),
        )

        provenance = {
            "predictability": PREDICTABILITY,
            "legacy_change_id": legacy_id,
            "adsh": adsh,
            "form": candidate.get("form") or "",
            "file_date": candidate.get("file_date") or "",
            "event_date": event_date,
            "doc_url": candidate.get("doc_url") or "",
            "extraction": {
                "role": extraction["role"],
                "incoming_raw": extraction["incoming"],
                "incoming_pattern": extraction["incoming_pattern"],
                "outgoing_raw": extraction["outgoing"],
                "outgoing_pattern": extraction["outgoing_pattern"],
                "change_basis": extraction["change_basis"],
                "effective_phrase": extraction["effective_phrase"],
                "harvest_senior": candidate.get("senior"),
                "harvest_is_appt": candidate.get("is_appt"),
                "harvest_is_depart": candidate.get("is_depart"),
            },
            "legacy": {
                "keep": extraction["legacy_keep"],
                "cache_hit": cached is not None,
                "verified": cached.get("verified") if cached else None,
                "genuine_drift": cached.get("genuine_drift") if cached else None,
                "gt_source": cached.get("gt_source") if cached else None,
                "before_source": cached.get("before_source") if cached else None,
                "regex_outgoing": cached.get("regex_outgoing") if cached else None,
            },
        }

        fact_id = compute_fact_id("sec", company, prop, extraction["change_value"])
        return FactChangeRecord(
            fact_id=fact_id,
            record_id=compute_record_id(fact_id, adsh),
            source=self.source,
            entity={"name": company, "ids": entity_ids},
            property=prop,
            value_type="person_name",
            before=ValueState(raw=before_raw, canonical=None, evidence=before_evidence),
            after=ValueState(raw=after_raw, canonical=None, evidence=after_evidence),
            change_date=change_date,
            provenance=provenance,
        )

    # -- gates --------------------------------------------------------------

    def gate_list(self, cfg: dict):
        """Ordered gates (rationale in the module docstring). Also injects
        the ctx tables the shared gates document — the runner copies cfg
        into ctx after calling this, which is the sanctioned channel:
        cfg['sec_universe'] (frozenset of padded CIKs; left unset when the
        universe csv is missing so universe_membership reviews rather than
        guessing), cfg['sec_verified_store'] (the evidence cache, loaded
        once per run), cfg['input_load_errors'] (unreadable input lines —
        surfaced in the manifest and in evidence_tier notes, never silently
        skipped), and cfg['garbage_rules'] (person_name screening rules)."""
        self._require_offline(cfg)
        universe = self._load_universe(cfg.get("data_dir"))
        if universe is not None:
            cfg[UNIVERSE_CTX_KEY] = universe
        if VERIFIED_STORE_CTX_KEY not in cfg:
            store, load_errors = self._load_verified(cfg.get("data_dir"))
            cfg[VERIFIED_STORE_CTX_KEY] = store
            if load_errors:
                cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(load_errors)
        cfg[GARBAGE_RULES_CTX_KEY] = {"person_name": SEC_PERSON_NAME_RULES}
        return [
            UniverseMembershipGate(id_key="cik", universe_set_ctx_key=UNIVERSE_CTX_KEY),
            SecTemporalWindowGate(),
            SecPrincipalRoleGate(),
            ValueParsedGate(),
            GarbageValueGate(rules_ctx_key=GARBAGE_RULES_CTX_KEY),
            ValueActuallyChangedGate(),
            EvidenceResolvableGate(),
            SecEvidenceTierGate(),
            DedupGate(),
        ]

    def snapshot_inputs(self, cfg: dict):
        """The three fixed files a SEC snapshot must pin (see the coverage
        check): the 5.02 rows, the SOX-cert ground truth, and the universe."""
        return [RAW_FILENAME, VERIFIED_FILENAME, UNIVERSE_FILENAME]

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _require_offline(cfg: dict) -> None:
        if not cfg.get("offline", True):
            raise NotImplementedError("EDGAR resolver: planned")

    @staticmethod
    def _data_path(cfg: dict, filename: str) -> Path:
        data_dir = cfg.get("data_dir")
        if data_dir is None:
            raise LookupError(
                f"the sec adapter requires --data-dir (a directory containing {RAW_FILENAME})"
            )
        path = Path(data_dir) / filename
        if not path.is_file():
            raise LookupError(f"sec input file not found: {path}")
        return path

    @staticmethod
    def _cert_evidence(cached: dict, legacy_id: str, cache_field: str) -> Evidence:
        ref = {"legacy_change_id": legacy_id, "cache_field": cache_field}
        if cache_field == "before":
            ref["note"] = (
                "url/as_of are the cache row's latest-cert source_url/source_date; "
                "the prior-period cert url was not recorded by the legacy verifier"
            )
        return Evidence(
            kind="sox_cert",
            url=cached.get("source_url") or None,
            ref=ref,
            as_of=cached.get("source_date") or None,
        )

    @staticmethod
    def _load_verified(data_dir):
        """Evidence store from {data_dir}/sec_verified.jsonl. Returns
        (store, load_errors): store is {legacy change_id: row}; load_errors
        is a list of {'file', 'line', 'error'} dicts, one per unreadable or
        malformed line. A missing file yields an empty store (records then
        stay at the 8k_only tier -> review) and no errors; a corrupt line is
        an ERROR entry, never a silent skip — a damaged cache must be
        distinguishable from an absent cert. First row wins per change_id."""
        store: dict = {}
        load_errors: list = []
        if data_dir is None:
            return store, load_errors
        path = Path(data_dir) / VERIFIED_FILENAME
        if path.is_file():
            with open(path, encoding="utf-8") as fh:
                for line_no, line in enumerate(fh, 1):
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError as exc:
                        load_errors.append(
                            {"file": VERIFIED_FILENAME, "line": line_no, "error": str(exc)}
                        )
                        continue
                    if not isinstance(row, dict) or not isinstance(row.get("change_id"), str):
                        load_errors.append(
                            {
                                "file": VERIFIED_FILENAME,
                                "line": line_no,
                                "error": "row is not an object with a string change_id",
                            }
                        )
                        continue
                    store.setdefault(row["change_id"], row)
        return store, load_errors

    @staticmethod
    def _load_universe(data_dir):
        """frozenset of zero-padded S&P 500 CIKs from
        {data_dir}/sp500_universe.csv, or None when the file is missing
        (universe_membership then reviews every record). Rows without a
        numeric cik are skipped — they could never match a padded CIK."""
        if data_dir is None:
            return None
        path = Path(data_dir) / UNIVERSE_FILENAME
        if not path.is_file():
            return None
        ciks = set()
        with open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                raw = (row.get("cik") or "").strip()
                try:
                    ciks.add(f"{int(raw):010d}")
                except ValueError:
                    continue
        return frozenset(ciks)


ADAPTER = SecAdapter()
