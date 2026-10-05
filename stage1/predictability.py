"""Canonical PREDICTABILITY taxonomy (owner decision B, 2026-07-21).

A predictability tag is stamped into EVERY Stage-1 record's
``provenance['predictability']`` as METADATA for Stage-2 stratification. It is
never a gate: it never includes or excludes a record, and never changes a
disposition, a value, or a change_date. It records how forecastable a fact
class was at the model's training cutoff, so Stage-2 can stratify faithfulness
by whether the model could plausibly have anticipated the change.

Exactly three values are permitted — any other string is a bug:

* ``"unpredictable"`` — genuinely unforecastable at cutoff: a near-random-walk
  or an unannounced event. Applies to: people **death-event** facts (date of
  death and its circumstances — place / cause / manner / resting place, all
  knowable only once the death occurs); finance ``share_price`` and
  ``market_cap``; chemical **IARC carcinogen reclassifications**
  (``carcinogen_group`` — a Working Group's Monographs verdict, e.g. IARC
  Volume 142's phthalate 2B calls: the evaluation agenda is announced ahead
  but the resulting group is unknowable until the Working Group meets).
* ``"guided"``        — partly forecastable from prior guidance / analyst
  consensus. Applies to: people **company financials / operating statistics**
  (revenue, net income, employee headcount, ...); finance
  ``quarterly_revenue`` (earnings guidance + analyst estimates make the print
  partly anticipable).
* ``"announced"``     — typically disclosed or rumoured ahead of the effective
  change. Applies to: people **announced-ahead biographical/corporate**
  changes (term successions, foundings, elections, HQ relocations, offices,
  ...); finance ``ipo_listing`` and ``ticker_change``; SEC officer changes
  (successions are usually disclosed ahead); sports transfers
  (rumoured/announced before completion); chemical **regulatory listings /
  risk determinations** (``prop65_carcinogen_listing`` — the substance's
  carcinogenicity was already public, only the formal Proposition 65 listing
  and its date are post-cutoff; ``tsca_risk_determination`` — the EPA draft
  risk evaluation of the same direction publishes months ahead of the final).

The three strings live ONLY here and are imported by the adapters, so the
taxonomy is defined once and cannot drift. Each adapter owns the
family/property -> value mapping for its own source (finance and wiki_people
are multi-valued — a people record's tag depends on its FIELD CLASS, so an
out-of-scope term succession reads ``announced`` and a revenue swing reads
``guided`` even though only the ``unpredictable`` death class is in scope; sec
and sports are single-valued). This module only fixes the canonical vocabulary
they all share.

The chemical adapter is multi-valued like finance / wiki_people — its tag
depends on the fact CLASS:

    ================================  =================
    chemical property                 predictability
    ================================  =================
    carcinogen_group (IARC)           unpredictable
    prop65_carcinogen_listing         announced
    tsca_risk_determination           announced
    ================================  =================
"""

from __future__ import annotations

UNPREDICTABLE = "unpredictable"
GUIDED = "guided"
ANNOUNCED = "announced"

# The complete, closed set of legal predictability tags. Nothing outside this
# set may ever be stamped into provenance['predictability'].
PREDICTABILITY_VALUES = frozenset({UNPREDICTABLE, GUIDED, ANNOUNCED})


def is_valid_predictability(value) -> bool:
    """True iff ``value`` is one of the three canonical predictability strings."""
    return value in PREDICTABILITY_VALUES


def check_predictability(value: str) -> str:
    """Return ``value`` unchanged when it is canonical, else raise ValueError.

    Adapters call this on the tag they are about to stamp so a typo in a
    family/property mapping fails LOUDLY at build time rather than silently
    writing an off-taxonomy value into the benchmark. Pure and total."""
    if value not in PREDICTABILITY_VALUES:
        raise ValueError(
            f"predictability {value!r} is not one of the canonical values "
            f"{sorted(PREDICTABILITY_VALUES)}"
        )
    return value
