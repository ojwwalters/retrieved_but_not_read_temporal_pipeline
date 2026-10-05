"""Core data contract for Stage-1 fact-change records.

This module is the single source of truth for the record shape shared by all
adapters, comparators, gates, and the runner. Everything here is plain-data:
dataclasses with JSON round-trip helpers, pure functions for identity
(compute_fact_id) and disposition (derive_disposition), and validation that
reports precise, path-qualified error messages instead of raising mid-parse.

Validation philosophy: ``validate()`` on every dataclass returns a list of
human-readable error strings (empty list == valid). It never raises; callers
that want raise-on-invalid semantics use ``ensure_valid()``. This mirrors the
pipeline-wide rule that data errors become logged verdicts, not crashes — the
runner records validation errors in the manifest rather than aborting.

JSON round-trip: ``to_dict()`` emits every field (including None-valued ones)
so the serialized form is stable and self-describing; ``from_dict()`` requires
every field to be present and raises ValueError naming the exact missing or
mistyped key with its path. ``from_dict(x.to_dict()) == x`` holds for all
valid (and most invalid) instances.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date as _date

from stage1 import PIPELINE_VERSION

VALID_VERDICTS = ("pass", "fail", "review")
VALID_PRECISIONS = ("day", "month", "year")

# value_type values with shipped comparators. The set is registry-extensible:
# validation only requires a non-empty string, membership is NOT enforced here.
KNOWN_VALUE_TYPES = ("person_name", "quantity", "date", "org", "text_span")

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _sha1_of_fields(fields: list) -> str:
    """sha1 over an unambiguous JSON-list encoding of identity fields.

    json.dumps with ensure_ascii and compact separators gives every field
    list exactly one byte encoding, so no delimiter character occurring
    INSIDE a field ('|', ',', ...) can ever make two distinct field tuples
    collide — a plain '|'.join would.
    """
    payload = json.dumps(fields, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def compute_fact_id(source: str, entity_name: str, property: str, change_date: str) -> str:
    """Return the stable 12-hex-char SEMANTIC identity of a fact change.

    Defined as sha1 over the JSON-list encoding of
    [source, entity_name, property, change_date], truncated to 12 hex
    characters. ``change_date`` is the 'YYYY-MM-DD' string
    (ChangeDate.value), not a date object. This is the ONLY way fact_ids are
    ever produced; adapters must not invent their own.

    fact_id names WHAT changed, not which physical record observed it: two
    source documents reporting the same (entity, property, date) change
    legitimately share a fact_id. Per-record uniqueness is record_id
    (compute_record_id); the full ledger may contain several records per
    fact_id while the included set is deduplicated by the dedup gate.
    """
    return _sha1_of_fields([source, entity_name, property, change_date])


def compute_record_id(fact_id: str, discriminator: str) -> str:
    """Return the 12-hex-char PHYSICAL identity of one record.

    Defined as sha1 over the JSON-list encoding of [fact_id, discriminator].
    ``discriminator`` is a source-declared provenance key that distinguishes
    physical observations of the same semantic fact (for SEC: the filing
    accession number ``adsh``). record_id is unique per record in a release
    and is the key for record-addressed manifest entries; the runner reports
    any collision explicitly in the manifest.
    """
    return _sha1_of_fields([fact_id, discriminator])


def derive_disposition(gate_results: list["GateResult"]) -> str:
    """Derive a record's disposition from its ordered gate results.

    Precedence (shared by the whole pipeline — never reimplement):

    1. The FIRST gate (in list order) with verdict 'fail' wins:
       -> 'excluded:<that gate's name>'.
    2. Otherwise, if ANY gate has verdict 'review' -> 'review'.
    3. Otherwise -> 'included'. An empty gate list is 'included' by this rule;
       adapters that want a mandatory gate must include it in gate_list().

    Any verdict outside VALID_VERDICTS is treated conservatively as 'review'
    (an invalid gate result must never cause an include or exclude).
    """
    for g in gate_results:
        if g.verdict == "fail":
            return f"excluded:{g.name}"
    for g in gate_results:
        if g.verdict != "pass":
            return "review"
    return "included"


def _check_str(value, path: str, errors: list, allow_none: bool = False, allow_empty: bool = False) -> None:
    if value is None:
        if not allow_none:
            errors.append(f"{path}: must be a string, got None")
        return
    if not isinstance(value, str):
        errors.append(f"{path}: must be a string, got {type(value).__name__} ({value!r})")
        return
    if not allow_empty and value == "":
        errors.append(f"{path}: must be a non-empty string")


def _check_dict(value, path: str, errors: list, allow_none: bool = False) -> None:
    if value is None:
        if not allow_none:
            errors.append(f"{path}: must be a dict, got None")
        return
    if not isinstance(value, dict):
        errors.append(f"{path}: must be a dict, got {type(value).__name__} ({value!r})")


def _get(d: dict, key: str, path: str):
    if key not in d:
        raise ValueError(f"{path}: missing required key {key!r}")
    return d[key]


def _require_dict_input(d, path: str) -> dict:
    if not isinstance(d, dict):
        raise ValueError(f"{path}: expected a dict, got {type(d).__name__} ({d!r})")
    return d


@dataclass
class Evidence:
    """Provenance for a single value or date.

    kind: short machine tag for the evidence channel, e.g. 'sox_cert',
        '8k_section', 'cached_verify'. Free-form but must be non-empty.
    url: canonical document URL when one exists, else None.
    ref: source-specific locator details (accession number, section id,
        snapshot key, ...). Always a dict, may be empty.
    as_of: ISO date/timestamp string for when the evidence was observed,
        or None when not applicable. Format is not enforced beyond str.
    """

    kind: str
    url: str | None = None
    ref: dict = field(default_factory=dict)
    as_of: str | None = None

    def validate(self, path: str = "evidence") -> list:
        errors: list = []
        _check_str(self.kind, f"{path}.kind", errors)
        _check_str(self.url, f"{path}.url", errors, allow_none=True, allow_empty=True)
        _check_dict(self.ref, f"{path}.ref", errors)
        _check_str(self.as_of, f"{path}.as_of", errors, allow_none=True, allow_empty=True)
        return errors

    def ensure_valid(self, path: str = "evidence") -> None:
        errors = self.validate(path)
        if errors:
            raise ValueError("; ".join(errors))

    def to_dict(self) -> dict:
        return {"kind": self.kind, "url": self.url, "ref": self.ref, "as_of": self.as_of}

    @classmethod
    def from_dict(cls, d: dict, path: str = "evidence") -> "Evidence":
        d = _require_dict_input(d, path)
        return cls(
            kind=_get(d, "kind", path),
            url=_get(d, "url", path),
            ref=_get(d, "ref", path),
            as_of=_get(d, "as_of", path),
        )


@dataclass
class ValueState:
    """The before- or after-state of the changing fact.

    raw: the value exactly as extracted from the source (never normalized).
    canonical: comparator-produced canonical form (dict), or None when the
        raw value has not been parsed yet or failed to parse. The canonical
        dict shape is owned by the comparator for the record's value_type.
    evidence: where this value was observed.
    """

    raw: str
    canonical: dict | None
    evidence: Evidence

    def validate(self, path: str = "value_state") -> list:
        errors: list = []
        _check_str(self.raw, f"{path}.raw", errors, allow_empty=True)
        _check_dict(self.canonical, f"{path}.canonical", errors, allow_none=True)
        if isinstance(self.evidence, Evidence):
            errors.extend(self.evidence.validate(f"{path}.evidence"))
        else:
            errors.append(f"{path}.evidence: must be an Evidence, got {type(self.evidence).__name__}")
        return errors

    def to_dict(self) -> dict:
        return {"raw": self.raw, "canonical": self.canonical, "evidence": self.evidence.to_dict()}

    @classmethod
    def from_dict(cls, d: dict, path: str = "value_state") -> "ValueState":
        d = _require_dict_input(d, path)
        return cls(
            raw=_get(d, "raw", path),
            canonical=_get(d, "canonical", path),
            evidence=Evidence.from_dict(_get(d, "evidence", path), f"{path}.evidence"),
        )


@dataclass
class ChangeDate:
    """When the fact changed.

    value: 'YYYY-MM-DD' string. Must be a real calendar date. When precision
        is 'month' or 'year' the unknown components are pinned to '01' by
        convention (e.g. 2024-06-01 with precision 'month'); the precision
        field is what consumers must honor, the pinning is only so value
        stays a parseable date and fact_id stays stable.
    precision: 'day' | 'month' | 'year'.
    evidence: where the date was observed.
    """

    value: str
    precision: str
    evidence: Evidence

    def validate(self, path: str = "change_date") -> list:
        errors: list = []
        if not isinstance(self.value, str):
            errors.append(f"{path}.value: must be a 'YYYY-MM-DD' string, got {type(self.value).__name__}")
        elif not _ISO_DATE_RE.match(self.value):
            errors.append(f"{path}.value: must match 'YYYY-MM-DD', got {self.value!r}")
        else:
            try:
                _date.fromisoformat(self.value)
            except ValueError:
                errors.append(f"{path}.value: not a real calendar date: {self.value!r}")
        if self.precision not in VALID_PRECISIONS:
            errors.append(
                f"{path}.precision: must be one of {list(VALID_PRECISIONS)}, got {self.precision!r}"
            )
        if isinstance(self.evidence, Evidence):
            errors.extend(self.evidence.validate(f"{path}.evidence"))
        else:
            errors.append(f"{path}.evidence: must be an Evidence, got {type(self.evidence).__name__}")
        return errors

    def to_dict(self) -> dict:
        return {"value": self.value, "precision": self.precision, "evidence": self.evidence.to_dict()}

    @classmethod
    def from_dict(cls, d: dict, path: str = "change_date") -> "ChangeDate":
        d = _require_dict_input(d, path)
        return cls(
            value=_get(d, "value", path),
            precision=_get(d, "precision", path),
            evidence=Evidence.from_dict(_get(d, "evidence", path), f"{path}.evidence"),
        )


@dataclass
class GateResult:
    """Outcome of one gate evaluation on one record.

    name: the gate's stable name (also used in 'excluded:<name>' dispositions).
    version: the gate's version tag, e.g. 'ceo_universe:v1'. For the runner's
        normalization gate this is the normalize-step version; the comparator
        version lives inside evidence.
    verdict: 'pass' | 'fail' | 'review'.
    evidence: free-form dict explaining the verdict (must be JSON-safe).
    """

    name: str
    version: str
    verdict: str
    evidence: dict = field(default_factory=dict)

    def validate(self, path: str = "gate_result") -> list:
        errors: list = []
        _check_str(self.name, f"{path}.name", errors)
        _check_str(self.version, f"{path}.version", errors)
        if self.verdict not in VALID_VERDICTS:
            errors.append(
                f"{path}.verdict: must be one of {list(VALID_VERDICTS)}, got {self.verdict!r}"
            )
        _check_dict(self.evidence, f"{path}.evidence", errors)
        return errors

    def to_dict(self) -> dict:
        return {"name": self.name, "version": self.version, "verdict": self.verdict, "evidence": self.evidence}

    @classmethod
    def from_dict(cls, d: dict, path: str = "gate_result") -> "GateResult":
        d = _require_dict_input(d, path)
        return cls(
            name=_get(d, "name", path),
            version=_get(d, "version", path),
            verdict=_get(d, "verdict", path),
            evidence=_get(d, "evidence", path),
        )


@dataclass
class FactChangeRecord:
    """One fact change, with full provenance and an explicit disposition.

    fact_id: semantic identity (compute_fact_id) — shared by every record
        observing the same (source, entity, property, change_date) change.
    record_id: physical identity (compute_record_id) — unique per record;
        adapters derive it from fact_id plus a provenance discriminator
        (e.g. the SEC accession number).
    entity: exactly {'name': str (non-empty), 'ids': dict[str, str]} — e.g.
        {'name': 'NRG Energy, Inc.', 'ids': {'cik': '1013871', 'ticker': 'NRG'}}.
        No other keys are permitted (validated).
    property: what changed, e.g. 'ceo', 'cfo'. Non-empty string.
    value_type: comparator registry key ('person_name', 'quantity', 'date',
        'org', 'text_span', or a registered extension). Non-empty string;
        registry membership is checked at normalization time, not here.
    gates: ordered list of every GateResult produced for this record. Nothing
        is ever removed from this list.
    disposition: 'included' | 'excluded:<gate_name>' | 'review'. Must equal
        derive_disposition(gates) — validate() cross-checks this, and also
        that fact_id equals compute_fact_id over the identity fields.
    provenance: source-specific dict (accession numbers, legacy change_id,
        doc URLs, snapshot info). JSON-safe.
    """

    fact_id: str
    record_id: str
    source: str
    entity: dict
    property: str
    value_type: str
    before: ValueState
    after: ValueState
    change_date: ChangeDate
    gates: list = field(default_factory=list)
    disposition: str = "review"
    provenance: dict = field(default_factory=dict)
    pipeline_version: str = PIPELINE_VERSION

    def validate(self) -> list:
        """Return a list of precise error strings; empty list means valid.

        Intended for finished records (after gating). Draft records straight
        out of build_record() may legitimately fail the disposition
        cross-check until apply_gates() has run.
        """
        errors: list = []
        path = "record"
        _check_str(self.fact_id, f"{path}.fact_id", errors)
        _check_str(self.record_id, f"{path}.record_id", errors)
        _check_str(self.source, f"{path}.source", errors)
        _check_str(self.property, f"{path}.property", errors)
        _check_str(self.value_type, f"{path}.value_type", errors)
        _check_str(self.pipeline_version, f"{path}.pipeline_version", errors)
        _check_dict(self.provenance, f"{path}.provenance", errors)

        entity_name = None
        if not isinstance(self.entity, dict):
            errors.append(f"{path}.entity: must be a dict, got {type(self.entity).__name__}")
        else:
            extra = sorted(set(self.entity) - {"name", "ids"})
            if extra:
                errors.append(f"{path}.entity: unexpected keys {extra}; only 'name' and 'ids' are allowed")
            if "name" not in self.entity:
                errors.append(f"{path}.entity: missing required key 'name'")
            else:
                entity_name = self.entity["name"]
                _check_str(entity_name, f"{path}.entity.name", errors)
            if "ids" not in self.entity:
                errors.append(f"{path}.entity: missing required key 'ids'")
            elif not isinstance(self.entity["ids"], dict):
                errors.append(f"{path}.entity.ids: must be a dict, got {type(self.entity['ids']).__name__}")
            else:
                for k, v in self.entity["ids"].items():
                    if not isinstance(k, str) or not isinstance(v, str):
                        errors.append(f"{path}.entity.ids[{k!r}]: keys and values must be strings, got value {v!r}")

        for attr, cls_, sub in (
            ("before", ValueState, self.before),
            ("after", ValueState, self.after),
            ("change_date", ChangeDate, self.change_date),
        ):
            if isinstance(sub, cls_):
                errors.extend(sub.validate(f"{path}.{attr}"))
            else:
                errors.append(f"{path}.{attr}: must be a {cls_.__name__}, got {type(sub).__name__}")

        if not isinstance(self.gates, list):
            errors.append(f"{path}.gates: must be a list, got {type(self.gates).__name__}")
        else:
            for i, g in enumerate(self.gates):
                if isinstance(g, GateResult):
                    errors.extend(g.validate(f"{path}.gates[{i}]"))
                else:
                    errors.append(f"{path}.gates[{i}]: must be a GateResult, got {type(g).__name__}")

        if not isinstance(self.disposition, str):
            errors.append(f"{path}.disposition: must be a string, got {type(self.disposition).__name__}")
        elif not (
            self.disposition in ("included", "review")
            or (self.disposition.startswith("excluded:") and len(self.disposition) > len("excluded:"))
        ):
            errors.append(
                f"{path}.disposition: must be 'included', 'review', or 'excluded:<gate_name>', got {self.disposition!r}"
            )
        elif isinstance(self.gates, list) and all(isinstance(g, GateResult) for g in self.gates):
            derived = derive_disposition(self.gates)
            if self.disposition != derived:
                errors.append(
                    f"{path}.disposition: {self.disposition!r} does not match gates-derived {derived!r}"
                )

        if (
            isinstance(self.fact_id, str)
            and isinstance(self.source, str)
            and isinstance(entity_name, str)
            and isinstance(self.property, str)
            and isinstance(self.change_date, ChangeDate)
            and isinstance(self.change_date.value, str)
        ):
            expected = compute_fact_id(self.source, entity_name, self.property, self.change_date.value)
            if self.fact_id != expected:
                errors.append(
                    f"{path}.fact_id: {self.fact_id!r} does not match computed {expected!r} "
                    f"(sha1(json[source, entity_name, property, change_date])[:12])"
                )

        return errors

    def ensure_valid(self) -> None:
        """Raise ValueError joining all validation errors; no-op when valid."""
        errors = self.validate()
        if errors:
            raise ValueError("; ".join(errors))

    def to_dict(self) -> dict:
        return {
            "fact_id": self.fact_id,
            "record_id": self.record_id,
            "source": self.source,
            "entity": self.entity,
            "property": self.property,
            "value_type": self.value_type,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "change_date": self.change_date.to_dict(),
            "gates": [g.to_dict() for g in self.gates],
            "disposition": self.disposition,
            "provenance": self.provenance,
            "pipeline_version": self.pipeline_version,
        }

    @classmethod
    def from_dict(cls, d: dict, path: str = "record") -> "FactChangeRecord":
        d = _require_dict_input(d, path)
        gates_raw = _get(d, "gates", path)
        if not isinstance(gates_raw, list):
            raise ValueError(f"{path}.gates: must be a list, got {type(gates_raw).__name__}")
        return cls(
            fact_id=_get(d, "fact_id", path),
            record_id=_get(d, "record_id", path),
            source=_get(d, "source", path),
            entity=_get(d, "entity", path),
            property=_get(d, "property", path),
            value_type=_get(d, "value_type", path),
            before=ValueState.from_dict(_get(d, "before", path), f"{path}.before"),
            after=ValueState.from_dict(_get(d, "after", path), f"{path}.after"),
            change_date=ChangeDate.from_dict(_get(d, "change_date", path), f"{path}.change_date"),
            gates=[GateResult.from_dict(g, f"{path}.gates[{i}]") for i, g in enumerate(gates_raw)],
            disposition=_get(d, "disposition", path),
            provenance=_get(d, "provenance", path),
            pipeline_version=_get(d, "pipeline_version", path),
        )
