"""Adapter protocol and importlib-based source discovery.

An adapter owns one source ('sec', 'fda', 'wiki_people', 'wiki_sports',
'finance', ...). It enumerates candidate fact changes from that source's data
files, builds FactChangeRecord drafts, and declares which gates apply, in
which order. Everything source-specific lives in the adapter; the runner and
all shared machinery are source-agnostic.

Implementing an adapter (in stage1/adapters/<source>.py):

    from stage1.adapters import Adapter

    class SecAdapter(Adapter):
        source = "sec"
        def enumerate_candidates(self, cfg): ...
        def build_record(self, candidate, cfg): ...
        def gate_list(self, cfg): ...

    ADAPTER = SecAdapter()

Discovery convention (this is the contract get_adapter enforces): the module
``stage1.adapters.<source>`` must exist and expose a module-level ``ADAPTER``
attribute that is an Adapter instance whose ``source`` equals the module's
source name. Adapters must be importable offline and side-effect-free at
import time (no I/O until enumerate_candidates is called).

cfg is a plain dict built by the runner:
    {"source": str, "cutoff": datetime.date, "asof": datetime.date,
     "data_dir": pathlib.Path | None, "offline": bool, "out_dir": pathlib.Path}
Adapters may read additional keys they document, but must tolerate their
absence. With offline=True (the default) adapters must touch nothing outside
data_dir.

Input-load error channel: an adapter that finds unreadable content inside an
input file (a corrupt cache line, a malformed row) must not silently skip
it — it appends {"file": str, "line": int, "error": str} dicts to
cfg["input_load_errors"] (creating the list on first use, typically from
gate_list). The runner copies these into manifest["input_load_errors"], so a
damaged input is always distinguishable from an absent one.
"""

from __future__ import annotations

import importlib
import re

_SOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class Adapter:
    """Base class for source adapters. Subclasses set ``source`` and
    implement the three methods below."""

    source: str = ""

    def enumerate_candidates(self, cfg: dict):
        """Yield source-specific candidate dicts, in a deterministic order.

        A candidate carries whatever the source needs to identify one
        potential fact change (row of a legacy CSV, filing accession, ...).
        Determinism matters: the same data_dir must yield the same candidates
        in the same order. Enumeration must not decide inclusion — emit every
        potential change and let gates judge; anything skipped here is
        invisible to the ledger and therefore forbidden unless it is
        structurally not a candidate at all (e.g. a header row).
        """
        raise NotImplementedError(f"{type(self).__name__}.enumerate_candidates is abstract")

    def build_record(self, candidate: dict, cfg: dict):
        """Return a FactChangeRecord draft for one candidate.

        The draft must have: fact_id (via schema.compute_fact_id), source,
        entity, property, value_type, before/after with raw + evidence
        (canonical left None — the shared normalization step fills it via the
        comparator registry), change_date, and provenance. gates should be
        left empty and disposition at its default; the runner owns both.

        Must not raise on bad data: encode the problem in the draft (e.g.
        empty raw, evidence describing what was missing) so a gate can send
        it to review. Raising is reserved for programming errors; the runner
        still catches exceptions defensively and logs them in the manifest's
        build_errors so no candidate ever disappears silently.
        """
        raise NotImplementedError(f"{type(self).__name__}.build_record is abstract")

    def gate_list(self, cfg: dict):
        """Return the ordered list of Gate instances for this source.

        Order is semantic: derive_disposition names the FIRST failing gate,
        so put the most fundamental checks (universe membership, date window)
        before refinements. The runner prepends its own 'normalize' gate
        result; adapters must not add one.
        """
        raise NotImplementedError(f"{type(self).__name__}.gate_list is abstract")

    def snapshot_inputs(self, cfg: dict):
        """The fixed snapshot filenames this adapter reads by name, or None.

        Optional. When the runner derives from a harvested snapshot it passes
        this list to the coverage check, which then asserts every one is listed
        in the snapshot manifest's ``files`` block (and thus sha1-verified) — so
        a trimmed/partial snapshot missing an input the adapter reads is refused
        loudly instead of deriving on unverified bytes. Returning None keeps the
        legacy behaviour (only whatever the manifest happens to list is verified).
        It affects ONLY the snapshot coverage check; it never changes derive
        output. Default: None.
        """
        return None


def get_adapter(source: str) -> "Adapter":
    """Import stage1.adapters.<source> and return its ADAPTER instance.

    Raises LookupError with a precise message when the source name is
    malformed, the module does not exist, ADAPTER is missing, ADAPTER is not
    an Adapter instance, or ADAPTER.source does not equal ``source``.
    Import errors from within a broken adapter module propagate as
    LookupError with the underlying error chained (raised from it).
    """
    if not isinstance(source, str) or not _SOURCE_NAME_RE.match(source):
        raise LookupError(
            f"invalid source name {source!r}: must match {_SOURCE_NAME_RE.pattern}"
        )
    module_name = f"stage1.adapters.{source}"
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise LookupError(
            f"no adapter for source {source!r}: could not import {module_name} ({exc})"
        ) from exc
    adapter = getattr(module, "ADAPTER", None)
    if adapter is None:
        raise LookupError(
            f"adapter module {module_name} does not define a module-level ADAPTER instance"
        )
    if not isinstance(adapter, Adapter):
        raise LookupError(
            f"{module_name}.ADAPTER must be a stage1.adapters.Adapter instance, "
            f"got {type(adapter).__name__}"
        )
    if adapter.source != source:
        raise LookupError(
            f"{module_name}.ADAPTER.source is {adapter.source!r}, expected {source!r}"
        )
    return adapter
