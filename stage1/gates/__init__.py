"""Gate interface and the shared gate-application runner.

A gate is one named, versioned inclusion check (universe membership, date
window, before/after actually differ, ...). Gates read a FactChangeRecord and
a context dict and return a GateResult; they NEVER mutate the record and
NEVER raise on bad data — anything unparseable or ambiguous becomes verdict
'review' with evidence explaining what could not be decided.

Implementing a gate (in stage1/gates/<name>.py):

    from stage1.gates import Gate
    from stage1.schema import GateResult

    class CutoffWindowGate(Gate):
        name = "cutoff_window"
        version = "cutoff_window:v1"
        def evaluate(self, record, ctx):
            ...
            return GateResult(name=self.name, version=self.version,
                              verdict="pass", evidence={...})

ctx is a plain dict filled by the adapter/runner: expect at least the cfg
keys ('cutoff', 'asof', 'data_dir', 'offline') plus whatever caches or
universe tables the adapter's gate_list() documented. Gates must treat a
missing ctx key as data-to-review, not as a crash.

Evidence dicts must be JSON-safe and deterministic (no timestamps, no memory
addresses) — they are written verbatim into facts.jsonl.
"""

from __future__ import annotations

from stage1.schema import VALID_VERDICTS, GateResult, derive_disposition

__all__ = ["Gate", "GateResult", "apply_gates"]


class Gate:
    """Base class for gates. Subclasses set name and version (both non-empty;
    version like 'cutoff_window:v1') and implement evaluate()."""

    name: str = ""
    version: str = ""

    def evaluate(self, record, ctx: dict) -> GateResult:
        """Return a GateResult for this record.

        Contract: total and pure with respect to (record, ctx) — same inputs,
        same result. Never raise; return verdict 'review' with explanatory
        evidence for anything the gate cannot decide.
        """
        raise NotImplementedError(f"{type(self).__name__}.evaluate is abstract")


def apply_gates(record, gates, ctx: dict):
    """Run every gate in order, append all results, derive the disposition.

    ALL gates run even after a 'fail' — the full gate ledger is part of the
    record (nothing is short-circuited or dropped). After the loop,
    record.disposition = derive_disposition(record.gates), which also folds
    in any gate results already present on the record (e.g. the runner's
    'normalize' result).

    Defensive containment (gate-contract violations become review verdicts,
    never crashes and never silent drops):
      * a gate that raises yields verdict 'review' with evidence
        {"error": "gate_raised", "exception": repr(exc)};
      * a gate that returns a non-GateResult, or a GateResult with a verdict
        outside VALID_VERDICTS, yields verdict 'review' with evidence naming
        the violation.

    Returns the (mutated) record.
    """
    for gate in gates:
        try:
            result = gate.evaluate(record, ctx)
        except Exception as exc:
            result = GateResult(
                name=gate.name or type(gate).__name__,
                version=gate.version or "unknown",
                verdict="review",
                evidence={"error": "gate_raised", "exception": repr(exc)},
            )
        if not isinstance(result, GateResult):
            result = GateResult(
                name=gate.name or type(gate).__name__,
                version=gate.version or "unknown",
                verdict="review",
                evidence={
                    "error": "gate_returned_non_gateresult",
                    "returned_type": type(result).__name__,
                },
            )
        elif result.verdict not in VALID_VERDICTS:
            result = GateResult(
                name=result.name,
                version=result.version,
                verdict="review",
                evidence={
                    "error": "gate_returned_invalid_verdict",
                    "invalid_verdict": repr(result.verdict),
                    "original_evidence": result.evidence,
                },
            )
        record.gates.append(result)
    record.disposition = derive_disposition(record.gates)
    return record
