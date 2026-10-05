#!/usr/bin/env python3
"""fetch_dailymed_history.py -- BEFORE/AFTER text for 2026 FDA label changes.

A spike toward a Stage-1 FDA adapter. The RMC harvest (drugs/fda1_rmc_harvest.py
-> drugs/fda_rmc_2026.jsonl) already captured the CURRENT ("after") text of every
2026-changed label section. FDA/openFDA and DailyMed serve ONLY the current label
version -- historical section text is NOT retrievable from them. This tool
recovers the pre-change ("before") text from the Internet Archive's snapshots of
the DailyMed drug page, so downstream code can diff BEFORE vs AFTER deterministically.

WHAT IT DOES, per RMC row (grouped by set_id so each page is fetched once):

  1. Ask Wayback's CDX API for snapshots of the DailyMed drugInfo page
     (dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=<set_id>).
  2. Select the BEST pre-change snapshot: the LATEST statuscode-200 text/html
     capture whose timestamp is on or after 2023-01-01 and strictly BEFORE the
     first day of the change month (change_date "MM/YYYY" -> "YYYYMM01").
     If none exists -> status "no_pre_change_snapshot".
  3. Fetch that snapshot's RAW archived HTML (the Wayback ``id_`` form, which
     omits the Wayback banner/rewrites: /web/<ts>id_/<original>).
  4. Extract the target section (row['section'] / row['section_num']) from the
     archived HTML deterministically (see extract_section) and normalize it the
     SAME way fda1_rmc_harvest.py normalized the "after" text (tag-strip ->
     collapse whitespace -> subsection_text() narrowing -> 1200-char cap), so
     BEFORE and AFTER are comparably normalized. If the section can't be located
     -> status "section_not_found_in_snapshot" (the row is KEPT, never faked).

OUTPUT (deterministic, sorted by (set_id, section, section_num); no wall-clock in
the jsonl itself):

  * cache  (default stage1/cache/fda_dailymed_history.jsonl): one row per RMC row:
      {set_id, generic, brand, section, section_num, change_date,
       before_text, before_full_section, before_snapshot_ts, before_snapshot_url,
       after_text (= the RMC row's section_text), snapshot_count,
       extract_method, status}
  * sidecar (default stage1/cache/fda_dailymed_history.meta.json): retrieval
    time, endpoints, params, input sha1, counts, and the status breakdown.

RESUMABLE: rows whose status is terminal (ok / no_pre_change_snapshot /
section_not_found_in_snapshot) are skipped on restart; rows left in a transient
state (cdx_error / snapshot_fetch_error -- e.g. a Wayback timeout, which is NOT a
true miss) are refetched. Progress is appended to <cache>.part.jsonl and the
sorted final cache is written atomically at the end.

POLITE + ROBUST: descriptive User-Agent, generous timeouts, retry-with-backoff on
timeouts/5xx, ~1-2s between calls. The offline pipeline never runs this tool -- it
only READS the cache -- so a persistent transient error just leaves the row to be
retried on a later run rather than crashing anything.

Run from the repo root (network access to web.archive.org required):

    python3 -m stage1.tools.fetch_dailymed_history \
        --rmc drugs/fda_rmc_2026.jsonl
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html as htmllib
import json
import os
import zlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from stage1.config import require_contact_email, user_agent

TOOL_VERSION = "fetch_dailymed_history:v1"
CDX_ENDPOINT = "http://web.archive.org/cdx/search/cdx"
WAYBACK_BASE = "http://web.archive.org/web"
DAILYMED_PAGE = "dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={set_id}"
USER_AGENT = user_agent()
# Oldest snapshot we will consider (the study is about post-2023 label states).
MIN_SNAPSHOT_DAY = "20230101"
SNIPPET_CAP = 1200                 # mirror fda1_rmc_harvest.py's [:1200]
# The FULL prior-section body is kept for auditing/diffing AND for the
# content-based cutoff-anchored DELTA (stage1.adapters.fda). The cap was raised
# from 6000 to 40000 (owner decision 2026-07-24 (1)): a real prior sentence must
# never be lost to truncation and thereby false-flagged as NEW content by the
# delta. 40000 comfortably holds an entire Warnings-and-Precautions body; the
# raise never truncates fewer characters than before (monotonic), so no existing
# capture loses text.
FULL_SECTION_CAP = 40000

_DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
CACHE_NAME = "fda_dailymed_history.jsonl"
META_NAME = "fda_dailymed_history.meta.json"

# Statuses that mean "we are done with this row" (skip on resume).
TERMINAL_STATUSES = frozenset(
    {"ok", "no_pre_change_snapshot", "section_not_found_in_snapshot"}
)

# ---------------------------------------------------------------------------
# Pure helpers (no network) -- these are what the unit tests exercise.
# ---------------------------------------------------------------------------

_TOC_RE = re.compile(r'<a class="toc" href="#(section-[0-9.]+)">(.*?)</a>', re.S)
_NAME_RE = re.compile(r'<a name="(section-\d+)"[^>]*>')  # top-level content anchors
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_CHANGE_DATE_RE = re.compile(r"^\s*(\d{1,2})\s*/\s*(\d{4})\s*$")

# RMC section label -> canonical (uppercased) SPL section title used in the
# DailyMed table-of-contents. Mirrors fda1_rmc_harvest.SECTION_FIELDS' coverage.
SECTION_TITLE = {
    "indications and usage": "INDICATIONS AND USAGE",
    "indications": "INDICATIONS AND USAGE",
    "dosage and administration": "DOSAGE AND ADMINISTRATION",
    "contraindications": "CONTRAINDICATIONS",
    "warnings and precautions": "WARNINGS AND PRECAUTIONS",
    "warnings": "WARNINGS AND PRECAUTIONS",
    "adverse reactions": "ADVERSE REACTIONS",
    "drug interactions": "DRUG INTERACTIONS",
    "use in specific populations": "USE IN SPECIFIC POPULATIONS",
}


def change_month_first_day(change_date: str) -> str:
    """'MM/YYYY' -> 'YYYYMM01' (the first day of the change month). Any snapshot
    on/after this instant is treated as 'after' the change. Raises ValueError on
    a malformed value (never silently guesses a date)."""
    m = _CHANGE_DATE_RE.match(change_date or "")
    if not m:
        raise ValueError(f"unparseable change_date {change_date!r} (want MM/YYYY)")
    month = int(m.group(1))
    year = m.group(2)
    if not 1 <= month <= 12:
        raise ValueError(f"month out of range in change_date {change_date!r}")
    return f"{year}{month:02d}01"


def strip_tags(fragment: str) -> str:
    """HTML fragment -> normalized plain text: drop <script>/<style>, turn every
    remaining tag into a space, unescape entities, then collapse runs of
    whitespace to single spaces (the exact whitespace rule fda1_rmc_harvest.py
    applies to the openFDA 'after' text), so BEFORE and AFTER normalize alike."""
    x = _SCRIPT_STYLE_RE.sub(" ", fragment)
    x = _TAG_RE.sub(" ", x)
    x = htmllib.unescape(x)
    return re.sub(r"\s+", " ", x).strip()


def subsection_text(field_text: str, num: str) -> str:
    """Extract subsection `num` (e.g. '5.6') text up to the next sibling, else ''.

    Copied verbatim (behaviourally) from fda1_rmc_harvest.subsection_text so the
    BEFORE text is narrowed to the same subsection the AFTER text was."""
    if not num:
        return ""
    num = num.strip().split(",")[0].strip()  # first number if a list
    if not re.match(r"^\d+\.\d+$", num):
        return ""
    nxt = re.escape(num.rsplit(".", 1)[0]) + r"\.\d+"
    m = re.search(rf"(\b{re.escape(num)}\b.*?)(?:\b{nxt}\b|\Z)", field_text, re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def _snippet(field_text: str, num: str) -> str:
    """Mirror fda1_rmc_harvest: use the subsection text when it is real prose
    (>60 chars), otherwise the whole section body, capped at 1200 chars."""
    sub = subsection_text(field_text, num)
    return (sub if len(sub) > 60 else field_text)[:SNIPPET_CAP]


def parse_toc(html: str):
    """Parse the DailyMed table-of-contents into ordered TOP-LEVEL entries:
    [{'anchor': 'section-5', 'title': 'WARNINGS AND PRECAUTIONS', 'is_boxed': bool}].

    The TOC lists both sections and subsections; only top-level entries (anchor
    'section-N' with no dot) are kept. A boxed warning appears as a numberless
    entry whose title starts 'WARNING' (it shares 'section-1' with Indications)."""
    out = []
    for m in _TOC_RE.finditer(html):
        anchor = m.group(1)
        if not re.fullmatch(r"section-\d+", anchor):
            continue
        label = strip_tags(m.group(2))
        nm = re.match(r"^(\d+)\s+(.*)$", label)
        if nm:
            out.append({"anchor": anchor, "title": nm.group(2).upper().strip(),
                        "is_boxed": False})
        else:
            title = label.upper().strip()
            out.append({"anchor": anchor, "title": title,
                        "is_boxed": title.startswith("WARNING")})
    return out


def top_level_anchors(html: str):
    """Ordered top-level content anchors: [(name, tag_start, tag_end), ...] for
    every <a name="section-N"> (no dot). These delimit the full-text section
    bodies; a body runs from one anchor to the next anchor in document order."""
    return [(m.group(1), m.start(), m.end()) for m in _NAME_RE.finditer(html)]


def _body_is_boxed(body: str) -> bool:
    """A section body belongs to the boxed warning iff its first word is WARNING."""
    return re.sub(r"^[^A-Za-z]+", "", body)[:20].upper().startswith("WARNING")


def extract_section(html: str, section: str, section_num: str,
                    full_cap: int = FULL_SECTION_CAP):
    """Locate `section` in an archived DailyMed page and return
    (before_text, full_section_text, extract_method, status).

    ``full_cap`` bounds the returned full-section body (default FULL_SECTION_CAP,
    now 40000). The cutoff-anchor re-anchor pass passes the raised cap so the
    FULL prior section is captured (no real prior sentence lost to truncation).

    status is 'ok' or 'section_not_found_in_snapshot'. Strategy:
      * map the RMC section name to its canonical SPL title, find that title in
        the TOC, and read the DOM anchor the TOC links to (e.g. 'section-5');
      * slice the matching <a name="section-5"> content body up to the next
        top-level anchor, tag-strip it, then narrow to the subsection like the
        harvest did. The TOC->anchor indirection means we never assume the DOM
        index equals the printed section number (they diverge above section 8).
      * boxed warning: matched by the TOC's numberless 'WARNING...' entry and, at
        the content level, by the body that begins with 'WARNING' (it shares the
        'section-1' anchor with Indications and Usage)."""
    toc = parse_toc(html)
    anchors = top_level_anchors(html)
    if not anchors or not toc:
        return None, None, "none", "section_not_found_in_snapshot"

    sec_l = (section or "").strip().lower()
    want_boxed = sec_l in ("boxed warning",)
    target_anchor = None
    if want_boxed:
        for e in toc:
            if e["is_boxed"]:
                target_anchor = e["anchor"]
                break
    else:
        want_title = SECTION_TITLE.get(sec_l)
        if want_title:
            for e in toc:  # exact canonical-title match first
                if not e["is_boxed"] and e["title"] == want_title:
                    target_anchor = e["anchor"]
                    break
            if target_anchor is None:  # then a containment fallback
                for e in toc:
                    if not e["is_boxed"] and want_title in e["title"]:
                        target_anchor = e["anchor"]
                        break
    if target_anchor is None:
        return None, None, "none", "section_not_found_in_snapshot"

    starts = sorted(s for (_, s, _) in anchors)
    cands = [(s, e) for (nm, s, e) in anchors if nm == target_anchor]
    if not cands:
        return None, None, "none", "section_not_found_in_snapshot"

    chosen_body = None
    for (s, e) in cands:
        nxt = next((p for p in starts if p > s), len(html))
        body = strip_tags(html[e:nxt])
        is_boxed_body = _body_is_boxed(body)
        # Disambiguate the shared 'section-1' anchor: the boxed target wants the
        # WARNING body, everything else wants the non-WARNING body.
        if want_boxed and is_boxed_body:
            chosen_body = body
            break
        if not want_boxed and not is_boxed_body:
            chosen_body = body
            break
    if chosen_body is None:  # single-candidate sections fall straight through
        s, e = cands[0]
        nxt = next((p for p in starts if p > s), len(html))
        chosen_body = strip_tags(html[e:nxt])

    if not chosen_body:
        return None, None, "toc_anchor", "section_not_found_in_snapshot"

    before = _snippet(chosen_body, section_num)
    return before, chosen_body[:full_cap], "toc_anchor", "ok"


def parse_cdx(data):
    """Wayback CDX JSON (list-of-lists with a header row) -> list of dicts. An
    empty payload, or one that is only the header, yields []. Field order is read
    from the header row rather than assumed."""
    if not isinstance(data, list) or len(data) < 2:
        return []
    header = data[0]
    idx = {name: i for i, name in enumerate(header)}
    need = ("timestamp", "original", "statuscode", "mimetype")
    if not all(k in idx for k in need):
        return []
    rows = []
    for raw in data[1:]:
        if not isinstance(raw, list) or len(raw) < len(header):
            continue
        rows.append({
            "timestamp": raw[idx["timestamp"]],
            "original": raw[idx["original"]],
            "statuscode": raw[idx["statuscode"]],
            "mimetype": raw[idx["mimetype"]],
        })
    return rows


def pre_change_candidates(cdx_rows, change_first_day: str,
                          min_day: str = MIN_SNAPSHOT_DAY):
    """The usable pre-change snapshots, LATEST FIRST: statuscode 200, text/html,
    timestamp in [min_day 00:00:00, change_first_day 00:00:00). Deterministic:
    sorted by timestamp descending (ties broken by original URL) so the caller
    always tries the newest capture first."""
    lo = min_day + "000000"
    hi = change_first_day + "000000"
    good = []
    for r in cdx_rows:
        ts = str(r.get("timestamp", ""))
        if len(ts) != 14 or not ts.isdigit():
            continue
        if str(r.get("statuscode")) != "200":
            continue
        if not str(r.get("mimetype", "")).lower().startswith("text/html"):
            continue
        if not (lo <= ts < hi):
            continue
        good.append(r)
    good.sort(key=lambda r: (r["timestamp"], r["original"]), reverse=True)
    return good


def snapshot_raw_url(timestamp: str, original: str) -> str:
    """The Wayback raw ('id_') URL that returns the archived page without the
    Wayback banner/link-rewriting."""
    return f"{WAYBACK_BASE}/{timestamp}id_/{original}"


# ---------------------------------------------------------------------------
# Location-independent identifiers for the sidecar (never leak local paths).
# ---------------------------------------------------------------------------

def rmc_identifier(as_given: str) -> str:
    """Repo-relative --rmc paths are kept verbatim; an absolute path (a
    session-local scratchpad, a machine-specific layout) is reduced to its bare
    filename so the sidecar stays byte-comparable across machines."""
    return os.path.basename(as_given) if os.path.isabs(as_given) else as_given


def cache_identifier(out_path: Path, as_given: str) -> str:
    """The cache written to the package-default dir is identified by its
    repo-relative id; any other destination keeps the CLI path verbatim."""
    try:
        resolved = out_path.resolve()
    except OSError:
        return as_given
    if resolved.parent == _DEFAULT_CACHE_DIR:
        return f"stage1/cache/{resolved.name}"
    return as_given


# ---------------------------------------------------------------------------
# Network (retry/backoff). Only main() reaches these.
# ---------------------------------------------------------------------------

class _Gone(Exception):
    """A snapshot the archive lists but cannot serve (404/410) -- try an older one."""


def _http_bytes(url: str, timeout: float) -> bytes:
    require_contact_email()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
    return _decompress(raw, encoding)


def _decompress(raw: bytes, encoding: str) -> bytes:
    """Wayback's raw ('id_') form replays the ORIGINAL response bytes, so some
    captures arrive gzip/deflate-encoded even though urllib does not decode them.
    Decode by declared Content-Encoding, and defensively by magic bytes (gzip
    starts 0x1f8b) in case the header is absent. On any failure, return the raw
    bytes unchanged rather than crash."""
    try:
        if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
            return gzip.decompress(raw)
        if encoding == "deflate":
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)  # raw deflate
    except (OSError, zlib.error, EOFError):
        return raw
    return raw


def fetch_cdx(set_id: str, change_first_day: str, *, sleep: float,
              tries: int, timeout: float):
    """(rows, error): query CDX for the DailyMed page, statuscode-200 only, in
    [MIN_SNAPSHOT_DAY, change_first_day]. Returns (parsed_rows, None) on success
    (rows may be []), or (None, 'reason') if every retry timed out/5xx'd -- a
    transient failure the caller records for a later retry, NOT a true miss."""
    page = DAILYMED_PAGE.format(set_id=set_id)
    query = urllib.parse.urlencode({
        "url": page,
        "output": "json",
        "from": MIN_SNAPSHOT_DAY,
        "to": change_first_day,
        "filter": "statuscode:200",
    })
    url = f"{CDX_ENDPOINT}?{query}"
    last = "no attempt"
    for attempt in range(1, tries + 1):
        try:
            raw = _http_bytes(url, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):     # CDX: no captures for this URL
                return [], None
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                return [], None
            try:
                data = json.loads(text)
            except ValueError as exc:
                last = f"bad CDX JSON: {exc}"
            else:
                return parse_cdx(data), None
        wait = min(60.0, 2.0 * (2 ** (attempt - 1)))
        print(f"    [cdx {set_id[:8]} {last}] retry in {wait:.0f}s "
              f"({attempt}/{tries})", file=sys.stderr, flush=True)
        time.sleep(wait)
    return None, f"cdx unreachable after {tries} tries: {last}"


def fetch_snapshot(original: str, timestamp: str, *, tries: int, timeout: float):
    """(html, error): the raw archived page. Raises _Gone on 404/410 so the
    caller can fall back to an older capture; returns (None, reason) only when
    every retry timed out/5xx'd (transient)."""
    url = snapshot_raw_url(timestamp, original)
    last = "no attempt"
    for attempt in range(1, tries + 1):
        try:
            raw = _http_bytes(url, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                raise _Gone(f"HTTP {exc.code} for {timestamp}")
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            return raw.decode("utf-8", "replace"), None
        wait = min(60.0, 2.0 * (2 ** (attempt - 1)))
        print(f"    [snap {timestamp} {last}] retry in {wait:.0f}s "
              f"({attempt}/{tries})", file=sys.stderr, flush=True)
        time.sleep(wait)
    return None, f"snapshot unreachable after {tries} tries: {last}"


def _fetch_best_html(candidates, *, sleep: float, tries: int, timeout: float):
    """Walk pre-change candidates newest-first; return (html, ts, original,
    error). Skips captures the archive 404s (tries the next older one); a
    transient failure on all reachable captures yields (None, None, None, reason)."""
    transient = None
    for r in candidates:
        try:
            html, err = fetch_snapshot(r["original"], r["timestamp"],
                                       tries=tries, timeout=timeout)
        except _Gone as exc:
            print(f"    [snap gone] {exc}; trying older", file=sys.stderr, flush=True)
            time.sleep(sleep)
            continue
        time.sleep(sleep)
        if html is not None:
            return html, r["timestamp"], r["original"], None
        transient = err  # remember and try an older capture as a courtesy
    return None, None, None, transient or "all listed captures were 404/410"


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def row_key(row: dict):
    return (row["set_id"], row["section"], row["section_num"])


def load_rmc(path: Path):
    """(ordered set_id -> list of rmc rows, load_errors). Grouping by set_id lets
    one page fetch serve every changed section of a drug."""
    groups: "OrderedDict[str, list]" = OrderedDict()
    errors = []
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                errors.append({"line": line_no, "error": f"unparseable JSON: {exc}"})
                continue
            sid = row.get("set_id") if isinstance(row, dict) else None
            if not isinstance(sid, str) or not sid:
                errors.append({"line": line_no, "error": f"missing set_id: {sid!r}"})
                continue
            groups.setdefault(sid, []).append(row)
    return groups, errors


def load_cached(path: Path):
    cached = {}
    if not path.is_file():
        return cached
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue  # torn last line of an interrupted append is expected
            if isinstance(row, dict) and row.get("set_id") and "section" in row:
                cached[row_key(row)] = row
    return cached


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_cache_row(rmc_row, *, before_text, before_full, snap_ts, snap_url,
                   snapshot_count, extract_method, status):
    return {
        "set_id": rmc_row["set_id"],
        "generic": rmc_row.get("generic", ""),
        "brand": rmc_row.get("brand", ""),
        "section": rmc_row.get("section", ""),
        "section_num": rmc_row.get("section_num", ""),
        "change_date": rmc_row.get("change_date", ""),
        "before_text": before_text,
        "before_full_section": before_full,
        "before_snapshot_ts": snap_ts,
        "before_snapshot_url": snap_url,
        "after_text": rmc_row.get("section_text", ""),
        "snapshot_count": snapshot_count,
        "extract_method": extract_method,
        "status": status,
    }


def process_group(set_id, rmc_rows, *, sleep, tries, timeout):
    """Fetch the page once, extract every changed section. Returns
    (cache_rows, n_network_requests).

    All sections of a drug share ONE CDX call and ONE snapshot fetch (that is the
    point of grouping). On a CDX or snapshot transient failure, every section row
    gets that transient status (so the whole group is retried next run).
    change_date parse errors are per-row (a bad date does not sink its siblings)."""
    # Change months can (rarely) differ per section; take the EARLIEST so the
    # snapshot predates every change on the page. Rows with a bad date are
    # emitted individually as 'bad_change_date'.
    firsts, bad_rows = [], []
    for r in rmc_rows:
        try:
            firsts.append(change_month_first_day(r.get("change_date", "")))
        except ValueError:
            bad_rows.append(r)
    out = [make_cache_row(r, before_text=None, before_full=None, snap_ts=None,
                          snap_url=None, snapshot_count=0, extract_method="none",
                          status="bad_change_date") for r in bad_rows]
    good_rows = [r for r in rmc_rows if r not in bad_rows]
    if not good_rows:
        return out, 0
    change_first = min(firsts)

    cdx_rows, cdx_err = fetch_cdx(set_id, change_first, sleep=sleep,
                                  tries=tries, timeout=timeout)
    time.sleep(sleep)
    if cdx_err is not None:
        return out + [make_cache_row(r, before_text=None, before_full=None,
                                     snap_ts=None, snap_url=None, snapshot_count=0,
                                     extract_method="none", status="cdx_error")
                      for r in good_rows], 1

    candidates = pre_change_candidates(cdx_rows, change_first)
    n_cand = len(candidates)
    if not candidates:
        return out + [make_cache_row(r, before_text=None, before_full=None,
                                     snap_ts=None, snap_url=None,
                                     snapshot_count=0, extract_method="none",
                                     status="no_pre_change_snapshot")
                      for r in good_rows], 1

    html, snap_ts, snap_orig, snap_err = _fetch_best_html(
        candidates, sleep=sleep, tries=tries, timeout=timeout)
    if html is None:
        return out + [make_cache_row(r, before_text=None, before_full=None,
                                     snap_ts=None, snap_url=None,
                                     snapshot_count=n_cand, extract_method="none",
                                     status="snapshot_fetch_error")
                      for r in good_rows], 2

    snap_url = snapshot_raw_url(snap_ts, snap_orig)
    for r in good_rows:
        before, full, method, status = extract_section(
            html, r.get("section", ""), r.get("section_num", ""))
        out.append(make_cache_row(
            r, before_text=before, before_full=full, snap_ts=snap_ts,
            snap_url=snap_url, snapshot_count=n_cand, extract_method=method,
            status=status))
    return out, 2  # 1 CDX + 1 snapshot (ignoring any 404-skip retries)


def build_meta(args, rmc_path, groups, load_errors, final_rows, n_requests):
    status_counts = Counter(r["status"] for r in final_rows.values())
    return {
        "tool_version": TOOL_VERSION,
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cdx_endpoint": CDX_ENDPOINT,
        "wayback_base": WAYBACK_BASE,
        "user_agent": USER_AGENT,
        "params": {
            "min_snapshot_day": MIN_SNAPSHOT_DAY,
            "snapshot_selection": "latest statuscode-200 text/html capture "
                                  "before the change month's first day",
            "snippet_cap": SNIPPET_CAP,
            "full_section_cap": FULL_SECTION_CAP,
            "sleep": args.sleep,
            "tries": args.tries,
            "timeout": args.timeout,
        },
        "rmc_file": rmc_identifier(args.rmc),
        "rmc_sha1": sha1_file(rmc_path),
        "rmc_load_errors": load_errors,
        "counts": {
            "rmc_rows": sum(len(v) for v in groups.values()),
            "set_ids": len(groups),
            "cached_rows": len(final_rows),
            "requests_this_run": n_requests,
        },
        "status_breakdown": dict(sorted(status_counts.items())),
        "cache_file": cache_identifier(Path(args.out_dir) / CACHE_NAME,
                                       str(Path(args.out_dir) / CACHE_NAME)),
        "cache_sha1": None,  # filled in after the cache is written
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m stage1.tools.fetch_dailymed_history",
        description="Recover BEFORE/AFTER FDA label section text via the Internet Archive.",
    )
    parser.add_argument("--rmc", default="../0_prior_work/drugs/fda_rmc_2026.jsonl",
                        help="RMC jsonl (one row per changed drug-section)")
    parser.add_argument("--out-dir", default=str(_DEFAULT_CACHE_DIR),
                        help="output directory (default stage1/cache)")
    parser.add_argument("--sample", default="",
                        help="comma-separated set_ids to fetch (testing)")
    parser.add_argument("--limit", type=int, default=0,
                        help="process at most N set_ids this run (0 = all)")
    parser.add_argument("--sleep", type=float, default=1.5,
                        help="seconds between network calls (politeness)")
    parser.add_argument("--tries", type=int, default=6,
                        help="retry attempts per CDX/snapshot request")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="per-request timeout in seconds")
    parser.add_argument("--print-diffs", action="store_true",
                        help="print a BEFORE/AFTER diff for each processed row")
    args = parser.parse_args(argv)

    rmc_path = Path(args.rmc)
    if not rmc_path.is_file():
        print(f"error: rmc file not found: {rmc_path}", file=sys.stderr)
        return 2

    groups, load_errors = load_rmc(rmc_path)
    for err in load_errors:
        print(f"[rmc] line {err['line']}: {err['error']}", file=sys.stderr)

    sample = {s.strip() for s in args.sample.split(",") if s.strip()}
    if sample:
        missing = sorted(sample - set(groups))
        for sid in missing:
            print(f"[warn] --sample set_id not in rmc: {sid}", file=sys.stderr)
        groups = OrderedDict((k, v) for k, v in groups.items() if k in sample)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / CACHE_NAME
    meta_path = out_dir / META_NAME
    part_path = out_path.with_suffix(out_path.suffix + ".part")

    cached = load_cached(out_path)
    cached.update(load_cached(part_path))

    # A group is done iff EVERY one of its section rows has a terminal status.
    def group_done(sid):
        return all(
            cached.get((sid, r["section"], r["section_num"]), {}).get("status")
            in TERMINAL_STATUSES
            for r in groups[sid]
        )

    todo = [sid for sid in groups if not group_done(sid)]
    if args.limit and args.limit > 0:
        todo = todo[:args.limit]
    print(f"[fetch] {len(groups)} set_ids ({sum(len(v) for v in groups.values())} "
          f"rows); {len(groups) - len([s for s in groups if not group_done(s)])} "
          f"already done; processing {len(todo)} this run", file=sys.stderr, flush=True)

    n_requests = 0
    produced = {}
    with open(part_path, "a", encoding="utf-8", newline="\n") as part:
        for i, sid in enumerate(todo, 1):
            rows, n_req = process_group(sid, groups[sid], sleep=args.sleep,
                                        tries=args.tries, timeout=args.timeout)
            n_requests += n_req
            for r in rows:
                produced[row_key(r)] = r
                cached[row_key(r)] = r
                part.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
                if args.print_diffs:
                    _print_diff(r)
            part.flush()
            print(f"[fetch] {i}/{len(todo)} set_ids ({n_requests} requests)",
                  file=sys.stderr, flush=True)

    # Final cache = every row of the CURRENT rmc groups, newest values winning.
    final_rows = {}
    for sid, rows in groups.items():
        for r in rows:
            k = (sid, r["section"], r["section_num"])
            if k in produced:
                final_rows[k] = produced[k]
            elif k in cached:
                final_rows[k] = cached[k]
            else:  # never fetched this run and no prior cache row: record as pending
                final_rows[k] = make_cache_row(
                    r, before_text=None, before_full=None, snap_ts=None,
                    snap_url=None, snapshot_count=0, extract_method="none",
                    status="pending")

    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
        for k in sorted(final_rows):
            fh.write(json.dumps(final_rows[k], sort_keys=True, ensure_ascii=False) + "\n")
    os.replace(tmp_path, out_path)
    if part_path.exists():
        part_path.unlink()

    meta = build_meta(args, rmc_path, groups, load_errors, final_rows, n_requests)
    meta["cache_sha1"] = sha1_file(out_path)
    with open(meta_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")

    print(f"[fetch] wrote {len(final_rows)} rows -> {out_path}", file=sys.stderr)
    print(f"[fetch] status breakdown: {meta['status_breakdown']}", file=sys.stderr)
    print(f"[fetch] sidecar -> {meta_path}", file=sys.stderr)
    return 0


def _print_diff(row):
    def clip(s, n=420):
        s = s or ""
        return s[:n] + (" ..." if len(s) > n else "")
    print("\n" + "=" * 90)
    print(f"{row['generic']} ({row['brand']}) | {row['section']} "
          f"[{row['section_num']}] | changed {row['change_date']} | status={row['status']}")
    if row["before_snapshot_ts"]:
        print(f"  snapshot {row['before_snapshot_ts']}  ({row['snapshot_count']} "
              f"pre-change captures)  {row['before_snapshot_url']}")
    print(f"  BEFORE: {clip(row['before_text'])}")
    print(f"  AFTER : {clip(row['after_text'])}")


if __name__ == "__main__":
    raise SystemExit(main())
