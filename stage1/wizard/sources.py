"""Source registry for the wizard: what each Stage-1 source needs and how to
invoke it.

One `SourceSpec` per source — menu copy, prerequisites (EDGAR contact email,
POLYGON_API_KEY), the scope csv it takes, and honest duration expectations —
plus the pure constructors for every path and command line the wizard will
use. Nothing here touches the terminal or a subprocess; the only file read is
config.toml (via stage1.config), for the scope csv path. Each generated `argv`
matches the invocations documented in README.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Mapping

from stage1.config import setting

SNAPSHOTS_BASE = PurePosixPath("stage1/snapshots")
RELEASES_BASE = PurePosixPath("stage1/releases")
# The S&P 500 scope csv is not shipped; config.toml points at your own copy.
SP500_UNIVERSE = setting("STAGE1_SP500_UNIVERSE")


@dataclass(frozen=True)
class SourceSpec:
    key: str                        # name shared by stage1.harvest and stage1.run
    title: str                      # short human name for the menu
    detail: str                     # one-line description for the menu
    duration_hint: str              # honest expectation ("~1 min", "hours")
    needs_contact: bool = False     # SEC: EDGAR requires a contact User-Agent
    env_key: str | None = None      # required environment variable, if any
    universe: str | None = None     # repo-relative scope csv for --universe
    needs_universe: bool = False    # sec/finance: no run without the scope csv
    sample_hint: str | None = None  # what --sample items mean for this source


# Execution order: fastest first, so a mixed run shows results early.
REGISTRY: tuple[SourceSpec, ...] = (
    SourceSpec(
        key="chemical",
        title="Chemical",
        detail="IARC / Prop 65 / EPA TSCA carcinogenicity reclassifications",
        duration_hint="~1 min (one live cross-check)",
    ),
    SourceSpec(
        key="fda",
        title="FDA drug labels",
        detail="post-cutoff label-section changes (openFDA RMC + DailyMed + Wayback)",
        duration_hint="long — openFDA and Wayback are politely paced",
    ),
    SourceSpec(
        key="sec",
        title="SEC officers",
        detail="S&P 500 CEO/CFO changes from EDGAR filings (SOX-cert evidence)",
        duration_hint="medium — EDGAR at <=8 req/s",
        needs_contact=True,
        universe=SP500_UNIVERSE,
        needs_universe=True,
        sample_hint="CIKs",
    ),
    SourceSpec(
        key="sports",
        title="Sports transfers",
        detail="Wikipedia infobox club changes, Wikidata-corroborated",
        duration_hint="long — SPARQL discovery + pinned revisions",
        sample_hint="enwiki titles",
    ),
    SourceSpec(
        key="sports_controls",
        title="Sports controls",
        detail="v2 discovery: continuously-tenured players at the treatment clubs "
               "(anchored <=2024-03-31, tier/tenure/prominence-matched; cutoff must "
               "equal the treatment release's cutoff)",
        duration_hint="medium-long — per-club WDQS discovery + two pinned revisions "
                      "per selected candidate",
        sample_hint="club QIDs (a subset of the treatment club universe)",
    ),
    SourceSpec(
        key="finance_controls",
        title="Finance controls",
        detail="pinned Q1-2024 vs Q4-2023 quarterly revenue for the finance treatment "
               "entities (EDGAR companyfacts; window must be 2024-01-01..2024-03-31 — "
               "the 2026-08-05 ruling's pin)",
        duration_hint="short — ~66 EDGAR calls at <=2 req/s",
        needs_contact=True,
        sample_hint="tickers (a subset of the control pool)",
    ),
    SourceSpec(
        key="people_controls",
        title="People controls",
        detail="sitelink-decile matched living persons (ruling A7 pool; matched to the "
               "eval draw's 50 treatment people; cutoff must equal the treatment "
               "release's cutoff, default stage1/releases/dev-people)",
        duration_hint="medium — Category:Living people scan + ~80 pinned revisions",
        sample_hint="enwiki titles (scan skipped; screens still apply)",
    ),
    SourceSpec(
        key="finance",
        title="Finance",
        detail="S&P 500 prices / caps / revenue / tickers / IPOs (Polygon.io)",
        duration_hint="very long — >=12 s between Polygon calls",
        env_key="POLYGON_API_KEY",
        universe=SP500_UNIVERSE,
        needs_universe=True,
        sample_hint="tickers",
    ),
    SourceSpec(
        key="wiki_people",
        title="People deaths",
        detail="post-cutoff Wikipedia/Wikidata death records",
        duration_hint="very long — the largest source",
        sample_hint="enwiki titles",
    ),
)

_BY_KEY = {s.key: s for s in REGISTRY}


def spec(key: str) -> SourceSpec:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise KeyError(
            f"unknown source {key!r}; valid sources: {', '.join(all_keys())}"
        ) from None


def all_keys() -> list[str]:
    return [s.key for s in REGISTRY]


def window_name(key: str, cutoff: str, asof: str) -> str:
    return f"{key}_{cutoff}_{asof}"


def snapshot_rel(key: str, cutoff: str, asof: str) -> PurePosixPath:
    return SNAPSHOTS_BASE / window_name(key, cutoff, asof)


def harvest_log_rel(key: str, cutoff: str, asof: str) -> PurePosixPath:
    # Sidecar next to the snapshot dir, matching the existing
    # stage1/snapshots/people_*.log precedent.
    return SNAPSHOTS_BASE / (window_name(key, cutoff, asof) + ".log")


def release_rel(key: str, cutoff: str, asof: str) -> PurePosixPath:
    return RELEASES_BASE / window_name(key, cutoff, asof)


def harvest_argv(
    sp: SourceSpec,
    cutoff: str,
    asof: str,
    *,
    contact: str | None = None,
    sample: str | None = None,
    resume: bool = False,
) -> list[str]:
    """The `python3 -m stage1.harvest ...` argument vector (sans interpreter)."""
    argv = [
        "-m", "stage1.harvest",
        "--source", sp.key,
        "--cutoff", cutoff,
        "--asof", asof,
        "--out-dir", str(snapshot_rel(sp.key, cutoff, asof)),
    ]
    if sp.universe:
        argv += ["--universe", sp.universe]
    if sp.needs_contact:
        if not contact:
            raise ValueError(f"source {sp.key!r} requires a contact email")
        argv += ["--contact", contact]
    if sample:
        argv += ["--sample", sample]
    if resume:
        argv += ["--resume"]
    return argv


def run_argv(sp: SourceSpec, cutoff: str, asof: str) -> list[str]:
    """The `python3 -m stage1.run ...` argument vector (sans interpreter).

    `--data-dir` is the frozen snapshot itself; stage1.run's coverage check
    then verifies source and window against the snapshot manifest.
    """
    return [
        "-m", "stage1.run",
        "--source", sp.key,
        "--cutoff", cutoff,
        "--asof", asof,
        "--data-dir", str(snapshot_rel(sp.key, cutoff, asof)),
        "--out-dir", str(release_rel(sp.key, cutoff, asof)),
    ]


def missing_prereqs(
    sp: SourceSpec, *, contact: str | None, env: Mapping[str, str]
) -> list[str]:
    """Human-readable list of unmet requirements for running this source."""
    missing = []
    if not contact:
        missing.append(
            "a contact email for request User-Agents (set STAGE1_CONTACT_EMAIL in "
            "config.toml or pass --contact)"
        )
    if sp.needs_universe and not sp.universe:
        missing.append("an S&P 500 universe csv (set STAGE1_SP500_UNIVERSE in config.toml)")
    if sp.env_key and not env.get(sp.env_key):
        missing.append(f"{sp.env_key} (set it in config.toml or the environment)")
    return missing
