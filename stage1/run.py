"""Stage-1 CLI runner: enumerate -> build -> normalize -> gate -> write.

Usage (from the repo root):

    python3 -m stage1.run --source sec --cutoff 2025-01-01 --asof 2026-07-01 \
        --data-dir sec --out-dir stage1/releases/dev

Flow per candidate: the adapter builds a FactChangeRecord draft; the shared
normalization step parses before/after raw into canonical via the comparator
registry and its outcome becomes the record's first gate result (name
'normalize' — verdict 'pass' when both sides parsed, 'review' otherwise, with
the normalize info dict as evidence); then the adapter's gates run in order
and the disposition is derived. ALL records — included, excluded, review —
are written to facts.jsonl, sorted by (fact_id, record_id), one JSON object
per line with sorted keys. manifest.json records the arguments (with
resolved absolute data/out dirs), pipeline version, the gate name->version
table, per-gate verdict counts, disposition counts (plus any duplicate
fact_id/record_id occurrences — fact_id is semantic and MAY repeat across
records, record_id must not), comparator versions used, sha1s of the input
files under data_dir PLUS any extra inputs the adapter declares via
cfg['extra_input_files'] (an {identifier: sha1} dict for inputs living
outside data_dir, e.g. the sports adapter's package-default Wikidata P54
cache, keyed by a location-independent identifier — the manifest must
fingerprint every byte the release depends on and reproduce across
checkouts), extra-input retrieval metadata via cfg['extra_input_meta']
(binding a pinned cache sha1 to when/how it was fetched), input-load
errors reported by the adapter, and any defensive-containment events (build
errors, record validation errors keyed by record_id). When --data-dir is a
harvested snapshot (snapshot_manifest.json present), the manifest also
records ``snapshot_coverage`` — the snapshot's coverage block plus its
harvested_at/tool_version — next to args, so a NARROWED derive (a snapshot
declaring coverage.cutoff_narrowable) states both the harvested window and
the derived window; legacy data-dirs get no such key. The manifest timestamp
is the ONLY non-deterministic byte in the outputs (snapshot_coverage copies
frozen snapshot constants, never this run's clock).

Nothing is silently dropped: a build_record exception removes the record from
facts.jsonl only because there is no record to write — the candidate is
logged in manifest['build_errors'] instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from stage1 import PIPELINE_VERSION
from stage1.adapters import get_adapter
from stage1.gates import apply_gates
from stage1.harvest.snapshot import (
    SNAPSHOT_MANIFEST_NAME,
    check_coverage,
    load_snapshot_manifest,
)
from stage1.normalize import normalize_record
from stage1.schema import GateResult

NORMALIZE_GATE_NAME = "normalize"
NORMALIZE_GATE_VERSION = "normalize:v1"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python3 -m stage1.run",
        description="Build a Stage-1 fact-change release for one source.",
    )
    parser.add_argument("--source", required=True, help="adapter name, e.g. 'sec'")
    parser.add_argument("--cutoff", required=True, help="training-cutoff date, YYYY-MM-DD")
    parser.add_argument("--asof", required=True, help="as-of date for 'after' truth, YYYY-MM-DD")
    parser.add_argument("--data-dir", default=None, help="root of the source's input files")
    parser.add_argument(
        "--out-dir",
        default="stage1/releases/dev",
        help="output directory (default: stage1/releases/dev)",
    )
    parser.add_argument(
        "--online",
        action="store_true",
        help="allow adapters to fetch from the network (default: offline, data-dir only)",
    )
    return parser.parse_args(argv)


def _parse_iso_date(value: str, arg_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"error: {arg_name} must be a valid YYYY-MM-DD date, got {value!r}")


def build_cfg(args: argparse.Namespace) -> dict:
    return {
        "source": args.source,
        "cutoff": _parse_iso_date(args.cutoff, "--cutoff"),
        "asof": _parse_iso_date(args.asof, "--asof"),
        "data_dir": Path(args.data_dir) if args.data_dir is not None else None,
        "offline": not args.online,
        "out_dir": Path(args.out_dir),
    }


def _sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_input_files(data_dir) -> dict:
    """sha1 of every input file under data_dir, keyed by posix-style
    relative path, sorted. Empty dict when data_dir is None or missing
    (the manifest then shows explicitly that no inputs were hashed).

    Hashing policy (so the manifest is reproducible across machines and
    states exactly what was and was not hashed):

    * machine-local derived artifacts are excluded — '__pycache__' and
      dot-directories are not traversed, and '*.pyc'/'*.pyo' and dotfiles
      (.DS_Store, ...) are skipped: their bytes embed local mtimes and would
      make identical inputs hash differently per checkout;
    * a symlinked file is hashed through its target when the target exists;
      a DANGLING symlink is recorded with the sentinel value
      'dangling-symlink' instead of crashing mid-run;
    * a symlinked directory is not traversed (its contents live outside
      data_dir); it is recorded with the sentinel value
      'symlinked-directory-not-traversed' so its presence is visible.
    * ``snapshot_manifest.json`` is EXCLUDED: it is a harvested snapshot's
      provenance sidecar (it embeds the wall-clock ``harvested_at``), not a
      derived-from input. Its data files are already hashed here individually
      and the coverage check re-verifies their sha1s against it, so input_files
      stays a pure function of the DATA bytes and reproduces across re-harvests
      of byte-identical data. (Legacy dirs have no such file, so this is a no-op
      for them.)

    For that reproducibility to hold, EVERY OTHER snapshot file this walk hashes
    must itself be deterministic — including the cache ``.meta.json`` sidecars,
    which ARE hashed here (only ``snapshot_manifest.json`` is excluded). A
    harvester therefore keeps its snapshot's sole wall-clock in the manifest's
    ``harvested_at`` and writes its sidecars WITHOUT one (finance omits
    ``retrieved_at``); a wall-clock inside a hashed sidecar would change this
    fingerprint on every re-harvest of identical data. This is a per-source
    harvester responsibility, not something excluded here (a sidecar is a real,
    tamper-checked input and must stay fingerprinted).
    """
    if data_dir is None or not Path(data_dir).is_dir():
        return {}
    hashes = {}
    root = Path(data_dir)
    for dirpath, dirnames, filenames in os.walk(root):
        kept_dirs = []
        for dirname in sorted(dirnames):
            if dirname == "__pycache__" or dirname.startswith("."):
                continue
            sub = Path(dirpath) / dirname
            if sub.is_symlink():
                hashes[sub.relative_to(root).as_posix()] = "symlinked-directory-not-traversed"
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for filename in sorted(filenames):
            if filename.startswith(".") or filename.endswith((".pyc", ".pyo")):
                continue
            if filename == SNAPSHOT_MANIFEST_NAME and Path(dirpath) == root:
                continue
            full = Path(dirpath) / filename
            rel = full.relative_to(root).as_posix()
            if full.is_symlink() and not full.exists():
                hashes[rel] = "dangling-symlink"
                continue
            hashes[rel] = _sha1_file(full)
    return dict(sorted(hashes.items()))


def combined_input_files(input_files: dict, cfg: dict) -> dict:
    """Merge the data-dir file hashes with any adapter-declared extra inputs
    (cfg['extra_input_files'], keyed by a LOCATION-INDEPENDENT identifier —
    inputs that live OUTSIDE data_dir, like the sports Wikidata P54 cache
    under stage1/cache/, would otherwise be invisible to the manifest's
    input fingerprint; identifiers must never be machine-absolute paths, so
    the manifest reproduces across checkouts).

    Keys and values are coerced to str defensively so a malformed adapter
    entry can never make the manifest unserializable. A vendored extra input
    may share a key with the data-dir walk's relative entry; the sha1s then
    agree, so the merge is a harmless dedup. Sorted for determinism."""
    merged = dict(input_files)
    extra = cfg.get("extra_input_files")
    if isinstance(extra, dict):
        for key, value in extra.items():
            merged[str(key)] = str(value)
    return dict(sorted(merged.items()))


def _jsonable(value, depth: int = 0):
    """Defensive deep-copy of adapter-provided metadata into JSON-safe types
    so a malformed entry can never make the manifest unserializable."""
    if depth > 6:
        return repr(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v, depth + 1) for v in value]
    return repr(value)


def extra_input_meta(cfg: dict) -> dict:
    """Adapter-declared metadata about extra inputs (cfg['extra_input_meta'],
    e.g. the sports Wikidata cache's sidecar retrieval info: tool version,
    retrieved_at, endpoint). Recorded in the manifest next to the input
    sha1s so a pinned cache version stays bound to when/how it was retrieved
    even if the sidecar file is later overwritten by a refetch."""
    meta = cfg.get("extra_input_meta")
    if not isinstance(meta, dict):
        return {}
    return {str(key): _jsonable(value) for key, value in sorted(meta.items(), key=lambda kv: str(kv[0]))}


def adapter_policy(cfg: dict) -> dict:
    """The adapter's active curation policy (cfg['policy'], set by the
    adapter's gate_list — e.g. the sports loans / window-edge decisions).
    Recorded in the manifest so every release states which one-flag-revisable
    policy produced it. Empty dict when the adapter declares none (e.g. sec)."""
    policy = cfg.get("policy")
    if not isinstance(policy, dict):
        return {}
    return {str(key): _jsonable(value) for key, value in sorted(policy.items(), key=lambda kv: str(kv[0]))}


def _normalize_gate_result(info: dict) -> GateResult:
    """Turn normalize_record() info into the record's first gate result.

    'pass' only when a comparator was found and BOTH sides parsed; anything
    else (missing comparator, either side unparseable) is 'review' — never
    'fail', because a normalization problem is ambiguity, not evidence the
    record is out of scope. The comparator version is inside the evidence.
    """
    both_ok = (
        info.get("comparator_found")
        and info.get("before") is not None
        and info["before"]["ok"]
        and info.get("after") is not None
        and info["after"]["ok"]
    )
    return GateResult(
        name=NORMALIZE_GATE_NAME,
        version=NORMALIZE_GATE_VERSION,
        verdict="pass" if both_ok else "review",
        evidence=info,
    )


def run(cfg: dict, args_echo: dict) -> int:
    # WINDOW SANITY — before any output: an inverted window (cutoff after asof)
    # is never derivable from any source; refuse loudly (LookupError -> main()'s
    # exit 2) instead of producing a structurally-valid release in which every
    # candidate is a temporal exclusion. Complements the snapshot coverage
    # layer's narrowing bound (a legacy data-dir has no coverage check at all).
    if cfg["cutoff"] > cfg["asof"]:
        raise LookupError(
            f"--cutoff {cfg['cutoff'].isoformat()} is AFTER --asof {cfg['asof'].isoformat()}: "
            "an inverted [cutoff, asof] window cannot be derived"
        )
    adapter = get_adapter(cfg["source"])
    # SNAPSHOT COVERAGE CHECK — before any output is written. When --data-dir is
    # a harvested snapshot (contains snapshot_manifest.json), enforce that the
    # requested [cutoff, asof] is within the snapshot's coverage window (and that
    # the snapshot files are untampered) — refuse loudly otherwise. A CoverageError
    # is a LookupError, so main()'s existing handler returns exit code 2. A legacy
    # --data-dir with no manifest returns None here -> behaviour identical to before.
    snap = load_snapshot_manifest(cfg["data_dir"])
    snapshot_coverage = None
    if snap is not None:
        # The adapter may declare the fixed input filenames it reads so the
        # coverage check can assert every one is present, listed, and sha1-matched
        # (a trimmed files block can no longer green-light an unverified input).
        required = None
        declare = getattr(adapter, "snapshot_inputs", None)
        if callable(declare):
            required = declare(cfg)
        check_coverage(snap, cfg["source"], cfg["cutoff"], cfg["asof"], cfg["data_dir"],
                       required_files=required)
        # Record the snapshot's coverage promise next to the derive args, so a
        # NARROWED derive (coverage.cutoff_narrowable) states BOTH windows in the
        # release manifest: what was harvested and what was derived. Deterministic —
        # these are constants copied from snapshot_manifest.json (harvested_at is
        # the snapshot's frozen stamp, not this run's clock). Legacy --data-dirs
        # (no snapshot manifest) get no key, so their manifests are unchanged.
        snapshot_coverage = {
            "coverage": _jsonable(snap.get("coverage")),
            "harvested_at": _jsonable(snap.get("harvested_at")),
            "tool_version": _jsonable(snap.get("tool_version")),
        }
    # Fingerprint the inputs BEFORE producing any output, so a failure while
    # writing can never leave a release whose manifest describes inputs that
    # were re-read (or changed) after the facts were built.
    input_files = hash_input_files(cfg["data_dir"])
    gates = adapter.gate_list(cfg)
    gate_versions = {NORMALIZE_GATE_NAME: NORMALIZE_GATE_VERSION}
    gate_versions.update({g.name: g.version for g in gates})
    ctx = dict(cfg)

    records = []
    build_errors = []
    comparator_versions: dict = {}

    for candidate in adapter.enumerate_candidates(cfg):
        try:
            record = adapter.build_record(candidate, cfg)
            info = normalize_record(record)
            if info.get("comparator_version"):
                comparator_versions[info["value_type"]] = info["comparator_version"]
            record.gates.append(_normalize_gate_result(info))
            apply_gates(record, gates, ctx)
        except Exception as exc:
            build_errors.append({"candidate": repr(candidate)[:1000], "error": repr(exc)})
            continue
        records.append(record)

    records.sort(key=lambda r: (r.fact_id, r.record_id))

    validation_errors = {}
    fact_id_counts: dict = {}
    record_id_counts: dict = {}
    for record in records:
        fact_id_counts[record.fact_id] = fact_id_counts.get(record.fact_id, 0) + 1
        record_id_counts[record.record_id] = record_id_counts.get(record.record_id, 0) + 1
        errors = record.validate()
        if errors:
            entry = validation_errors.setdefault(
                record.record_id, {"fact_id": record.fact_id, "errors": []}
            )
            entry["errors"].extend(errors)
    duplicate_fact_ids = {k: v for k, v in sorted(fact_id_counts.items()) if v > 1}
    duplicate_record_ids = {k: v for k, v in sorted(record_id_counts.items()) if v > 1}

    disposition_counts: dict = {}
    gate_counts: dict = {}
    for record in records:
        disposition_counts[record.disposition] = disposition_counts.get(record.disposition, 0) + 1
        for g in record.gates:
            per_gate = gate_counts.setdefault(g.name, {"pass": 0, "fail": 0, "review": 0})
            per_gate[g.verdict] = per_gate.get(g.verdict, 0) + 1

    out_dir = cfg["out_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    facts_path = out_dir / "facts.jsonl"
    with open(facts_path, "w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record.to_dict(), sort_keys=True, ensure_ascii=False))
            fh.write("\n")

    manifest = {
        "pipeline_version": PIPELINE_VERSION,
        "source": cfg["source"],
        "args": args_echo,
        "data_dir_resolved": (
            str(Path(cfg["data_dir"]).resolve()) if cfg["data_dir"] is not None else None
        ),
        "out_dir_resolved": str(Path(out_dir).resolve()),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gate_versions": dict(sorted(gate_versions.items())),
        "counts": {
            "records": len(records),
            "dispositions": dict(sorted(disposition_counts.items())),
            "duplicate_fact_ids": duplicate_fact_ids,
            "duplicate_record_ids": duplicate_record_ids,
            "gates": {
                name: dict(sorted(verdicts.items()))
                for name, verdicts in sorted(gate_counts.items())
            },
        },
        "comparator_versions": dict(sorted(comparator_versions.items())),
        "policy": adapter_policy(cfg),
        "input_files": combined_input_files(input_files, cfg),
        "extra_input_meta": extra_input_meta(cfg),
        "input_load_errors": list(cfg.get("input_load_errors", [])),
        "build_errors": build_errors,
        "record_validation_errors": dict(sorted(validation_errors.items())),
    }
    if snapshot_coverage is not None:
        manifest["snapshot_coverage"] = snapshot_coverage
    manifest_path = out_dir / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")

    print(f"wrote {len(records)} records -> {facts_path}")
    print(f"manifest -> {manifest_path}")
    print(
        "dispositions: "
        + (
            " ".join(f"{k}={v}" for k, v in sorted(disposition_counts.items()))
            or "(none)"
        )
    )
    for name, verdicts in sorted(gate_counts.items()):
        print(
            f"gate {name}: "
            + " ".join(f"{k}={v}" for k, v in sorted(verdicts.items()))
        )
    if manifest["input_load_errors"]:
        print(
            f"input load errors: {len(manifest['input_load_errors'])} (see manifest input_load_errors)",
            file=sys.stderr,
        )
    if duplicate_record_ids:
        print(
            f"duplicate record_ids: {len(duplicate_record_ids)} (see manifest counts.duplicate_record_ids)",
            file=sys.stderr,
        )
    if build_errors:
        print(f"build errors: {len(build_errors)} (see manifest build_errors)", file=sys.stderr)
    if validation_errors:
        print(
            f"record validation errors: {len(validation_errors)} records (see manifest record_validation_errors)",
            file=sys.stderr,
        )
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    cfg = build_cfg(args)
    args_echo = {
        "source": args.source,
        "cutoff": args.cutoff,
        "asof": args.asof,
        "data_dir": args.data_dir,
        "out_dir": args.out_dir,
        "online": args.online,
    }
    try:
        return run(cfg, args_echo)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except NotImplementedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
