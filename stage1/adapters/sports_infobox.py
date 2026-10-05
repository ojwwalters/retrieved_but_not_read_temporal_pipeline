"""Sports-specific current-club extraction policy on top of stage1.wikitext.

One pure function — extract_current_club(wikitext) — implements the
versioned, deterministic policy for reading a player's current club out of
a pinned enwiki revision. It exists to repair the legacy harvest's parser
failures (the 258 empty-club review rows) and to consistency-check the
legacy extractions, per the failure taxonomy diagnosed against
stage1/cache/sports_wikitext.jsonl.

TEMPLATE REGISTRY (extends the legacy 7-substring whitelist with the
templates the taxonomy found on the empty sides). Matching is by substring
of the normalized FIRST-infobox name, first rule wins — ORDER MATTERS:
'gridiron football biography' must precede 'football biography' (substring
containment), exactly like the legacy list. Additions vs legacy:

* 'nfl biography'          -> current_team, team (NFL pages use this, not
                              'gridiron football biography')
* 'college football player'-> school (the college program IS the current
                              org; the corroborating Wikidata P54 item for
                              these players is the college program, so the
                              org-match corroboration gate still
                              adjudicates)
* 'npb player'             -> team ONLY ('debutteam'/'finalteam' must never
                              substitute — they are historical facts)
* 'mlb player'             -> team
* 'ice hockey player'      -> team (enwiki's ice-hockey template is
                              'ice hockey player'; the legacy whitelist
                              only knew the nonexistent 'ice hockey
                              biography')
* the gridiron/NFL family gains 'team' as an ordered alias after
  'current_team' (5 draftee pages store the club there).

CURRENT-CLUB RESOLUTION POLICY (per side, in order):

1. #REDIRECT page -> no value, reason 'redirect_at_snapshot' (following the
   redirect would bind the fact to a volatile roster page — refused).
2. The FIRST infobox on the page is selected. If its template is not in
   the registry: when exactly one REGISTERED infobox is nested inside its
   extent (the |module= embedding pattern), that nested infobox is used
   (recorded as embedded_in); otherwise no value, reason
   'template_not_recognized' with the template name recorded.
3. The registry's dedicated current-team field aliases are tried in order
   (stage1.wikitext.extract_field): the first alias with a readable value
   wins. Params are resolved ONLY within the selected infobox's balanced-
   brace extent — never by regex over the whole page (the legacy bug that
   let a 'team' param of an unrelated template match).
   3b. (v2) A readable team-field value whose raw span carries a LOAN
   annotation ('[[X]]<br>(on loan from [[Y]])', a leading '→') is returned
   with an explicit machine-readable flag: loan=True, with the matched
   marker in detail['loan_marker'] (the parent club is visible in
   detail['values']). The club value is still extracted (it IS what the
   infobox displays), but the flag is part of the result contract: callers
   MUST NOT present a loan-flagged value as a senior-club fact without
   routing it to a human — the sports adapter refuses to gap-fill from a
   flagged side and reviews flagged records via its sports_loan gate.
   Detection strips <ref>...</ref> spans first (a citation mentioning
   'loan' must not flag a clean value) and mirrors the career-list loan
   guard (which refuses outright rather than flagging, because a career
   row, unlike the displayed team field, is a pure inference).
4. A readable value equal to an explicit free-agency marker ('Free agent',
   'Retired', ...) -> the canonical 'unattached' state, reason
   'explicit_marker'.
5. A present-but-BLANK field (or an absent field) is NOT a parse failure:
   the career-history check runs first (football-biography family only,
   where the taxonomy validated it): if the highest-numbered clubsN/teamsN
   row has an OPEN-ENDED yearsN ('2026–'), that club is the current club
   (via 'career_list') — unless the row is a loan row ('→' prefix /
   'loan'), which is refused with reason 'career_loan_row' (extracting a
   loan destination as the senior club would fabricate a transfer).
   Otherwise the side is the explicit canonical state 'unattached'
   (reason 'field_blank' / 'field_absent', career state recorded) —
   deliberately DISTINCT from 'no value': a blank currentclub with a
   closed career list is positive evidence the player is clubless, while
   'no value' means the page could not be read.
6. A field whose raw value is non-blank but yields nothing readable
   (unknown template spanning it, external link, ...) -> no value, reason
   'value_unreadable' with the cleaner's refuse-to-guess notes.

The result dict is JSON-safe and carries the extractor versions so every
recovered value is auditable back to this exact policy.
"""

from __future__ import annotations

import re

from stage1.wikitext import (
    WIKI_INFOBOX_VERSION,
    clean_value,
    extract_field,
    find_infoboxes,
    find_templates,
    strip_comments,
    strip_refs,
)

SPORTS_INFOBOX_VERSION = "sports_infobox:v2"
EXTRACTOR_VERSION = f"{WIKI_INFOBOX_VERSION}+{SPORTS_INFOBOX_VERSION}"

# (substring of the normalized infobox name, sport tag, ordered current-team
#  field aliases, career-history fallback enabled). First match wins; see
# the module docstring for the ordering constraints.
TEMPLATE_REGISTRY = (
    ("gridiron football biography", "nfl", ("current_team", "team"), False),
    ("nfl biography", "nfl", ("current_team", "team"), False),
    ("college football player", "college_football", ("school",), False),
    ("football biography", "football", ("currentclub",), True),
    ("basketball biography", "nba", ("current_team", "team"), False),
    ("npb player", "npb", ("team",), False),
    ("mlb player", "mlb", ("team",), False),
    ("baseball biography", "mlb", ("team", "current_team"), False),
    ("ice hockey player", "nhl", ("team",), False),
    ("ice hockey biography", "nhl", ("team", "current_team"), False),
    ("rugby biography", "rugby", ("currentclub", "club"), False),
    ("afl biography", "afl", ("currentclub", "club"), False),
)

# Explicit unattached-state markers (exact match on the cleaned, lowercased
# value). The downstream sports_free_agent gate additionally regex-screens
# after-values; this set only decides the extraction-state classification.
FREE_AGENT_MARKERS = frozenset({
    "free agent", "free-agent", "unattached", "retired", "without club",
    "without a club", "none", "n/a",
})

_REDIRECT_RE = re.compile(r"^\s*#redirect\s*(?:\[\[([^\]|]+)\]\])?", re.I)
_CAREER_ROW_RE = re.compile(r"^(clubs|teams)(\d+)$")
_OPEN_YEARS_RE = re.compile(r"\d{4}\s*[–—-]\s*$")
# Loan annotation on a value ('(on loan from [[X]])', '(loan)', 'loaned to
# ...'). Word-bounded so a club name like 'Sloane' can never match; shared
# by the team-field flag (v2) and the career-row guard.
_LOAN_RE = re.compile(r"\bloan(?:ed|s)?\b", re.I)


def _loan_marker(raw: str):
    """The loan marker found in one raw field/row value, or None. The text
    is comment/ref-stripped BEFORE matching (a citation whose title mentions
    'loan' must not flag a clean value); a leading '→' (the loan-row arrow
    convention) counts as a marker."""
    if not isinstance(raw, str):
        return None
    text = strip_refs(strip_comments(raw))
    if text.strip().startswith("→"):
        return "→"
    match = _LOAN_RE.search(text)
    return match.group(0) if match else None


def lookup_template(template_name: str):
    """(sport, aliases, career_fallback) for the first registry rule whose
    substring occurs in the normalized template name, or None."""
    if not isinstance(template_name, str):
        return None
    for substring, sport, aliases, career in TEMPLATE_REGISTRY:
        if substring in template_name:
            return sport, aliases, career
    return None


def _result(status, reason=None, **extra) -> dict:
    result = {
        "extractor": EXTRACTOR_VERSION,
        "status": status,          # 'club' | 'unattached' | 'no_value'
        "club": None,
        "template": None,
        "template_registered": False,
        "sport": None,
        "field": None,
        "via": None,
        "loan": False,             # v2: True when the value is loan-annotated
        "reason": reason,
        "detail": {},
    }
    result.update(extra)
    return result


def _career_row(fields: dict):
    """(state, detail) for the highest-numbered clubsN/teamsN career row.

    state: 'open' (current club recovered in detail['club']),
    'career_loan_row' (open-ended but a loan row — refused),
    'career_row_unreadable', 'career_row_closed', 'no_career_rows'."""
    best = None
    for key, value in fields.items():
        match = _CAREER_ROW_RE.match(key)
        if match and isinstance(value, str) and value.strip():
            number = int(match.group(2))
            if best is None or number > best[0]:
                best = (number, key, value)
    if best is None:
        return "no_career_rows", {}
    number, key, raw = best
    years = fields.get(f"years{number}", "")
    years = years.strip() if isinstance(years, str) else ""
    detail = {"field": key, "years": years[:40]}
    values, notes = clean_value(raw.lstrip("→ "))
    club = values[0] if values else None
    if club is not None:
        detail["club_hint"] = club
    if not _OPEN_YEARS_RE.search(years):
        return "career_row_closed", detail
    if _loan_marker(raw) is not None:
        return "career_loan_row", detail
    if club is None:
        detail["notes"] = notes
        return "career_row_unreadable", detail
    detail["club"] = club
    if notes:
        detail["notes"] = notes
    return "open", detail


def _select_infobox(boxes: list):
    """(infobox, embedded_in) per the selection policy: the first infobox;
    if unregistered, exactly one registered infobox nested inside its
    extent may stand in for it (the |module= embedding pattern)."""
    first = boxes[0]
    if lookup_template(first["template_name"]) is not None:
        return first, None
    nested = [
        b for b in boxes[1:]
        if b["start"] > first["start"] and b["end"] <= first["end"]
        and lookup_template(b["template_name"]) is not None
    ]
    if len(nested) == 1:
        return nested[0], first["template_name"]
    return first, None


def extract_current_club(wikitext) -> dict:
    """Apply the sports current-club policy to one pinned revision's full
    wikitext. Returns the JSON-safe result dict described in _result();
    never raises. See the module docstring for the policy."""
    if not isinstance(wikitext, str) or not wikitext.strip():
        return _result("no_value", "empty_wikitext")

    redirect = _REDIRECT_RE.match(strip_comments(wikitext).lstrip())
    if redirect:
        target = redirect.group(1)
        detail = {"target": " ".join(target.split())} if target else {}
        return _result("no_value", "redirect_at_snapshot", detail=detail)

    boxes = find_infoboxes(wikitext)
    if not boxes:
        names = [t["name"] for t in find_templates(strip_comments(wikitext))[:8]]
        return _result("no_value", "no_infobox_template", detail={"templates_seen": names})

    box, embedded_in = _select_infobox(boxes)
    registry = lookup_template(box["template_name"])
    base = {
        "template": box["template_name"],
        "template_registered": registry is not None,
    }
    detail: dict = {}
    if embedded_in:
        detail["embedded_in"] = embedded_in
    if registry is None:
        return _result(
            "no_value", "template_not_recognized", detail=detail, **base
        )
    sport, aliases, career_enabled = registry
    base["sport"] = sport

    extracted = extract_field(box, aliases)
    if extracted is not None and extracted["values"]:
        first = extracted["values"][0]
        if first.lower() in FREE_AGENT_MARKERS:
            detail["marker"] = first
            if extracted["notes"]:
                detail["notes"] = extracted["notes"]
            return _result(
                "unattached", "explicit_marker",
                field=extracted["field"], detail=detail, **base,
            )
        if len(extracted["values"]) > 1:
            detail["values"] = extracted["values"]
        if extracted["notes"]:
            detail["notes"] = extracted["notes"]
        loan_marker = _loan_marker(extracted["raw"])
        if loan_marker is not None:
            # v2 loan flag (policy step 3b): the displayed club is a loan
            # destination ('[[X]]<br>(on loan from [[Y]])'). The value is
            # still returned — it IS what the infobox displays — but with a
            # machine-readable flag so no caller can silently present it as
            # the senior club (the career-list path refuses the identical
            # situation as 'career_loan_row'; here the parent club stays
            # visible in detail['values']).
            detail["loan_marker"] = loan_marker
            detail.setdefault("values", extracted["values"])
            return _result(
                "club", None, club=first, field=extracted["field"],
                via="team_field", loan=True, detail=detail, **base,
            )
        return _result(
            "club", None, club=first, field=extracted["field"],
            via="team_field", detail=detail, **base,
        )

    # No readable dedicated-field value. Distinguish blank from unreadable.
    if extracted is not None and not extracted["blank"]:
        detail["notes"] = extracted["notes"]
        detail["raw"] = extracted["raw"][:120]
        return _result(
            "no_value", "value_unreadable", field=extracted["field"],
            detail=detail, **base,
        )

    # Blank or absent field: the career-history check runs BEFORE any
    # unattached determination (taxonomy guard — Băluță's club lives in an
    # open-ended clubsN row while currentclub is blank).
    career_state, career_detail = ("career_check_disabled", {})
    if career_enabled:
        career_state, career_detail = _career_row(box["fields"])
        if career_state == "open":
            detail.update({k: v for k, v in career_detail.items() if k != "club"})
            return _result(
                "club", None, club=career_detail["club"],
                field=career_detail["field"], via="career_list",
                detail=detail, **base,
            )
        if career_state in ("career_loan_row", "career_row_unreadable"):
            detail["career"] = {"state": career_state, **career_detail}
            return _result("no_value", career_state, detail=detail, **base)
    detail["career"] = {"state": career_state, **career_detail}
    reason = "field_blank" if extracted is not None else "field_absent"
    field = extracted["field"] if extracted is not None else None
    return _result("unattached", reason, field=field, detail=detail, **base)
