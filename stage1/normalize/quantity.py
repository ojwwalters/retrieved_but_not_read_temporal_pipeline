"""Quantity comparator (value_type 'quantity', version 'quantity:v2').

Canonical form::

    {"kind": "quantity", "value": float, "unit": str | None,
     "currency": str | None, "is_range": bool, "note": str | None,
     "alt_readings": list}

plus ``{"low": float, "high": float}`` when ``is_range`` is True (``value``
is then the midpoint). ``note`` records human-readable caveats and never
participates in compare(). ``alt_readings`` is the machine-readable form of
ambiguity: every defensible alternative reading of the raw string, as
``{"value", "unit"}`` scalars or ``{"low", "high", "unit"}`` ranges.
compare() enumerates all readings of both sides and returns 'review'
whenever the verdict is not invariant across them — an ambiguous rendering
must never fabricate a confident 'different' or 'equal'.

Deliberate design decisions (versioned — changing any of them bumps VERSION):

* Magnitudes: attached suffixes (``5k``, ``1.2M``, ``3.4bn``) and separate
  words (``million``, ``bn``, ``crore``, ``lakh``) scale the value. An
  ATTACHED ``m``/``M`` reads as million (the dominant filing convention);
  when the number carries a decimal point and no currency (``1.85m``, a
  plausible compact metric length) the metres reading is carried in
  alt_readings. ``mm`` reads as millimetres, except in a currency context
  (``$5mm``) where it is finance-style millions.
* Separators: ``1,000,000`` and European ``1.000.000`` both parse. A single
  separator group is ambiguous across conventions: the conservative reading
  (comma -> grouping, dot -> decimal) is the primary value and the other
  convention's reading is carried in alt_readings, so ``1,500`` vs ``1.500``
  is 'review', never a fabricated fact change. Identical canonicals compare
  'equal': under either convention the same rendering denotes the same
  number.
* Currencies: £/GBP, $/US$/USD, €/EUR; bare ``$`` maps to USD. Differing
  currencies are 'incomparable'; a currency marker on only one side is
  'review'; conflicting markers in one raw string fail to parse. In a
  currency context ``pound(s)`` is the GBP marker (conflicting with a
  non-GBP marker fails to parse), and a canonical may never carry both a
  currency and a physical unit — such values fail to parse and become
  'review' downstream instead of comparing money against mass.
* Percent is its own unit (``%``), never converted to a fraction; percent vs
  non-percent is 'incomparable'.
* Units: a small hand-rolled table converts length to metres, mass to
  kilograms, and durations to days (``ton`` is the US short ton, ``tonne``
  metric; month/year use mean Gregorian lengths). Compound feet-inches
  (``6 ft 2 in``, ``6'2"``) collapse to metres. Unknown unit strings are kept
  case-folded and are comparable only when fold-equal.
* Equality: values converted between physical units (base units m/kg/day)
  are 'equal' within the constructor-configurable relative tolerance
  (default 1%) — cross-unit rounding like 6 ft 2 in vs 188 cm is expected.
  Everything else (unit-free counts, money, percentages, unknown units) is
  'equal' only at float precision (EXACT_MATCH_TOLERANCE): a +1 caps
  increment or a +300 capacity change is a genuine change, never cosmetic.
* Ranges (``10-15``, ``1 to 2 billion``) never auto-resolve against a single
  value ('review'). Range vs range: 'equal' when both endpoint pairs match,
  'different' when disjoint, otherwise 'review'.

Known limitations (v2): space-grouped digits (``1 000 000``), scientific
notation, multi-word units (``metric ton``), and negative ranges do not
parse — they fail with a reason and become 'review' downstream.
"""

from __future__ import annotations

import math
import re

from stage1.normalize import Comparator, Comparison, ParseResult, register

DEFAULT_RELATIVE_TOLERANCE = 0.01
EXACT_MATCH_TOLERANCE = 1e-9

_CONVERTED_BASE_UNITS = frozenset({"m", "kg", "day"})

_CURRENCY_CODE_RE = re.compile(r"(?<![A-Za-z])(usd|gbp|eur)(?![A-Za-z])", re.IGNORECASE)
_CURRENCY_SYMBOLS = {"US$": "USD", "$": "USD", "£": "GBP", "€": "EUR"}
_CURRENCY_SYM_RE = re.compile("US\\$|[$£€]")

_NUM_TOKEN = r"(?:\d[\d.,]*\d|\d|\.\d+)"
_SINGLE_RE = re.compile(
    r"^([+-])?\s*(" + _NUM_TOKEN + r")"
    r'([A-Za-z%\'"′″]*)'
    r"(.*)$"
)
_RANGE_RE = re.compile(
    r"^(" + _NUM_TOKEN + r")\s*(?:-|–|—|\bto\b)\s*(" + _NUM_TOKEN + r")(.*)$",
    re.IGNORECASE,
)
_RANGE_ATTACHED_RE = re.compile(r'^([A-Za-z%\'"′″]+)(.*)$')
_FEET_INCHES_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(?:feet|foot|ft\.?|'|′)\s*"
    r'(\d+(?:\.\d+)?)\s*(?:inches|inch|in\.?|"|″)?\s*$',
    re.IGNORECASE,
)

_INT_RE = re.compile(r"^\d+$")
_COMMA_GROUPED_RE = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$")
_DOT_GROUPED_RE = re.compile(r"^\d{1,3}(\.\d{3})+(,\d+)?$")
_DOT_DECIMAL_RE = re.compile(r"^(?:\d+)?\.\d+$")
_DOT_DECIMAL_AMBIGUOUS_RE = re.compile(r"^(?!0\.)\d{1,3}\.\d{3}$")
_COMMA_DECIMAL_RE = re.compile(r"^\d+,\d+$")
_UNKNOWN_UNIT_RE = re.compile(r"^[a-z][a-z\-]*$")

_MAGNITUDES = {
    "k": 1e3, "thousand": 1e3, "thousands": 1e3,
    "mn": 1e6, "million": 1e6, "millions": 1e6,
    "b": 1e9, "bn": 1e9, "billion": 1e9, "billions": 1e9,
    "tn": 1e12, "trn": 1e12, "trillion": 1e12, "trillions": 1e12,
    "crore": 1e7, "crores": 1e7,
    "lakh": 1e5, "lakhs": 1e5,
}


def _aliases(base: str, factor: float, *names: str) -> dict:
    return {name: (base, factor) for name in names}


_UNIT_ALIASES: dict = {}
_UNIT_ALIASES.update(_aliases("m", 0.001, "mm", "millimeter", "millimeters", "millimetre", "millimetres"))
_UNIT_ALIASES.update(_aliases("m", 0.01, "cm", "centimeter", "centimeters", "centimetre", "centimetres"))
_UNIT_ALIASES.update(_aliases("m", 1.0, "m", "meter", "meters", "metre", "metres"))
_UNIT_ALIASES.update(_aliases("m", 1000.0, "km", "kilometer", "kilometers", "kilometre", "kilometres"))
_UNIT_ALIASES.update(_aliases("m", 0.0254, "in", "inch", "inches", '"', "″"))
_UNIT_ALIASES.update(_aliases("m", 0.3048, "ft", "foot", "feet", "'", "′"))
_UNIT_ALIASES.update(_aliases("m", 0.9144, "yd", "yard", "yards"))
_UNIT_ALIASES.update(_aliases("m", 1609.344, "mi", "mile", "miles"))
_UNIT_ALIASES.update(_aliases("kg", 1e-6, "mg", "milligram", "milligrams"))
_UNIT_ALIASES.update(_aliases("kg", 0.001, "g", "gram", "grams"))
_UNIT_ALIASES.update(_aliases("kg", 1.0, "kg", "kilogram", "kilograms"))
_UNIT_ALIASES.update(_aliases("kg", 0.45359237, "lb", "lbs", "pound", "pounds"))
_UNIT_ALIASES.update(_aliases("kg", 0.028349523125, "oz", "ounce", "ounces"))
_UNIT_ALIASES.update(_aliases("kg", 1000.0, "tonne", "tonnes"))
_UNIT_ALIASES.update(_aliases("kg", 907.18474, "ton", "tons"))
_UNIT_ALIASES.update(_aliases("day", 1.0, "day", "days"))
_UNIT_ALIASES.update(_aliases("day", 7.0, "week", "weeks", "wk", "wks"))
_UNIT_ALIASES.update(_aliases("day", 30.436875, "month", "months", "mo", "mos"))
_UNIT_ALIASES.update(_aliases("day", 365.2425, "year", "years", "yr", "yrs"))


def _extract_currency(text: str):
    """Remove currency markers from text. Returns (currency, cleaned, error)."""
    found = set()

    def _code_sub(mo):
        found.add(mo.group(1).upper())
        return " "

    def _sym_sub(mo):
        found.add(_CURRENCY_SYMBOLS[mo.group(0)])
        return " "

    cleaned = _CURRENCY_CODE_RE.sub(_code_sub, text)
    cleaned = _CURRENCY_SYM_RE.sub(_sym_sub, cleaned)
    if len(found) > 1:
        return None, text, f"conflicting currency markers {sorted(found)} in {text!r}"
    return (next(iter(found)) if found else None), cleaned, None


def _parse_number(token: str):
    """Parse one numeric token. Returns (value, alt_value, note, error).

    Grouping/decimal conventions: unambiguous forms parse directly; a single
    separator group takes the conservative reading (comma -> grouping,
    dot -> decimal) with the other convention's reading returned as
    alt_value so compare() can treat the ambiguity honestly.
    """
    if _INT_RE.match(token):
        return float(token), None, None, None
    mo = _COMMA_GROUPED_RE.match(token)
    if mo:
        value = float(token.replace(",", ""))
        if mo.group(2) is None and token.count(",") == 1:
            alt = float(token.replace(",", "."))
            note = (
                f"{token!r}: comma read as thousands separator "
                f"(European decimal reading would be {alt!r})"
            )
            return value, alt, note, None
        return value, None, None, None
    mo = _DOT_GROUPED_RE.match(token)
    if mo and (mo.group(2) is not None or token.count(".") >= 2):
        return float(token.replace(".", "").replace(",", ".")), None, None, None
    if _DOT_DECIMAL_RE.match(token):
        value = float(token)
        if _DOT_DECIMAL_AMBIGUOUS_RE.match(token):
            alt = float(token.replace(".", ""))
            note = (
                f"{token!r}: dot read as decimal point "
                f"(European grouping reading would be {alt!r})"
            )
            return value, alt, note, None
        return value, None, None, None
    if _COMMA_DECIMAL_RE.match(token):
        return float(token.replace(",", ".")), None, f"{token!r}: comma read as decimal separator", None
    return None, None, None, f"unparseable number token {token!r}"


def _resolve_suffix(attached: str, tokens: list, currency, decimal_number: bool):
    """Interpret an attached suffix plus trailing word tokens.

    Returns (magnitude, unit, factor, alt_suffix, notes, error) where unit is
    a base unit symbol ('m'/'kg'/'day'), '%', a folded unknown-unit string,
    or None; factor converts the raw value into that base unit; and
    alt_suffix is None or an (alt_magnitude, alt_unit, alt_factor) triple
    carrying an alternative reading of an ambiguous suffix. ``currency`` and
    ``decimal_number`` provide the context that disambiguates 'mm', 'm' and
    'pound(s)'.
    """
    magnitude = None
    unit = None
    factor = 1.0
    alt_suffix = None
    notes: list = []

    att = attached.lower()
    if att:
        if att in ("%", "percent", "pct"):
            unit = "%"
        elif att == "m":
            magnitude = 1e6
            if currency is None and decimal_number:
                alt_suffix = (None, "m", 1.0)
                notes.append(
                    "attached suffix 'm' is ambiguous: million (primary) or metres (alternative)"
                )
            else:
                notes.append("attached suffix 'm' read as million (metres must be a separate token)")
        elif att == "mm" and currency is not None:
            magnitude = 1e6
            notes.append("'mm' read as finance-style millions (currency context)")
        elif att in _UNIT_ALIASES:
            unit, factor = _UNIT_ALIASES[att]
        elif att in _MAGNITUDES:
            magnitude = _MAGNITUDES[att]
        else:
            unit = att

    for tok in tokens:
        t = tok.lower().rstrip(".")
        if not t:
            continue
        if t in ("%", "percent", "pct"):
            if unit is not None and unit != "%":
                return None, None, None, None, None, f"conflicting unit and percent token {tok!r}"
            unit = "%"
        elif currency is not None and t == "mm":
            if magnitude is not None or unit is not None:
                return None, None, None, None, None, f"unexpected magnitude token {tok!r}"
            magnitude = 1e6
            notes.append("'mm' read as finance-style millions (currency context)")
        elif currency is not None and t in ("pound", "pounds"):
            if currency != "GBP":
                return None, None, None, None, None, (
                    f"conflicting currency markers: {currency} and 'pounds'"
                )
            notes.append("'pounds' read as the GBP currency marker (currency context)")
        elif t in _MAGNITUDES and t not in _UNIT_ALIASES:
            if magnitude is not None or unit is not None:
                return None, None, None, None, None, f"unexpected magnitude token {tok!r}"
            magnitude = _MAGNITUDES[t]
        elif t in _UNIT_ALIASES:
            if unit is not None:
                return None, None, None, None, None, f"conflicting unit token {tok!r}"
            unit, factor = _UNIT_ALIASES[t]
        elif _UNKNOWN_UNIT_RE.match(t):
            if unit is not None:
                return None, None, None, None, None, f"unrecognized trailing token {tok!r}"
            unit = t
        else:
            return None, None, None, None, None, f"unrecognized trailing token {tok!r}"
    return magnitude, unit, factor, alt_suffix, notes, None


def _scalar(value: float, unit, currency, notes: list, alt_readings=None) -> dict:
    return {
        "kind": "quantity",
        "value": value,
        "unit": unit,
        "currency": currency,
        "is_range": False,
        "note": "; ".join(notes) if notes else None,
        "alt_readings": list(alt_readings or []),
    }


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _canonical_error(c):
    """Return a reason string when c is not a valid quantity canonical, else None."""
    if not isinstance(c, dict):
        return f"canonical is not a dict: {type(c).__name__}"
    if c.get("kind") != "quantity":
        return f"expected kind 'quantity', got {c.get('kind')!r}"
    for key in ("value", "unit", "currency", "is_range"):
        if key not in c:
            return f"canonical missing key {key!r}"
    if not _is_number(c["value"]):
        return f"canonical value is not a finite number: {c['value']!r}"
    if not (c["unit"] is None or isinstance(c["unit"], str)):
        return f"canonical unit is not str|None: {c['unit']!r}"
    if not (c["currency"] is None or isinstance(c["currency"], str)):
        return f"canonical currency is not str|None: {c['currency']!r}"
    if not isinstance(c["is_range"], bool):
        return f"canonical is_range is not bool: {c['is_range']!r}"
    if c["is_range"]:
        for key in ("low", "high"):
            if key not in c or not _is_number(c[key]):
                return f"range canonical missing finite {key!r}"
    alts = c.get("alt_readings", [])
    if not isinstance(alts, list):
        return f"canonical alt_readings is not a list: {type(alts).__name__}"
    for i, reading in enumerate(alts):
        if not isinstance(reading, dict):
            return f"alt_readings[{i}] is not a dict"
        if not (reading.get("unit") is None or isinstance(reading.get("unit"), str)):
            return f"alt_readings[{i}] unit is not str|None: {reading.get('unit')!r}"
        keys = ("low", "high") if c["is_range"] else ("value",)
        for key in keys:
            if not _is_number(reading.get(key)):
                return f"alt_readings[{i}] missing finite {key!r}"
    return None


def _close(x: float, y: float, tolerance: float) -> bool:
    if x == y:
        return True
    denom = max(abs(x), abs(y))
    if denom == 0.0:
        return True
    return abs(x - y) / denom <= tolerance


def _identity(c: dict):
    """Comparison identity of a canonical: everything except the note.
    Identical identities denote the same rendering, hence the same value
    under any consistent reading of an ambiguous convention."""
    alts = tuple(sorted(tuple(sorted(r.items(), key=lambda kv: kv[0])) for r in c.get("alt_readings", [])))
    return (
        c["value"], c["unit"], c["currency"], c["is_range"],
        c.get("low"), c.get("high"), alts,
    )


def _readings(c: dict) -> list:
    """All readings of a canonical: the primary one, then the alternatives."""
    if c["is_range"]:
        primary = {"low": c["low"], "high": c["high"], "unit": c["unit"]}
    else:
        primary = {"value": c["value"], "unit": c["unit"]}
    return [primary] + list(c.get("alt_readings", []))


def _core_verdict(ra: dict, rb: dict, tolerance: float, is_range: bool):
    """Verdict for one (reading, reading) pair. Returns (verdict, reason)."""
    ua, ub = ra.get("unit"), rb.get("unit")
    if ua != ub:
        if ua is not None and ub is not None:
            return "incomparable", f"unit mismatch: {ua!r} vs {ub!r}"
        return "review", f"unit on one side only: {ua!r} vs {ub!r}"
    tol = tolerance if ua in _CONVERTED_BASE_UNITS else EXACT_MATCH_TOLERANCE
    if is_range:
        if _close(ra["low"], rb["low"], tol) and _close(ra["high"], rb["high"], tol):
            return "equal", "range endpoints match"
        if ra["high"] < rb["low"] or rb["high"] < ra["low"]:
            return "different", (
                f"disjoint ranges: [{ra['low']!r}, {ra['high']!r}] vs [{rb['low']!r}, {rb['high']!r}]"
            )
        return "review", (
            f"overlapping but unequal ranges: [{ra['low']!r}, {ra['high']!r}] vs [{rb['low']!r}, {rb['high']!r}]"
        )
    if _close(ra["value"], rb["value"], tol):
        return "equal", ""
    return "different", f"values differ: {ra['value']!r} vs {rb['value']!r}"


class QuantityComparator(Comparator):
    """Comparator for magnitude/currency/unit-laden numeric values."""

    NAME = "quantity"
    VERSION = "quantity:v2"

    def __init__(self, tolerance: float = DEFAULT_RELATIVE_TOLERANCE):
        self.tolerance = float(tolerance)

    def parse(self, raw) -> ParseResult:
        if not isinstance(raw, str):
            return ParseResult.failure(f"raw value is not a string: {type(raw).__name__}")
        text = raw.replace("−", "-").replace(" ", " ")
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return ParseResult.failure("empty raw value")

        currency, text, err = _extract_currency(text)
        if err:
            return ParseResult.failure(err)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return ParseResult.failure(f"no number found in {raw[:80]!r}")

        mo = _FEET_INCHES_RE.match(text)
        if mo:
            if currency is not None:
                return ParseResult.failure(
                    f"value carries both a currency ({currency}) and a physical unit: {raw[:80]!r}"
                )
            value = float(mo.group(1)) * 0.3048 + float(mo.group(2)) * 0.0254
            return ParseResult.success(_scalar(value, "m", None, []))

        if not text.startswith("-"):
            mo = _RANGE_RE.match(text)
            if mo:
                return self._parse_range(mo, currency, raw)

        mo = _SINGLE_RE.match(text)
        if not mo:
            return ParseResult.failure(f"could not find a number in {raw[:80]!r}")
        sign, num_tok, attached, rest = mo.groups()

        value, alt_value, note, err = _parse_number(num_tok)
        if err:
            return ParseResult.failure(err)
        notes = [note] if note else []

        magnitude, unit, factor, alt_suffix, suffix_notes, err = _resolve_suffix(
            attached, rest.split(), currency, "." in num_tok
        )
        if err:
            return ParseResult.failure(err)
        notes.extend(suffix_notes)

        if currency is not None and unit is not None:
            return ParseResult.failure(
                f"value carries both a currency ({currency}) and a unit ({unit!r}): {raw[:80]!r}"
            )

        sign_mult = -1.0 if sign == "-" else 1.0
        variants = [((magnitude if magnitude is not None else 1.0) * factor, unit)]
        if alt_suffix is not None:
            alt_mag, alt_unit, alt_factor = alt_suffix
            variants.append(((alt_mag if alt_mag is not None else 1.0) * alt_factor, alt_unit))
        values = [value] + ([alt_value] if alt_value is not None else [])

        readings = [
            {"value": sign_mult * v * scale, "unit": u}
            for v in values
            for scale, u in variants
        ]
        for reading in readings:
            if not math.isfinite(reading["value"]):
                return ParseResult.failure(f"value overflows a float: {raw[:80]!r}")
        primary = readings[0]
        return ParseResult.success(
            _scalar(primary["value"], primary["unit"], currency, notes, readings[1:])
        )

    def _parse_range(self, mo, currency, raw) -> ParseResult:
        low_tok, high_tok, rest = mo.groups()
        notes: list = []

        low, low_alt, note, err = _parse_number(low_tok)
        if err:
            return ParseResult.failure(err)
        if note:
            notes.append(note)
        high, high_alt, note, err = _parse_number(high_tok)
        if err:
            return ParseResult.failure(err)
        if note:
            notes.append(note)

        attached = ""
        if rest and not rest[0].isspace():
            m2 = _RANGE_ATTACHED_RE.match(rest)
            if m2 is None:
                return ParseResult.failure(f"unrecognized text after range in {raw[:80]!r}")
            attached, rest = m2.groups()
        magnitude, unit, factor, alt_suffix, suffix_notes, err = _resolve_suffix(
            attached, rest.split(), currency, ("." in low_tok) or ("." in high_tok)
        )
        if err:
            return ParseResult.failure(err)
        notes.extend(suffix_notes)

        if currency is not None and unit is not None:
            return ParseResult.failure(
                f"value carries both a currency ({currency}) and a unit ({unit!r}): {raw[:80]!r}"
            )

        variants = [((magnitude if magnitude is not None else 1.0) * factor, unit)]
        if alt_suffix is not None:
            alt_mag, alt_unit, alt_factor = alt_suffix
            variants.append(((alt_mag if alt_mag is not None else 1.0) * alt_factor, alt_unit))
        low_values = [low] + ([low_alt] if low_alt is not None else [])
        high_values = [high] + ([high_alt] if high_alt is not None else [])

        readings = [
            {"low": lv * scale, "high": hv * scale, "unit": u}
            for lv in low_values
            for hv in high_values
            for scale, u in variants
        ]
        primary = readings[0]
        if not (math.isfinite(primary["low"]) and math.isfinite(primary["high"])):
            return ParseResult.failure(f"range endpoint overflows a float: {raw[:80]!r}")
        if primary["low"] > primary["high"]:
            return ParseResult.failure(f"range endpoints out of order in {raw[:80]!r}")
        alts = [
            r for r in readings[1:]
            if math.isfinite(r["low"]) and math.isfinite(r["high"]) and r["low"] <= r["high"]
        ]

        canonical = _scalar(
            (primary["low"] + primary["high"]) / 2.0, primary["unit"], currency, notes, alts
        )
        canonical["is_range"] = True
        canonical["low"] = primary["low"]
        canonical["high"] = primary["high"]
        return ParseResult.success(canonical)

    def compare(self, a_canonical, b_canonical) -> Comparison:
        for label, c in (("a", a_canonical), ("b", b_canonical)):
            err = _canonical_error(c)
            if err:
                return Comparison("incomparable", f"{label}: {err}")
        a, b = a_canonical, b_canonical

        if (a["unit"] == "%") != (b["unit"] == "%"):
            return Comparison("incomparable", "percent vs non-percent quantity")
        if a["currency"] is not None and b["currency"] is not None and a["currency"] != b["currency"]:
            return Comparison("incomparable", f"currency mismatch: {a['currency']} vs {b['currency']}")
        if (a["currency"] is None) != (b["currency"] is None):
            return Comparison("review", "currency marker on one side only")
        if a["is_range"] != b["is_range"]:
            return Comparison("review", "range compared against a single value")

        if _identity(a) == _identity(b):
            return Comparison("equal", "identical canonical values")

        results = [
            _core_verdict(ra, rb, self.tolerance, a["is_range"])
            for ra in _readings(a)
            for rb in _readings(b)
        ]
        verdicts = {verdict for verdict, _ in results}
        if len(verdicts) == 1:
            return Comparison(*results[0])
        return Comparison(
            "review",
            "ambiguous reading changes the comparison verdict: " + "/".join(sorted(verdicts)),
        )


register(QuantityComparator())
