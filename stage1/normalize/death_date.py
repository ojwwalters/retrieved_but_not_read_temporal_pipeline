"""Death-date comparator (value_type 'death_date', version 'death_date:v1').

A death date is the ONE fact class the wiki_people source retains under owner
decision A (people = genuinely unpredictable facts only = deaths only). Unlike a
term start or a founding date, a death date has a meaningful ABSENT state: at the
training cutoff a living (or not-yet-recorded) person has no death date, and the
post-cutoff appearance of one is a genuine single-sided change (``''`` -> a date)
that the benchmark must be able to INCLUDE. The plain ``date`` comparator treats
an empty string as a parse FAILURE (correctly, for a field that is always
present), which would strand every added death in ``review`` on the runner's
``normalize`` gate. This comparator gives the empty string a first-class,
comparable canonical instead — WITHOUT touching the shared ``date`` comparator or
any other source.

Canonical form::

    absent:  {"kind": "death_date", "absent": True}
    present: {"kind": "death_date", "absent": False, "date": <date-canonical>}

where ``<date-canonical>`` is EXACTLY the ``date`` comparator's canonical, so all
real date parsing and comparison is delegated to ``date`` (the death date's
precision, ambiguity, and equality rules are the date comparator's, unchanged).

Comparison semantics:

* both absent                 -> 'equal'   (no death date on either side).
* exactly one absent          -> 'different' (a death date was ADDED or REMOVED —
  a genuine single-sided change; the wiki_people ``death_change`` gate then
  applies the owner's single-sided policy: admit an addition, review a removal).
* both present                -> delegated verbatim to the ``date`` comparator.

Both methods are pure and total (never raise): a non-empty non-date string is a
parse FAILURE (so it becomes ``review`` downstream, never a guess), and a
malformed canonical is 'incomparable'.
"""

from __future__ import annotations

from stage1.normalize import Comparator, Comparison, ParseResult, register
from stage1.normalize.date import DateComparator

# A private delegate for all real-date parsing/comparison. It is pure and shares
# no state, so using an instance directly (rather than the registry) keeps this
# module independent of comparator registration order.
_DATE = DateComparator()


class DeathDateComparator(Comparator):
    """Comparator for a death date that may be ABSENT (empty) or PRESENT."""

    NAME = "death_date"
    VERSION = "death_date:v1"

    def parse(self, raw) -> ParseResult:
        if not isinstance(raw, str):
            return ParseResult.failure(f"raw value is not a string: {type(raw).__name__}")
        if not raw.split():  # empty or whitespace-only -> a first-class ABSENT state
            return ParseResult.success({"kind": "death_date", "absent": True})
        parsed = _DATE.parse(raw)
        if not parsed.ok or not isinstance(parsed.canonical, dict):
            return ParseResult.failure(parsed.failure_reason or "unparseable death date")
        return ParseResult.success({"kind": "death_date", "absent": False, "date": parsed.canonical})

    def compare(self, a_canonical, b_canonical) -> Comparison:
        for label, c in (("a", a_canonical), ("b", b_canonical)):
            if not isinstance(c, dict) or c.get("kind") != "death_date" or "absent" not in c:
                return Comparison("incomparable", f"{label}: not a death_date canonical")
        a_absent = bool(a_canonical["absent"])
        b_absent = bool(b_canonical["absent"])
        if a_absent or b_absent:
            if a_absent and b_absent:
                return Comparison("equal", "both death dates absent")
            return Comparison(
                "different",
                "death date present on exactly one side (a single-sided addition or removal)",
            )
        # both present: delegate to the date comparator on the inner canonicals.
        return _DATE.compare(a_canonical.get("date"), b_canonical.get("date"))


register(DeathDateComparator())
