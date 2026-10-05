"""Shared, source-agnostic gate library.

These gates encode the inclusion checks every source needs — the temporal
window around the training cutoff, universe membership, parse/change
verification, evidence resolvability, per-value_type garbage screening, and
deduplication. Adapters compose them (interleaved with source-specific gates)
in gate_list(); nothing here knows about any particular source.

Verdict philosophy (pipeline-wide, applied uniformly):

* 'fail' is reserved for POSITIVE evidence a record is out of scope: the
  change date is outside the window, the entity id is provably not in the
  universe, before equals after, or the record duplicates an earlier one.
* Everything the gate cannot decide — missing ctx keys, unparseable dates,
  absent ids, parse failures, garbage-looking values — is 'review'. This is
  how the "nothing silently dropped" rule survives contact with dirty data:
  garbage that previously slipped through the legacy sec/ pipeline now lands
  in the review queue with evidence, instead of being excluded (or included)
  by accident.

Evidence dicts are JSON-safe and deterministic: strings, numbers, booleans,
lists, dicts only; no timestamps, no unstable reprs. They are written
verbatim into facts.jsonl.

Configuration travels through constructors (per-adapter wiring: id keys, ctx
key names, dedup key fields) and through ctx (per-run data: cutoff/asof,
universe sets, garbage-rule tables, the dedup seen-map).
"""

from __future__ import annotations

from datetime import date as _date, datetime as _datetime

from stage1.gates import Gate
from stage1.normalize import Comparison, get_comparator
from stage1.schema import ChangeDate, Evidence, GateResult

__all__ = [
    "TemporalWindowGate",
    "UniverseMembershipGate",
    "ValueParsedGate",
    "ValueActuallyChangedGate",
    "EvidenceResolvableGate",
    "GarbageValueGate",
    "CorroborationGate",
    "DedupGate",
    "DEFAULT_PERSON_NAME_STOPWORDS",
    "DEFAULT_PERSON_NAME_FRAGMENT_PHRASES",
    "DEFAULT_PERSON_NAME_RULES",
]

_NORMALIZE_GATE_NAME = "normalize"  # mirrors stage1.run.NORMALIZE_GATE_NAME


def _coerce_date(value):
    """Return (date, None) for a date/datetime/'YYYY-MM-DD' string, else
    (None, reason). Total: never raises."""
    if isinstance(value, _datetime):
        return value.date(), None
    if isinstance(value, _date):
        return value, None
    if isinstance(value, str):
        try:
            return _date.fromisoformat(value), None
        except ValueError:
            return None, f"is not a valid 'YYYY-MM-DD' date: {value!r}"
    return None, f"is not a date or 'YYYY-MM-DD' string: {type(value).__name__}"


def _json_safe(value):
    """Render an arbitrary value safely for gate evidence."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


class TemporalWindowGate(Gate):
    """Pass iff cutoff <= change date <= asof (both bounds inclusive).

    Bounds come from ctx['cutoff'] and ctx['asof'] (dates or 'YYYY-MM-DD'
    strings, per the runner's cfg). The record's date comes from the
    attribute named by date_field (default 'change_date'); a ChangeDate is
    unwrapped to its value. A missing or unparseable date on either side of
    the comparison is 'review', never a default include or exclude.
    """

    name = "temporal_window"
    version = "temporal_window:v1"

    def __init__(self, date_field: str = "change_date"):
        self.date_field = date_field

    def evaluate(self, record, ctx: dict) -> GateResult:
        evidence: dict = {"date_field": self.date_field}
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

        raw_value = getattr(record, self.date_field, None)
        if isinstance(raw_value, ChangeDate):
            raw_value = raw_value.value
        if raw_value is None:
            problems.append(f"record has no {self.date_field!r} value")
        else:
            change, why = _coerce_date(raw_value)
            if change is None:
                problems.append(f"record.{self.date_field} {why}")
            else:
                evidence["change_date"] = change.isoformat()

        if problems:
            evidence["problems"] = problems
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)

        in_window = bounds["cutoff"] <= change <= bounds["asof"]
        evidence["in_window"] = in_window
        return GateResult(
            name=self.name,
            version=self.version,
            verdict="pass" if in_window else "fail",
            evidence=evidence,
        )


class UniverseMembershipGate(Gate):
    """Pass iff the entity id under id_key is in the whitelist set carried in
    ctx[universe_set_ctx_key] (e.g. S&P 500 CIKs).

    Membership failure is a genuine 'fail' (the entity is provably out of
    universe). An absent id, a missing/malformed universe set, or an
    unhashable id is 'review' — the gate cannot decide, so a human must.
    """

    name = "universe_membership"
    version = "universe_membership:v1"

    def __init__(self, id_key: str, universe_set_ctx_key: str):
        self.id_key = id_key
        self.universe_set_ctx_key = universe_set_ctx_key

    def evaluate(self, record, ctx: dict) -> GateResult:
        evidence: dict = {
            "id_key": self.id_key,
            "universe_ctx_key": self.universe_set_ctx_key,
        }

        if self.universe_set_ctx_key not in ctx:
            evidence["problem"] = f"ctx is missing {self.universe_set_ctx_key!r}"
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        universe = ctx[self.universe_set_ctx_key]
        if not isinstance(universe, (set, frozenset, dict, list, tuple)):
            evidence["problem"] = (
                f"ctx[{self.universe_set_ctx_key!r}] is not a collection: {type(universe).__name__}"
            )
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        evidence["universe_size"] = len(universe)

        entity = getattr(record, "entity", None)
        ids = entity.get("ids") if isinstance(entity, dict) else None
        if not isinstance(ids, dict):
            evidence["problem"] = "record.entity.ids is missing or not a dict"
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        if self.id_key not in ids:
            evidence["problem"] = f"entity has no {self.id_key!r} id"
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)

        entity_id = ids[self.id_key]
        evidence["entity_id"] = _json_safe(entity_id)
        try:
            member = entity_id in universe
        except TypeError:
            evidence["problem"] = f"entity id is not hashable: {type(entity_id).__name__}"
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)

        evidence["member"] = member
        return GateResult(
            name=self.name,
            version=self.version,
            verdict="pass" if member else "fail",
            evidence=evidence,
        )


def _recorded_parse_failure(record, side: str):
    """Look up the parse failure_reason for one side from the runner's
    'normalize' gate result, if that result is present on the record."""
    gates = getattr(record, "gates", None)
    if not isinstance(gates, list):
        return None
    for g in gates:
        if isinstance(g, GateResult) and g.name == _NORMALIZE_GATE_NAME and isinstance(g.evidence, dict):
            side_info = g.evidence.get(side)
            if isinstance(side_info, dict):
                return side_info.get("failure_reason")
            if "error" in g.evidence:
                return _json_safe(g.evidence["error"])
            return None
    return None


class ValueParsedGate(Gate):
    """Fail-safe check that both before.canonical and after.canonical exist.

    Normalization leaves canonical as None when a raw value did not parse;
    this gate turns that into an explicit 'review' verdict carrying the parse
    failure_reason (read from the runner's 'normalize' gate evidence) — this
    is how parse failures surface in dispositions instead of vanishing.
    """

    name = "value_parsed"
    version = "value_parsed:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        evidence: dict = {}
        unparsed = []
        for side in ("before", "after"):
            state = getattr(record, side, None)
            canonical = getattr(state, "canonical", None)
            ok = isinstance(canonical, dict)
            evidence[f"{side}_parsed"] = ok
            if ok:
                continue
            unparsed.append(side)
            if canonical is not None:
                evidence[f"{side}_failure_reason"] = (
                    f"canonical is {type(canonical).__name__}, expected dict"
                )
                continue
            reason = _recorded_parse_failure(record, side)
            if reason is None:
                reason = "parse failure reason not recorded (no 'normalize' gate evidence on record)"
            evidence[f"{side}_failure_reason"] = reason
        return GateResult(
            name=self.name,
            version=self.version,
            verdict="review" if unparsed else "pass",
            evidence=evidence,
        )


class ValueActuallyChangedGate(Gate):
    """Verify the before/after canonicals denote genuinely different values.

    Uses the registry comparator for record.value_type. Comparator 'equal' is
    a 'fail' — an equal pair is not a real change and is excluded with the
    comparator's reasoning as evidence. 'different' passes. 'review' and
    'incomparable' become 'review'. Missing canonicals, an unregistered
    value_type, or a comparator contract violation (raise, wrong type,
    invalid verdict) also become 'review'. The comparator version and reason
    are always recorded when a comparison ran.
    """

    name = "value_changed"
    version = "value_changed:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        evidence: dict = {"value_type": _json_safe(getattr(record, "value_type", None))}

        before = getattr(getattr(record, "before", None), "canonical", None)
        after = getattr(getattr(record, "after", None), "canonical", None)
        missing = [
            side
            for side, canonical in (("before", before), ("after", after))
            if not isinstance(canonical, dict)
        ]
        if missing:
            evidence["problem"] = f"canonical missing for: {', '.join(missing)}"
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)

        try:
            comparator = get_comparator(record.value_type)
        except (LookupError, TypeError) as exc:
            evidence["problem"] = str(exc)
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        evidence["comparator_version"] = comparator.VERSION

        try:
            comparison = comparator.compare(before, after)
        except Exception as exc:
            evidence["problem"] = f"comparator {comparator.VERSION} raised: {exc!r}"
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        if not isinstance(comparison, Comparison):
            evidence["problem"] = (
                f"comparator {comparator.VERSION} returned {type(comparison).__name__}, expected Comparison"
            )
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)

        evidence["comparison_verdict"] = _json_safe(comparison.verdict)
        evidence["reason"] = _json_safe(comparison.reason)
        if comparison.verdict == "different":
            verdict = "pass"
        elif comparison.verdict == "equal":
            verdict = "fail"
            evidence["problem"] = "before and after canonicals are equal: not a real change"
        elif comparison.verdict in ("review", "incomparable"):
            verdict = "review"
        else:
            verdict = "review"
            evidence["problem"] = (
                f"comparator {comparator.VERSION} returned invalid verdict {comparison.verdict!r}"
            )
        return GateResult(name=self.name, version=self.version, verdict=verdict, evidence=evidence)


class EvidenceResolvableGate(Gate):
    """Pass iff both before.evidence and after.evidence identify their source:
    a non-empty kind AND (a non-empty url OR a non-empty ref dict).

    Unresolvable evidence is 'review' (the value may be perfectly real — we
    just cannot point a verifier at it), never a silent exclude.
    """

    name = "evidence_resolvable"
    version = "evidence_resolvable:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        evidence: dict = {}
        problems: list = []
        for side in ("before", "after"):
            state = getattr(record, side, None)
            side_evidence = getattr(state, "evidence", None)
            if not isinstance(side_evidence, Evidence):
                evidence[f"{side}_resolvable"] = False
                problems.append(f"{side}.evidence is missing or not an Evidence")
                continue
            side_problems = []
            if not (isinstance(side_evidence.kind, str) and side_evidence.kind):
                side_problems.append(f"{side}.evidence.kind is missing or empty")
            has_url = isinstance(side_evidence.url, str) and side_evidence.url != ""
            has_ref = isinstance(side_evidence.ref, dict) and len(side_evidence.ref) > 0
            if not (has_url or has_ref):
                side_problems.append(f"{side}.evidence has neither url nor non-empty ref")
            evidence[f"{side}_resolvable"] = not side_problems
            problems.extend(side_problems)
        if problems:
            evidence["problems"] = problems
        return GateResult(
            name=self.name,
            version=self.version,
            verdict="review" if problems else "pass",
            evidence=evidence,
        )


def _rule_min_tokens(value: str, config: dict):
    minimum = config.get("min")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
        return f"invalid rule config: 'min' must be a positive int, got {minimum!r}"
    count = len(value.split())
    if count < minimum:
        return f"{count} token(s), need >= {minimum}"
    return None


_TOKEN_PUNCTUATION = ".,;:!?'\"()[]"


def _rule_no_stopwords(value: str, config: dict):
    stopwords = config.get("stopwords")
    if not isinstance(stopwords, (list, tuple, set, frozenset)):
        return f"invalid rule config: 'stopwords' must be a list, got {type(stopwords).__name__}"
    stopset = {str(word).lower() for word in stopwords}
    hits = sorted(
        {
            token
            for token in (t.strip(_TOKEN_PUNCTUATION).lower() for t in value.split())
            if token in stopset
        }
    )
    if hits:
        return "stopword token(s): " + ", ".join(hits)
    return None


def _rule_no_phrases(value: str, config: dict):
    phrases = config.get("phrases")
    if not isinstance(phrases, (list, tuple)):
        return f"invalid rule config: 'phrases' must be a list, got {type(phrases).__name__}"
    bad = [p for p in phrases if not isinstance(p, str)]
    if bad:
        return f"invalid rule config: non-string phrase(s): {bad!r}"
    hits = [p for p in phrases if p in value]
    if hits:
        return "fragment phrase(s): " + ", ".join(hits)
    return None


_GARBAGE_RULE_PREDICATES = {
    "min_tokens": _rule_min_tokens,
    "no_stopwords": _rule_no_stopwords,
    "no_phrases": _rule_no_phrases,
}

DEFAULT_PERSON_NAME_STOPWORDS = (
    "the", "a", "an", "and", "of",
    "board", "company", "inc", "corp", "corporation", "llc", "ltd",
    "officer", "officers", "director", "directors", "committee",
    "chief", "executive", "president", "chairman", "interim",
)

DEFAULT_PERSON_NAME_FRAGMENT_PHRASES = (
    "to serve", "will serve", "ceased", "resigned", "appointed",
    "retired", "stepped down", "down from", "effective",
)

DEFAULT_PERSON_NAME_RULES = (
    ("min_tokens", {"min": 2}),
    ("no_stopwords", {"stopwords": list(DEFAULT_PERSON_NAME_STOPWORDS)}),
    ("no_phrases", {"phrases": list(DEFAULT_PERSON_NAME_FRAGMENT_PHRASES)}),
)


class GarbageValueGate(Gate):
    """Per-value_type sanity screening of the raw before/after values.

    Rules are configured in ctx[rules_ctx_key] as
    {value_type: [(rule_name, config_dict), ...]}; the engine is generic and
    rule_name selects a predicate from the built-in table ('min_tokens',
    'no_stopwords', 'no_phrases'). DEFAULT_PERSON_NAME_RULES is the shipped
    person_name instance: >= 2 tokens, no organizational stopwords
    (the/board/company/inc/...), no lowercase sentence-fragment verbs
    ('to serve', 'ceased', 'down from', ...) — the class of garbage that
    previously survived the legacy sec/ pipeline by accident.

    Any violation is 'review', NEVER 'fail': a garbage-looking value means a
    human must look, not that the record is provably out of scope. Phrase
    matching is case-sensitive (defaults are lowercase) so capitalized name
    tokens do not false-positive. A value_type with no configured rules
    passes explicitly (evidence says so); a missing or malformed rules table,
    an unknown rule name, or a malformed rule spec is 'review' — a
    configuration hole must not become a silent no-op.
    """

    name = "garbage_value"
    version = "garbage_value:v1"

    def __init__(self, rules_ctx_key: str = "garbage_rules"):
        self.rules_ctx_key = rules_ctx_key

    def evaluate(self, record, ctx: dict) -> GateResult:
        evidence: dict = {
            "rules_ctx_key": self.rules_ctx_key,
            "value_type": _json_safe(getattr(record, "value_type", None)),
        }

        if self.rules_ctx_key not in ctx:
            evidence["problem"] = f"ctx is missing {self.rules_ctx_key!r}"
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        rules_by_type = ctx[self.rules_ctx_key]
        if not isinstance(rules_by_type, dict):
            evidence["problem"] = (
                f"ctx[{self.rules_ctx_key!r}] is not a dict: {type(rules_by_type).__name__}"
            )
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)

        try:
            rules = rules_by_type.get(getattr(record, "value_type", None))
        except TypeError:
            evidence["problem"] = "record.value_type is not hashable"
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        if rules is None:
            evidence["rules"] = []
            evidence["note"] = "no rules configured for this value_type"
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
        if not isinstance(rules, (list, tuple)):
            evidence["problem"] = f"rules for this value_type are not a list: {type(rules).__name__}"
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)

        violations: list = []
        applied: list = []

        sides = []
        for side in ("before", "after"):
            raw = getattr(getattr(record, side, None), "raw", None)
            if isinstance(raw, str):
                sides.append((side, raw))
            else:
                violations.append(
                    {"side": side, "rule": None, "detail": "raw value is missing or not a string"}
                )

        for index, spec in enumerate(rules):
            if not (
                isinstance(spec, (list, tuple))
                and len(spec) == 2
                and isinstance(spec[0], str)
                and isinstance(spec[1], dict)
            ):
                violations.append(
                    {
                        "side": None,
                        "rule": f"[{index}]",
                        "detail": f"malformed rule spec (expected (name, config) pair): {spec!r}",
                    }
                )
                continue
            rule_name, config = spec
            applied.append(rule_name)
            predicate = _GARBAGE_RULE_PREDICATES.get(rule_name)
            if predicate is None:
                violations.append(
                    {
                        "side": None,
                        "rule": rule_name,
                        "detail": f"unknown rule (known: {sorted(_GARBAGE_RULE_PREDICATES)})",
                    }
                )
                continue
            for side, raw in sides:
                detail = predicate(raw, config)
                if detail is not None:
                    violations.append({"side": side, "rule": rule_name, "detail": detail, "value": raw})

        evidence["rules"] = applied
        evidence["violations"] = violations
        return GateResult(
            name=self.name,
            version=self.version,
            verdict="review" if violations else "pass",
            evidence=evidence,
        )


class CorroborationGate(Gate):
    """Check record.after.canonical against an independent second source.

    The second source travels in ctx[lookup_ctx_key]: a dict mapping entity
    id (record.entity.ids[id_key]) to either one raw string or a list of raw
    candidate strings (e.g. a Wikidata team's enwiki sitelink title, English
    label, and aliases). Each candidate is parsed with the registry
    comparator for record.value_type and compared against after.canonical.

    Verdicts (agreement can pass; NOTHING here can fail — a second-source
    disagreement is a human-review flag, since it may be vandalism on either
    side, a timing lag, or a genuinely different event like a loan, and the
    record's own source remains the declared ground truth):

    * some candidate compares 'equal'            -> 'pass' (matched value and
      its index recorded in evidence);
    * no 'equal' but some 'review'/'incomparable'-> 'review' (undecidable);
    * every candidate compares 'different'       -> 'review',
      reason 'sources_disagree';
    * second source missing for this entity, empty candidate list, malformed
      lookup/entry, missing entity id, unparsed after.canonical, unknown
      comparator, or a comparator contract violation -> 'review' with the
      specific reason.

    Per-candidate outcomes are all recorded, so a review is self-contained.
    """

    name = "corroboration"
    version = "corroboration:v1"

    def __init__(self, id_key: str, lookup_ctx_key: str, source_label: str = "second_source"):
        self.id_key = id_key
        self.lookup_ctx_key = lookup_ctx_key
        self.source_label = source_label

    def evaluate(self, record, ctx: dict) -> GateResult:
        evidence: dict = {
            "id_key": self.id_key,
            "lookup_ctx_key": self.lookup_ctx_key,
            "source_label": self.source_label,
        }

        def review(problem: str) -> GateResult:
            evidence["problem"] = problem
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)

        if self.lookup_ctx_key not in ctx:
            return review(f"ctx is missing {self.lookup_ctx_key!r}")
        lookup = ctx[self.lookup_ctx_key]
        if not isinstance(lookup, dict):
            return review(
                f"ctx[{self.lookup_ctx_key!r}] is not a dict: {type(lookup).__name__}"
            )

        entity = getattr(record, "entity", None)
        ids = entity.get("ids") if isinstance(entity, dict) else None
        if not isinstance(ids, dict) or self.id_key not in ids:
            return review(f"entity has no {self.id_key!r} id")
        entity_id = ids[self.id_key]
        evidence["entity_id"] = _json_safe(entity_id)
        try:
            entry = lookup.get(entity_id)
        except TypeError:
            return review(f"entity id is not hashable: {type(entity_id).__name__}")
        if entry is None:
            return review(
                f"{self.source_label} has no value for this entity"
            )
        if isinstance(entry, str):
            candidates = [entry]
        elif isinstance(entry, (list, tuple)):
            candidates = list(entry)
        else:
            return review(
                f"{self.source_label} entry is neither a string nor a list: {type(entry).__name__}"
            )
        if not candidates:
            return review(f"{self.source_label} entry is an empty list")
        bad = [c for c in candidates if not isinstance(c, str)]
        if bad:
            return review(f"{self.source_label} entry contains non-string value(s): {bad!r}")

        after_canonical = getattr(getattr(record, "after", None), "canonical", None)
        if not isinstance(after_canonical, dict):
            return review("after.canonical is missing (raw value did not parse)")

        try:
            comparator = get_comparator(getattr(record, "value_type", None))
        except (LookupError, TypeError) as exc:
            return review(str(exc))
        evidence["comparator_version"] = comparator.VERSION

        outcomes: list = []
        matched = None
        undecided = False
        for index, candidate in enumerate(candidates):
            outcome: dict = {"index": index, "value": candidate}
            try:
                parsed = comparator.parse(candidate)
            except Exception as exc:
                outcome["outcome"] = f"comparator {comparator.VERSION} raised on parse: {exc!r}"
                outcomes.append(outcome)
                undecided = True
                continue
            if not getattr(parsed, "ok", False) or not isinstance(
                getattr(parsed, "canonical", None), dict
            ):
                outcome["outcome"] = (
                    f"unparseable: {getattr(parsed, 'failure_reason', 'comparator contract violation')}"
                )
                outcomes.append(outcome)
                continue
            try:
                comparison = comparator.compare(after_canonical, parsed.canonical)
            except Exception as exc:
                outcome["outcome"] = f"comparator {comparator.VERSION} raised on compare: {exc!r}"
                outcomes.append(outcome)
                undecided = True
                continue
            if not isinstance(comparison, Comparison) or comparison.verdict not in (
                "equal", "different", "review", "incomparable"
            ):
                outcome["outcome"] = f"comparator {comparator.VERSION} returned an invalid comparison"
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

        evidence["candidates"] = outcomes
        if matched is not None:
            evidence["matched_index"] = matched["index"]
            evidence["matched_value"] = matched["value"]
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
        if undecided:
            evidence["problem"] = (
                f"comparator could not decide agreement with the {self.source_label}"
            )
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)
        evidence["problem"] = (
            f"sources_disagree: no {self.source_label} value matches after.canonical"
        )
        return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)


def _resolve_key_field(record, path: str):
    """Resolve a dotted field path ('entity.name', 'property', 'change_date')
    against a record to a string. A terminal ChangeDate is unwrapped to its
    value. Returns (string, None) or (None, reason)."""
    current = record
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return None, f"missing key {part!r}"
            current = current[part]
        else:
            if not hasattr(current, part):
                return None, f"missing attribute {part!r}"
            current = getattr(current, part)
    if isinstance(current, ChangeDate):
        current = current.value
    if not isinstance(current, str):
        return None, f"resolved to {type(current).__name__}, expected str"
    return current, None


def _raw_value(record, side: str):
    """The raw string of one ValueState side, or None when absent."""
    value = getattr(getattr(record, side, None), "raw", None)
    return value if isinstance(value, str) else None


class DedupGate(Gate):
    """Fail duplicate records, keeping the first fully-valid occurrence.

    The identity key is a tuple of dotted field paths (default:
    entity.name | property | change_date). Seen keys live in a ctx-carried
    dict mapping key tuple -> claimant info (fact_id, record_id, raw
    before/after values); the gate owns this ctx entry and creates it on
    first use.

    A record CLAIMS its key only when every gate result already on the
    record has verdict 'pass' — a record already bound for review or
    exclusion must never knock a later fully-valid record out of the
    included set (this gate is therefore meant to run LAST in gate_list).
    Against a claimed key:

    * matching raw before/after values -> 'fail', a genuine duplicate;
      evidence names the kept record's fact_id and record_id.
    * DIFFERING values -> 'review', never 'fail': the same
      (entity, property, date) key with different values is possibly two
      distinct events (e.g. two departures announced for the same
      effective date), which only a human can collapse. Evidence carries
      both records' values so the review is self-contained.

    A record whose key cannot be computed is 'review' and never claims a
    key. Unclaimed keys stay open, so duplicates among review-bound records
    are deferred to the review queue rather than adjudicated here.

    Determinism: "first fully-valid occurrence wins" is deterministic
    because adapters enumerate candidates in a stable sorted order (an
    adapter contract).
    """

    name = "dedup"
    version = "dedup:v2"

    def __init__(
        self,
        key_fields=("entity.name", "property", "change_date"),
        seen_ctx_key: str = "dedup_seen",
    ):
        self.key_fields = tuple(key_fields)
        self.seen_ctx_key = seen_ctx_key

    def evaluate(self, record, ctx: dict) -> GateResult:
        evidence: dict = {
            "key_fields": list(self.key_fields),
            "seen_ctx_key": self.seen_ctx_key,
        }

        parts = []
        problems = []
        for field_path in self.key_fields:
            value, why = _resolve_key_field(record, field_path)
            if value is None:
                problems.append(f"{field_path}: {why}")
            else:
                parts.append(value)
        if problems:
            evidence["problems"] = problems
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)

        seen = ctx.setdefault(self.seen_ctx_key, {})
        if not isinstance(seen, dict):
            evidence["problem"] = (
                f"ctx[{self.seen_ctx_key!r}] is not a dict: {type(seen).__name__}"
            )
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)

        key = tuple(parts)
        evidence["key"] = list(key)
        values = (_raw_value(record, "before"), _raw_value(record, "after"))

        if key in seen:
            claimant = seen[key]
            evidence["duplicate_of"] = claimant["fact_id"]
            evidence["duplicate_of_record_id"] = claimant["record_id"]
            if values == claimant["values"]:
                evidence["values_match"] = True
                return GateResult(name=self.name, version=self.version, verdict="fail", evidence=evidence)
            evidence["values_match"] = False
            evidence["kept_values"] = list(claimant["values"])
            evidence["this_values"] = list(values)
            evidence["problem"] = (
                "same identity key as an earlier record but with differing values; "
                "possibly two distinct events"
            )
            return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)

        gates_so_far = getattr(record, "gates", None)
        prior_all_passed = isinstance(gates_so_far, list) and all(
            isinstance(g, GateResult) and g.verdict == "pass" for g in gates_so_far
        )
        if prior_all_passed:
            seen[key] = {
                "fact_id": _json_safe(getattr(record, "fact_id", None)),
                "record_id": _json_safe(getattr(record, "record_id", None)),
                "values": values,
            }
            evidence["claimed_key"] = True
        else:
            evidence["claimed_key"] = False
            evidence["note"] = (
                "key not claimed: a prior gate did not pass, so this record must not "
                "shadow a later fully-valid record"
            )
        return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
