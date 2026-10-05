"""Snapshot writing + coverage checking — the shared spine of harvest<->derive.

A SNAPSHOT is a directory the harvester writes and a derive adapter reads. It
holds the raw fetched input files (the exact fixed filenames the adapter
already reads by name) plus one self-describing ``snapshot_manifest.json``
recording provenance, the coverage window actually fetched, and a sha1 of every
snapshot file. Because a snapshot dir carries the adapter's fixed filenames, it
IS a valid ``--data-dir`` and the adapter needs zero change.

This module is the SINGLE SOURCE OF TRUTH for the coverage rule, imported by
BOTH the harvest runner (which writes the manifest) and stage1/run.py (which
enforces it before deriving). The rule guarantees a researcher can never
silently derive a [cutoff, asof] window the snapshot does not cover.

Harvest is a frozen LIVE snapshot: unlike derive it is not required to be
byte-deterministic across runs (a re-harvest may reorder rows), but the
manifest sha1s pin the frozen bytes so a later derive off that snapshot is
reproducible.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

SNAPSHOT_MANIFEST_NAME = "snapshot_manifest.json"
# A non-JSON marker file the writer drops next to the manifest. Its whole job is
# to distinguish "this dir IS a harvested snapshot" from "this is a legacy
# --data-dir": if the marker is present but snapshot_manifest.json is gone (a
# copy/rsync/tar that filtered *.json, a partial copy), derive REFUSES loudly
# instead of silently degrading to the no-coverage legacy path. It is a dotfile,
# so hash_input_files skips it and it never enters a derive output.
SNAPSHOT_SENTINEL_NAME = ".stage1_snapshot"
SNAPSHOT_VERSION = "1"

# Coverage keys preserved verbatim into the manifest coverage block. ``window_basis``
# documents WHICH date the [cutoff, asof] window bounds (SEC: "event_date" — the
# 8-K report/period date — NOT the effective/change date the derive gate uses), so
# the coverage promise and the harvest filter describe the same thing. ``cutoff_exact``
# mirrors ``asof_exact`` for the LOWER edge: a snapshot whose evidence is pinned to
# the EXACT cutoff (finance: grouped-daily closes are fetched only for the resolved
# anchor trading day of THIS cutoff, so a different cutoff resolves to a different
# anchor whose close is not in the snapshot) sets it True. SEC leaves it unset, so
# rule (4b) is a no-op for SEC and its behaviour is unchanged.
#
# ``back_datable`` is a DOCUMENTARY honesty flag (no coverage RULE reads it): a
# source whose candidate SET can be reconstructed at a past window (SEC/finance/
# sports — permanent archives) is back-datable; FDA is NOT (openFDA's Recent Major
# Changes only surfaces changes on CURRENT labels, and the 'after' is the
# contemporaneous current label), so its harvest is only sound near the window it
# ran for. A source that omits the key leaves it absent (behaviour unchanged); FDA
# records back_datable=False so the manifest states the limitation plainly. The
# WINDOW-refusal enforcement still comes from cutoff_exact/asof_exact (rules 4/4b).
#
# ``cutoff_narrowable`` is a sanctioned NARROWING exception to rule (4b): a
# cutoff-pinned snapshot whose before/after evidence is PER-CHANGE (FDA: each
# change row carries its own pre-change 'before' and asof-pinned 'after', so the
# evidence set at a LATER cutoff is a pure subset of what was harvested — the
# excluded early candidates stay enumerated and become audit-visible temporal
# exclusions) may declare cutoff_narrowable=True to allow a derive cutoff LATER
# than coverage.cutoff. It NEVER relaxes anything else: a cutoff EARLIER than
# coverage.cutoff stays refused (rule 2), and asof_exact still demands equality
# (rule 4). A source whose 'before' evidence is cutoff-resolved rather than
# per-change (finance: the anchor trading day's close exists only for the exact
# harvest cutoff) must NOT set it — omitting the key keeps rule (4b) strict.
_COVERAGE_KEYS = ("cutoff", "asof", "cutoff_exact", "asof_exact",
                  "cutoff_narrowable", "precision", "window_basis", "back_datable")


class CoverageError(LookupError):
    """A requested derive window is not covered by the snapshot (or the
    snapshot is tampered/truncated). Subclasses LookupError so stage1/run.py's
    existing ``except LookupError -> return 2`` handles it with no new clause,
    while remaining a distinct type callers can catch specifically."""


# --------------------------------------------------------------------------- #
# sha1 helpers (kept local so this module has zero intra-package deps)
# --------------------------------------------------------------------------- #
def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


# --------------------------------------------------------------------------- #
# Writer (harvest side)
# --------------------------------------------------------------------------- #
class SnapshotWriter:
    """Owns writing a snapshot dir + its manifest. The harvester feeds it files
    and metadata; finalize() stamps harvested_at and writes the manifest.

    EVERY snapshot input the deriving adapter reads must go through ONE of these
    writers so it lands in ``files`` with a sha1 (which the derive-side coverage
    check re-verifies). Four input primitives cover the five sources' shapes:

    * add_jsonl(name, rows)  — deterministic sorted JSONL (each row
      json.dumps(sort_keys=True); LINES sorted so byte-order is independent of
      fetch order). Records per-file sha1/rows/bytes.
    * add_json(name, obj)    — a single-object JSON file (deterministic:
      sort_keys, indent 2). This is how a cache ``.meta.json`` sidecar (finance /
      fda / sports / people all read one next to each cache) is emitted THROUGH
      the writer so it is sha1-pinned and coverage-re-verified, instead of being
      written out-of-band and invisible to tamper detection.
    * add_file(name, src, rows=None) — verbatim byte copy of an arbitrary input
      (a non-JSONL cache, an .xlsx gold file, a .csv change file). ``rows`` is
      optional provenance (None when a row count is not meaningful for a binary).
    * add_scope_file(name, src, kind, member_count) — a verbatim copy that is
      ALSO recorded as a semantic scope digest. May be called more than once (a
      source can declare several scope/universe files); the first distinct file
      is the primary ``scope`` (backward compatible) and the full set is emitted
      as ``scopes`` when there is more than one.
    * set_params / set_stats / set_coverage — manifest metadata.
    """

    def __init__(self, out_dir, source: str, tool_version: str):
        self.out_dir = Path(out_dir)
        self.source = source
        self.tool_version = tool_version
        self._files: dict = {}
        self._scope: dict = {}        # the PRIMARY scope digest (backward compat)
        self._scopes: list = []       # ALL scope digests, deduped by 'file'
        self._params: dict = {}
        self._stats: dict = {}
        self._coverage: dict = {}
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def _record_file(self, name: str, path: Path, rows=None) -> None:
        self._files[name] = {
            "sha1": sha1_file(path),
            "rows": int(rows) if rows is not None else None,
            "bytes": path.stat().st_size,
        }

    def add_jsonl(self, name: str, rows) -> None:
        lines = sorted(
            json.dumps(row, sort_keys=True, ensure_ascii=False) for row in rows
        )
        path = self.out_dir / name
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            for line in lines:
                fh.write(line)
                fh.write("\n")
        self._record_file(name, path, rows=len(lines))

    def add_json(self, name: str, obj) -> None:
        """A single-object JSON file (e.g. a cache .meta.json sidecar), written
        deterministically and recorded in ``files`` with sha1 so the coverage
        check re-verifies it exactly like a jsonl input."""
        path = self.out_dir / name
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(obj, fh, sort_keys=True, indent=2, ensure_ascii=False)
            fh.write("\n")
        self._record_file(name, path, rows=1)

    def add_file(self, name: str, src, rows=None) -> None:
        """Verbatim byte copy of an arbitrary input file, recorded in ``files``
        with sha1. For non-JSONL inputs an adapter reads by name (.xlsx gold,
        .csv change files, pre-built caches)."""
        data = Path(src).read_bytes()
        path = self.out_dir / name
        with open(path, "wb") as fh:
            fh.write(data)
        self._record_file(name, path, rows=rows)

    def _add_scope_digest(self, entry: dict) -> None:
        """Accumulate a scope/universe semantic digest, deduped by 'file' so a
        re-declaration of the same scope (e.g. coverage() returned it AND
        add_scope_file wrote it) updates in place rather than double-recording.
        The first distinct scope stays the primary ``self._scope``."""
        fname = entry.get("file")
        self._scopes = [s for s in self._scopes if s.get("file") != fname]
        self._scopes.append(dict(entry))
        self._scope = dict(self._scopes[0])

    def add_scope_file(self, name: str, src, kind: str, member_count: int) -> None:
        self.add_file(name, src, rows=int(member_count))
        self._add_scope_digest({
            "kind": kind,
            "file": name,
            "sha1": self._files[name]["sha1"],
            "size": int(member_count),
        })

    def set_params(self, params: dict) -> None:
        self._params = dict(params)

    def set_stats(self, stats: dict) -> None:
        self._stats = dict(stats)

    def set_coverage(self, coverage: dict) -> None:
        # keep only the enforced/documented coverage keys; a scope digest
        # returned by coverage() is folded into the scope accumulator (deduped
        # by file with any later add_scope_file for the same universe).
        self._coverage = {k: coverage[k] for k in _COVERAGE_KEYS if k in coverage}
        scope = coverage.get("scope")
        if isinstance(scope, dict) and scope:
            self._add_scope_digest(scope)

    def finalize(self) -> dict:
        """Write snapshot_manifest.json (with the wall-clock harvested_at — the
        ONLY non-deterministic byte, and a SIDECAR value that never enters a
        derive output) plus the snapshot sentinel, and return the manifest dict."""
        manifest = {
            "snapshot_version": SNAPSHOT_VERSION,
            "source": self.source,
            "tool_version": self.tool_version,
            "harvested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "coverage": dict(self._coverage),
            "scope": dict(self._scope),
            "params": dict(self._params),
            "files": {k: self._files[k] for k in sorted(self._files)},
            "fetch_stats": dict(self._stats),
        }
        if len(self._scopes) > 1:
            manifest["scopes"] = [dict(s) for s in self._scopes]
        path = self.out_dir / SNAPSHOT_MANIFEST_NAME
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
        # Drop the sentinel LAST so a dir that reaches this state is unambiguously
        # a completed snapshot; if its manifest later vanishes, derive refuses.
        with open(self.out_dir / SNAPSHOT_SENTINEL_NAME, "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write(
                "stage1 harvested-snapshot marker — provenance is in "
                f"{SNAPSHOT_MANIFEST_NAME}.\nDo NOT delete: if this marker is present but "
                f"{SNAPSHOT_MANIFEST_NAME} is missing, derive refuses loudly rather than "
                "silently deriving an unverified window.\n"
            )
        return manifest


# --------------------------------------------------------------------------- #
# Reader + coverage rule (derive side)
# --------------------------------------------------------------------------- #
def load_snapshot_manifest(data_dir):
    """Return the parsed snapshot_manifest.json under data_dir, or None when
    there is none (a legacy --data-dir with no manifest — behaviour is then
    identical to before). A PRESENT but corrupt/non-object manifest raises
    CoverageError loudly (a broken snapshot must never be treated as absent).

    A dir carrying the snapshot SENTINEL but no manifest is a harvested snapshot
    that LOST its manifest (a copy/rsync/tar that filtered *.json, a partial
    copy) — it raises CoverageError rather than degrading to the no-coverage
    legacy path, closing the exact 'silently derive an unverified window' hole
    the layer exists to prevent."""
    if data_dir is None:
        return None
    base = Path(data_dir)
    path = base / SNAPSHOT_MANIFEST_NAME
    if not path.is_file():
        if (base / SNAPSHOT_SENTINEL_NAME).is_file():
            raise CoverageError(
                f"{base} carries the snapshot marker {SNAPSHOT_SENTINEL_NAME} but its "
                f"{SNAPSHOT_MANIFEST_NAME} is missing: this is a harvested snapshot that lost "
                "its manifest, so the coverage window cannot be enforced — refusing to derive "
                "(restore the manifest, or delete the marker if this is intentionally a legacy dir)"
            )
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except ValueError as exc:
        raise CoverageError(f"{path} is present but not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise CoverageError(f"{path} must be a JSON object, got {type(manifest).__name__}")
    return manifest


def check_coverage(manifest: dict, source: str, cutoff, asof, data_dir,
                   required_files=None) -> None:
    """Enforce the 5-part coverage rule; raise CoverageError (loud) on any
    violation. cutoff/asof may be datetime.date or ISO strings.

      (1) manifest.source == source;
      (2) coverage.cutoff <= cutoff   (snapshot starts at least as early);
      (3) asof <= coverage.asof       (snapshot runs at least as late);
      (4) if coverage.asof_exact: asof == coverage.asof   (asof-pinned 'after');
      (4b) if coverage.cutoff_exact: cutoff == coverage.cutoff   (cutoff-pinned
           'before'; finance's anchor close is fetched for the resolved anchor
           trading day of THIS exact cutoff — a wider/narrower cutoff resolves to
           a DIFFERENT anchor whose close is not in the snapshot). SEC leaves
           cutoff_exact unset, so this is a no-op for SEC (rule 2 still governs).
           EXCEPTION: if coverage.cutoff_narrowable, a cutoff LATER than
           coverage.cutoff is ALLOWED — the snapshot declares its before/after
           evidence per-change, so a narrower window is an evidence-complete
           subset. An earlier cutoff stays refused (rule 2), asof is still
           governed by rules 3/4 (asof_exact still demands equality), and the
           narrowed cutoff must not exceed coverage.asof (an inverted
           cutoff>asof window is never derivable);
      (5) manifest.files is NON-EMPTY, every declared ``required_files`` input is
          LISTED in it, and every listed file exists under data_dir with a
          MATCHING sha1 (catches a truncated/edited/partial/trimmed snapshot).

    ``required_files`` (the filenames the deriving adapter will read, e.g.
    SEC's three inputs) closes the vacuous-pass hole: an empty or subset
    ``files`` block can no longer green-light a derive that reads an input the
    manifest never pinned. It defaults to None (no per-file requirement) so a
    direct caller that does not know the adapter's inputs still gets the
    non-empty-block guard and full sha1 re-verification of whatever IS listed.
    """
    if not isinstance(manifest, dict):
        raise CoverageError("snapshot manifest is not a JSON object")

    # (1) source
    msrc = manifest.get("source")
    if msrc != source:
        raise CoverageError(
            f"snapshot source {msrc!r} does not match requested --source {source!r}"
        )

    cov = manifest.get("coverage")
    if not isinstance(cov, dict) or "cutoff" not in cov or "asof" not in cov:
        raise CoverageError("snapshot manifest has no valid 'coverage' block")
    try:
        cov_cutoff = _as_date(cov["cutoff"])
        cov_asof = _as_date(cov["asof"])
        d_cutoff = _as_date(cutoff)
        d_asof = _as_date(asof)
    except (ValueError, TypeError) as exc:
        raise CoverageError(f"unparseable coverage/derive dates: {exc}") from exc

    # (2) snapshot must start at least as early as the requested cutoff
    if cov_cutoff > d_cutoff:
        raise CoverageError(
            f"snapshot coverage.cutoff {cov_cutoff.isoformat()} is LATER than the requested "
            f"cutoff {d_cutoff.isoformat()}: earlier events were not harvested, so this window "
            "cannot be derived from this snapshot"
        )
    # (3) snapshot must run at least as late as the requested asof
    if d_asof > cov_asof:
        raise CoverageError(
            f"requested asof {d_asof.isoformat()} is LATER than snapshot coverage.asof "
            f"{cov_asof.isoformat()}: later events were not harvested, so this window cannot be "
            "derived from this snapshot"
        )
    # (4) asof-pinned 'after' evidence must be derived at the exact asof
    if cov.get("asof_exact") and d_asof != cov_asof:
        raise CoverageError(
            f"snapshot is asof-pinned (coverage.asof_exact): the requested asof "
            f"{d_asof.isoformat()} must EQUAL coverage.asof {cov_asof.isoformat()} — a narrower "
            "asof would silently attach a too-new 'after' holder"
        )
    # (4b) cutoff-pinned 'before' evidence must be derived at the exact cutoff.
    #      Finance fetches the anchor (before) close only for the resolved trading
    #      day of THIS cutoff; a different cutoff resolves to a different anchor
    #      whose close is not in the snapshot. SEC leaves cutoff_exact unset, so
    #      this never fires for SEC (rule 2's coverage.cutoff <= cutoff still holds).
    #      SANCTIONED NARROWING: a snapshot declaring coverage.cutoff_narrowable
    #      states its before/after evidence is per-change (FDA), so a LATER derive
    #      cutoff is an evidence-complete subset and is allowed; an EARLIER cutoff
    #      is still refused here (and by rule 2 above), and asof is untouched by
    #      this exception (rules 3/4 already ran).
    if cov.get("cutoff_exact") and d_cutoff != cov_cutoff:
        #      The narrowed cutoff is BOUNDED ABOVE by coverage.asof: a cutoff
        #      beyond the asof would be an inverted window (cutoff > asof) that
        #      no snapshot supports — every candidate would be a temporal
        #      exclusion and the derive nonsensical, so it is refused here
        #      rather than producing a structurally-valid but empty release.
        narrowed_ok = (bool(cov.get("cutoff_narrowable"))
                       and cov_cutoff < d_cutoff <= cov_asof)
        if not narrowed_ok:
            raise CoverageError(
                f"snapshot is cutoff-pinned (coverage.cutoff_exact): the requested cutoff "
                f"{d_cutoff.isoformat()} must EQUAL coverage.cutoff {cov_cutoff.isoformat()} — a "
                "different cutoff resolves to different 'before' evidence not in this snapshot "
                "(finance: a different anchor trading day's close; sports: a different pinned "
                "revision); only a snapshot declaring coverage.cutoff_narrowable (per-change "
                "evidence) may derive a LATER cutoff"
            )

    # (5) per-file sha1 re-verification
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise CoverageError(
            "snapshot manifest has no non-empty 'files' block to verify — cannot confirm the "
            "snapshot inputs are present and untampered (an emptied files block must not pass "
            "tamper detection vacuously)"
        )
    # (5b) every input the deriving adapter declares it will read must be LISTED
    #      (and is therefore sha1-verified below); a files block trimmed to a
    #      subset can no longer leave a read-but-unverified input undetected.
    if required_files:
        unlisted = [f for f in sorted({str(x) for x in required_files}) if f not in files]
        if unlisted:
            raise CoverageError(
                f"snapshot manifest.files does not list required input(s) {unlisted} that the "
                f"{source!r} adapter reads: the snapshot cannot be verified as complete (a trimmed "
                "files block or a partial snapshot)"
            )
    base = Path(data_dir) if data_dir is not None else Path(".")
    for name in sorted(files):
        meta = files[name]
        expected = meta.get("sha1") if isinstance(meta, dict) else None
        path = base / name
        if not path.is_file():
            raise CoverageError(
                f"snapshot file {name!r} listed in the manifest is missing under {base}"
            )
        actual = sha1_file(path)
        if expected != actual:
            raise CoverageError(
                f"snapshot file {name!r} sha1 mismatch (manifest {expected}, on disk {actual}): "
                "the snapshot has been truncated or edited since it was harvested"
            )
