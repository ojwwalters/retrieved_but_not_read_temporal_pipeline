"""Deterministic death-infobox extraction + body-prose measure (people_death:v1).

Shared by the people HARVESTER (stage1/harvest/wiki_people.py — which reads the
pinned cutoff/asof revisions live and freezes what it saw) and the people
ADAPTER (stage1/adapters/wiki_people.py — which RE-extracts the same values
offline from the frozen ``people_wikitext.jsonl`` cache), exactly as
``sports_infobox.py`` is shared by the sports pair. Because both sides call the
SAME versioned pure functions over the SAME pinned wikitext bytes, the
harvest-time reading and the derive-time reading cannot silently drift.

Three pure capabilities:

* :func:`extract_death_fields` — locate the page's person infobox and read the
  RAW wikitext spans of the three death fields (date / place / cause), by an
  alias whitelist. Refuse-to-guess: no infobox, or no death alias present,
  reports exactly that; nothing is inferred from prose.
* :func:`clean_death_value` — reduce one raw field span to a comparator-ready
  value string: the death DATE via the shared
  ``stage1.people_infobox.clean_date_field`` (the compound
  ``{{Death date and age}}`` template family + free-text dates, precision
  honored), the PLACE / CAUSE via ``stage1.wikitext.clean_value`` (first
  readable item — for a place that is the leading wikilink target, the same
  namespace as the Wikidata P20 sitelink the corroboration gate compares
  against). Every refusal carries a machine-readable reason.
* :func:`prose_chars` — the BODY-PROSE length measure backing the
  ``real_article_body`` gate (the 'poisoned paragraph' bio study needs a real
  article body, not a one-line stub). A pure function of the wikitext:
  comments, refs, ALL balanced templates (the infobox included), tables,
  file/image/category links, headings, HTML tags and quote markup are
  stripped; wikilinks reduce to their display text; whitespace collapses.
  Paragraph text counts at FULL weight; list/indent/definition-line text
  counts at HALF weight (people_prose:v2 — v1 dropped list lines wholesale,
  conflating 'formatted as a list' with 'no article body' and stubbing out
  genuine bullet-formatted bios; half weight keeps a real bulleted biography
  above the floor while a bare linkfarm stub still measures near zero).
  KNOWN CAVEAT: table-formatted content still measures 0 — a bio whose whole
  body is a table reads as a stub (errs toward exclusion, by name, never
  contamination). Versioned (:data:`PROSE_VERSION`) so a future tweak to the
  stripping rules cannot silently move articles across the prose floor; v2 is
  MONOTONICALLY >= v1 for every page (the paragraph component is identical
  and the list component only adds), so no article that passed the floor
  under v1 can fail under v2 and the v1-measured grounding percentiles remain
  a conservative floor basis.

Contract (pipeline-wide): pure, total, no I/O / network / clock; malformed
input degrades to empty results with reasons, never an exception.
"""

from __future__ import annotations

import re

from stage1.people_infobox import EXTRACTOR_VERSION as _DATE_CLEANER_VERSION
from stage1.people_infobox import clean_date_field
from stage1.wikitext import (
    WIKI_INFOBOX_VERSION,
    clean_value,
    extract_field,
    find_infoboxes,
    find_templates,
    strip_comments,
    strip_refs,
)

PEOPLE_DEATH_VERSION = "people_death:v1"
# The full extractor pedigree recorded in evidence/params: the shared infobox
# parser + the shared date-field cleaner + this module's field/prose rules.
EXTRACTOR_VERSION = f"{WIKI_INFOBOX_VERSION}+{_DATE_CLEANER_VERSION}+{PEOPLE_DEATH_VERSION}"
# The body-prose measure's own version tag (recorded wherever a prose_chars
# number is stamped, so the 600-char floor is pinned to the rules that measured
# it). v2: list/indent-line text counts at half weight instead of being dropped
# (monotonically >= v1 — see the module docstring).
PROSE_VERSION = "people_prose:v2"

# Death-field aliases per property, in fall-through order (the enwiki person
# infobox family overwhelmingly uses the underscored spellings; the collapsed
# variants are real template-redirect spellings kept defensively).
DEATH_FIELD_ALIASES = {
    "deathdate": ("death_date", "deathdate", "date_of_death", "dateofdeath", "death-date"),
    "deathplace": ("death_place", "deathplace", "place_of_death", "placeofdeath"),
    "deathcause": ("death_cause", "deathcause", "cause_of_death", "causeofdeath"),
}
DEATH_PROPERTIES = ("deathdate", "deathplace", "deathcause")


# --------------------------------------------------------------------------
# Infobox death-field extraction
# --------------------------------------------------------------------------


def _select_infobox(boxes: list):
    """The infobox to read death fields from: the FIRST infobox carrying ANY
    death alias, else the first infobox on the page (document order — the
    page's primary subject box). None when the page has no infobox."""
    if not boxes:
        return None
    all_aliases = [a for aliases in DEATH_FIELD_ALIASES.values() for a in aliases]
    for box in boxes:
        fields = box.get("fields") or {}
        if any(alias in fields for alias in all_aliases):
            return box
    return boxes[0]


def extract_death_fields(content) -> dict:
    """Read the raw death-field spans from a full page's wikitext.

    Returns::

        {"infobox_found": bool,
         "template_name": str | None,      # the selected infobox's name
         "fields": {property: {"present": bool,
                               "field": alias | None,   # the alias used
                               "raw": str,              # raw span ('' if absent)
                               "blank": bool,           # present but empty
                               "notes": [...]} }}

    Total: a non-string page yields infobox_found=False with every field
    absent. NOTHING is guessed from prose — an infobox-less article reports
    exactly that (the adapter names it in the disposition)."""
    out = {
        "infobox_found": False,
        "template_name": None,
        "fields": {
            prop: {"present": False, "field": None, "raw": "", "blank": False, "notes": []}
            for prop in DEATH_PROPERTIES
        },
    }
    if not isinstance(content, str) or not content.strip():
        return out
    boxes = find_infoboxes(content)
    box = _select_infobox(boxes)
    if box is None:
        return out
    out["infobox_found"] = True
    out["template_name"] = box.get("template_name")
    for prop, aliases in DEATH_FIELD_ALIASES.items():
        got = extract_field(box, aliases)
        if got is None:
            continue
        out["fields"][prop] = {
            "present": True,
            "field": got.get("field"),
            "raw": got.get("raw") if isinstance(got.get("raw"), str) else "",
            "blank": bool(got.get("blank")),
            "notes": list(got.get("notes") or []),
        }
    return out


def clean_death_value(prop: str, raw) -> dict:
    """Reduce one raw death-field span to a comparator-ready value string.

    Returns ``{"ok", "value", "precision", "reason", "notes"}``:

    * ``deathdate``  -> :func:`stage1.people_infobox.clean_date_field` (the
      emitted string re-parses with the date comparator; ``precision`` carries
      the template's/free text's own precision — never promoted).
    * ``deathplace`` / ``deathcause`` -> :func:`stage1.wikitext.clean_value`,
      pinned to the FIRST readable item (a multi-item value notes the pin).

    Refuse-to-guess: an unreadable span yields ok=False with the reason; the
    caller keeps the side empty (absent) and the refusal in evidence."""
    out = {"ok": False, "value": None, "precision": None, "reason": None, "notes": []}
    if prop not in DEATH_FIELD_ALIASES:
        out["reason"] = f"unknown death property {prop!r}"
        return out
    if not isinstance(raw, str) or not raw.strip():
        out["reason"] = "empty field wikitext"
        return out
    if prop == "deathdate":
        rec = clean_date_field(raw)
        out["notes"] = list(rec.notes)
        if not rec.ok or rec.value is None:
            out["reason"] = rec.reason or "date recovery refused"
            return out
        out.update(ok=True, value=rec.value, precision=rec.precision)
        return out
    values, notes = clean_value(raw)
    out["notes"] = list(notes)
    if not values:
        out["reason"] = "no readable value item (see notes)"
        return out
    if len(values) > 1:
        out["notes"].append(f"multi-item value; pinned to the first of {len(values)}")
    out.update(ok=True, value=values[0])
    return out


# --------------------------------------------------------------------------
# Body-prose measure (people_prose:v2)
# --------------------------------------------------------------------------

# [[File:...]] / [[Image:...]] / [[Category:...]] / [[Media:...]] link heads
# (case-insensitive, optional leading colon) — removed with their FULL nested
# span (captions may nest wikilinks), by a balanced scan.
_NONPROSE_LINK_RE = re.compile(r"^\s*:?\s*(file|image|category|media)\s*:", re.I)
_TABLE_RE = re.compile(r"\{\|.*?\|\}", re.S)
_WIKILINK_REDUCE_RE = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]|]*)\]\]")
_EXTLINK_LABELED_RE = re.compile(r"\[\s*(?:https?|ftp)://\S*\s+([^\]]*)\]", re.I)
_EXTLINK_BARE_RE = re.compile(r"\[\s*(?:https?|ftp)://[^\]\s]*\s*\]", re.I)
_TAG_RE = re.compile(r"<[^<>]*>")
_QUOTES_RE = re.compile(r"''+")


def _strip_top_level_templates(text: str) -> str:
    """Remove every balanced top-level ``{{...}}`` span (the infobox and all
    other templates). Unbalanced braces are left in place (refuse-to-guess;
    they are stripped as stray characters later)."""
    templates = find_templates(text)
    if not templates:
        return text
    spans = []
    for t in templates:
        if any(o["start"] < t["start"] and o["end"] >= t["end"] for o in templates
               if (o["start"], o["end"]) != (t["start"], t["end"])):
            continue  # nested inside another template
        spans.append((t["start"], t["end"]))
    spans.sort()
    parts = []
    last = 0
    for start, end in spans:
        if start >= last:
            parts.append(text[last:start])
            last = end
    parts.append(text[last:])
    return "".join(parts)


def _strip_nonprose_links(text: str) -> str:
    """Remove [[File:...]]/[[Image:...]]/[[Category:...]]/[[Media:...]] links
    with their full (possibly nested) span via a balanced ``[[``/``]]`` scan."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("[[", i):
            head_end = i + 2
            while head_end < n and text[head_end] not in "|]":
                head_end += 1
            if _NONPROSE_LINK_RE.match(text[i + 2:head_end]):
                depth = 1
                j = i + 2
                while j < n - 1 and depth:
                    if text.startswith("[[", j):
                        depth += 1
                        j += 2
                    elif text.startswith("]]", j):
                        depth -= 1
                        j += 2
                    else:
                        j += 1
                i = j
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _finalize_prose(text: str) -> str:
    """The shared tail of the prose pipeline: strip HTML tags, quote markup
    and stray leftover markup characters, collapse whitespace."""
    text = _TAG_RE.sub(" ", text)
    text = _QUOTES_RE.sub("", text)
    # Stray leftover markup characters are not prose.
    text = re.sub(r"[\[\]{}|]", " ", text)
    return " ".join(text.split())


def prose_chars(content) -> int:
    """The BODY-PROSE character count of a full page's wikitext (see the
    module docstring for the exact stripping pipeline). Pure and total: a
    non-string page measures 0. Versioned as :data:`PROSE_VERSION`
    (people_prose:v2: paragraph text at full weight + list/indent-line text at
    HALF weight — monotonically >= the v1 measure, which dropped list lines
    wholesale and stubbed out genuine bullet-formatted bios)."""
    if not isinstance(content, str):
        return 0
    text = strip_refs(strip_comments(content))
    # Templates to a fixed point: stripping a top-level span can expose no new
    # top-level templates (nested ones went with their parent), but a page can
    # interleave unbalanced+balanced braces — one pass is exact for balanced text.
    text = _strip_top_level_templates(text)
    for _ in range(4):  # nested tables
        stripped = _TABLE_RE.sub(" ", text)
        if stripped == text:
            break
        text = stripped
    text = _strip_nonprose_links(text)
    # [[target|label]] -> label, [[target]] -> target (loop for adjacent links).
    prev = None
    while prev != text:
        prev = text
        text = _WIKILINK_REDUCE_RE.sub(lambda m: m.group(1), text)
    text = _EXTLINK_LABELED_RE.sub(lambda m: m.group(1), text)
    text = _EXTLINK_BARE_RE.sub(" ", text)
    body_lines = []
    list_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("=") and stripped.endswith("="):
            continue  # == heading ==
        if stripped[0] in "*#:;":
            # A list / indent / definition line: real bios (filmographies,
            # bullet-formatted careers) put genuine content here, so its text
            # counts at HALF weight (v2) instead of being dropped wholesale.
            item = stripped.lstrip("*#:; \t")
            if item:
                list_lines.append(item)
            continue
        if stripped.startswith("|") or stripped.startswith("{|") or stripped.startswith("!"):
            continue  # stray table row outside a balanced {| |} (measures 0 — see caveat)
        body_lines.append(stripped)
    body = _finalize_prose(" ".join(body_lines))
    listed = _finalize_prose(" ".join(list_lines))
    return len(body) + len(listed) // 2
