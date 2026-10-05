"""People-infobox value recovery on top of ``stage1.wikitext`` (people_infobox:v1).

The legacy ``wikipedia/people`` step-2 harvest FLATTENED each changed infobox
field into a plain string (``changes.csv`` ``old_value``/``new_value``), and
that flatten is LOSSY on exactly the value shapes the typed comparators need to
parse.  The failure taxonomy (diagnosed against the 803 normalize-review gold
rows) is dominated by:

* MONEY / COUNT quantities wrapped in trend / currency-conversion templates
  and trailed by a fiscal-year annotation:
  ``{{increase}} {{US$|40.32&nbsp;billion}} (2024)`` flattens to
  ``40.32 billion (2024)`` (currency dropped, ``(2024)`` blocks the
  comparator), ``{{INRconvert|8703|c}}`` to ``8703; c`` (scale letter detached),
  ``{{circa|24,300}}`` to ``(2024)`` (the NUMBER lost entirely).
* DATE fields from the compound ``{{Death date and age|Y|M|D|Y|M|D}}`` family,
  flattened to ``2026-2-17-1941-10-8`` (death triple THEN birth triple) — the
  date comparator refuses to guess which half is the event date.

This module RE-EXTRACTS a comparator-parseable value from the FIELD wikitext
(the raw span the harvest started from, carried on every record as
``evidence.ref.raw_wikitext`` and, untruncated, in the pinned-revision cache),
NOT from the lossy flattened string.  It honors the pipeline-wide contract:

* Pure, versioned (:data:`PEOPLE_INFOBOX_VERSION`), no I/O / network / clock.
* Refuse-to-guess: a WRONG value is worse than an empty one.  Every recovery
  is VALIDATED by re-parsing the emitted string with the value's own
  comparator; anything ambiguous (unknown currency-scale template, incomplete
  date, multi-person composite, embedded/nested infobox, no surviving digit)
  returns a REFUSAL plus a machine-readable reason so the caller keeps the row
  in review.
* Currency handling keyed by a per-template/per-symbol registry (never a
  guessed magnitude): the comparator natively understands ``$``/``£``/``€``;
  every other currency (JPY / INR / KRW / CNY / ...) is stripped to the bare
  magnitude and recorded in the recovery detail, which is sound because a
  field's before and after always share the same currency template, so a
  ``value_changed`` compare of the magnitudes is exact and never crosses
  currencies.

Only MONEY/COUNT (``quantity``) and DATE (``date``) values are recovered here —
the classes the taxonomy shows are string-recoverable.  keypeople composites
(a multi-person LEAD list the ``person_name`` comparator cannot reduce to one
name) are explicitly REFUSED, and every other value_type is left untouched.
"""

from __future__ import annotations

import datetime
import re

import stage1.normalize.date  # noqa: F401  (registers the date comparator)
import stage1.normalize.quantity  # noqa: F401  (registers the quantity comparator)
from stage1.normalize import get_comparator
from stage1.wikitext import (
    WIKI_INFOBOX_VERSION,
    find_infoboxes,
    find_templates,
    normalize_template_name,
    split_top_level,
    strip_comments,
    strip_refs,
)

PEOPLE_INFOBOX_VERSION = "people_infobox:v1"
EXTRACTOR_VERSION = f"{WIKI_INFOBOX_VERSION}+{PEOPLE_INFOBOX_VERSION}"


# --------------------------------------------------------------------------
# Result container
# --------------------------------------------------------------------------


class Recovery:
    """Outcome of a single-side recovery attempt (JSON-safe via ``as_dict``).

    ``value`` is the cleaned, comparator-parseable string when ``ok`` is True,
    else None; ``reason`` names why nothing was emitted; ``notes`` records
    non-fatal cleaning observations; ``method``/``currency``/``precision`` carry
    the recovery specifics for the evidence ledger."""

    __slots__ = ("ok", "value", "method", "reason", "notes", "currency", "precision")

    def __init__(self, ok, value=None, method=None, reason=None, notes=None,
                 currency=None, precision=None):
        self.ok = ok
        self.value = value
        self.method = method
        self.reason = reason
        self.notes = list(notes or [])
        self.currency = currency
        self.precision = precision

    def as_dict(self) -> dict:
        out = {"extractor": EXTRACTOR_VERSION, "ok": self.ok, "method": self.method}
        if self.value is not None:
            out["value"] = self.value
        if self.reason is not None:
            out["reason"] = self.reason
        if self.currency is not None:
            out["currency"] = self.currency
        if self.precision is not None:
            out["precision"] = self.precision
        if self.notes:
            out["notes"] = list(self.notes)
        return out


def _ok(value, method, notes=None, currency=None, precision=None) -> Recovery:
    return Recovery(True, value=value, method=method, notes=notes,
                    currency=currency, precision=precision)


def _refuse(reason, method=None, notes=None) -> Recovery:
    return Recovery(False, method=method, reason=reason, notes=notes)


# --------------------------------------------------------------------------
# Shared markup cleaning (pure)
# --------------------------------------------------------------------------

_NBSP_RE = re.compile(r"&nbsp;|&#160;|&#xA0;| |&thinsp;|&#8201;|&ensp;|&emsp;")
_MINUS_ENTITY_RE = re.compile(r"&minus;|&#8722;|&#x2212;")
_TAG_RE = re.compile(r"<[^<>]+>")
_WIKILINK_REDUCE_RE = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]|]+)\]\]")
# A dash used as a leading arithmetic sign: directly before a digit and after
# start / space / currency / open-paren — NOT an interior range dash ('2024-25').
_SIGN_DASH_RE = re.compile(r"(^|[\s$£€¥₹₩(])[–—-](\d)")
_SPACE_TEMPLATES = frozenset({"nbsp", "spaces", "space", "thinsp"})
_TREND_TEMPLATES = frozenset({
    "increase", "decrease", "gain", "loss", "profit", "steady", "growth",
    "positive decrease", "negative increase", "nochange", "no change",
    "increasenegative", "decreasepositive", "increasepositive",
    "decreasenegative", "increase positive", "decrease negative",
    "up", "down", "flat",
})
_LAST_ARG_WRAPPERS = frozenset({"nowrap", "nobold", "noitalic", "small", "center",
                                "nowraplinks", "resize", "big", "align", "nobr"})
_COLOR_TEMPLATES = frozenset({"color", "colour", "font color", "font colour",
                              "fontcolor", "fontcolour", "textcolor", "textcolour"})
_LIST_TEMPLATES = frozenset({"unbulleted list", "ubl", "plainlist", "plain list",
                             "flatlist", "flat list", "hlist", "bulleted list",
                             "ublist", "blist"})


def _decode_entities(text: str) -> str:
    """Decode entities that block numeric parsing / hide a sign; normalize
    unicode minus and the figure dash used as a sign to ASCII '-'."""
    text = _NBSP_RE.sub(" ", text)
    text = _MINUS_ENTITY_RE.sub("-", text)
    text = text.replace("−", "-").replace("‒", "-")
    text = text.replace("&#36;", "$").replace("&pound;", "£").replace("&euro;", "€")
    text = text.replace("&yen;", "¥").replace("&amp;", "&")
    return text


def _reduce_wikilinks(text: str) -> str:
    """``[[target|label]]`` -> ``label``, ``[[target]]`` -> ``target`` (applied
    early so a currency wikilink like ``[[GBP|£]]`` /
    ``[[1,000,000,000|billion]]`` collapses to its rendered token)."""
    prev = None
    while prev != text:
        prev = text
        text = _WIKILINK_REDUCE_RE.sub(lambda m: m.group(1), text)
    return text


def _top_level_templates(text: str) -> list:
    templates = list(find_templates(text))
    out = []
    for t in templates:
        nested = any(o["start"] < t["start"] and o["end"] >= t["end"]
                     for o in templates if (o["start"], o["end"]) != (t["start"], t["end"]))
        if not nested:
            out.append(t)
    return out


def _template_args(text: str, start: int, end: int) -> list:
    inner = text[start + 2:end - 2]
    positional = []
    for part in split_top_level(inner)[1:]:
        if re.match(r"^\s*[^=\[\]{}|<>]+?\s*=", part):
            continue
        positional.append(part.strip())
    return positional


def _first_template(text: str):
    """The first top-level ``{{...}}`` template (name, start, end, positional
    args stripped of named params), or None."""
    tops = _top_level_templates(text)
    if not tops:
        return None
    top = tops[0]
    return {"name": top["name"], "start": top["start"], "end": top["end"],
            "args": _template_args(text, top["start"], top["end"])}


def _expand_inline_markup(text: str, notes: list) -> str:
    """Replace, ANYWHERE and to a fixed point: space templates ({{nbsp}}) ->
    ' ', colour templates -> their last positional arg (colour name dropped),
    content-preserving inline wrappers ({{small|X}}/{{sub|X}}) -> X."""
    for _ in range(16):
        tops = _top_level_templates(text)
        if not tops:
            break
        did = False
        for top in tops:
            name = top["name"]
            if name in _SPACE_TEMPLATES:
                text = text[:top["start"]] + " " + text[top["end"]:]
                did = True
                break
            if name in _COLOR_TEMPLATES:
                args = _template_args(text, top["start"], top["end"])
                if args:
                    notes.append(f"dropped {name!r} colour markup")
                    text = text[:top["start"]] + args[-1] + text[top["end"]:]
                    did = True
                    break
            if name in _INLINE_WRAPPERS:
                args = _template_args(text, top["start"], top["end"])
                if args:
                    text = text[:top["start"]] + args[-1] + text[top["end"]:]
                    did = True
                    break
        if not did:
            break
    return text


def _unwrap_wrappers(text: str, notes: list, depth: int = 0) -> str:
    """Unwrap a whole-value wrapper/list template to its displayed content: a
    wrapper ({{nowrap|X}}) -> X; a list ({{ubl|A|B}}) -> its FIRST item."""
    if depth > 8:
        return text
    trimmed = text.strip()
    tpl = _first_template(trimmed)
    if tpl is None or not (trimmed.startswith("{{") and tpl["end"] == len(trimmed)):
        return trimmed
    name = tpl["name"]
    if name in _LAST_ARG_WRAPPERS and tpl["args"]:
        notes.append(f"unwrapped {name!r} wrapper")
        return _unwrap_wrappers(tpl["args"][-1], notes, depth + 1)
    if name in _LIST_TEMPLATES and tpl["args"]:
        notes.append(f"multi-value {name!r}; pinned to the first (most-recent) item")
        return _unwrap_wrappers(tpl["args"][0], notes, depth + 1)
    return trimmed


def _strip_leading_trend(text: str, notes: list) -> str:
    """Drop a leading trend template ({{increase}}/{{decrease}}/{{gain}}/...)."""
    trimmed = text.strip()
    tpl = _first_template(trimmed)
    if tpl is not None and tpl["start"] == 0 and tpl["name"] in _TREND_TEMPLATES:
        notes.append(f"dropped trend template {tpl['name']!r}")
        return trimmed[tpl["end"]:].strip()
    return trimmed


def _normalize_leading(text: str, notes: list) -> str:
    """Fixed-point normalization of the leading markup: unwrap wrappers/lists
    and drop trend templates until neither applies (handles
    ``{{nowrap|{{increase}} {{US$|X}} (Y)}}``)."""
    prev = None
    for _ in range(10):
        if prev == text:
            break
        prev = text
        text = _unwrap_wrappers(text, notes)
        text = _strip_leading_trend(text, notes)
    return text


# --------------------------------------------------------------------------
# Currency / quantity template + symbol registry
# --------------------------------------------------------------------------

_NATIVE_CURRENCY_SYMBOL = {"USD": "$", "GBP": "£", "EUR": "€"}

# Bare currency symbols/prefixes -> ISO code (matched longest-first).
_CURRENCY_SYMBOLS = [
    ("US$", "USD"), ("U.S.$", "USD"), ("US-$", "USD"), ("CN¥", "CNY"),
    ("HK$", "HKD"), ("S$", "SGD"), ("NT$", "TWD"), ("R$", "BRL"), ("A$", "AUD"),
    ("C$", "CAD"), ("NZ$", "NZD"), ("$", "USD"), ("£", "GBP"), ("€", "EUR"),
    ("¥", "JPY"), ("₩", "KRW"), ("₹", "INR"), ("₱", "PHP"),
    ("฿", "THB"), ("₺", "TRY"), ("₴", "UAH"), ("₦", "NGN"),
    ("₽", "RUB"), ("₪", "ILS"), ("₫", "VND"),
]
# Detached currency words / ISO codes (a leading marker; some carry a scale).
_CURRENCY_WORD = {
    "usd": ("USD", 1.0), "gbp": ("GBP", 1.0), "eur": ("EUR", 1.0),
    "jpy": ("JPY", 1.0), "yen": ("JPY", 1.0), "inr": ("INR", 1.0),
    "krw": ("KRW", 1.0), "won": ("KRW", 1.0), "cny": ("CNY", 1.0),
    "rmb": ("CNY", 1.0), "yuan": ("CNY", 1.0), "aed": ("AED", 1.0),
    "sek": ("SEK", 1.0), "msek": ("SEK", 1e6), "mkr": ("SEK", 1e6),
    "chf": ("CHF", 1.0), "php": ("PHP", 1.0), "thb": ("THB", 1.0),
    "baht": ("THB", 1.0), "sgd": ("SGD", 1.0), "brl": ("BRL", 1.0),
    "myr": ("MYR", 1.0), "twd": ("TWD", 1.0), "hkd": ("HKD", 1.0),
    "nok": ("NOK", 1.0), "dkk": ("DKK", 1.0), "pln": ("PLN", 1.0),
    "rs": ("INR", 1.0), "ugx": ("UGX", 1.0),
}
# Convert-style templates: positional arg1 = number, arg2 = scale letter.
_CONVERT_TEMPLATES = {
    "inrconvert": "INR", "inr convert": "INR", "jpyconvert": "JPY",
    "jpy convert": "JPY", "krwconvert": "KRW", "krw convert": "KRW",
    "cnyconvert": "CNY", "usdconvert": "USD", "eurconvert": "EUR",
    "rubconvert": "RUB", "twdconvert": "TWD", "phpconvert": "PHP",
    "bdtconvert": "BDT", "vndconvert": "VND",
}
_SCALE_LETTER = {
    "k": "thousand", "l": "lakh", "lakh": "lakh", "c": "crore", "cr": "crore",
    "crore": "crore", "m": "million", "mn": "million", "b": "billion",
    "bn": "billion", "t": "trillion", "tn": "trillion",
}
# Wrapper-style currency templates: positional arg1 = the whole magnitude
# string (may carry a scale WORD).  Keyed by normalized name -> ISO currency.
_WRAPPER_CURRENCY_TEMPLATES = {
    "us$": "USD", "usd": "USD", "gbp": "GBP", "£": "GBP",
    "pound sterling": "GBP", "eur": "EUR", "€": "EUR", "cny": "CNY",
    "rmb": "CNY", "yen": "JPY", "jpy": "JPY", "¥": "JPY", "krw": "KRW",
    "won": "KRW", "inr": "INR", "₹": "INR", "rs": "INR", "php": "PHP",
    "peso": "PHP", "thb": "THB", "baht": "THB", "brl": "BRL", "real": "BRL",
    "chf": "CHF", "sek": "SEK", "aud": "AUD", "cad": "CAD", "sgd": "SGD",
    "hkd": "HKD", "nzd": "NZD", "myr": "MYR", "twd": "TWD",
}
_ALL_CURRENCY_TEMPLATES = (set(_CONVERT_TEMPLATES) | set(_WRAPPER_CURRENCY_TEMPLATES)
                           | {"circa", "c.", "ca"})
# Scale words the quantity comparator applies as a magnitude MULTIPLIER (they
# are consumed, so a correctly-scaled value never carries them as a canonical
# 'unit').  A recovered quantity that DOES land a non-null canonical unit means
# a trailing token was absorbed as an OPAQUE unit instead — either a misspelled
# scale word ('trilion' -> value off by 10^12) or a descriptive noun/currency
# word ('euros', 'permanent') that leaves a spurious unit — so the recovery is
# refused (a magnitude on an unrecognized scale is the module's 'emit nothing'
# case).  The set is intentionally broad/explicit even though these never
# surface as a unit, documenting exactly which trailing words are legitimate.
_RECOGNIZED_SCALE_UNITS = frozenset({
    "thousand", "thousands", "million", "millions", "billion", "billions",
    "trillion", "trillions", "crore", "crores", "lakh", "lakhs",
})

_NUMBER_RE = re.compile(r"-?\d[\d.,]*\d|-?\d")
_ACCOUNTING_NEG_RE = re.compile(r"^([$£€¥₹₩]?)\(\s*(\d[\d.,]*)\s*\)(.*)$")
_SPACE_THOUSANDS_RE = re.compile(r"(?<=\d)\s(?=\d{3}(?:\D|$))")
# Leading approximation words.  Each alternative must be followed by whitespace,
# a digit, or end-of-string, so a bare 'c'/'ca' cannot swallow the leading
# letter of a currency prefix ('CN¥').  'c.'/'ca.'/'est.' require their dot.
_LEADING_APPROX_RE = re.compile(
    r"^(?:approximately|approx\.?|circa|around|roughly|about|nearly|almost|"
    r"more\s+than|less\s+than|at\s+least|at\s+most|up\s+to|over|under|"
    r"c\.|ca\.|est\.|~|≈)(?:\s+|(?=\d)|(?=[$£€¥₹₩])|$)",
    re.IGNORECASE)
# Trailing head-count qualifier words the count comparator cannot read.
_TRAILING_QUALIFIER_RE = re.compile(
    r"\s+(?:full[- ]?time|part[- ]?time|employees?|staff|fte|people|persons?|"
    r"workers?|worldwide|globally|total|approx\.?|est\.?)\s*$", re.IGNORECASE)
# Content-preserving inline wrapper templates (drop the wrapper, keep the value).
_INLINE_WRAPPERS = frozenset({"small", "sub", "sup", "nobold", "noitalic",
                              "resize", "big", "nowrap"})
_TRAILING_LOSS_RE = re.compile(r"[\s(]*\b(?:loss|profit|net|gross|deficit)\b\)?\s*$", re.IGNORECASE)
_TRAILING_COMMA_YEAR_RE = re.compile(r",\s*(?:fy\s?)?\d{4}(?:[/–-]\d{2,4})?\s*$", re.IGNORECASE)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
# An unclosed / truncated <ref ...> (the 500-char head cap cuts refs mid-tag);
# strip from it to the end so a citation body cannot leak markup into the value.
_OPEN_REF_RE = re.compile(r"<ref\b.*\Z", re.I | re.S)
# Citation / footnote templates ({{sfn}}, {{r|...}}, {{cite ...}}, {{refn}}) —
# annotations, never part of the value; removed anywhere.
_CITATION_TEMPLATE_NAMES = ("sfn", "sfnp", "harvnb", "r", "refn", "efn",
                            "notetag", "rp", "ref")
# Country qualifiers that merely disambiguate a '$' (US$, CA$, ...) — dropped,
# with the '$' kept as the currency marker.
_COUNTRY_QUALIFIER_RE = re.compile(r"(?i)\b(?:u\.?\s?s\.?a?|can|aus|nz|hk|sg)\b\s*(\$)|(\$)\s*\b(?:u\.?\s?s\.?a?|can|aus|nz|hk|sg)\b")
# Currency WORDS (unambiguous) removed anywhere, recording the currency.
_CURRENCY_WORD_ANYWHERE = {
    "yuan": "CNY", "yen": "JPY", "won": "KRW", "baht": "THB", "peso": "PHP",
    "pesos": "PHP", "rupees": "INR", "rupee": "INR", "renminbi": "CNY",
    "ringgit": "MYR", "kronor": "SEK", "kroner": "NOK", "shillings": "UGX",
}
# A trailing unbalanced '{{...' remnant (the 500-char cap cuts a template).
_OPEN_BRACE_TAIL_RE = re.compile(r"\{\{[^{}]*\Z", re.S)


def _strip_citation_templates(text: str) -> str:
    """Remove {{sfn}}/{{r|...}}/{{cite ...}}/{{refn}} templates anywhere."""
    for _ in range(8):
        removed = False
        for top in _top_level_templates(text):
            name = top["name"]
            if name in _CITATION_TEMPLATE_NAMES or name.startswith("cite "):
                text = (text[:top["start"]] + " " + text[top["end"]:])
                removed = True
                break
        if not removed:
            break
    return text


def _split_top_level_semicolon(text: str) -> list:
    """Split on ';' occurring OUTSIDE parentheses and templates (a top-level
    period separator, not a ';' inside a '(2024; up 36%)' annotation or a
    template arg)."""
    parts = []
    pdepth = tdepth = 0
    cur = []
    i = 0
    n = len(text)
    while i < n:
        two = text[i:i + 2]
        if two == "{{":
            tdepth += 1
            cur.append(two)
            i += 2
            continue
        if two == "}}":
            tdepth = max(0, tdepth - 1)
            cur.append(two)
            i += 2
            continue
        ch = text[i]
        if ch == "(":
            pdepth += 1
        elif ch == ")":
            pdepth = max(0, pdepth - 1)
        if ch == ";" and pdepth == 0 and tdepth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


def _resolve_currency_template(name: str, args: list):
    """('MAGNITUDE', 'ISO') / ('REFUSE', reason) / ('MARKER', 'ISO') for a
    currency/convert/circa template, or None when not a currency template.
    'MARKER' means an arg-less currency template (a bare currency prefix)."""
    if name in ("circa", "c.", "ca"):
        return ("MAGNITUDE", args[0].strip(), None) if args else None
    if name in _CONVERT_TEMPLATES:
        if not args:
            return ("MARKER", _CONVERT_TEMPLATES[name])
        iso = _CONVERT_TEMPLATES[name]
        number = args[0].strip()
        scale_word = None
        if len(args) >= 2 and args[1].strip():
            letter = args[1].strip().lower()
            scale_word = _SCALE_LETTER.get(letter)
            if scale_word is None:
                return ("REFUSE", f"unknown scale letter {args[1]!r} in {name!r} template")
        mag = f"{number} {scale_word}" if scale_word else number
        return ("MAGNITUDE", mag, iso)
    if name in _WRAPPER_CURRENCY_TEMPLATES:
        iso = _WRAPPER_CURRENCY_TEMPLATES[name]
        return ("MAGNITUDE", args[0].strip(), iso) if args else ("MARKER", iso)
    return None


def _strip_currency_words(text: str, currency):
    """Strip a leading detached currency word / ISO code (SEK, UGX, ...) and
    any unambiguous currency word (yuan/yen/won/baht/...) ANYWHERE; return
    (text, currency, scale)."""
    scale = 1.0
    tokens = text.split()
    if tokens:
        head = tokens[0].strip(":.,").lower()
        if head in _CURRENCY_WORD and currency is None:
            iso, scale = _CURRENCY_WORD[head]
            currency = iso
            text = text[len(tokens[0]):].strip().lstrip(":").strip()
    for word, iso in _CURRENCY_WORD_ANYWHERE.items():
        pat = re.compile(rf"\b{word}\b", re.IGNORECASE)
        if pat.search(text):
            if currency is None:
                currency = iso
            text = pat.sub(" ", text)
    return text, currency, scale


def _strip_currency_symbol(text: str, currency):
    """Strip a bare currency symbol adjacent to the number; return (text, iso)."""
    if currency is not None:
        return text, currency
    for sym, code in _CURRENCY_SYMBOLS:
        idx = text.find(sym)
        if idx == -1:
            continue
        after = text[idx + len(sym):].lstrip()
        if after[:1] in "-0123456789.(":
            text = (text[:idx] + " " + text[idx + len(sym):]).strip()
            return text, code
    return text, currency


def _strip_trailing_parens(text: str) -> tuple:
    """Strip trailing ``(...)`` annotations (fiscal year, conversion, note),
    requiring a digit to remain BEFORE the stripped paren (so an accounting
    ``£(172)`` whose scale word follows is never mistaken for a trailing
    annotation).  Returns (text, stripped_any)."""
    stripped = False
    pat = re.compile(r"^(.*)\([^()]*\)\s*$", re.S)
    for _ in range(6):
        mo = pat.match(text)
        if not mo or not re.search(r"\d", mo.group(1)):
            break
        text = mo.group(1).rstrip()
        stripped = True
    return text, stripped


def clean_quantity_field(field_wikitext) -> Recovery:
    """Recover a comparator-parseable quantity string from a MONEY/COUNT field's
    raw wikitext, or REFUSE.  The emitted string is always re-parsed by the
    quantity comparator before it is returned (see module docstring)."""
    if not isinstance(field_wikitext, str):
        return _refuse("field wikitext is not a string", method="quantity")
    notes: list = []
    text = _decode_entities(strip_refs(strip_comments(field_wikitext)))
    text = _OPEN_REF_RE.sub("", text)          # truncated / unclosed <ref ...>
    text = _strip_citation_templates(text)      # {{sfn}}, {{r|...}}, {{cite ...}}
    text = _reduce_wikilinks(text)
    text = _expand_inline_markup(text, notes)
    text = text.strip()
    if not text:
        return _refuse("empty field wikitext", method="quantity")

    text = _normalize_leading(text, notes)

    # Take the FIRST <br>-separated segment (a second line is usually a currency
    # conversion or a prior-year figure).
    br_parts = _BR_RE.split(text)
    if len(br_parts) > 1 and _NUMBER_RE.search(br_parts[0]):
        text = br_parts[0].strip()
        notes.append("multi-line value; pinned to the first line")

    currency_iso = None
    scale_mult = 1.0

    # Expand every currency/convert/circa template to a fixed point, re-running
    # inline markup after each (a currency template may reveal a nested
    # {{nbsp}}/{{color}} once expanded).
    for _ in range(8):
        text = _expand_inline_markup(text, notes)
        tpl = _first_template(text)
        if tpl is None or tpl["name"] not in _ALL_CURRENCY_TEMPLATES:
            break
        resolved = _resolve_currency_template(tpl["name"], tpl["args"])
        if resolved is None:
            break
        if resolved[0] == "REFUSE":
            return _refuse(resolved[1], method="quantity", notes=notes)
        if resolved[0] == "MARKER":
            if currency_iso is None:
                currency_iso = resolved[1]
            text = (text[:tpl["start"]] + " " + text[tpl["end"]:]).strip()
            notes.append(f"currency marker template -> {resolved[1]}")
            continue
        mag, iso = resolved[1], resolved[2]
        if iso is not None and currency_iso is None:
            currency_iso = iso
            notes.append(f"expanded currency template {tpl['name']!r} -> {iso}")
        text = (text[:tpl["start"]] + " " + mag + " " + text[tpl["end"]:]).strip()

    text = _expand_inline_markup(text, notes)
    # Drop a trailing unbalanced '{{...' remnant left by the 500-char cap.
    if _OPEN_BRACE_TAIL_RE.search(text) and "}}" not in _OPEN_BRACE_TAIL_RE.search(text).group(0):
        text = _OPEN_BRACE_TAIL_RE.sub("", text).strip()
        notes.append("dropped a truncated trailing template remnant")

    # Isolate the FIRST value BEFORE the template check: a ';'-separated second
    # period, or an ', including ...' clause, may still carry a template that
    # must not veto a clean first value.
    text = re.split(r",?\s+including\b", text, maxsplit=1, flags=re.IGNORECASE)[0]
    segments = _split_top_level_semicolon(text)
    if len(segments) > 1 and _NUMBER_RE.search(segments[0]):
        if any(_NUMBER_RE.search(s) for s in segments[1:]):
            notes.append("multi-period value; pinned to the first (most-recent) period")
        text = segments[0].strip()

    # Any surviving BALANCED template means we could not safely read the value.
    if "{{" in text or "}}" in text:
        return _refuse(f"unresolved template in value: {text[:80]!r}",
                       method="quantity", notes=notes)

    if "<" in text and ">" in text:
        text = _TAG_RE.sub(" ", text)
    text = text.replace("[[", " ").replace("]]", " ").replace("[", " ").replace("]", " ")
    text = text.replace("''", "")
    text = re.sub(r"\s+", " ", text).strip()

    # Country qualifier that merely disambiguates '$' (US$, CA$ -> $).
    text = _COUNTRY_QUALIFIER_RE.sub(lambda m: m.group(1) or m.group(2), text)
    # Detached currency word / ISO code (SEK 5 billion, UGX:35.4 billion, yuan).
    text = re.sub(r"^([A-Za-z]{2,4}):\s*", r"\1 ", text)  # 'UGX:35.4' -> 'UGX 35.4'
    text, currency_iso, scale_mult = _strip_currency_words(text, currency_iso)

    # Trailing loss/profit annotation, head-count qualifier, comma-year, parens.
    text = _TRAILING_LOSS_RE.sub("", text).strip()
    text = _TRAILING_QUALIFIER_RE.sub("", text).strip()
    text, _ = _strip_trailing_parens(text)
    text = _TRAILING_COMMA_YEAR_RE.sub("", text).strip()
    text = _TRAILING_QUALIFIER_RE.sub("", text).strip()

    # Leading approx / circa word, trailing '+' (>=) marker.
    text = _LEADING_APPROX_RE.sub("", text).strip()
    text = re.sub(r"\+\s*$", "", text).strip()

    # Bare currency symbol.
    text, currency_iso = _strip_currency_symbol(text, currency_iso)

    # Dash-as-sign, then accounting-parentheses negative.
    text = _SIGN_DASH_RE.sub(lambda m: f"{m.group(1)}-{m.group(2)}", text).strip()
    mo = _ACCOUNTING_NEG_RE.match(text)
    if mo:
        text = f"-{mo.group(2)}{mo.group(3)}"
        notes.append("accounting parentheses read as a negative")

    if _SPACE_THOUSANDS_RE.search(text):
        text = _SPACE_THOUSANDS_RE.sub("", text)
        notes.append("collapsed space-grouped thousands")

    text = re.sub(r"\s+", " ", text).strip()
    if not _NUMBER_RE.search(text):
        return _refuse(f"no surviving number in value: {field_wikitext[:80]!r}",
                       method="quantity", notes=notes)

    emit = text
    if currency_iso in _NATIVE_CURRENCY_SYMBOL and not any(s in text for s, _ in _CURRENCY_SYMBOLS):
        emit = f"{_NATIVE_CURRENCY_SYMBOL[currency_iso]}{text}"
    if scale_mult != 1.0:
        notes.append(f"currency word carried a x{scale_mult:g} scale; kept as a bare magnitude")

    comparator = get_comparator("quantity")
    parsed = comparator.parse(emit)
    if not parsed.ok:
        return _refuse(f"recovered value still unparseable ({parsed.failure_reason})",
                       method="quantity", notes=notes)
    # Refuse when the comparator absorbed a trailing token as an opaque unit
    # rather than applying it as a scale multiplier: the parse "succeeds" but a
    # misspelled scale word ('trilion') lands the magnitude orders of magnitude
    # off, and a descriptive/currency word ('euros', 'permanent') leaves a unit
    # a MONEY/COUNT value should never carry.  A canonical unit here is never a
    # recognized scale word (those are consumed into the magnitude), so any
    # non-null unit means an un-vetted absorption -> emit nothing.
    canon_unit = parsed.canonical.get("unit") if isinstance(parsed.canonical, dict) else None
    if canon_unit is not None and str(canon_unit).strip().lower() not in _RECOGNIZED_SCALE_UNITS:
        return _refuse(
            f"recovered value carries an unrecognized unit {canon_unit!r} "
            f"(a misspelled scale word or a descriptive/currency token absorbed "
            f"as a unit): {emit!r}",
            method="quantity", notes=notes)
    return _ok(emit, method="quantity", notes=notes, currency=currency_iso)


# --------------------------------------------------------------------------
# Date template registry / date recovery
# --------------------------------------------------------------------------
#
# Keyed by NORMALIZED template name.  The FIRST positional triple is always the
# EVENT date the field records; ``event`` is a note only.  Templates whose
# fourth positional arg is an AGE (``... given age``) are handled the same way
# (the triple is the first three positional args).
def _with_spacefree_aliases(table: dict) -> dict:
    """Add the space-free redirect alias of every spaced key ('death date and
    age' -> 'deathdateandage', 'start date' -> 'startdate'), which are REAL
    enwiki template redirects that ``normalize_template_name`` leaves intact
    (it only collapses internal whitespace runs, it does not remove spaces).
    Without this a ``{{deathdateandage|...}}`` field would fail recovery and the
    legacy semicolon-concatenated garbage string would survive into the release.
    All spaced keys of a family map to the same value, so a collapsed alias can
    never conflict; ``setdefault`` keeps the explicit key when both exist."""
    out = dict(table)
    for key, value in table.items():
        out.setdefault(key.replace(" ", ""), value)
    return out


_DATE_TRIPLE_TEMPLATES = _with_spacefree_aliases({
    "death date and age": "death", "death date and given age": "death",
    "dda": "death", "death-date and age": "death", "death date": "death",
    "deathdate": "death", "d-da": "death",
    "birth date and age": "birth", "birth date and given age": "birth",
    "bda": "birth", "birth-date and age": "birth", "birth date": "birth",
    "birthdate": "birth", "b-da": "birth", "date of birth and age": "birth",
    "dob": "birth",
    "start date and age": "start", "start date": "start", "start-date": "start",
    "end date and age": "end", "end date": "end",
})
_DATE_YEAR_TEMPLATES = _with_spacefree_aliases({
    "death year and age": "death", "birth year and age": "birth",
    "death-year and age": "death",
})
_AGED_SUFFIX_RE = re.compile(r"\s*\(\s*aged?\b[^)]*\)\s*$", re.IGNORECASE)
_TRAILING_PAREN_RE = re.compile(r"^(.*\S)\s*\([^()]*\)\s*$", re.S)
_YEAR_IN_PLACE_RE = re.compile(r"^(\d{3,4})\s+(?:in|at|near)\b", re.IGNORECASE)


def _triple_to_iso(args):
    """(iso, precision) for a [Y, M, D, ...] positional-arg list, or None.
    Honors the template's OWN precision: an empty/absent day -> month, an
    empty/absent month -> year.  Never promotes a coarse date to day."""
    if not args:
        return None

    def as_int(tok):
        tok = (tok or "").strip()
        return int(tok) if tok.isdigit() else None

    y = as_int(args[0])
    if y is None or not 1 <= y <= 9999:
        return None
    m = as_int(args[1]) if len(args) >= 2 else None
    d = as_int(args[2]) if len(args) >= 3 else None
    if m is None:
        return f"{y:04d}", "year"
    if not 1 <= m <= 12:
        return None
    if d is None:
        return f"{y:04d}-{m:02d}", "month"
    if not 1 <= d <= 31:
        return None
    try:
        datetime.date(y, m, d)
    except ValueError:
        return None
    return f"{y:04d}-{m:02d}-{d:02d}", "day"


def clean_date_field(field_wikitext) -> Recovery:
    """Recover a comparator-parseable ISO date string from a DATE field's raw
    wikitext (the compound death/birth-date template family, {{Start date}},
    or a markup-wrapped free-text date), or REFUSE.  The emitted string is
    re-parsed by the date comparator before it is returned."""
    if not isinstance(field_wikitext, str):
        return _refuse("field wikitext is not a string", method="date")
    notes: list = []
    text = _decode_entities(strip_refs(strip_comments(field_wikitext))).strip()
    if not text:
        return _refuse("empty field wikitext", method="date")
    comparator = get_comparator("date")

    # 1) A leading/whole-value date template.
    tpl = _first_template(text)
    if tpl is not None and tpl["start"] == 0:
        name = tpl["name"]
        if name in _DATE_TRIPLE_TEMPLATES:
            iso = _triple_to_iso(tpl["args"])
            if iso is None:
                return _refuse(f"could not read a date triple from {name!r}",
                               method="date", notes=notes)
            notes.append(f"parsed {_DATE_TRIPLE_TEMPLATES[name]} date from {name!r} "
                         "(first positional triple)")
            parsed = comparator.parse(iso[0])
            if not parsed.ok:
                return _refuse(f"recovered date unparseable ({parsed.failure_reason})",
                               method="date", notes=notes)
            return _ok(iso[0], method="date", notes=notes, precision=iso[1])
        if name in _DATE_YEAR_TEMPLATES:
            year = tpl["args"][0].strip() if tpl["args"] else ""
            if not (year.isdigit() and 1 <= int(year) <= 9999):
                return _refuse(f"could not read a year from {name!r}", method="date", notes=notes)
            notes.append(f"parsed {_DATE_YEAR_TEMPLATES[name]} year from {name!r}")
            return _ok(f"{int(year):04d}", method="date", notes=notes, precision="year")
        return _refuse(f"unrecognized leading date template {name!r}", method="date", notes=notes)

    # 2) Free-text date.  Take the FIRST <br>-separated line, THEN require it to
    #    be template/infobox free (a later line's markup must not veto a clean
    #    leading date).
    first_line = _BR_RE.split(text)[0].strip()
    if first_line != text.strip():
        notes.append("took the first <br>-separated line")
    line = first_line
    if "{{" in line or "}}" in line or "infobox" in line.lower():
        return _refuse(f"embedded template/infobox in date value: {line[:80]!r}",
                       method="date", notes=notes)
    line = _AGED_SUFFIX_RE.sub("", line)
    mo = _TRAILING_PAREN_RE.match(line)
    if mo:
        line = mo.group(1).strip()
        notes.append("stripped trailing parenthetical annotation")
    line = _reduce_wikilinks(line)
    if "<" in line and ">" in line:
        line = _TAG_RE.sub(" ", line)
    line = line.replace("''", "")
    line = _LEADING_APPROX_RE.sub("", line)
    line = re.sub(r"\s+", " ", line).strip().rstrip(".,;")
    # 'YYYY in <place>' -> the year (a full year-bearing date is present).
    place_mo = _YEAR_IN_PLACE_RE.match(line)
    if place_mo:
        line = place_mo.group(1)
        notes.append("free-text 'YYYY in <place>' -> year precision")
    if not line:
        return _refuse("no date text after markup removal", method="date", notes=notes)
    parsed = comparator.parse(line)
    if not parsed.ok:
        return _refuse(f"free-text date unparseable ({parsed.failure_reason})",
                       method="date", notes=notes)
    return _ok(line, method="date", notes=notes, precision=parsed.canonical.get("precision"))


# --------------------------------------------------------------------------
# keypeople composite guard
# --------------------------------------------------------------------------

_COMPOSITE_LIST_MARKERS = ("<br", "{{plainlist", "{{unbulleted", "{{ubl",
                           "{{hlist", "{{flatlist", "{{plain list", "\n*", ";")


def is_keypeople_composite(field_wikitext, flattened_value) -> bool:
    """True when a LEAD keypeople value is a multi-person composite list (a
    ``person_name`` comparator compares a single name; a leadership reshuffle
    list must never be reduced to its first name)."""
    for src in (field_wikitext, flattened_value):
        if isinstance(src, str):
            low = src.lower()
            if any(marker in low for marker in _COMPOSITE_LIST_MARKERS):
                return True
    return False


# --------------------------------------------------------------------------
# Public entry point: recover one side
# --------------------------------------------------------------------------


def field_wikitext_from_page(content, template, leaf):
    """The UNTRUNCATED raw wikitext of the ``leaf`` field of the ``template``
    infobox on a full pinned-revision page, or None.

    Used only to repair a TRUNCATED embedded field value (the 500-char head
    cap).  Refuse-to-guess: returns None unless exactly ONE infobox matches by
    normalized template name AND carries the field (or, when the template name
    does not match any infobox, exactly one infobox on the page carries the
    field).  A wrong-infobox field is worse than keeping the review."""
    if not isinstance(content, str) or not isinstance(leaf, str) or not leaf:
        return None
    leaf_key = leaf.strip().lower()
    boxes = find_infoboxes(content)
    if not boxes:
        return None
    want = normalize_template_name(template) if isinstance(template, str) else ""
    by_name = [b for b in boxes
               if want and b["template_name"] == want and leaf_key in b["fields"]]
    if len(by_name) == 1:
        return by_name[0]["fields"][leaf_key]
    if by_name:
        return None  # ambiguous: several same-named infoboxes carry the field
    any_box = [b for b in boxes if leaf_key in b["fields"]]
    if len(any_box) == 1:
        return any_box[0]["fields"][leaf_key]
    return None


def recover_side(value_type, field_class, field_wikitext, flattened_value) -> Recovery:
    """Recover a comparator-parseable value for ONE before/after side.

    Precedence is enforced by the CALLER (recovery runs only when the flattened
    legacy value fails to parse); this function decides, per value_type,
    whether the field wikitext yields a safe value:

    * ``quantity`` (MONEY/COUNT)  -> :func:`clean_quantity_field`.
    * ``date`` / ``death_date`` (DATE) -> :func:`clean_date_field` (a death date
      is a plain calendar date once present; only its EMPTY state is special, and
      an empty side is never recovered — see the caller's precedence).
    * ``person_name`` keypeople composite -> explicit REFUSE (multi-valued).
    * anything else              -> REFUSE (no recovery defined) — the row
      keeps its review, never a guessed value.
    """
    if value_type == "quantity":
        return clean_quantity_field(field_wikitext)
    if value_type in ("date", "death_date"):
        return clean_date_field(field_wikitext)
    if value_type == "person_name" and is_keypeople_composite(field_wikitext, flattened_value):
        return _refuse(
            "keypeople composite: a multi-person leadership list cannot be reduced to a "
            "single person_name value (held for a list-aware comparator)",
            method="person_name",
        )
    return _refuse(f"no recovery defined for value_type {value_type!r}",
                   method=value_type or "unknown")
