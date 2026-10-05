"""Organization-name comparator (value_type 'org', version v3).

Canonical form: NFKD accent-folded, lowercased tokens split at the first
comma into head and qualifier. Hyphens split tokens exactly as whitespace
does (mirroring text_span), so pure punctuation variants like 'Al-Nassr' vs
'Al Nassr' canonicalize identically. Legal/club suffix tokens (FC, F.C.,
AFC, CF, Inc., Corp., Ltd., Co., plc, LLC) are folded away from the
TRAILING position only — 'Ayutthaya United F.C.' and 'Ayutthaya United FC'
canonicalize identically, and 'NRG Energy, Inc.' keeps no qualifier because
', Inc.' is a legal suffix, not a location — but a suffix-lookalike token
elsewhere is part of the name: 'CF Industries Holdings' keeps its 'CF', and
'AFC Wimbledon' keeps its leading 'AFC' (it names a different club than
'Wimbledon F.C.'; the pair compares 'review' via token-set extension, never
'equal'). The leading article 'the' is dropped. '&' is normalized to 'and'.

The suffix fold is deliberately NOT extended to other club-type tokens
(SC, SK, FK, VV, BC, AC, FF, ...): a cache-wide collision scan over the
sports Wikidata P54 team names showed that folding them equates DISTINCT
clubs — 'Vitória S.C.' (Guimarães) vs 'Vitória F.C.' (Setúbal),
'Panathinaikos B.C.' vs 'Panathinaikos F.C.' (different sports of one
multi-sport club), 'Al Ahli SC' vs 'Al Ahli FC', 'Valencia BC' vs
'Valencia CF'. Pairs like 'Al Ahly' vs 'Al Ahly SC' therefore stay at
'review' (token-set extension) rather than 'equal': a review costs a human
look, a false equal silently corrupts the dataset.

Comparison policy: identical head tokens with identical qualifiers ->
'equal'; identical head tokens with differing (or one-sided) qualifiers ->
'review' ('London, England' vs 'London, UK' may or may not denote the same
thing); one head's token set strictly extending the other's -> 'review';
heads that differ ONLY by leading club-type tokens (FC/FK/SC/SK/KS/NK/PFC/
CF/AFC) -> 'review' ('FC Soligorsk' vs 'FK Soligorsk' is usually one club
under two romanization stylings, but 'FC Saksan' vs 'Saksan FC' and
'FC Kuban' vs 'PFC Kuban' are genuinely distinct/refounded clubs, so this
can never be 'equal' — and never 'different' either, which had produced
misleading sources_disagree verdicts); anything else -> 'different'.
"""

from __future__ import annotations

import unicodedata

from stage1.normalize import Comparator, Comparison, ParseResult, register

_SUFFIXES = frozenset({"fc", "afc", "cf", "inc", "corp", "ltd", "co", "plc", "llc"})
_ARTICLES = frozenset({"the"})

# Leading club-type tokens for the review-only prefix rule in compare().
# NEVER folded at parse time (canonical keeps them): folding to equality is
# provably unsafe (see the module docstring); the rule only demotes
# 'different' to 'review' when heads match after stripping these.
_CLUB_PREFIXES = frozenset({"fc", "afc", "cf", "fk", "sc", "sk", "ks", "nk", "pfc"})


def _fold(text: str) -> str:
    """NFKD-decompose, drop combining marks, lowercase ('Atlético' -> 'atletico')."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _clean_token(token: str) -> str:
    """Keep letters, digits, and name-internal apostrophes."""
    kept = "".join(ch for ch in token if ch.isalnum() or ch == "'")
    return kept.strip("'")


def _tokens(part: str) -> list:
    """Cleaned tokens of one part: hyphens split like whitespace, leading
    articles and TRAILING suffix tokens folded away (a suffix-lookalike
    token in any other position is part of the name and is kept)."""
    raw_tokens = part.replace(",", " ").replace("-", " ").split()
    cleaned = [t for t in (_clean_token(tok) for tok in raw_tokens) if t]
    while cleaned and cleaned[0] in _ARTICLES:
        cleaned.pop(0)
    while cleaned and cleaned[-1] in _SUFFIXES:
        cleaned.pop()
    return cleaned


def _shape_problem(canonical) -> str:
    """Return '' when canonical looks like this comparator's output, else a reason."""
    if not isinstance(canonical, dict):
        return f"canonical is not a dict: {type(canonical).__name__}"
    if canonical.get("kind") != OrgComparator.NAME:
        return f"canonical kind is {canonical.get('kind')!r}, expected 'org'"
    head = canonical.get("head_tokens")
    if not isinstance(head, list) or not head or not all(isinstance(t, str) and t for t in head):
        return "canonical 'head_tokens' must be a non-empty list of non-empty strings"
    if "qualifier" not in canonical:
        return "canonical is missing key 'qualifier'"
    qualifier = canonical["qualifier"]
    if qualifier is not None and not isinstance(qualifier, str):
        return "canonical 'qualifier' must be a string or None"
    if not isinstance(canonical.get("folded"), str):
        return "canonical 'folded' must be a string"
    return ""


def _strip_club_prefixes(tokens: list) -> list:
    stripped = list(tokens)
    while stripped and stripped[0] in _CLUB_PREFIXES:
        stripped.pop(0)
    return stripped


class OrgComparator(Comparator):
    NAME = "org"
    VERSION = "org:v3"

    def parse(self, raw) -> ParseResult:
        if not isinstance(raw, str):
            return ParseResult.failure(f"raw is not a string: {type(raw).__name__}")
        if not raw.strip():
            return ParseResult.failure("empty or whitespace-only raw")

        folded_all = _fold(raw).replace("&", " and ")
        head_part, _, qualifier_part = folded_all.partition(",")
        head_tokens = _tokens(head_part)
        if not head_tokens:
            return ParseResult.failure(f"no head tokens after folding/suffix stripping: {raw!r}")
        qualifier_tokens = _tokens(qualifier_part)
        qualifier = " ".join(qualifier_tokens) if qualifier_tokens else None

        folded = " ".join(head_tokens)
        if qualifier is not None:
            folded = f"{folded}, {qualifier}"

        return ParseResult.success(
            {
                "kind": self.NAME,
                "head_tokens": head_tokens,
                "qualifier": qualifier,
                "folded": folded,
            }
        )

    def compare(self, a_canonical, b_canonical) -> Comparison:
        problem = _shape_problem(a_canonical) or _shape_problem(b_canonical)
        if problem:
            return Comparison("incomparable", problem)
        a, b = a_canonical, b_canonical

        if a["head_tokens"] == b["head_tokens"]:
            if a["qualifier"] == b["qualifier"]:
                return Comparison("equal")
            if a["qualifier"] is None or b["qualifier"] is None:
                return Comparison("review", "same head but a qualifier appears on only one side")
            return Comparison(
                "review",
                f"same head but differing qualifiers: {a['qualifier']!r} vs {b['qualifier']!r}",
            )

        set_a, set_b = set(a["head_tokens"]), set(b["head_tokens"])
        if set_a < set_b or set_b < set_a:
            return Comparison(
                "review",
                "one org's head tokens strictly extend the other's; possible renaming or short form",
            )

        stripped_a = _strip_club_prefixes(a["head_tokens"])
        stripped_b = _strip_club_prefixes(b["head_tokens"])
        if stripped_a and stripped_a == stripped_b:
            return Comparison(
                "review",
                "heads differ only by leading club-type token(s) (FC/FK/SC/...): "
                "possibly one club under two stylings or romanizations, possibly a "
                "distinct refounded or sibling club — cannot be decided by name alone",
            )
        return Comparison("different", "head tokens differ")


register(OrgComparator())
