"""Date comparator (value_type 'date', version 'date:v2').

Canonical form::

    {"kind": "date", "y": int, "m": int | None, "d": int | None,
     "precision": "day" | "month" | "year", "ambiguous": bool}

Unknown components are None and ``precision`` names the finest component
present. ``ambiguous`` is True only for numeric little/middle-endian dates
(``05/04/2026``) where both the day-first and month-first readings are valid
calendar dates and disagree; the day-first reading is stored by convention
but compare() never trusts it below year precision.

Parsing is hand-rolled over stdlib only. Accepted forms: ISO
(``2026-02-17``, ``2026-05``, timestamps trimmed), ``YYYY``, ``D Month
YYYY``, ``Month D, YYYY``, ``Month YYYY`` (month names, 3-letter
abbreviations and 'Sept', optional periods, ordinal suffixes, and filler
'the'/'of' tokens), and numeric ``A/B/YYYY`` with '/', '.' or '-' as a
consistent separator plus ``YYYY/MM/DD``. Anything else — including bare
``Month D`` with no year and two-digit years — fails with a reason and
becomes 'review' downstream, never a guess.

Comparison semantics: dates are compared at the coarsest common precision.
Differing years are always 'different'; matching years at year precision are
'equal' even for ambiguous dates (the year is never ambiguous). Below year
precision, ambiguity yields 'review' — two sources may follow different
day/month conventions — with one exception: two IDENTICAL ambiguous
canonicals are 'equal', because either convention maps the identical stored
components to the same day, so equality is convention-invariant.
"""

from __future__ import annotations

import re
from datetime import date as _date

from stage1.normalize import Comparator, Comparison, ParseResult, register

_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{1,2}-\d{1,2})[Tt ]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$"
)
_ISO_DAY_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_YMD_SLASH_RE = re.compile(r"^(\d{4})[/.](\d{1,2})[/.](\d{1,2})$")
_ISO_MONTH_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})$")
_YEAR_RE = re.compile(r"^\d{4}$")
_DMY_NUMERIC_RE = re.compile(r"^(\d{1,2})([/.\-])(\d{1,2})\2(\d{4})$")
_ORDINAL_RE = re.compile(r"^(\d{1,2})(st|nd|rd|th)$")
_DAY_TOKEN_RE = re.compile(r"^\d{1,2}$")

_PRECISION_RANK = {"year": 0, "month": 1, "day": 2}


def _valid_ymd(y: int, m: int, d: int) -> bool:
    try:
        _date(y, m, d)
    except ValueError:
        return False
    return True


def _canon(y: int, m, d, precision: str, ambiguous: bool) -> dict:
    return {"kind": "date", "y": y, "m": m, "d": d, "precision": precision, "ambiguous": ambiguous}


def _word_tokens(text: str) -> list:
    """Lowercased tokens with commas/periods, ordinal suffixes, and filler
    'the'/'of' removed — the input to the word-form date patterns."""
    tokens = []
    for tok in text.replace(",", " ").split():
        t = tok.strip(".").lower()
        if t in ("the", "of") or not t:
            continue
        mo = _ORDINAL_RE.match(t)
        if mo:
            t = mo.group(1)
        tokens.append(t)
    return tokens


def _canonical_error(c):
    """Return a reason string when c is not a valid date canonical, else None."""
    if not isinstance(c, dict):
        return f"canonical is not a dict: {type(c).__name__}"
    if c.get("kind") != "date":
        return f"expected kind 'date', got {c.get('kind')!r}"
    for key in ("y", "m", "d", "precision", "ambiguous"):
        if key not in c:
            return f"canonical missing key {key!r}"
    if not isinstance(c["y"], int) or isinstance(c["y"], bool):
        return f"canonical y is not an int: {c['y']!r}"
    if c["precision"] not in _PRECISION_RANK:
        return f"canonical precision is not day/month/year: {c['precision']!r}"
    if not isinstance(c["ambiguous"], bool):
        return f"canonical ambiguous is not bool: {c['ambiguous']!r}"
    rank = _PRECISION_RANK[c["precision"]]
    if rank >= 1:
        if not isinstance(c["m"], int) or isinstance(c["m"], bool) or not 1 <= c["m"] <= 12:
            return f"canonical m invalid for precision {c['precision']!r}: {c['m']!r}"
    elif c["m"] is not None:
        return f"canonical m must be None at year precision: {c['m']!r}"
    if rank == 2:
        if not isinstance(c["d"], int) or isinstance(c["d"], bool) or not 1 <= c["d"] <= 31:
            return f"canonical d invalid for precision 'day': {c['d']!r}"
    elif c["d"] is not None:
        return f"canonical d must be None at {c['precision']!r} precision: {c['d']!r}"
    return None


class DateComparator(Comparator):
    """Comparator for calendar dates of day, month, or year precision."""

    NAME = "date"
    VERSION = "date:v2"

    def parse(self, raw) -> ParseResult:
        if not isinstance(raw, str):
            return ParseResult.failure(f"raw value is not a string: {type(raw).__name__}")
        text = " ".join(raw.split())
        if not text:
            return ParseResult.failure("empty raw value")

        mo = _TIMESTAMP_RE.match(text)
        if mo:
            text = mo.group(1)

        mo = _ISO_DAY_RE.match(text) or _YMD_SLASH_RE.match(text)
        if mo:
            y, m, d = int(mo.group(1)), int(mo.group(2)), int(mo.group(3))
            if not _valid_ymd(y, m, d):
                return ParseResult.failure(f"not a real calendar date: {text!r}")
            return ParseResult.success(_canon(y, m, d, "day", False))

        mo = _ISO_MONTH_RE.match(text)
        if mo:
            y, m = int(mo.group(1)), int(mo.group(2))
            if not 1 <= m <= 12:
                return ParseResult.failure(f"month out of range: {text!r}")
            return ParseResult.success(_canon(y, m, None, "month", False))

        if _YEAR_RE.match(text):
            return ParseResult.success(_canon(int(text), None, None, "year", False))

        mo = _DMY_NUMERIC_RE.match(text)
        if mo:
            return self._parse_numeric_dmy(int(mo.group(1)), int(mo.group(3)), int(mo.group(4)), text)

        tokens = _word_tokens(text)
        if len(tokens) == 3 and _YEAR_RE.match(tokens[2]):
            y = int(tokens[2])
            if _DAY_TOKEN_RE.match(tokens[0]) and tokens[1] in _MONTHS:
                d, m = int(tokens[0]), _MONTHS[tokens[1]]
            elif tokens[0] in _MONTHS and _DAY_TOKEN_RE.match(tokens[1]):
                m, d = _MONTHS[tokens[0]], int(tokens[1])
            else:
                return ParseResult.failure(f"unrecognized date format: {raw[:80]!r}")
            if not _valid_ymd(y, m, d):
                return ParseResult.failure(f"not a real calendar date: {text!r}")
            return ParseResult.success(_canon(y, m, d, "day", False))
        if len(tokens) == 2 and tokens[0] in _MONTHS and _YEAR_RE.match(tokens[1]):
            return ParseResult.success(_canon(int(tokens[1]), _MONTHS[tokens[0]], None, "month", False))

        return ParseResult.failure(f"unrecognized date format: {raw[:80]!r}")

    def _parse_numeric_dmy(self, a: int, b: int, y: int, text: str) -> ParseResult:
        """Resolve A<sep>B<sep>YYYY. Day-first (d=A, m=B) and month-first
        (m=A, d=B) readings are both tried; when both are valid and disagree
        the date is ambiguous and stored day-first by convention."""
        day_first_ok = _valid_ymd(y, b, a)
        month_first_ok = _valid_ymd(y, a, b)
        if day_first_ok and month_first_ok:
            if a == b:
                return ParseResult.success(_canon(y, a, b, "day", False))
            return ParseResult.success(_canon(y, b, a, "day", True))
        if day_first_ok:
            return ParseResult.success(_canon(y, b, a, "day", False))
        if month_first_ok:
            return ParseResult.success(_canon(y, a, b, "day", False))
        return ParseResult.failure(f"no valid day/month reading: {text!r}")

    def compare(self, a_canonical, b_canonical) -> Comparison:
        for label, c in (("a", a_canonical), ("b", b_canonical)):
            err = _canonical_error(c)
            if err:
                return Comparison("incomparable", f"{label}: {err}")
        a, b = a_canonical, b_canonical

        if a["y"] != b["y"]:
            return Comparison("different", f"years differ: {a['y']} vs {b['y']}")
        common = min(_PRECISION_RANK[a["precision"]], _PRECISION_RANK[b["precision"]])
        if common == 0:
            return Comparison("equal", "equal at year precision (coarsest common)")
        if a["ambiguous"] or b["ambiguous"]:
            if (
                a["ambiguous"] and b["ambiguous"]
                and a["precision"] == b["precision"]
                and a["m"] == b["m"] and a["d"] == b["d"]
            ):
                return Comparison(
                    "equal",
                    "identical ambiguous canonicals; equal under either day/month convention",
                )
            return Comparison(
                "review",
                "day/month order ambiguous in at least one date; cannot compare below year precision",
            )
        if a["m"] != b["m"]:
            return Comparison("different", f"months differ: {a['m']} vs {b['m']}")
        if common == 1:
            return Comparison("equal", "equal at month precision (coarsest common)")
        if a["d"] != b["d"]:
            return Comparison("different", f"days differ: {a['d']} vs {b['d']}")
        return Comparison("equal", "")


register(DateComparator())
