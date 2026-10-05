"""Free-text span comparator (value_type 'text_span', version v2).

The fallback comparator for values with no richer structure. Canonical form:
NFKD accent-folded, lowercased text with every non-alphanumeric character
treated as whitespace and runs of whitespace collapsed, plus the resulting
token list.

Comparison policy: identical folded text -> 'equal'; purely additive edits —
the shorter span's tokens appearing IN ORDER within the longer's, whether as
a prefix, a suffix, or with internal insertions ('James Smith' vs
'James Alan Smith') -> 'review', because added words are as likely a
restyling as a real change (this deliberately includes semantic inversions
like an inserted 'not': a token-level comparator cannot judge semantics, so
a human must); reorderings and substitutions -> 'different'. Containment is
checked on whole tokens, never substrings, so 'cat' does not match
'catalog'.
"""

from __future__ import annotations

import unicodedata

from stage1.normalize import Comparator, Comparison, ParseResult, register


def _fold(text: str) -> str:
    """NFKD-decompose, drop combining marks, lowercase ('Café' -> 'cafe')."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _shape_problem(canonical) -> str:
    """Return '' when canonical looks like this comparator's output, else a reason."""
    if not isinstance(canonical, dict):
        return f"canonical is not a dict: {type(canonical).__name__}"
    if canonical.get("kind") != TextSpanComparator.NAME:
        return f"canonical kind is {canonical.get('kind')!r}, expected 'text_span'"
    tokens = canonical.get("tokens")
    if not isinstance(tokens, list) or not tokens or not all(isinstance(t, str) and t for t in tokens):
        return "canonical 'tokens' must be a non-empty list of non-empty strings"
    if not isinstance(canonical.get("folded"), str):
        return "canonical 'folded' must be a string"
    return ""


def _is_token_subsequence(short, long) -> bool:
    """True when every token of ``short`` appears in ``long`` in order
    (whole-token matches only) — i.e. ``long`` is ``short`` plus insertions."""
    position = 0
    for token in short:
        while position < len(long) and long[position] != token:
            position += 1
        if position == len(long):
            return False
        position += 1
    return True


class TextSpanComparator(Comparator):
    NAME = "text_span"
    VERSION = "text_span:v2"

    def parse(self, raw) -> ParseResult:
        if not isinstance(raw, str):
            return ParseResult.failure(f"raw is not a string: {type(raw).__name__}")
        if not raw.strip():
            return ParseResult.failure("empty or whitespace-only raw")

        spaced = "".join(ch if ch.isalnum() else " " for ch in _fold(raw))
        tokens = spaced.split()
        if not tokens:
            return ParseResult.failure(f"no alphanumeric content after folding: {raw!r}")

        return ParseResult.success(
            {
                "kind": self.NAME,
                "folded": " ".join(tokens),
                "tokens": tokens,
            }
        )

    def compare(self, a_canonical, b_canonical) -> Comparison:
        problem = _shape_problem(a_canonical) or _shape_problem(b_canonical)
        if problem:
            return Comparison("incomparable", problem)

        tokens_a = tuple(a_canonical["tokens"])
        tokens_b = tuple(b_canonical["tokens"])
        if a_canonical["folded"] == b_canonical["folded"]:
            return Comparison("equal")

        if len(tokens_a) != len(tokens_b):
            short, long = sorted((tokens_a, tokens_b), key=len)
            if long[: len(short)] == short:
                return Comparison("review", "one span extends the other with additive trailing text")
            if long[-len(short):] == short:
                return Comparison("review", "one span extends the other with additive leading text")
            if _is_token_subsequence(short, long):
                return Comparison(
                    "review", "one span extends the other with additive internal token(s)"
                )

        return Comparison("different", "spans differ beyond additive token insertions")


register(TextSpanComparator())
