#!/usr/bin/env python3
"""reanchor_dailymed_cutoff.py -- anchor the FDA 'before' to the CUTOFF.

Owner decision 2026-07-24 (1). The FDA history cache's ``before_*`` fields hold
the prior DailyMed section at the CHANGE-MONTH anchor (the latest Wayback capture
before each change month's first day). For the benchmark the 'before' must be the
label AS IT STOOD AT THE CUTOFF (2026-02-01) -- the model's knowledge boundary --
so it aligns with every other source (SEC cert in force at cutoff; finance close
at 2026-01-30; sports/people infobox at the cutoff revision). This pass adds a
CUTOFF-ANCHORED prior to every history row without disturbing the existing
change-month ``before_*`` provenance:

    prior_cutoff_full_section    the FULL prior section body at the cutoff anchor
    prior_cutoff_snapshot_ts     the Wayback timestamp of the cutoff-anchor capture
    prior_cutoff_snapshot_url    its raw (id_) URL
    prior_cutoff_status          ok | section_not_found_in_snapshot |
                                 no_pre_cutoff_snapshot | fetch_error
    prior_cutoff_anchor          cache_reuse | fetched | none
    prior_cutoff_extract_method  toc_anchor | none

SELECTION (PREFER THE CACHE, fetch only the genuinely-missing anchors):

  * A set_id whose cached ``before_snapshot_ts`` is STRICTLY BEFORE the cutoff is
    already the cutoff anchor -- PROVABLY: the harvest picked the latest capture
    below ``change_first_day`` (>= cutoff), and that maximum falling below the
    cutoff makes it also the latest capture below the cutoff. Its cached
    ``before_full_section`` is SELECTED OFFLINE as ``prior_cutoff_full_section``
    (anchor=cache_reuse) -- no network. (A cached section that was capped at the
    old 6000 limit is reused as-is: truncation can only ADD false-new sentences
    to the delta, never drop a real one, so it never false-routes a fact to
    review; a fresh full harvest under the raised cap would complete it.)

  * A set_id whose cached anchor is ON/AFTER the cutoff (a later capture was the
    latest before the change month) OR is ABSENT is GENUINELY MISSING its cutoff
    anchor -> FETCH: one CDX query for captures in [MIN_SNAPSHOT_DAY, cutoff),
    take the latest, fetch it once, extract every changed section with the RAISED
    full-section cap (anchor=fetched). No pre-cutoff capture -> no_pre_cutoff_
    snapshot (delta_unavailable, the RMC fact still stands). A transient Wayback
    failure -> fetch_error, preserved for a later --resume.

Networked (Wayback) ONLY for the missing anchors; polite pacing, resumable via a
scratch .part outside the cache. The DERIVE stays a pure offline function of the
frozen cache: this tool freezes the cutoff prior into the snapshot, and
stage1.adapters.fda computes the content-based delta deterministically from it.

Writes the augmented cache in place (sorted, atomic) and refreshes the .meta.json
sidecar (cache_sha1 + a ``reanchor`` provenance block; NO fresh wall-clock in the
reanchor block so the frozen input stays reproducible)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, OrderedDict
from datetime import date
from pathlib import Path

from stage1.tools.fetch_dailymed_history import (
    FULL_SECTION_CAP,
    MIN_SNAPSHOT_DAY,
    TOOL_VERSION as FETCH_TOOL_VERSION,
    extract_section,
    fetch_cdx,
    load_cached,
    pre_change_candidates,
    row_key,
    sha1_file,
    snapshot_raw_url,
    _fetch_best_html,
)

TOOL_VERSION = "reanchor_dailymed_cutoff:v1"

# Provenance field names added to every history row.
PF_FULL = "prior_cutoff_full_section"
PF_TS = "prior_cutoff_snapshot_ts"
PF_URL = "prior_cutoff_snapshot_url"
PF_STATUS = "prior_cutoff_status"
PF_ANCHOR = "prior_cutoff_anchor"
PF_METHOD = "prior_cutoff_extract_method"

DEFAULT_SLEEP = 1.5
DEFAULT_TRIES = 6
DEFAULT_TIMEOUT = 120.0


def _load_rows(path: Path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path} line {line_no}: row is not an object")
            rows.append(row)
    return rows


def _set_prior(row, *, full, ts, url, status, anchor, method):
    row[PF_FULL] = full
    row[PF_TS] = ts
    row[PF_URL] = url
    row[PF_STATUS] = status
    row[PF_ANCHOR] = anchor
    row[PF_METHOD] = method


def _reuse_offline(row, cutoff_compact: str) -> bool:
    """Fill the cutoff-anchor fields from the cached change-month capture when it
    is PROVABLY the cutoff anchor (cached ts strictly before the cutoff). Returns
    True when handled offline, False when the set_id must be fetched."""
    ts = row.get("before_snapshot_ts")
    if not (isinstance(ts, str) and ts and ts < cutoff_compact + "000000"):
        return False
    status = row.get("status")
    if status == "ok" and (row.get("before_full_section") or "").strip():
        _set_prior(row, full=row.get("before_full_section"), ts=ts,
                   url=row.get("before_snapshot_url"), status="ok",
                   anchor="cache_reuse", method=row.get("extract_method") or "toc_anchor")
    else:
        # The cutoff-anchor capture exists but this section was not extractable
        # from it (section_not_found) -> no clean cutoff prior for this section.
        _set_prior(row, full=None, ts=ts, url=row.get("before_snapshot_url"),
                   status="section_not_found_in_snapshot", anchor="cache_reuse",
                   method=row.get("extract_method") or "none")
    return True


def reanchor(rows, cutoff: date, out_dir: Path, *, resume: bool,
             sleep: float, tries: int, timeout: float, full_cap: int):
    cutoff_compact = cutoff.strftime("%Y%m%d")
    groups: "OrderedDict[str, list]" = OrderedDict()
    for r in rows:
        groups.setdefault(r["set_id"], []).append(r)

    reuse_sids, fetch_sids = [], []
    for sid, grp in groups.items():
        if all(_reuse_offline(r, cutoff_compact) for r in grp):
            reuse_sids.append(sid)
        else:
            # Mixed groups cannot happen (one before_snapshot_ts per set_id), but
            # be defensive: if any row was not reusable offline, fetch the whole
            # group (re-filling every row from the cutoff capture).
            fetch_sids.append(sid)

    print(f"[reanchor] {len(groups)} set_id(s): {len(reuse_sids)} reused offline "
          f"(cached anchor already before {cutoff.isoformat()}), {len(fetch_sids)} to fetch",
          file=sys.stderr, flush=True)

    scratch = out_dir.parent / (".reanchor_scratch_" + out_dir.name)
    scratch.mkdir(parents=True, exist_ok=True)
    part_path = scratch / "reanchor.part.jsonl"
    cached = _load_part(part_path) if resume else {}

    transient = 0
    requests = 0
    with open(part_path, "a", encoding="utf-8", newline="\n") as part:
        for i, sid in enumerate(fetch_sids, 1):
            grp = groups[sid]
            if resume and all(row_key(r) in cached
                              and cached[row_key(r)].get(PF_STATUS) not in (None, "fetch_error")
                              for r in grp):
                for r in grp:
                    _apply_cached_prior(r, cached[row_key(r)])
                continue
            n_req, group_transient = self_fetch_group(
                sid, grp, cutoff_compact, sleep=sleep, tries=tries,
                timeout=timeout, full_cap=full_cap)
            requests += n_req
            transient += group_transient
            for r in grp:
                cached[row_key(r)] = {k: r.get(k) for k in
                                      (PF_FULL, PF_TS, PF_URL, PF_STATUS, PF_ANCHOR, PF_METHOD)}
                part.write(json.dumps({"key": list(row_key(r)),
                                       **cached[row_key(r)]}, ensure_ascii=False) + "\n")
            part.flush()
            if i % 10 == 0 or i == len(fetch_sids):
                print(f"[reanchor]   fetched {i}/{len(fetch_sids)} set_id(s) "
                      f"({requests} archive request(s))", file=sys.stderr, flush=True)

    if transient == 0:
        try:
            if part_path.exists():
                part_path.unlink()
            if scratch.is_dir() and not any(scratch.iterdir()):
                scratch.rmdir()
        except OSError as exc:
            print(f"[reanchor] scratch cleanup skipped: {exc}", file=sys.stderr, flush=True)
    else:
        print(f"[reanchor] {transient} row(s) left transient (fetch_error); scratch kept "
              f"at {scratch} for --resume", file=sys.stderr, flush=True)
    return requests, transient


def _load_part(part_path: Path):
    cached = {}
    if not part_path.is_file():
        return cached
    with open(part_path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            key = tuple(obj.get("key") or [])
            if len(key) == 3:
                cached[key] = obj
    return cached


def _apply_cached_prior(row, entry):
    _set_prior(row, full=entry.get(PF_FULL), ts=entry.get(PF_TS), url=entry.get(PF_URL),
               status=entry.get(PF_STATUS), anchor=entry.get(PF_ANCHOR),
               method=entry.get(PF_METHOD))


def self_fetch_group(sid, grp, cutoff_compact, *, sleep, tries, timeout, full_cap):
    """Fetch the latest pre-cutoff capture once and extract every changed section
    with the raised cap. Returns (n_requests, transient_count)."""
    cdx_rows, cdx_err = fetch_cdx(sid, cutoff_compact, sleep=sleep, tries=tries, timeout=timeout)
    if cdx_err is not None:
        for r in grp:
            _set_prior(r, full=None, ts=None, url=None, status="fetch_error",
                       anchor="fetched", method="none")
        return 1, len(grp)
    candidates = pre_change_candidates(cdx_rows, cutoff_compact)  # ts < cutoff (strictly before)
    if not candidates:
        for r in grp:
            _set_prior(r, full=None, ts=None, url=None, status="no_pre_cutoff_snapshot",
                       anchor="fetched", method="none")
        return 1, 0
    html, snap_ts, snap_orig, snap_err = _fetch_best_html(
        candidates, sleep=sleep, tries=tries, timeout=timeout)
    if html is None:
        for r in grp:
            _set_prior(r, full=None, ts=None, url=None, status="fetch_error",
                       anchor="fetched", method="none")
        return 2, len(grp)
    snap_url = snapshot_raw_url(snap_ts, snap_orig)
    for r in grp:
        _, full, method, status = extract_section(
            html, r.get("section", ""), r.get("section_num", ""), full_cap=full_cap)
        _set_prior(r, full=(full if status == "ok" else None), ts=snap_ts, url=snap_url,
                   status=status, anchor="fetched", method=method)
    return 2, 0


def write_cache(rows, out_path: Path):
    """Sorted (row_key), atomic write mirroring fetch_dailymed_history's layout."""
    rows_sorted = sorted(rows, key=row_key)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows_sorted:
            fh.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
    os.replace(tmp, out_path)


def anchor_breakdown(rows):
    return {
        "prior_cutoff_status": dict(sorted(Counter(r.get(PF_STATUS) for r in rows).items())),
        "prior_cutoff_anchor": dict(sorted(Counter(r.get(PF_ANCHOR) for r in rows).items())),
    }


def refresh_sidecar(meta_path: Path, cache_path: Path, rows, cutoff: date,
                    requests: int, full_cap: int):
    """Update cache_sha1 and add a deterministic ``reanchor`` provenance block.
    No fresh wall-clock in the reanchor block (the frozen input must reproduce)."""
    if not meta_path.is_file():
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["cache_sha1"] = sha1_file(cache_path)
    bd = anchor_breakdown(rows)
    meta["reanchor"] = {
        "tool_version": TOOL_VERSION,
        "fetch_tool_version": FETCH_TOOL_VERSION,
        "cutoff": cutoff.isoformat(),
        "anchor_rule": "latest Wayback capture strictly before the cutoff "
                       "(reuse cached change-month capture when it is already before "
                       "the cutoff; fetch only the missing anchors)",
        "full_section_cap": full_cap,
        "requests_this_run": requests,
        "prior_cutoff_status_breakdown": bd["prior_cutoff_status"],
        "prior_cutoff_anchor_breakdown": bd["prior_cutoff_anchor"],
    }
    tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, meta_path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m stage1.tools.reanchor_dailymed_cutoff",
        description="Anchor the FDA history-cache 'before' to the cutoff (2026-02-01).")
    parser.add_argument("--cache", required=True, help="fda_dailymed_history.jsonl to augment in place")
    parser.add_argument("--cutoff", default="2026-02-01", help="cutoff YYYY-MM-DD")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP)
    parser.add_argument("--tries", type=int, default=DEFAULT_TRIES)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--full-cap", type=int, default=FULL_SECTION_CAP)
    args = parser.parse_args(argv)

    cache_path = Path(args.cache)
    if not cache_path.is_file():
        print(f"error: cache not found: {cache_path}", file=sys.stderr)
        return 2
    cutoff = date.fromisoformat(args.cutoff)
    rows = _load_rows(cache_path)
    requests, transient = reanchor(
        rows, cutoff, cache_path.parent, resume=args.resume, sleep=args.sleep,
        tries=args.tries, timeout=args.timeout, full_cap=args.full_cap)
    write_cache(rows, cache_path)
    meta_path = cache_path.with_name("fda_dailymed_history.meta.json")
    refresh_sidecar(meta_path, cache_path, rows, cutoff, requests, args.full_cap)
    bd = anchor_breakdown(rows)
    print(f"[reanchor] wrote {len(rows)} rows -> {cache_path}", file=sys.stderr)
    print(f"[reanchor] prior_cutoff_status: {bd['prior_cutoff_status']}", file=sys.stderr)
    print(f"[reanchor] prior_cutoff_anchor: {bd['prior_cutoff_anchor']}", file=sys.stderr)
    if transient:
        print(f"[reanchor] {transient} transient row(s) -> re-run with --resume", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
