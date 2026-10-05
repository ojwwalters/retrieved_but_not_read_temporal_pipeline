"""Absent-able comparators for the secondary DEATH properties
(value_types 'death_place' and 'death_cause').

The redesigned wiki_people source records a death as up to THREE single-sided
fact changes read from the Wikipedia infobox: the death DATE (value_type
'death_date', see stage1.normalize.death_date) plus, when the infobox carries
them, the death PLACE and death CAUSE. Like the death date, place and cause
have a meaningful ABSENT state: at the training cutoff a living person's
infobox shows none of them, and the post-cutoff appearance of one is a genuine
single-sided change (``''`` -> a value) that the benchmark must be able to
INCLUDE. The plain ``org``/``text_span`` comparators treat an empty string as
a parse FAILURE (correctly, for always-present fields), which would strand
every added death circumstance in ``review`` on the runner's ``normalize``
gate. These comparators give the empty string a first-class, comparable
canonical instead — WITHOUT touching the shared org/text_span comparators or
any other source (exactly the death_date pattern).

Canonical form::

    absent:  {"kind": "<name>", "absent": True}
    present: {"kind": "<name>", "absent": False, "value": <delegate-canonical>}

where ``<delegate-canonical>`` is EXACTLY the delegate comparator's canonical
('org' for death_place — place values are Wikidata items compared by
sitelink/label token identity; 'text_span' for death_cause — a free-text
cause), so all real parsing and comparison is delegated unchanged.

Comparison semantics (mirroring death_date:v1):

* both absent                 -> 'equal'.
* exactly one absent          -> 'different' (a single-sided addition/removal;
  the wiki_people ``death_change`` gate then applies the owner's single-sided
  policy: admit an addition, review a removal).
* both present                -> delegated verbatim to the delegate comparator.

Both methods are pure and total (never raise)."""

from __future__ import annotations

from stage1.normalize import Comparator, Comparison, ParseResult, register
from stage1.normalize.org import OrgComparator
from stage1.normalize.text_span import TextSpanComparator


class _AbsentableComparator(Comparator):
    """Wrap a delegate comparator with a first-class ABSENT (empty) state.

    Subclasses set NAME/VERSION and ``_DELEGATE`` (a pure, stateless delegate
    instance — used directly rather than via the registry so this module is
    independent of registration order, mirroring death_date)."""

    _DELEGATE: Comparator = None  # set by subclasses

    def parse(self, raw) -> ParseResult:
        if not isinstance(raw, str):
            return ParseResult.failure(f"raw value is not a string: {type(raw).__name__}")
        if not raw.split():  # empty or whitespace-only -> a first-class ABSENT state
            return ParseResult.success({"kind": self.NAME, "absent": True})
        parsed = self._DELEGATE.parse(raw)
        if not parsed.ok or not isinstance(parsed.canonical, dict):
            return ParseResult.failure(parsed.failure_reason or f"unparseable {self.NAME}")
        return ParseResult.success(
            {"kind": self.NAME, "absent": False, "value": parsed.canonical}
        )

    def compare(self, a_canonical, b_canonical) -> Comparison:
        for label, c in (("a", a_canonical), ("b", b_canonical)):
            if not isinstance(c, dict) or c.get("kind") != self.NAME or "absent" not in c:
                return Comparison("incomparable", f"{label}: not a {self.NAME} canonical")
        a_absent = bool(a_canonical["absent"])
        b_absent = bool(b_canonical["absent"])
        if a_absent or b_absent:
            if a_absent and b_absent:
                return Comparison("equal", f"both {self.NAME} values absent")
            return Comparison(
                "different",
                f"{self.NAME} present on exactly one side (a single-sided addition or removal)",
            )
        return self._DELEGATE.compare(a_canonical.get("value"), b_canonical.get("value"))


class DeathPlaceComparator(_AbsentableComparator):
    """Death place that may be ABSENT; present values delegate to 'org'."""

    NAME = "death_place"
    VERSION = "death_place:v1"
    _DELEGATE = OrgComparator()


class DeathCauseComparator(_AbsentableComparator):
    """Death cause that may be ABSENT; present values delegate to 'text_span'."""

    NAME = "death_cause"
    VERSION = "death_cause:v1"
    _DELEGATE = TextSpanComparator()


register(DeathPlaceComparator())
register(DeathCauseComparator())
