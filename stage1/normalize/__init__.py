"""Comparator interface and registry for value normalization.

A comparator owns one value_type ('person_name', 'quantity', ...). It turns a
raw extracted string into a canonical dict (parse) and decides whether two
canonical dicts denote the same value (compare). Comparators are PURE
functions of their inputs: no I/O, no caches, no clock, no randomness — the
same raw string must canonicalize identically forever (bump VERSION when the
rules change).

Implementing a comparator (in stage1/normalize/<value_type>.py):

    from stage1.normalize import Comparator, Comparison, ParseResult, register

    class PersonNameComparator(Comparator):
        NAME = "person_name"          # the value_type this handles
        VERSION = "person_name:v1"    # recorded in gate evidence
        def parse(self, raw): ...
        def compare(self, a, b): ...

    register(PersonNameComparator())

Failure semantics: parse() NEVER raises on bad input — it returns
ParseResult.failure(reason). compare() never raises either — genuinely
undecidable pairs get verdict 'review', structurally incompatible canonical
dicts get 'incomparable'. The pipeline turns these into review dispositions;
a raised exception is a bug, not a data verdict (normalize_record still
contains it defensively).
"""

from __future__ import annotations

from dataclasses import dataclass

COMPARISON_VERDICTS = ("equal", "different", "review", "incomparable")


@dataclass
class ParseResult:
    """Outcome of Comparator.parse().

    ok: True iff parsing succeeded.
    canonical: the canonical dict when ok, else None. The dict shape is owned
        by the comparator and must be JSON-safe and deterministic (stable key
        order is not required — dicts are compared by content).
    failure_reason: short human-readable reason when not ok, else None.
    """

    ok: bool
    canonical: dict | None
    failure_reason: str | None

    @classmethod
    def success(cls, canonical: dict) -> "ParseResult":
        return cls(ok=True, canonical=canonical, failure_reason=None)

    @classmethod
    def failure(cls, reason: str) -> "ParseResult":
        return cls(ok=False, canonical=None, failure_reason=reason)


@dataclass
class Comparison:
    """Outcome of Comparator.compare().

    verdict:
        'equal'        — same value (a change candidate with equal before/after
                         is spurious).
        'different'    — genuinely different values.
        'review'       — the comparator cannot decide (e.g. nickname vs legal
                         name); a human or a later gate must look.
        'incomparable' — the canonical dicts are structurally incompatible
                         (wrong shape, wrong units with no conversion). Also
                         a review-path outcome, kept distinct for diagnostics.
    reason: short explanation, always non-empty for non-'equal' verdicts.
    """

    verdict: str
    reason: str = ""


class Comparator:
    """Base class for value_type comparators.

    Subclasses set NAME (the value_type key) and VERSION (e.g.
    'person_name:v1'; recorded in gate evidence so releases are auditable)
    and implement parse() and compare(). Both must be pure and total: any
    input yields a return value, never an exception.
    """

    NAME: str = ""
    VERSION: str = ""

    def parse(self, raw: str) -> ParseResult:
        """Parse a raw extracted string into canonical form.

        Must accept ANY string (empty, garbage, wrong language) and return
        ParseResult.failure(reason) rather than raising.
        """
        raise NotImplementedError(f"{type(self).__name__}.parse is abstract")

    def compare(self, a_canonical: dict, b_canonical: dict) -> Comparison:
        """Compare two canonical dicts produced by this comparator's parse().

        Must return a Comparison with a verdict from COMPARISON_VERDICTS,
        never raise. Inputs of unexpected shape yield 'incomparable'.
        """
        raise NotImplementedError(f"{type(self).__name__}.compare is abstract")


_REGISTRY: dict = {}


def register(comparator) -> "Comparator":
    """Register a comparator instance (or class, which gets instantiated).

    Keyed by NAME. Re-registering the same class for a NAME is an idempotent
    no-op (safe under re-imports); registering a DIFFERENT class for an
    already-claimed NAME raises ValueError — two comparators for one
    value_type would make releases ambiguous.
    Returns the registered instance (usable as a class decorator).
    """
    instance = comparator() if isinstance(comparator, type) else comparator
    if not isinstance(instance, Comparator):
        raise ValueError(f"register: expected a Comparator, got {type(instance).__name__}")
    if not instance.NAME or not instance.VERSION:
        raise ValueError(
            f"register: {type(instance).__name__} must set non-empty NAME and VERSION "
            f"(got NAME={instance.NAME!r}, VERSION={instance.VERSION!r})"
        )
    existing = _REGISTRY.get(instance.NAME)
    if existing is not None and type(existing) is not type(instance):
        raise ValueError(
            f"register: value_type {instance.NAME!r} already registered by "
            f"{type(existing).__name__}; refusing to replace with {type(instance).__name__}"
        )
    _REGISTRY[instance.NAME] = instance
    return instance


def get_comparator(value_type: str) -> "Comparator":
    """Return the registered comparator for value_type.

    Raises LookupError naming the missing type and listing what IS registered.
    Callers on the data path (normalize_record) catch this and convert it to
    review-gate evidence instead of letting it propagate.
    """
    try:
        return _REGISTRY[value_type]
    except KeyError:
        raise LookupError(
            f"no comparator registered for value_type {value_type!r}; "
            f"registered: {sorted(_REGISTRY)}"
        ) from None


def registered_value_types() -> list:
    """Sorted list of value_types with a registered comparator."""
    return sorted(_REGISTRY)


def normalize_record(record) -> dict:
    """Fill record.before.canonical / record.after.canonical via the registry.

    Mutates the record in place: each side's canonical is set when its raw
    parses, left as None otherwise. NEVER raises — every failure mode is
    reported in the returned info dict, which the runner turns into the
    evidence of a 'normalize' gate result:

        {
          "value_type": str,
          "comparator_found": bool,
          "comparator_version": str | None,   # e.g. "person_name:v1"
          "before": {"ok": bool, "failure_reason": str | None} | None,
          "after":  {"ok": bool, "failure_reason": str | None} | None,
        }

    before/after are None only when no comparator was found (nothing was
    attempted). A comparator that raises, or returns something other than a
    ParseResult, or returns ok=True with a non-dict canonical, is reported as
    a parse failure with a reason naming the contract violation.
    """
    info: dict = {
        "value_type": record.value_type,
        "comparator_found": False,
        "comparator_version": None,
        "before": None,
        "after": None,
    }
    try:
        comparator = get_comparator(record.value_type)
    except LookupError as exc:
        info["error"] = str(exc)
        return info

    info["comparator_found"] = True
    info["comparator_version"] = comparator.VERSION

    for side_name in ("before", "after"):
        state = getattr(record, side_name)
        result = _safe_parse(comparator, state.raw)
        if result.ok:
            state.canonical = result.canonical
        info[side_name] = {"ok": result.ok, "failure_reason": result.failure_reason}
    return info


def _safe_parse(comparator: Comparator, raw) -> ParseResult:
    """Run comparator.parse under the full contract, converting violations
    (raises, wrong return type, non-dict canonical) into ParseResult.failure."""
    if not isinstance(raw, str):
        return ParseResult.failure(f"raw value is not a string: {type(raw).__name__}")
    try:
        result = comparator.parse(raw)
    except Exception as exc:  # comparator contract violation, not a data verdict
        return ParseResult.failure(f"comparator {comparator.VERSION} raised: {exc!r}")
    if not isinstance(result, ParseResult):
        return ParseResult.failure(
            f"comparator {comparator.VERSION} returned {type(result).__name__}, expected ParseResult"
        )
    if result.ok and not isinstance(result.canonical, dict):
        return ParseResult.failure(
            f"comparator {comparator.VERSION} returned ok=True with non-dict canonical "
            f"({type(result.canonical).__name__})"
        )
    return result
