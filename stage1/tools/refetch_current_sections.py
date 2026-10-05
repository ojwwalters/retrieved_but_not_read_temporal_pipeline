#!/usr/bin/env python3
"""refetch_current_sections.py -- freeze the FULL CURRENT DailyMed section.

Owner decision 2026-07-24 ("Option B"). The FDA delta is DELTA-PRIMARY: it
compares the cutoff-anchored prior FULL section (``prior_cutoff_full_section``)
against the CURRENT content of the changed section. Until now the "current" side
was the openFDA RMC ``section_text``, which openFDA HARD-CAPS at exactly 1200
chars: a FULL prior was diffed against a TRUNCATED current, so genuinely-new
content BEYOND char 1200 of the current section was silently MISSED from the
delta (a false negative the prior-prefix guard cannot recover — it only removes
the truncation FALSE POSITIVES). openFDA/the RMC feed truncate at 1200; the
CURRENT DailyMed label page serves the WHOLE section, so this pass fetches it and
freezes the complete current section into the history cache.

WHAT IT ADDS, per history row (grouped by set_id so each page is fetched once):

    current_full_section        the FULL current section body (whole SPL section,
                                extracted with the raised full cap; the honest
                                recorded "after" value the delta and after.raw use)
    current_full_status         ok | section_not_found_in_snapshot |
                                no_current_label | fetch_error |
                                out_of_window_not_fetched
    current_full_extract_method toc_anchor | none
    current_snapshot_url        the stable public CURRENT DailyMed label URL
    current_full_source         "dailymed_current"

It ALSO COMPLETES THE PRIOR side where the frozen ``prior_cutoff_full_section``
was captured at the OLD 6000-char cap (the reanchor pass reused 6000-capped
change-month captures offline for ~half the set_ids). A whole-current-vs-whole-
prior delta with a TRUNCATED prior explodes with FALSE POSITIVES — every current
sentence beyond char 6000 of the prior looks "new" (verified: trametinib's whole
long-standing ILD/Ocular body flagged as new only because §5.7 sat past the prior
cap). So the delta is only complete, and "trametinib/canagliflozin/ketamine clean"
achievable, when BOTH sides are full. For every in-window row whose prior is at
the old cap AND carries a ``prior_cutoff_snapshot_url`` (the frozen Wayback
capture), this pass RE-EXTRACTS that immutable capture at the raised cap and
replaces ``prior_cutoff_full_section`` in place (recording
``prior_cutoff_full_recut``). Wayback ``id_`` captures are immutable, so the recut
is deterministic and content-equivalent (a superset of the truncated body).

SELECTION: only the set_ids with a change fully inside [cutoff, asof] are
fetched (the owner-scoped current-section authorization). An out-of-window row is
a temporal exclusion regardless of its delta, so it is marked
``out_of_window_not_fetched`` and keeps its RMC ``section_text`` fallback; a
fetched page's out-of-window sibling rows are still extracted for free.

Networked (CURRENT DailyMed + the immutable Wayback prior captures) ONLY for the
in-window set_ids; polite pacing, resumable via a scratch .part OUTSIDE the cache.
The DERIVE stays a pure OFFLINE function of the frozen cache: this tool freezes
the current section (a one-time harvest freeze — the live label drifts, so it is
fetched once and frozen, then read offline), and stage1.adapters.fda computes the
content delta deterministically from it. FDA is non-back-datable and validates by
content-equivalence, not byte-identity.

Writes the augmented cache in place (sorted, atomic) and refreshes the .meta.json
sidecar (cache_sha1 + a deterministic ``current_sections`` provenance block; NO
fresh wall-clock so the frozen input reproduces)."""

from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, OrderedDict
from datetime import date
from pathlib import Path

from stage1.config import require_contact_email
from stage1.tools.fetch_dailymed_history import (
    FULL_SECTION_CAP,
    SNIPPET_CAP,
    TOOL_VERSION as FETCH_TOOL_VERSION,
    USER_AGENT,
    _decompress,
    _Gone,
    change_month_first_day,
    extract_section,
    sha1_file,
    row_key,
)

TOOL_VERSION = "refetch_current_sections:v1"

# The stable public CURRENT DailyMed label page (the live 'after' source).
DAILYMED_CURRENT = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={set_id}"
CURRENT_SOURCE = "dailymed_current"

# A prior whose frozen body is at least this long was captured at the OLD 6000
# full-section cap (the reanchor cache_reuse path) and may be TRUNCATED — recut it
# at the raised cap from its immutable Wayback capture so the delta is complete.
OLD_FULL_CAP = SNIPPET_CAP * 5  # 6000 (the pre-2026-07-24 fetch_dailymed_history cap)

# Current-section field names added to every history row.
CF_FULL = "current_full_section"
CF_STATUS = "current_full_status"
CF_METHOD = "current_full_extract_method"
CF_URL = "current_snapshot_url"
CF_SOURCE = "current_full_source"
# Prior-recut marker (the prior body is UPGRADED in place; this records it).
PF_FULL = "prior_cutoff_full_section"
PF_RECUT = "prior_cutoff_full_recut"

# Statuses that mean "we are done with this row's current section" (skip on resume).
TERMINAL_CURRENT = frozenset(
    {"ok", "section_not_found_in_snapshot", "no_current_label", "out_of_window_not_fetched"}
)

DEFAULT_SLEEP = 1.5
DEFAULT_TRIES = 6
DEFAULT_TIMEOUT = 120.0


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def _fully_in_window(change_date: str, cutoff: date, asof: date) -> bool:
    """True iff the MM/YYYY change month lies fully inside [cutoff, asof]. An
    unparseable date is treated as out-of-window (never fetched; it will be a
    temporal exclusion in the derive). Pure and total."""
    try:
        compact = change_month_first_day(change_date)  # 'YYYYMM01'
    except ValueError:
        return False
    year, month = int(compact[:4]), int(compact[4:6])
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return first >= cutoff and last <= asof


def current_url(set_id: str) -> str:
    return DAILYMED_CURRENT.format(set_id=set_id)


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


def _set_current(row, *, full, status, method, url):
    row[CF_FULL] = full
    row[CF_STATUS] = status
    row[CF_METHOD] = method
    row[CF_URL] = url
    row[CF_SOURCE] = CURRENT_SOURCE


# --------------------------------------------------------------------------- #
# Network (retry/backoff). Only the fetch path reaches these.
# --------------------------------------------------------------------------- #
def _fetch_url(url: str, *, tries: int, timeout: float):
    """(html, error): a live GET with retry/backoff. Raises _Gone on 404/410 (the
    label/capture is genuinely gone); returns (None, reason) only when every retry
    timed out/5xx'd (transient)."""
    require_contact_email()
    last = "no attempt"
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                encoding = (resp.headers.get("Content-Encoding") or "").lower()
            return _decompress(raw, encoding).decode("utf-8", "replace"), None
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                raise _Gone(f"HTTP {exc.code} for {url}")
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        wait = min(60.0, 2.0 * (2 ** (attempt - 1)))
        print(f"    [get {last}] retry in {wait:.0f}s ({attempt}/{tries})",
              file=sys.stderr, flush=True)
        time.sleep(wait)
    return None, f"unreachable after {tries} tries: {last}"


def fetch_group(sid, grp, cutoff, asof, *, sleep, tries, timeout, full_cap):
    """Fetch the CURRENT DailyMed page once and extract every row's full section;
    recut a truncated prior from its immutable Wayback capture. Mutates the rows.
    Returns (n_requests, transient_count, recut_count)."""
    n_req = 0
    transient = 0
    recuts = 0

    # 1) CURRENT section (one live page fetch for the whole set_id).
    url = current_url(sid)
    try:
        html, err = _fetch_url(url, tries=tries, timeout=timeout)
        n_req += 1
    except _Gone:
        for r in grp:
            _set_current(r, full=None, status="no_current_label", method="none", url=url)
        return n_req + 1, transient, recuts
    time.sleep(sleep)
    if html is None:
        for r in grp:
            _set_current(r, full=None, status="fetch_error", method="none", url=url)
        return n_req, len(grp), recuts
    for r in grp:
        _, full, method, status = extract_section(
            html, r.get("section", ""), r.get("section_num", ""), full_cap=full_cap)
        _set_current(r, full=(full if status == "ok" else None),
                     status=status, method=method, url=url)

    # 2) PRIOR recut (only the truncated ones; one fetch per unique capture URL).
    prior_html_cache: dict = {}
    for r in grp:
        if not _fully_in_window(r.get("change_date", ""), cutoff, asof):
            continue
        if r.get("prior_cutoff_status") != "ok":
            continue
        body = r.get(PF_FULL) or ""
        if len(body) < OLD_FULL_CAP:
            continue  # already complete (never hit the old cap)
        purl = r.get("prior_cutoff_snapshot_url")
        if not purl:
            continue
        if purl not in prior_html_cache:
            try:
                phtml, perr = _fetch_url(purl, tries=tries, timeout=timeout)
                n_req += 1
            except _Gone:
                phtml = None
            time.sleep(sleep)
            prior_html_cache[purl] = phtml
        phtml = prior_html_cache[purl]
        if phtml is None:
            continue  # keep the (truncated) prior as a conservative fallback
        _, pfull, _pm, pstatus = extract_section(
            phtml, r.get("section", ""), r.get("section_num", ""), full_cap=full_cap)
        if pstatus == "ok" and pfull:
            r[PF_FULL] = pfull
            r[PF_RECUT] = True
            recuts += 1
    return n_req, transient, recuts


# --------------------------------------------------------------------------- #
# Resume checkpoint (scratch .part outside the cache)
# --------------------------------------------------------------------------- #
_CHECKPOINT_KEYS = (CF_FULL, CF_STATUS, CF_METHOD, CF_URL, CF_SOURCE, PF_FULL, PF_RECUT)


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


def _apply_checkpoint(row, entry):
    for k in _CHECKPOINT_KEYS:
        if k in entry:
            row[k] = entry[k]


def refetch(rows, cutoff: date, asof: date, out_dir: Path, *, resume: bool,
            sleep: float, tries: int, timeout: float, full_cap: int):
    groups: "OrderedDict[str, list]" = OrderedDict()
    for r in rows:
        groups.setdefault(r["set_id"], []).append(r)

    fetch_sids = [sid for sid, grp in groups.items()
                  if any(_fully_in_window(r.get("change_date", ""), cutoff, asof) for r in grp)]
    skip_sids = [sid for sid in groups if sid not in set(fetch_sids)]
    for sid in skip_sids:
        for r in groups[sid]:
            _set_current(r, full=None, status="out_of_window_not_fetched",
                         method="none", url=current_url(sid))

    print(f"[refetch] {len(groups)} set_id(s): {len(fetch_sids)} in-window to fetch, "
          f"{len(skip_sids)} out-of-window skipped", file=sys.stderr, flush=True)

    scratch = out_dir.parent / (".refetch_scratch_" + out_dir.name)
    scratch.mkdir(parents=True, exist_ok=True)
    part_path = scratch / "refetch.part.jsonl"
    cached = _load_part(part_path) if resume else {}

    transient = 0
    recuts = 0
    requests = 0
    with open(part_path, "a", encoding="utf-8", newline="\n") as part:
        for i, sid in enumerate(fetch_sids, 1):
            grp = groups[sid]
            if resume and all(row_key(r) in cached
                              and cached[row_key(r)].get(CF_STATUS) in TERMINAL_CURRENT
                              for r in grp):
                for r in grp:
                    _apply_checkpoint(r, cached[row_key(r)])
                continue
            n_req, grp_transient, grp_recuts = fetch_group(
                sid, grp, cutoff, asof, sleep=sleep, tries=tries,
                timeout=timeout, full_cap=full_cap)
            requests += n_req
            transient += grp_transient
            recuts += grp_recuts
            for r in grp:
                entry = {k: r.get(k) for k in _CHECKPOINT_KEYS}
                cached[row_key(r)] = entry
                part.write(json.dumps({"key": list(row_key(r)), **entry},
                                      ensure_ascii=False) + "\n")
            part.flush()
            if i % 10 == 0 or i == len(fetch_sids):
                print(f"[refetch]   fetched {i}/{len(fetch_sids)} set_id(s) "
                      f"({requests} request(s), {recuts} prior recut(s))",
                      file=sys.stderr, flush=True)

    if transient == 0:
        try:
            if part_path.exists():
                part_path.unlink()
            if scratch.is_dir() and not any(scratch.iterdir()):
                scratch.rmdir()
        except OSError as exc:
            print(f"[refetch] scratch cleanup skipped: {exc}", file=sys.stderr, flush=True)
    else:
        print(f"[refetch] {transient} row(s) left transient (fetch_error); scratch kept "
              f"at {scratch} for --resume", file=sys.stderr, flush=True)
    return requests, transient, recuts


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def write_cache(rows, out_path: Path):
    rows_sorted = sorted(rows, key=row_key)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows_sorted:
            fh.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
    os.replace(tmp, out_path)


def current_breakdown(rows):
    return {
        "current_full_status": dict(sorted(Counter(r.get(CF_STATUS) for r in rows).items())),
        "prior_recut": int(sum(1 for r in rows if r.get(PF_RECUT))),
    }


def refresh_sidecar(meta_path: Path, cache_path: Path, rows, cutoff: date, asof: date,
                    requests: int, recuts: int, full_cap: int):
    """Update cache_sha1 and add a deterministic ``current_sections`` provenance
    block. No fresh wall-clock (the frozen input must reproduce)."""
    if not meta_path.is_file():
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["cache_sha1"] = sha1_file(cache_path)
    bd = current_breakdown(rows)
    meta["current_sections"] = {
        "tool_version": TOOL_VERSION,
        "fetch_tool_version": FETCH_TOOL_VERSION,
        "cutoff": cutoff.isoformat(),
        "asof": asof.isoformat(),
        "source": CURRENT_SOURCE,
        "rule": "the FULL current SPL section from the live DailyMed label (whole "
                "section, raised full cap) is the recorded 'after'; the delta is "
                "whole-current-vs-whole-prior, both at the raised cap (a truncated "
                "prior is recut from its immutable Wayback capture)",
        "full_section_cap": full_cap,
        "old_full_cap_recut_threshold": OLD_FULL_CAP,
        "requests_this_run": requests,
        "prior_recuts": recuts,
        "current_full_status_breakdown": bd["current_full_status"],
        "prior_recut_count": bd["prior_recut"],
    }
    tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, meta_path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m stage1.tools.refetch_current_sections",
        description="Freeze the FULL current DailyMed section (removing the openFDA "
                    "1200-char cap) so the FDA delta is complete.")
    parser.add_argument("--cache", required=True,
                        help="fda_dailymed_history.jsonl to augment in place")
    parser.add_argument("--cutoff", default="2026-02-01", help="cutoff YYYY-MM-DD")
    parser.add_argument("--asof", default="2026-06-30", help="asof YYYY-MM-DD")
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
    asof = date.fromisoformat(args.asof)
    rows = _load_rows(cache_path)
    requests, transient, recuts = refetch(
        rows, cutoff, asof, cache_path.parent, resume=args.resume, sleep=args.sleep,
        tries=args.tries, timeout=args.timeout, full_cap=args.full_cap)
    write_cache(rows, cache_path)
    meta_path = cache_path.with_name("fda_dailymed_history.meta.json")
    refresh_sidecar(meta_path, cache_path, rows, cutoff, asof, requests, recuts, args.full_cap)
    bd = current_breakdown(rows)
    print(f"[refetch] wrote {len(rows)} rows -> {cache_path}", file=sys.stderr)
    print(f"[refetch] current_full_status: {bd['current_full_status']}", file=sys.stderr)
    print(f"[refetch] prior recuts: {bd['prior_recut']}", file=sys.stderr)
    if transient:
        print(f"[refetch] {transient} transient row(s) -> re-run with --resume", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
