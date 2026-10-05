"""Harvester protocol and importlib-based source discovery.

The HARVEST layer is the live, networked half of the pipeline. It is the
structural mirror of ``stage1/adapters``: a harvester owns one source ('sec',
'fda', ...), fetches candidate fact changes from that source's authoritative
feed for a requested [cutoff, asof] window over a declared scope, and emits a
FROZEN SNAPSHOT (raw input files + a snapshot_manifest.json) that the matching
DERIVE adapter reads offline. Harvest is the ONLY networked step; derive stays
deterministic and offline.

Implementing a harvester (in stage1/harvest/<source>.py):

    from stage1.harvest import Harvester

    class SecHarvester(Harvester):
        source = "sec"
        tool_version = "harvest_sec:v1"
        def coverage(self, cfg): ...
        def harvest(self, cfg, writer, ctx): ...

    HARVESTER = SecHarvester()

Discovery convention (this is the contract get_harvester enforces): the module
``stage1.harvest.<source>`` must exist and expose a module-level ``HARVESTER``
attribute that is a Harvester instance whose ``source`` equals the module's
source name. Harvesters must be importable offline and side-effect-free at
import time (no I/O — and certainly no network — until harvest() is called).

The three-method shape mirrors the Adapter: the framework runner
(stage1/harvest/run.py) owns writing and the manifest just as stage1/run.py
owns them for adapters, so a harvester only decides WHAT to fetch, never HOW
the snapshot is written.

cfg is a plain dict built by the harvest runner:
    {"source": str, "cutoff": datetime.date, "asof": datetime.date,
     "universe": pathlib.Path | None, "out_dir": pathlib.Path,
     "contact": str, "max_rps": float, "roles": list[str],
     "resume": bool, "sample": list[str] | None}
Harvesters may read additional keys they document, but must tolerate absence.
"""

from __future__ import annotations

import importlib
import re

_SOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class Harvester:
    """Base class for source harvesters. Subclasses set ``source`` (and
    typically ``tool_version``) and implement the two methods below."""

    source: str = ""
    tool_version: str = ""

    def coverage(self, cfg: dict) -> dict:
        """PURE, no network, unit-testable offline.

        Return the coverage descriptor the manifest records and the derive
        side enforces:
            {"cutoff": iso, "asof": iso, "asof_exact": bool,
             "precision": "day"}
        plus an optional "scope" digest of the declared universe and an optional
        "window_basis" naming WHICH date [cutoff, asof] bounds (e.g. SEC's
        "event_date" — the report/period date discovery filters on, as opposed to
        the effective/change date the derive gate uses) so the coverage promise
        and the harvest filter describe the same thing. ``asof_exact`` is True
        when the 'after' evidence is pinned to asof (so a sound re-derive must use
        the snapshot's exact asof), False when the snapshot can serve any narrower
        asof.
        """
        raise NotImplementedError(f"{type(self).__name__}.coverage is abstract")

    def harvest(self, cfg: dict, writer, ctx: dict) -> None:
        """The ONLY networked method.

        Fetch every candidate change within [cutoff, asof] over the declared
        scope and emit each snapshot file through ``writer`` (writer.add_jsonl,
        writer.add_scope_file, writer.set_params, writer.set_stats). Polite,
        resumable, backing off on rate limits via stage1/harvest/http.py.
        Nothing may be silently dropped: an HTTP miss is recorded in the
        writer's fetch_stats, never swallowed by a bare except. The framework
        runner calls writer.finalize() afterwards to write the manifest.
        """
        raise NotImplementedError(f"{type(self).__name__}.harvest is abstract")


def get_harvester(source: str) -> "Harvester":
    """Import stage1.harvest.<source> and return its HARVESTER instance.

    Raises LookupError with a precise message when the source name is
    malformed, the module does not exist, HARVESTER is missing, HARVESTER is
    not a Harvester instance, or HARVESTER.source does not equal ``source``.
    Import errors from within a broken harvester module propagate as
    LookupError with the underlying error chained (raised from it).
    """
    if not isinstance(source, str) or not _SOURCE_NAME_RE.match(source):
        raise LookupError(
            f"invalid source name {source!r}: must match {_SOURCE_NAME_RE.pattern}"
        )
    module_name = f"stage1.harvest.{source}"
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise LookupError(
            f"no harvester for source {source!r}: could not import {module_name} ({exc})"
        ) from exc
    harvester = getattr(module, "HARVESTER", None)
    if harvester is None:
        raise LookupError(
            f"harvester module {module_name} does not define a module-level HARVESTER instance"
        )
    if not isinstance(harvester, Harvester):
        raise LookupError(
            f"{module_name}.HARVESTER must be a stage1.harvest.Harvester instance, "
            f"got {type(harvester).__name__}"
        )
    if harvester.source != source:
        raise LookupError(
            f"{module_name}.HARVESTER.source is {harvester.source!r}, expected {source!r}"
        )
    return harvester
