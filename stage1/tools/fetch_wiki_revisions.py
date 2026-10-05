"""One-time enwiki wikitext fetch for the sports adapter's infobox re-extraction.

The legacy sports harvest (wikipedia/sports/sports_harvest.py) parsed the
player infobox from live API responses and stored ONLY the extracted club
strings plus the revision timestamps it saw (cutoff_rev_ts / cur_rev_ts).
258 review rows carry empty before/after club values because that parser
failed; diagnosing and fixing them requires the actual wikitext. Wikipedia
revisions are immutable, so a timestamp-pinned fetch is deterministic: this
tool fetches, for every row of sports_verified.jsonl, the newest revision at
or before each recorded timestamp (rvstart=<ts>, rvdir=older, rvlimit=1) —
i.e. EXACTLY the revisions the legacy harvest used — and freezes them into a
cache the offline pipeline can replay.

Rows the legacy harvest failed on before recording timestamps (sport=null,
and some rows with a null cutoff_rev_ts) carry no pin; for those the study
window boundaries are used as deterministic fallback pins and the row records
ts_source="window_fallback" so downstream code can tell the difference:

* cutoff side fallback: 2026-02-01T00:00:00Z (the legacy harvest's CUTOFF
  constant — the very rvstart it passed for the "old" snapshot).
* current side fallback: 2026-07-08T00:00:00Z (the date the legacy current-
  infobox harvest was frozen; the stage1 --asof).

Outputs:

* cache (default stage1/cache/sports_wikitext.jsonl): one JSON object per
  title, sorted by title, sorted keys — byte-identical for identical API
  data. Row shape:
      {"title": str,
       "cutoff":  {"pinned_ts", "ts_source": "row"|"window_fallback",
                   "ts", "revid", "sha1", "content"} | null,
       "current": {...same...} | null,
       "fetch_errors": ["cutoff: ...", "current: ..."]}
  A side is null exactly when a fetch_errors entry explains why (page
  missing, no revision at/before the pin, hidden text). Full-page wikitext
  is stored (the legacy harvest fetched rvsection=0; the full text is a
  superset, so section-0 behaviour can be replayed offline).
* sidecar (default stage1/cache/sports_wikitext.meta.json): retrieval
  timestamp, endpoint, query params, input sha1, counts. Wall-clock data
  lives ONLY here — the cache itself stays deterministic.

Resumable: completed titles (rows with no fetch_errors) are skipped on
restart; rows that previously errored are refetched. Progress is appended to
<out>.part.jsonl and the sorted final cache is written atomically at the end.

Prune guard: when the existing cache contains titles ABSENT from the current
--verified file (a rerun against a subset, or a --verified path typo), the
tool aborts loudly BEFORE any fetching — silently rewriting a frozen,
sha1-pinned cache down to a subset would invalidate release manifests. Pass
--prune to confirm the shrink explicitly.

Politeness: descriptive User-Agent with contact, maxlag=5 with retry, backoff
on HTTP errors, and a request-rate cap (default 0.15s sleep -> well under
5 req/s including latency).

The pipeline itself never calls this tool or the network: adapters only READ
the cache, and a missing cache entry surfaces as 'review', never a crash.

Run from the repo root (network access to en.wikipedia.org required):

    python3 -m stage1.tools.fetch_wiki_revisions \
        --verified wikipedia/sports/sports_verified.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from stage1.config import require_contact_email, user_agent

TOOL_VERSION = "fetch_wiki_revisions:v1"
API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = user_agent()
# Deterministic fallback pins for rows where the legacy harvest recorded no
# revision timestamp (see module docstring).
FALLBACK_CUTOFF_TS = "2026-02-01T00:00:00Z"
FALLBACK_CURRENT_TS = "2026-07-08T00:00:00Z"
RV_PROPS = "ids|timestamp|sha1|content"

_DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"


def api_get(params: dict, tries: int = 8, base_sleep: float = 2.0) -> dict:
    """GET the enwiki API with maxlag handling and retry/backoff. Raises
    RuntimeError after exhausting retries — this is a one-time online tool,
    so failing loudly is correct (the offline pipeline never runs it)."""
    require_contact_email()
    query = urllib.parse.urlencode(
        {**params, "format": "json", "formatversion": "2", "maxlag": "5"}
    )
    url = f"{API}?{query}"
    last_error = "no attempt made"
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=90) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if retry_after and retry_after.isdigit():
                wait = float(retry_after) + 1.0
            elif exc.code == 429:
                wait = 30.0
            else:
                wait = min(60.0, base_sleep * (2 ** (attempt - 1)))
            last_error = f"HTTP {exc.code}"
            print(f"  [{last_error}] retry in {wait:.0f}s (attempt {attempt}/{tries})",
                  file=sys.stderr, flush=True)
            time.sleep(wait)
            continue
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            wait = min(60.0, base_sleep * (2 ** (attempt - 1)))
            last_error = f"{type(exc).__name__}: {exc}"
            print(f"  [{last_error}] retry in {wait:.0f}s (attempt {attempt}/{tries})",
                  file=sys.stderr, flush=True)
            time.sleep(wait)
            continue
        if "error" in payload:
            code = payload["error"].get("code", "")
            last_error = f"API error {code}: {payload['error'].get('info', '')}"
            if code == "maxlag":
                wait = float(payload["error"].get("lag", 5) or 5) + 2.0
                print(f"  [maxlag] retry in {wait:.0f}s (attempt {attempt}/{tries})",
                      file=sys.stderr, flush=True)
                time.sleep(wait)
                continue
            raise RuntimeError(f"enwiki API error for {params.get('titles', '')!r}: {last_error}")
        return payload
    raise RuntimeError(f"enwiki API unreachable after {tries} tries: {last_error}")


def fetch_side(title: str, pinned_ts: str, ts_source: str):
    """(side_dict | None, error | None): the newest revision of `title` at or
    before `pinned_ts` (rvstart is inclusive, so pinning a row's exact
    revision timestamp returns exactly that revision)."""
    payload = api_get({
        "action": "query",
        "prop": "revisions",
        "titles": title,
        "rvprop": RV_PROPS,
        "rvslots": "main",
        "rvlimit": "1",
        "rvdir": "older",
        "rvstart": pinned_ts,
    })
    pages = payload.get("query", {}).get("pages", [])
    if not pages:
        return None, f"empty API response (no pages) for rvstart={pinned_ts}"
    page = pages[0]
    if page.get("invalid"):
        return None, f"invalid title: {page.get('invalidreason', '')!r}"
    if page.get("missing"):
        return None, "page missing (deleted or never existed)"
    revs = page.get("revisions") or []
    if not revs:
        return None, f"no revision at or before {pinned_ts} (page created later?)"
    rev = revs[0]
    slot = rev.get("slots", {}).get("main", {})
    if slot.get("texthidden") or "content" not in slot:
        return None, f"revision {rev.get('revid')} content hidden/absent"
    return {
        "pinned_ts": pinned_ts,
        "ts_source": ts_source,
        "ts": rev.get("timestamp"),
        "revid": rev.get("revid"),
        "sha1": rev.get("sha1"),  # absent key -> None below if sha1hidden
        "content": slot["content"],
    }, None


def load_rows(path: Path):
    """(rows keyed by title in input order, load_errors). Malformed lines are
    recorded, never silently skipped. Duplicate titles abort loudly — the
    cache is keyed by title."""
    rows = {}
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
            title = row.get("title") if isinstance(row, dict) else None
            if not isinstance(title, str) or not title:
                errors.append({"line": line_no, "error": f"missing/malformed title: {title!r}"})
                continue
            if title in rows:
                raise SystemExit(f"duplicate title in {path}: {title!r} (line {line_no})")
            rows[title] = row
    return rows, errors


def load_cached(path: Path):
    """title -> cached row, from a previous (complete or partial) run."""
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
            if isinstance(row, dict) and isinstance(row.get("title"), str):
                cached[row["title"]] = row
    return cached


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified_identifier(as_given: str) -> str:
    """LOCATION-INDEPENDENT identifier for the --verified input, recorded in the
    sidecar's ``verified_file`` field (which is fingerprinted into the release
    manifest via extra_input_meta).  A RELATIVE path ships with the checkout and
    is kept verbatim (e.g. 'wikipedia/sports/sports_verified.jsonl'); an ABSOLUTE
    path — a session-local scratchpad or a machine-specific layout — is reduced
    to its bare filename so it never leaks into the sidecar and identical fetches
    stay byte-comparable across machines, mirroring ``cache_identifier``."""
    if os.path.isabs(as_given):
        return os.path.basename(as_given)
    return as_given


def cache_identifier(out_path: Path, as_given: str) -> str:
    """LOCATION-INDEPENDENT identifier for the cache file, recorded in the
    sidecar's cache_file field: the sidecar ships with the checkout, so a
    machine-absolute path there would leak the local directory layout and
    make identical fetches byte-different across machines. A cache written
    to the package-default directory is identified by its repo-relative id
    ('stage1/cache/<name>', matching the adapter's PACKAGE_*_CACHE_ID);
    any other destination keeps the path exactly as given on the CLI."""
    try:
        resolved = out_path.resolve()
    except OSError:
        return as_given
    if resolved.parent == _DEFAULT_CACHE_DIR:
        return f"stage1/cache/{resolved.name}"
    return as_given


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m stage1.tools.fetch_wiki_revisions",
        description="One-time pinned enwiki wikitext fetch for the sports adapter.",
    )
    parser.add_argument(
        "--verified",
        default="../0_prior_work/wikipedia/sports/sports_verified.jsonl",
        help="legacy sports_verified.jsonl (one row per title)",
    )
    parser.add_argument(
        "--out",
        default=str(_DEFAULT_CACHE_DIR / "sports_wikitext.jsonl"),
        help="cache output path (default stage1/cache/sports_wikitext.jsonl)",
    )
    parser.add_argument(
        "--meta",
        default=str(_DEFAULT_CACHE_DIR / "sports_wikitext.meta.json"),
        help="sidecar metadata path (default stage1/cache/sports_wikitext.meta.json)",
    )
    parser.add_argument("--sleep", type=float, default=0.15,
                        help="seconds to sleep between API requests (politeness)")
    parser.add_argument(
        "--prune",
        action="store_true",
        help=(
            "allow dropping cached titles absent from --verified (without this "
            "flag the tool aborts rather than silently shrinking a frozen cache)"
        ),
    )
    args = parser.parse_args(argv)

    verified_path = Path(args.verified)
    if not verified_path.is_file():
        print(f"error: verified file not found: {verified_path}", file=sys.stderr)
        return 2

    rows, load_errors = load_rows(verified_path)
    for err in load_errors:
        print(f"[verified] line {err['line']}: {err['error']}", file=sys.stderr)
    titles = sorted(rows)
    print(f"[fetch] {len(titles)} unique titles from {verified_path}",
          file=sys.stderr, flush=True)

    out_path = Path(args.out)
    part_path = out_path.with_suffix(out_path.suffix + ".part")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Resume: rows without fetch_errors are done; errored rows are refetched.
    cached = load_cached(out_path)
    cached.update(load_cached(part_path))

    # Prune guard: never silently rewrite a frozen cache down to a subset.
    # final_rows is built from the CURRENT verified titles, so any cached
    # title absent from --verified would be destroyed on rewrite; that is
    # almost always a rerun against the wrong/partial verified file, and the
    # loss would only surface later as cache_missing reviews or a manifest
    # sha1 change. Aborting is deliberate (not carry-forward): the cache is
    # defined as 'the pinned revisions of THIS verified file', so stray rows
    # must be either a mistake (fix the --verified path) or an intentional
    # shrink (--prune).
    stray = sorted(set(cached) - set(rows))
    if stray and not args.prune:
        print(
            f"error: the existing cache {out_path} contains {len(stray)} title(s) "
            f"absent from {verified_path} (e.g. {stray[:3]!r}); a rewrite would "
            "silently destroy previously fetched pinned revisions. Re-check "
            "--verified, or pass --prune to shrink the cache deliberately.",
            file=sys.stderr,
        )
        return 2
    done = {t: r for t, r in cached.items()
            if t in rows and not r.get("fetch_errors")}
    todo = [t for t in titles if t not in done]
    print(f"[fetch] {len(done)} already cached, {len(todo)} to fetch "
          f"(~{2 * len(todo)} requests)", file=sys.stderr, flush=True)

    n_requests = 0
    fallback_pins = 0
    with open(part_path, "a", encoding="utf-8", newline="\n") as part:
        for i, title in enumerate(todo, 1):
            src = rows[title]
            sides = {}
            errors = []
            for side_name, ts_key, fallback in (
                ("cutoff", "cutoff_rev_ts", FALLBACK_CUTOFF_TS),
                ("current", "cur_rev_ts", FALLBACK_CURRENT_TS),
            ):
                row_ts = src.get(ts_key)
                if isinstance(row_ts, str) and row_ts:
                    pinned_ts, ts_source = row_ts, "row"
                else:
                    pinned_ts, ts_source = fallback, "window_fallback"
                    fallback_pins += 1
                side, err = fetch_side(title, pinned_ts, ts_source)
                n_requests += 1
                time.sleep(args.sleep)
                sides[side_name] = side
                if err is not None:
                    errors.append(f"{side_name}: {err}")
            out_row = {
                "title": title,
                "cutoff": sides["cutoff"],
                "current": sides["current"],
                "fetch_errors": errors,
            }
            done[title] = out_row
            part.write(json.dumps(out_row, sort_keys=True, ensure_ascii=False) + "\n")
            part.flush()
            if i % 25 == 0 or i == len(todo):
                print(f"[fetch] {i}/{len(todo)} titles ({n_requests} requests this run)",
                      file=sys.stderr, flush=True)

    # ---- write the sorted final cache atomically, then drop the part file ----
    missing_titles = [t for t in titles if t not in done]
    # errored rows live in `cached` but not `done`; carry them into the final
    # cache so the failure is visible offline (they will be retried next run).
    final_rows = dict(done)
    for t in missing_titles:
        if t in cached:
            final_rows[t] = cached[t]
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
        for t in sorted(final_rows):
            fh.write(json.dumps(final_rows[t], sort_keys=True, ensure_ascii=False) + "\n")
    os.replace(tmp_path, out_path)
    if part_path.exists():
        part_path.unlink()

    n_ok = sum(1 for r in final_rows.values() if not r.get("fetch_errors"))
    n_err = len(final_rows) - n_ok
    err_titles = sorted(t for t, r in final_rows.items() if r.get("fetch_errors"))
    meta = {
        "tool_version": TOOL_VERSION,
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "endpoint": API,
        "user_agent": USER_AGENT,
        "query_params": {
            "action": "query", "prop": "revisions", "rvprop": RV_PROPS,
            "rvslots": "main", "rvlimit": "1", "rvdir": "older",
            "rvstart": "per-row cutoff_rev_ts / cur_rev_ts, else fallback pins",
            "maxlag": "5",
        },
        "fallback_pins": {
            "cutoff": FALLBACK_CUTOFF_TS,
            "current": FALLBACK_CURRENT_TS,
            "used_this_run": fallback_pins,
        },
        "verified_file": verified_identifier(args.verified),
        "verified_sha1": sha1_file(verified_path),
        "verified_load_errors": load_errors,
        "counts": {
            "titles": len(titles),
            "cached_rows": len(final_rows),
            "rows_ok": n_ok,
            "rows_with_fetch_errors": n_err,
            "requests_this_run": n_requests,
        },
        "fetch_error_titles": err_titles,
        "cache_file": cache_identifier(out_path, args.out),
        "cache_sha1": sha1_file(out_path),
    }
    meta_path = Path(args.meta)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    with open(meta_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")

    print(f"[fetch] wrote {len(final_rows)} rows ({n_ok} ok, {n_err} with errors) -> {out_path}",
          file=sys.stderr, flush=True)
    print(f"[fetch] sidecar -> {meta_path}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
