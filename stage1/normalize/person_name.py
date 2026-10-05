"""Person-name comparator (value_type 'person_name', version v2).

Canonical form: NFKD accent-folded, lowercased tokens with honorifics
(Mr/Mrs/Ms/Miss/Mx/Dr) removed, generational suffixes (Jr/Sr/II-V) stripped
into a dedicated field, and 'Surname, Givens' comma inversion undone. The
last remaining token is the surname; earlier tokens are given names (full
words) or initials (single letters).

Comparison policy — deliberately conservative; 'equal' requires positive
identification, not mere compatibility (in succession data the people being
compared often share a surname, so compatibility is weak evidence):

* surname mismatch -> 'different', unless one name's token set strictly
  extends the other's (e.g. 'Shyamala Gopalan' vs 'Shyamala Gopalan Harris'),
  a likely restyling of the same person -> 'review';
* surname match with the FIRST given name agreeing in full -> 'equal'
  (middle names and middle initials are optional, and an initial may match a
  full middle name);
* surname match where given names are absent on one side ('Mr. Cook' vs
  'Timothy Cook'), or where the first given names are linked only by an
  initial-vs-full first-letter match ('J. Smith' vs 'Jane Smith'), ->
  'review': a bare surname or an initial cannot distinguish two people who
  share them;
* a generational suffix on exactly one side downgrades an otherwise-equal
  pair to 'review' — a father/son succession ('John Smith' vs
  'John Smith Jr.') differs only by the suffix; conflicting suffixes are
  'review' as before;
* surname match with conflicting given names -> 'review', never 'different':
  nicknames ('Robert' vs 'Bob') are indistinguishable from genuinely
  different people without a nickname table, which this comparator
  intentionally does not hardcode.
"""

from __future__ import annotations

import unicodedata

from stage1.normalize import Comparator, Comparison, ParseResult, register

_HONORIFICS = frozenset({"mr", "mrs", "ms", "miss", "mx", "dr"})
_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})


def _fold(text: str) -> str:
    """NFKD-decompose, drop combining marks, lowercase ('José' -> 'jose')."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _clean_token(token: str) -> str:
    """Keep letters and name-internal hyphens/apostrophes, drop the rest."""
    kept = "".join(ch for ch in token if ch.isalpha() or ch in "'-")
    return kept.strip("'-")


def _shape_problem(canonical) -> str:
    """Return '' when canonical looks like this comparator's output, else a reason."""
    if not isinstance(canonical, dict):
        return f"canonical is not a dict: {type(canonical).__name__}"
    if canonical.get("kind") != PersonNameComparator.NAME:
        return f"canonical kind is {canonical.get('kind')!r}, expected 'person_name'"
    tokens = canonical.get("tokens")
    if not isinstance(tokens, list) or not tokens or not all(isinstance(t, str) and t for t in tokens):
        return "canonical 'tokens' must be a non-empty list of non-empty strings"
    if not isinstance(canonical.get("surname"), str) or not isinstance(canonical.get("folded"), str):
        return "canonical 'surname' and 'folded' must be strings"
    if "suffix" not in canonical:
        return "canonical is missing key 'suffix'"
    suffix = canonical["suffix"]
    if suffix is not None and not isinstance(suffix, str):
        return "canonical 'suffix' must be a string or None"
    return ""


def _givens_compatible(x: str, y: str) -> bool:
    """Two given-position tokens agree: equal, or initial-vs-full first letter."""
    if x == y:
        return True
    if len(x) == 1 and y.startswith(x):
        return True
    if len(y) == 1 and x.startswith(y):
        return True
    return False


class PersonNameComparator(Comparator):
    NAME = "person_name"
    VERSION = "person_name:v2"

    def parse(self, raw) -> ParseResult:
        if not isinstance(raw, str):
            return ParseResult.failure(f"raw is not a string: {type(raw).__name__}")
        if not raw.strip():
            return ParseResult.failure("empty or whitespace-only raw")
        if any(ch.isdigit() for ch in raw):
            return ParseResult.failure(f"digits are not valid in a person name: {raw!r}")

        segments = []
        for segment in _fold(raw).split(","):
            tokens = [t for t in (_clean_token(tok) for tok in segment.split()) if t]
            tokens = [t for t in tokens if t not in _HONORIFICS]
            if tokens:
                segments.append(tokens)

        suffixes: list = []
        name_segments = []
        for tokens in segments:
            if all(t in _SUFFIXES for t in tokens):
                suffixes.extend(tokens)
            else:
                name_segments.append(tokens)

        if not name_segments:
            return ParseResult.failure(f"no name tokens after folding: {raw!r}")
        if len(name_segments) == 1:
            tokens = name_segments[0]
        elif len(name_segments) == 2:
            tokens = name_segments[1] + name_segments[0]
        else:
            return ParseResult.failure(f"too many comma-separated name segments: {raw!r}")

        trailing = []
        while len(tokens) > 1 and tokens[-1] in _SUFFIXES:
            trailing.append(tokens.pop())
        suffixes.extend(reversed(trailing))

        return ParseResult.success(
            {
                "kind": self.NAME,
                "tokens": tokens,
                "surname": tokens[-1],
                "givens": [t for t in tokens[:-1] if len(t) > 1],
                "initials": [t for t in tokens[:-1] if len(t) == 1],
                "suffix": " ".join(suffixes) if suffixes else None,
                "folded": " ".join(tokens),
            }
        )

    def compare(self, a_canonical, b_canonical) -> Comparison:
        problem = _shape_problem(a_canonical) or _shape_problem(b_canonical)
        if problem:
            return Comparison("incomparable", problem)
        a, b = a_canonical, b_canonical

        if a["suffix"] and b["suffix"] and a["suffix"] != b["suffix"]:
            return Comparison(
                "review",
                f"conflicting generational suffixes: {a['suffix']!r} vs {b['suffix']!r}",
            )
        one_sided_suffix = (a["suffix"] is None) != (b["suffix"] is None)

        if a["folded"] == b["folded"]:
            if one_sided_suffix:
                return Comparison(
                    "review",
                    "generational suffix on one side only; possible parent/child pair",
                )
            return Comparison("equal")

        if a["surname"] != b["surname"]:
            set_a, set_b = set(a["tokens"]), set(b["tokens"])
            if set_a < set_b or set_b < set_a:
                return Comparison(
                    "review",
                    "one name's tokens strictly extend the other's; possible restyling of the same person",
                )
            return Comparison("different", f"surnames differ: {a['surname']!r} vs {b['surname']!r}")

        givens_a, givens_b = a["tokens"][:-1], b["tokens"][:-1]
        if not givens_a or not givens_b:
            return Comparison(
                "review",
                "given names absent on one side; a bare surname cannot confirm the same person",
            )
        for x, y in zip(givens_a, givens_b):
            if not _givens_compatible(x, y):
                return Comparison(
                    "review",
                    f"surname matches but given names conflict: {x!r} vs {y!r}; possible nickname",
                )
        first_a, first_b = givens_a[0], givens_b[0]
        if not (first_a == first_b and len(first_a) > 1):
            return Comparison(
                "review",
                f"first given names agree only at initial level: {first_a!r} vs {first_b!r}",
            )
        if one_sided_suffix:
            return Comparison(
                "review",
                "generational suffix on one side only; possible parent/child pair",
            )
        return Comparison("equal")


register(PersonNameComparator())
