"""Chemical hazard/carcinogenicity reclassification adapter (source 'chemical').

The SIXTH source: a SMALL (~8 facts), high-quality supplement of genuinely
post-cutoff chemical hazard reclassifications, built the SAME way as the other
five (harvest -> derive -> adapter -> gate) so it flows into releases/dev-chemical,
the workbook, and the schema identically. Three authoritative sub-sources, each a
CHANGE record (a described reclassification with a before, an after, and a date):

  * IARC (``carcinogen_group``) — IARC Monographs Volume 142 (Working Group met
    9-16 June 2026; summary published in The Lancet Oncology 3 July 2026)
    reclassified three phthalate plasticizers to Group 2B (possibly carcinogenic):
    butyl benzyl phthalate, dibutyl phthalate, diisononyl phthalate. before = the
    chemical's PRIOR IARC group (Group 3). predictability = ``unpredictable`` (the
    evaluation agenda was announced in advance but the 2B OUTCOME was unknowable
    pre-cutoff).
  * Prop 65 (``prop65_carcinogen_listing``) — four substances ADDED to the
    California Proposition 65 CANCER list effective 2026-07-17 (Notice of Intent
    2026-05-08): hydrochlorothiazide, voriconazole, tacrolimus, welding fumes.
    SINGLE-SIDED addition (not-listed -> listed). predictability = ``announced``
    (the carcinogenicity was already public from the 2024 IARC Group-1 calls; only
    the formal listing/date is post-cutoff).
  * TSCA (``tsca_risk_determination``) — EPA's 1,2-dichloroethane final risk
    evaluation (91 FR 24230, doc 2026-08682, published 2026-05-05): EPA determined
    it presents an unreasonable risk to human health. predictability =
    ``announced`` (the draft risk evaluation of the same direction published
    2025-11-19, pre-cutoff).

entity = the chemical: {name: <common name>, ids: {cas: <CAS>}} (welding fumes has
no single CAS, so its ids are empty — a valid record). Recognisability is inherent
(hydrochlorothiazide is a top drug; the phthalates are famous plasticizers), so no
recognisability gate is needed. value_type = ``text_span`` (short hazard labels).

DETERMINISM / NO-LLM. Every recorded value comes from a SMALL COMMITTED, CITED
reference table (``chemical_reference.jsonl``) with a source URL + note per value —
the same pattern as a committed whitelist, NOT an LLM. OEHHA is behind an Incapsula
bot-wall and IARC's classification list is a JS feed, so those values cannot be
cleanly auto-extracted and are cross-referenced in the committed table; the TSCA
row's after value + citation are additionally LIVE-VERIFIED by the harvester
against the clean Federal Register JSON API (recorded in ``chemical_sources.jsonl``,
optional provenance enrichment). The derive is fully offline and byte-deterministic.

Gate order (first FAIL in ledger order names the disposition; EVERY gate always
runs; the runner PREPENDS the shared 'normalize' gate before these):

 1. temporal_window   (shared, day-precision): the change date must be inside
                       [cutoff, asof]. Every fact is strictly AFTER the 2026-02-01
                       cutoff (IARC 2026-07-03, Prop 65 2026-07-17, TSCA
                       2026-05-05) and within the 2026-07-31 asof, so all pass; a
                       date outside the window would FAIL (excluded:temporal_window).
 2. evidence_resolvable(shared): both sides carry a kind + (url or ref) pointing at
                       a real authoritative document.
 3. garbage_value     (shared): a light text_span plausibility screen (>= 1 token);
                       a violation is review, never a silent drop.
 4. dedup             (shared, near-last): key (entity.name, property, change_date)
                       — the SAME tuple fact_id is built from. The 8 facts are
                       distinct keys, so none collapse.
 5. predictability    (chemical-specific, REVIEW-ONLY): validates the record carries
                       a canonical predictability tag. It NEVER fails, so it can
                       never include or exclude a record on predictability — the tag
                       stays Stage-2 stratification METADATA, exactly as the taxonomy
                       requires. A missing / off-taxonomy tag reviews (a pipeline
                       defect a human resolves), never a silent include.

NO value_changed gate: the reference is the authoritative record of the change, and
Prop 65 / TSCA are single-sided additions with no meaningful before-value to
compare. NO universe/corroboration gate: recognisability is inherent and each
sub-source IS the single authoritative record.

record_id discriminator: "{sub_source}|{cas}|{property}|{change_date}". fact_id is
the SEMANTIC identity (source, entity_name, property, change_date).

Offline-only: the committed reference (and the harvest snapshot's copy) is read from
data_dir/the package default; ``--online`` raises. Each present input (the reference
cache, its sidecar, the sources enrichment) is fingerprinted into the manifest via
cfg['extra_input_files'] / cfg['extra_input_meta'] under a LOCATION-INDEPENDENT
identifier, so facts.jsonl is byte-identical across checkouts. A missing sources
enrichment degrades the ``provenance['harvest']`` annotation to None but NEVER
crashes and never drops a candidate.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import stage1.normalize.text_span  # noqa: F401  (registers the text_span comparator)
from stage1.adapters import Adapter
from stage1.gates import Gate
from stage1.gates.standard import (
    DedupGate,
    EvidenceResolvableGate,
    GarbageValueGate,
    TemporalWindowGate,
)
from stage1.predictability import (
    ANNOUNCED,
    UNPREDICTABLE,
    check_predictability,
    is_valid_predictability,
)
from stage1.schema import (
    ChangeDate,
    Evidence,
    FactChangeRecord,
    GateResult,
    ValueState,
    compute_fact_id,
    compute_record_id,
)

SOURCE = "chemical"

# --------------------------------------------------------------------------- #
# Input files. The committed cited reference lives under stage1/harvest/ (its
# canonical home, shared with the harvester); a harvest snapshot vendors a copy
# next to its other inputs, which the adapter reads in preference. Identifiers
# recorded in provenance/manifest are LOCATION-INDEPENDENT.
# --------------------------------------------------------------------------- #
REFERENCE_FILENAME = "chemical_reference.jsonl"
REFERENCE_META_FILENAME = "chemical_reference.meta.json"
SOURCES_FILENAME = "chemical_sources.jsonl"

HARVEST_DIR = Path(__file__).resolve().parent.parent / "harvest"
DEFAULT_REFERENCE_PATH = HARVEST_DIR / REFERENCE_FILENAME
PACKAGE_REFERENCE_ID = f"stage1/harvest/{REFERENCE_FILENAME}"
PACKAGE_REFERENCE_META_ID = f"stage1/harvest/{REFERENCE_META_FILENAME}"

# ctx keys (the runner copies cfg into ctx — the sanctioned channel).
GARBAGE_RULES_CTX_KEY = "garbage_rules"
SOURCES_INDEX_CTX_KEY = "chemical_sources_index"     # sub_source -> harvest provenance
CACHE_INFO_CTX_KEY = "chemical_cache_info"           # {reference: {...}}
LOAD_ERRORS_CTX_KEY = "input_load_errors"
EXTRA_INPUTS_CTX_KEY = "extra_input_files"
EXTRA_INPUT_META_CTX_KEY = "extra_input_meta"
POLICY_ACTIVE_CTX_KEY = "policy"
LOADED_FLAG_CTX_KEY = "_chemical_loaded"

# The adapter OWNS the property -> predictability mapping for its source (the
# taxonomy design: each adapter owns its own family->value mapping so identical
# families stay consistent across sources). IARC carcinogen verdicts are
# unpredictable (the agenda is announced but the group is unknowable until the
# Working Group meets); a Prop 65 listing and a TSCA determination are announced
# (the substance's hazard / the draft evaluation are public ahead of the formal
# post-cutoff action).
PREDICTABILITY_BY_PROPERTY = {
    "carcinogen_group": UNPREDICTABLE,
    "prop65_carcinogen_listing": ANNOUNCED,
    "tsca_risk_determination": ANNOUNCED,
}

# A light text_span plausibility screen: at least one token. The chemical labels
# ('Group 2B', 'listed as a carcinogen', ...) are clean and short, so this never
# false-positives; an EMPTY value (a data defect) would review. A violation is
# always review, never fail.
CHEMICAL_TEXT_SPAN_RULES = (("min_tokens", {"min": 1}),)


def _sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _evidence_from(obj, fallback_kind: str) -> Evidence:
    """Build an Evidence from a reference row's evidence sub-dict. Total: a
    missing/malformed evidence dict degrades to a kind-only Evidence with a note
    (evidence_resolvable then reviews it) rather than crashing the build."""
    if not isinstance(obj, dict):
        return Evidence(kind=fallback_kind, url=None,
                        ref={"problem": "evidence missing in reference row"}, as_of=None)
    kind = obj.get("kind")
    kind = kind if isinstance(kind, str) and kind else fallback_kind
    url = obj.get("url")
    url = url if isinstance(url, str) and url else None
    ref = obj.get("ref")
    ref = dict(ref) if isinstance(ref, dict) else {}
    as_of = obj.get("as_of")
    as_of = as_of if isinstance(as_of, str) and as_of else None
    return Evidence(kind=kind, url=url, ref=ref, as_of=as_of)


# --------------------------------------------------------------------------- #
# Chemical-specific gate
# --------------------------------------------------------------------------- #
class ChemicalPredictabilityGate(Gate):
    """REVIEW-ONLY validation that the record carries a canonical predictability
    tag. It is DISPOSITION-NEUTRAL: it NEVER returns 'fail', so predictability can
    never include or exclude a record — the tag stays Stage-2 stratification
    METADATA exactly as ``stage1/predictability.py`` requires. It exists as a
    logged gate only so the presence/canonicality of the tag is auditable in the
    gate ledger like every other check; it never reads the tag's VALUE to decide
    inclusion. A canonical tag passes; a missing / off-taxonomy tag reviews (a
    pipeline defect a human resolves), never a silent include."""

    name = "predictability"
    version = "predictability:v1"

    def evaluate(self, record, ctx: dict) -> GateResult:
        provenance = getattr(record, "provenance", None)
        tag = provenance.get("predictability") if isinstance(provenance, dict) else None
        evidence = {
            "predictability": tag if isinstance(tag, str) else repr(tag),
            "role": "stage2_stratification_metadata",
        }
        if is_valid_predictability(tag):
            evidence["note"] = (
                "canonical predictability tag; this gate is disposition-neutral "
                "(never fails), so predictability never includes or excludes a record"
            )
            return GateResult(name=self.name, version=self.version, verdict="pass", evidence=evidence)
        evidence["problem"] = (
            "predictability tag is missing or off-taxonomy; held for review (never a "
            "fail — predictability must never exclude a record)"
        )
        return GateResult(name=self.name, version=self.version, verdict="review", evidence=evidence)


# --------------------------------------------------------------------------- #
# The adapter
# --------------------------------------------------------------------------- #
class ChemicalAdapter(Adapter):
    """Adapter for source 'chemical'. See the module docstring for the design.

    Stateless: the sources-enrichment index and cache declarations are loaded once
    per run into cfg (the runner's sanctioned ctx channel), never onto the
    instance — a rewritten input file must be re-read on the next run."""

    source = SOURCE

    # -- enumeration --------------------------------------------------------
    def enumerate_candidates(self, cfg: dict):
        """Yield every committed-reference row, sorted by
        (sub_source, property, chemical, change_date, cas, file line) — a stable
        order independent of file order. No inclusion decision here. A malformed
        line is yielded as an error candidate so build_record raises it into the
        manifest's build_errors (never a silent skip)."""
        self._require_offline(cfg)
        self._ensure_loaded(cfg)
        path, _ident = self._resolve_reference_path(cfg)
        if path is None:
            cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).append(
                {"file": REFERENCE_FILENAME, "line": 0,
                 "error": "chemical reference not found (no candidates)"}
            )
            return
        candidates = []
        with open(path, encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError(f"row is {type(row).__name__}, expected object")
                except ValueError as exc:
                    candidates.append({"_line": line_no, "_parse_error": str(exc)})
                    continue
                row["_line"] = line_no
                candidates.append(row)
        candidates.sort(
            key=lambda c: (
                str(c.get("sub_source") or ""),
                str(c.get("property") or ""),
                str(c.get("chemical") or ""),
                str(c.get("change_date") or ""),
                str(c.get("cas") or ""),
                c["_line"],
            )
        )
        yield from candidates

    # -- record building ----------------------------------------------------
    def build_record(self, candidate: dict, cfg: dict) -> FactChangeRecord:
        if "_parse_error" in candidate:
            raise ValueError(
                f"{REFERENCE_FILENAME} line {candidate.get('_line')}: unparseable JSON "
                f"({candidate['_parse_error']})"
            )
        self._ensure_loaded(cfg)

        chemical = (candidate.get("chemical") or "").strip()
        if not chemical:
            raise ValueError(
                f"{REFERENCE_FILENAME} line {candidate.get('_line')}: missing/empty chemical"
            )
        prop = candidate.get("property")
        if not isinstance(prop, str) or not prop:
            raise ValueError(
                f"{REFERENCE_FILENAME} line {candidate.get('_line')} "
                f"({chemical!r}): missing/empty property"
            )
        change_value = candidate.get("change_date")
        precision = candidate.get("change_precision") or "day"
        if not isinstance(change_value, str) or not change_value:
            raise ValueError(
                f"{REFERENCE_FILENAME} line {candidate.get('_line')} "
                f"({chemical!r}): missing/empty change_date"
            )

        sub_source = (candidate.get("sub_source") or "").strip()
        cas = (candidate.get("cas") or "").strip()
        value_type = candidate.get("value_type") or "text_span"
        before_raw = candidate.get("before_raw")
        after_raw = candidate.get("after_raw")
        before_raw = before_raw if isinstance(before_raw, str) else ""
        after_raw = after_raw if isinstance(after_raw, str) else ""

        entity_ids = {"cas": cas} if cas else {}

        before_evidence = _evidence_from(candidate.get("before_evidence"),
                                         f"{sub_source or 'chemical'}_before")
        after_evidence = _evidence_from(candidate.get("after_evidence"),
                                        f"{sub_source or 'chemical'}_after")
        change_evidence = _evidence_from(candidate.get("change_evidence"),
                                         f"{sub_source or 'chemical'}_change")

        change_date = ChangeDate(value=change_value, precision=precision, evidence=change_evidence)

        # The adapter OWNS the predictability tag via its property mapping; the
        # reference row's declared value is recorded for audit and its agreement
        # is flagged. check_predictability raises on an off-taxonomy MAPPING value
        # (a code bug) — never on the reference data.
        tag = PREDICTABILITY_BY_PROPERTY.get(prop)
        if tag is None:
            # An unmapped property is a real gap: fall back to the reference's
            # declared tag when canonical, else leave it for the predictability
            # gate to review. Never guess a value into the benchmark.
            declared = candidate.get("predictability")
            tag = declared if is_valid_predictability(declared) else None
        predictability = check_predictability(tag) if tag is not None else candidate.get("predictability")

        harvest = self._sources_lookup(cfg, sub_source)

        provenance = {
            "predictability": predictability,
            "sub_source": sub_source,
            "cas": cas,
            "reference_declared_predictability": candidate.get("predictability"),
            "predictability_matches_reference": (
                candidate.get("predictability") == predictability
            ),
            "note": candidate.get("note"),
            # per-sub-source harvest provenance (live_fetched vs committed_reference,
            # and the live-verification result); None when the enrichment is absent.
            "harvest": harvest,
            "reference_cache": self._cache_info(cfg, "reference"),
        }

        fact_id = compute_fact_id(SOURCE, chemical, prop, change_value)
        discriminator = f"{sub_source}|{cas}|{prop}|{change_value}"
        return FactChangeRecord(
            fact_id=fact_id,
            record_id=compute_record_id(fact_id, discriminator),
            source=SOURCE,
            entity={"name": chemical, "ids": entity_ids},
            property=prop,
            value_type=value_type,
            before=ValueState(raw=before_raw, canonical=None, evidence=before_evidence),
            after=ValueState(raw=after_raw, canonical=None, evidence=after_evidence),
            change_date=change_date,
            provenance=provenance,
        )

    # -- gates --------------------------------------------------------------
    def gate_list(self, cfg: dict):
        """Ordered gates (rationale in the module docstring). Loads the sources
        enrichment + cache declarations, sets the garbage-rule table, and records
        the disposition-neutral predictability policy in the manifest."""
        self._require_offline(cfg)
        self._ensure_loaded(cfg)
        cfg[GARBAGE_RULES_CTX_KEY] = {"text_span": CHEMICAL_TEXT_SPAN_RULES}
        cfg[POLICY_ACTIVE_CTX_KEY] = {
            "predictability_by_property": dict(sorted(PREDICTABILITY_BY_PROPERTY.items())),
            "predictability_gate": "disposition-neutral (review-only; never excludes)",
        }
        return [
            TemporalWindowGate(),
            EvidenceResolvableGate(),
            GarbageValueGate(rules_ctx_key=GARBAGE_RULES_CTX_KEY),
            # Dedup on the SEMANTIC identity (entity.name | property | change_date)
            # — the SAME tuple fact_id is computed from (source is constant here).
            DedupGate(key_fields=("entity.name", "property", "change_date")),
            ChemicalPredictabilityGate(),
        ]

    def snapshot_inputs(self, cfg: dict):
        """The core chemical snapshot input the coverage check must find LISTED
        (and therefore sha1-verified) in a harvested snapshot's manifest: the
        committed cited reference, which carries every recorded value + citation.
        The sources enrichment and the sidecar are OPTIONAL provenance (a missing
        one degrades the harvest annotation / fingerprint, it does not drop a
        candidate), so they are not required here — though the harvester sha1-pins
        them in the manifest and they are verified like any listed file. Consulted
        ONLY when deriving from a harvested snapshot; never changes derive output
        (mirrors SecAdapter/FdaAdapter)."""
        return [REFERENCE_FILENAME]

    # -- loading / provenance ----------------------------------------------
    @staticmethod
    def _require_offline(cfg: dict) -> None:
        if not cfg.get("offline", True):
            raise NotImplementedError(
                "the chemical adapter is offline-only: the committed cited reference is "
                "built/verified once by stage1.harvest.chemical (the live Federal Register "
                "fetch lives only in the harvester)"
            )

    @staticmethod
    def _resolve_input_path(cfg: dict, filename: str, default_path, default_id):
        """(path, location-independent identifier): the vendored copy under
        data_dir when present, else the package/repo default; (None, None) when
        neither exists."""
        data_dir = cfg.get("data_dir")
        if data_dir is not None:
            local = Path(data_dir) / filename
            if local.is_file():
                return local, filename
        if default_path is not None and Path(default_path).is_file():
            return Path(default_path), default_id
        return None, None

    def _resolve_reference_path(self, cfg: dict):
        return self._resolve_input_path(
            cfg, REFERENCE_FILENAME, DEFAULT_REFERENCE_PATH, PACKAGE_REFERENCE_ID)

    def _ensure_loaded(self, cfg: dict) -> None:
        """Load the sources-enrichment index and declare the input fingerprints
        into the manifest channels exactly once per run."""
        if cfg.get(LOADED_FLAG_CTX_KEY):
            return
        cfg[LOADED_FLAG_CTX_KEY] = True
        cfg.setdefault(CACHE_INFO_CTX_KEY, {})

        ref_path, ref_id = self._resolve_reference_path(cfg)
        self._declare_input(cfg, "reference", ref_path, ref_id)
        self._declare_sidecar(cfg, ref_path)

        cfg[SOURCES_INDEX_CTX_KEY] = self._load_sources_index(cfg)

    def _declare_input(self, cfg: dict, tag: str, path, ident) -> None:
        info = {"file": None, "sha1": None, "present": False}
        if path is not None:
            digest = _sha1_file(Path(path))
            info = {"file": ident, "sha1": digest, "present": True}
            cfg.setdefault(EXTRA_INPUTS_CTX_KEY, {})[ident] = digest
        cfg.setdefault(CACHE_INFO_CTX_KEY, {})[tag] = info

    def _declare_sidecar(self, cfg: dict, ref_path) -> None:
        """Fingerprint the reference's .meta.json sidecar (when present) and
        surface its retrieval metadata into the manifest's extra_input_meta."""
        if ref_path is None:
            return
        sidecar = Path(ref_path).with_name(REFERENCE_META_FILENAME)
        if not sidecar.is_file():
            return
        vendored = cfg.get("data_dir") is not None and (
            Path(cfg["data_dir"]) / REFERENCE_FILENAME).is_file()
        ident = REFERENCE_META_FILENAME if vendored else PACKAGE_REFERENCE_META_ID
        digest = _sha1_file(sidecar)
        cfg.setdefault(EXTRA_INPUTS_CTX_KEY, {})[ident] = digest
        entry = {"sidecar": ident, "sha1": digest}
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            entry["error"] = f"unreadable sidecar: {exc}"
            meta = None
        if isinstance(meta, dict):
            for key in ("tool_version", "reference_sha1", "counts", "sub_sources",
                        "live_verification"):
                if key in meta:
                    entry[key] = meta[key]
            if "reference_sha1" in meta:
                cache_info = cfg.get(CACHE_INFO_CTX_KEY, {}).get("reference") or {}
                entry["sidecar_matches_cache"] = meta.get("reference_sha1") == cache_info.get("sha1")
        cfg.setdefault(EXTRA_INPUT_META_CTX_KEY, {})[ident] = entry

    def _load_sources_index(self, cfg: dict) -> dict:
        """{sub_source: harvest-provenance dict} from the OPTIONAL
        chemical_sources.jsonl (only ever vendored into a harvest snapshot). A
        missing file yields an empty index (provenance['harvest'] then degrades to
        None); a malformed line is an input_load_error, never a silent skip. First
        row wins per sub_source; the file is also fingerprinted into the manifest."""
        index: dict = {}
        path, ident = self._resolve_input_path(cfg, SOURCES_FILENAME, None, None)
        if path is None:
            return index
        digest = _sha1_file(Path(path))
        cfg.setdefault(EXTRA_INPUTS_CTX_KEY, {})[ident] = digest
        errors: list = []
        with open(path, encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    errors.append({"file": ident, "line": line_no, "error": str(exc)})
                    continue
                if not isinstance(row, dict):
                    errors.append({"file": ident, "line": line_no,
                                   "error": f"row is {type(row).__name__}, expected object"})
                    continue
                key = row.get("sub_source")
                if not isinstance(key, str) or not key:
                    continue
                index.setdefault(key, row)
        if errors:
            cfg.setdefault(LOAD_ERRORS_CTX_KEY, []).extend(errors)
        return index

    def _sources_lookup(self, cfg: dict, sub_source: str):
        index = cfg.get(SOURCES_INDEX_CTX_KEY)
        if not isinstance(index, dict):
            return None
        return index.get(sub_source)

    def _cache_info(self, cfg: dict, tag: str) -> dict:
        info = (cfg.get(CACHE_INFO_CTX_KEY) or {}).get(tag) or {}
        return {"file": info.get("file"), "sha1": info.get("sha1"),
                "present": bool(info.get("present"))}


ADAPTER = ChemicalAdapter()
