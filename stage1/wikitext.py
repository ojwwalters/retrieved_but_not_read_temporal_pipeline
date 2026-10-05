"""Shared deterministic wikitext/infobox extraction primitives.

Source-agnostic building blocks for reading MediaWiki infobox values out of
raw wikitext: balanced-brace template discovery, top-level parameter
splitting, HTML-comment / <ref> stripping, and value cleaning (first
wikilink target, wrapper-template unwrapping, <br>/list splitting, plain-
text fallback). Used by the sports adapter's infobox re-extraction and
intended for reuse by the upcoming wikipedia/people port.

Contract (matching the pipeline-wide rules):

* Pure functions, no I/O, no network, no wall-clock — same input text, same
  output, always.
* Versioned: WIKI_INFOBOX_VERSION names this module's behavior; any change
  to parsing/cleaning semantics must bump it, and callers record it in
  evidence/provenance so extracted values stay auditable.
* Refuse-to-guess: a WRONG extraction is worse than an empty one. Anything
  ambiguous (a template we cannot safely unwrap, an external link, stray
  markup) yields NO value plus a note explaining what was refused — the
  caller routes that to review, never to a silent guess.
* Total: malformed input (unbalanced braces, non-string values) degrades to
  empty results with notes, never an exception.

Structure conventions:

* find_infoboxes(wikitext) -> ordered list of
      {"template_name": str, "fields": {name: raw_value}, "start", "end"}
  Template names are normalized (lowercased, whitespace collapsed,
  'template:' prefix dropped); field names are lowercased/stripped; field
  values are raw wikitext spans (surrounding whitespace stripped) with
  HTML comments already removed. Later duplicate parameters win, mirroring
  MediaWiki. Nested infoboxes are reported too (their span lies inside the
  outer's), in document order.
* extract_field(infobox, field_aliases) -> the first alias with a readable
  value (or the first present alias when all are blank), as
      {"field", "raw", "values", "notes", "present", "blank"}
  values is the ordered cleaned value list from clean_value().
* clean_value(raw) -> (values, notes): comments/refs stripped, known
  wrapper templates ({{nowrap}}, {{plainlist}}, {{hlist}}, ...) unwrapped,
  <br>-separated and *-bulleted lists split into items, each item reduced
  to its first wikilink target or a plain-text fallback. Unknown templates
  spanning a value are refused with a note.
"""

from __future__ import annotations

import re

WIKI_INFOBOX_VERSION = "wiki_infobox:v1"

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
# An unterminated comment hides everything to the end of the text (MediaWiki
# behavior); handled separately so a stray '<!--' cannot leak markup.
_OPEN_COMMENT_RE = re.compile(r"<!--.*\Z", re.S)
_REF_SELF_RE = re.compile(r"<ref\b[^>]*?/\s*>", re.I | re.S)
_REF_PAIR_RE = re.compile(r"<ref\b[^>]*?>.*?</ref\s*>", re.I | re.S)
_BR_RE = re.compile(r"<br\s*/?\s*>", re.I)
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)")
_TAG_RE = re.compile(r"<[^<>]+>")
_QUOTES_RE = re.compile(r"''+")
_NAMED_PARAM_RE = re.compile(r"^\s*([^=\[\]{}|<>]+?)\s*=")

# Wrapper templates whose positional content IS the value (safe to unwrap).
WRAPPER_TEMPLATES = frozenset({"nowrap", "nobold", "noitalic", "small", "center", "nowraplinks"})
# List templates whose positional parameters (or *-bulleted lines) are the
# value items, in order.
LIST_TEMPLATES = frozenset({
    "plainlist", "plain list", "flatlist", "flat list", "hlist",
    "ubl", "unbulleted list", "bulleted list", "blist", "ublist",
})

_MAX_DEPTH = 6


def strip_comments(text: str) -> str:
    """Remove HTML comments (including an unterminated trailing one).
    Non-string input yields ''."""
    if not isinstance(text, str):
        return ""
    return _OPEN_COMMENT_RE.sub("", _COMMENT_RE.sub("", text))


def strip_refs(text: str) -> str:
    """Remove <ref .../> and <ref ...>...</ref> spans. Non-string -> ''."""
    if not isinstance(text, str):
        return ""
    return _REF_SELF_RE.sub("", _REF_PAIR_RE.sub("", text))


def normalize_template_name(name: str) -> str:
    """Lowercase, collapse all whitespace runs (incl. newlines) to single
    spaces, drop a leading 'template:' prefix."""
    if not isinstance(name, str):
        return ""
    normalized = " ".join(name.split()).lower()
    if normalized.startswith("template:"):
        normalized = normalized[len("template:"):].strip()
    return normalized


def find_templates(text: str) -> list:
    """Every balanced {{...}} template in `text`, in document order (by
    start offset): [{"name": normalized_name, "start": int, "end": int}].
    end is one past the closing '}}'. Nested templates are reported too.
    Unbalanced braces are simply not reported (refuse-to-guess). Single-pass
    stack scan — O(len(text))."""
    if not isinstance(text, str):
        return []
    found = []
    stack = []
    i = 0
    n = len(text)
    while i < n - 1:
        two = text[i:i + 2]
        if two == "{{":
            stack.append(i)
            i += 2
            continue
        if two == "}}":
            if stack:
                start = stack.pop()
                end = i + 2
                inner = text[start + 2:i]
                head = inner.split("|", 1)[0]
                found.append({
                    "name": normalize_template_name(head),
                    "start": start,
                    "end": end,
                })
            i += 2
            continue
        i += 1
    found.sort(key=lambda t: (t["start"], -t["end"]))
    return found


def split_top_level(body: str, sep: str = "|") -> list:
    """Split `body` on `sep` occurring at zero [[..]] / {{..}} nesting depth
    (the template-parameter split). Always returns at least one part."""
    if not isinstance(body, str):
        return [""]
    parts = []
    current = []
    depth_link = depth_tpl = 0
    i = 0
    n = len(body)
    while i < n:
        two = body[i:i + 2]
        if two == "[[":
            depth_link += 1
            current.append(two)
            i += 2
            continue
        if two == "]]":
            depth_link = max(0, depth_link - 1)
            current.append(two)
            i += 2
            continue
        if two == "{{":
            depth_tpl += 1
            current.append(two)
            i += 2
            continue
        if two == "}}":
            depth_tpl = max(0, depth_tpl - 1)
            current.append(two)
            i += 2
            continue
        ch = body[i]
        if ch == sep and depth_link == 0 and depth_tpl == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts


def template_fields(text: str, start: int, end: int) -> dict:
    """Named parameters of the template spanning [start, end) in `text`:
    {lowercased_name: raw_value_stripped}. Splits ONLY at top-level '|'
    (nested templates/wikilinks stay inside their values); a later duplicate
    parameter wins, mirroring MediaWiki. Positional parameters are ignored
    here (use split_top_level directly for them)."""
    if not isinstance(text, str):
        return {}
    inner = text[start + 2:end - 2]
    fields: dict = {}
    for part in split_top_level(inner)[1:]:
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip().lower()
        if not key:
            continue
        fields[key] = value.strip()
    return fields


def find_infoboxes(wikitext: str) -> list:
    """Ordered list of every {{Infobox ...}} template in the page:
    [{"template_name", "fields", "start", "end"}], document order. HTML
    comments are stripped BEFORE brace scanning (a commented-out brace must
    not unbalance the scan), so start/end index into the comment-stripped
    text. Nested infoboxes (e.g. embedded via a |module= parameter) are
    included; the caller chooses its selection policy."""
    text = strip_comments(wikitext)
    boxes = []
    for tpl in find_templates(text):
        name = tpl["name"]
        if name == "infobox" or name.startswith("infobox "):
            boxes.append({
                "template_name": name,
                "fields": template_fields(text, tpl["start"], tpl["end"]),
                "start": tpl["start"],
                "end": tpl["end"],
            })
    return boxes


def first_wikilink_target(text: str):
    """The first [[target|...]] link target in `text` (whitespace collapsed),
    or None. Anchors are kept verbatim ('Page#Section')."""
    if not isinstance(text, str):
        return None
    match = _WIKILINK_RE.search(text)
    if not match:
        return None
    target = " ".join(match.group(1).split())
    return target or None


def _positional_parts(parts: list) -> list:
    """Positional (unnamed) parameters among template parts (everything
    after the name part). A part is 'named' when it starts with a plain
    key= (no brackets/braces in the key)."""
    positional = []
    for part in parts:
        if _NAMED_PARAM_RE.match(part):
            continue
        positional.append(part)
    return positional


def _whole_value_template(text: str):
    """(name, parts) when the trimmed text is exactly ONE balanced template,
    else None."""
    trimmed = text.strip()
    if not (trimmed.startswith("{{") and trimmed.endswith("}}")):
        return None
    for tpl in find_templates(trimmed):
        if tpl["start"] == 0 and tpl["end"] == len(trimmed):
            parts = split_top_level(trimmed[2:-2])
            return tpl["name"], parts[1:]
    return None


def _bullet_lines(text: str):
    """Split '*'-bulleted lines into items; None when the text has no
    bulleted line (then it is not a list)."""
    if "\n" not in text:
        return None
    lines = [line.strip() for line in text.splitlines()]
    if not any(line.startswith("*") for line in lines if line):
        return None
    items = []
    for line in lines:
        if not line:
            continue
        items.append(line.lstrip("*").strip())
    return items


def _expand(text: str, notes: list, depth: int) -> list:
    """Recursively expand wrappers/lists/<br> splits into leaf item strings."""
    text = text.strip()
    if not text:
        return []
    if depth > _MAX_DEPTH:
        notes.append("nesting depth exceeded; refusing to guess")
        return []

    whole = _whole_value_template(text)
    if whole is not None:
        name, parts = whole
        if name in WRAPPER_TEMPLATES:
            items = []
            for part in _positional_parts(parts):
                items.extend(_expand(part, notes, depth + 1))
            return items
        if name in LIST_TEMPLATES:
            items = []
            for part in _positional_parts(parts):
                bullets = _bullet_lines(part)
                for piece in (bullets if bullets is not None else [part]):
                    items.extend(_expand(piece, notes, depth + 1))
            return items
        notes.append(
            f"unrecognized template {name!r} spans the whole value; refusing to guess"
        )
        return []

    br_parts = _split_top_level_br(text)
    if len(br_parts) > 1:
        items = []
        for part in br_parts:
            items.extend(_expand(part, notes, depth + 1))
        return items

    bullets = _bullet_lines(text)
    if bullets is not None:
        items = []
        for piece in bullets:
            items.extend(_expand(piece, notes, depth + 1))
        return items

    return [text]


def _split_top_level_br(text: str) -> list:
    """Split on <br> tags occurring at zero [[..]]/{{..}} depth."""
    cut_points = []
    depth_link = depth_tpl = 0
    i = 0
    n = len(text)
    while i < n:
        two = text[i:i + 2]
        if two == "[[":
            depth_link += 1
            i += 2
            continue
        if two == "]]":
            depth_link = max(0, depth_link - 1)
            i += 2
            continue
        if two == "{{":
            depth_tpl += 1
            i += 2
            continue
        if two == "}}":
            depth_tpl = max(0, depth_tpl - 1)
            i += 2
            continue
        if text[i] == "<" and depth_link == 0 and depth_tpl == 0:
            match = _BR_RE.match(text, i)
            if match:
                cut_points.append((i, match.end()))
                i = match.end()
                continue
        i += 1
    if not cut_points:
        return [text]
    parts = []
    last = 0
    for start, end in cut_points:
        parts.append(text[last:start])
        last = end
    parts.append(text[last:])
    return parts


def _clean_leaf(text: str, notes: list) -> str:
    """One leaf item -> cleaned value string ('' + note when refused)."""
    target = first_wikilink_target(text)
    if target is not None:
        remainder = _WIKILINK_RE.sub("", text, count=1)
        if _WIKILINK_RE.search(remainder):
            notes.append(f"additional wikilinks after {target!r} ignored")
        return target
    if "{{" in text or "}}" in text:
        notes.append(
            f"embedded template inside a plain-text value; refusing to guess: {text[:80]!r}"
        )
        return ""
    if "[" in text or "]" in text:
        notes.append(
            f"external link or stray bracket in value; refusing to guess: {text[:80]!r}"
        )
        return ""
    cleaned = _QUOTES_RE.sub("", text)
    cleaned = cleaned.replace("&nbsp;", " ")
    if "<" in cleaned and ">" in cleaned:
        notes.append(f"html tag stripped from value: {cleaned[:80]!r}")
        cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned


def clean_value(raw) -> tuple:
    """(values, notes): the ordered readable items of one raw field value.

    Pipeline: strip comments and <ref>s; unwrap whitelisted wrapper/list
    templates; split <br>-separated and *-bulleted items; reduce each item
    to its first wikilink target, else a plain-text fallback (bold/italic
    markup dropped, entities/tags normalized). Anything that cannot be read
    SAFELY (an unknown template spanning the value, an external link, stray
    braces) contributes no value and a human-readable note instead — the
    refuse-to-guess contract."""
    notes: list = []
    if not isinstance(raw, str):
        notes.append(f"value is not a string: {type(raw).__name__}")
        return [], notes
    text = strip_refs(strip_comments(raw))
    values = []
    for item in _expand(text, notes, 0):
        cleaned = _clean_leaf(item, notes)
        if cleaned:
            values.append(cleaned)
    return values, notes


def extract_field(infobox: dict, field_aliases) -> dict | None:
    """Resolve an ordered alias list against one infobox's fields.

    Returns None when NO alias is present at all. Otherwise returns

        {"field": str,      # the alias whose value was used
         "raw": str,        # its raw wikitext value (verbatim span)
         "values": list,    # clean_value() items, [] when blank/unreadable
         "notes": list,     # refuse-to-guess notes from cleaning
         "present": list,   # every alias present, in alias order
         "blank": bool}     # True when raw strips to nothing

    Alias policy: the first alias with a non-empty cleaned value wins
    (mirroring the legacy harvest's fall-through); when every present alias
    is blank/unreadable, the FIRST present alias is reported so the caller
    can distinguish 'present but blank' (an explicit empty state) from
    'absent'."""
    if not isinstance(infobox, dict) or not isinstance(infobox.get("fields"), dict):
        return None
    fields = infobox["fields"]
    present = []
    candidates = []
    for alias in field_aliases:
        if not isinstance(alias, str):
            continue
        key = alias.strip().lower()
        if key in fields:
            present.append(key)
            raw = fields[key]
            values, notes = clean_value(raw)
            candidates.append({
                "field": key,
                "raw": raw,
                "values": values,
                "notes": notes,
                "blank": not raw.strip(),
            })
    if not candidates:
        return None
    chosen = next((c for c in candidates if c["values"]), candidates[0])
    result = dict(chosen)
    result["present"] = present
    return result
