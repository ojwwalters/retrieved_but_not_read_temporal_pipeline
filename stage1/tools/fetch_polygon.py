"""One-time Polygon.io ("Massive") fetch for the Stage-1 finance adapter.

The finance adapter (source="finance") measures post-cutoff drift of five
families — share_price, market_cap, quarterly_revenue, ticker_change,
ipo_listing — over the S&P 500 universe (sec/sp500_universe.csv), plus a
market-wide 2026 IPO family. The ground truth is deterministic arithmetic over
an authoritative feed, so ALL judgement lives in the adapter's gates; this tool
only pulls RAW values and freezes them into per-family caches the offline
adapter replays. The Polygon key touches the network ONLY here (read from
POLYGON_API_KEY, sent as an Authorization: Bearer header so it never enters a
URL string or a log line); the adapter is offline-only.

REQUEST ECONOMY (a hard design goal — the key is on a slow ~5 requests/min
tier, >=12s spacing enforced):

* PRICE via the GROUPED-DAILY endpoint: ALL ~11.8k US stocks' OHLC for one date
  in ONE request. Two resolved trading days (anchor + asof) cover the whole
  universe in ~3 calls, filtered locally to the requested tickers — NOT one
  call per ticker. The anchor/asof trading days are DERIVED from the data by
  stepping the requested date back until grouped-daily returns rows (holidays
  and weekends return HTTP 200 with the 'results' key ABSENT), so there is no
  output-affecting date literal in this source — cutoff and asof are CLI flags.
* FINANCIALS: one call per ticker returns quarterly total revenues AND the
  period basic/diluted average shares (the shares proxy for computed
  market_cap — Polygon exposes no point-in-time shares_outstanding), for every
  quarter through --asof. The adapter selects the last pre-cutoff quarter, the
  first post-cutoff quarter, and the shares filing on/before each of the anchor
  and asof dates.
* TICKER EVENTS: one call per ticker (symbol/name changes).
* IPO: ONE market-wide paginated call for the [cutoff, asof] window (the SPAC /
  exchange / issuer-country discriminators all ride in this one endpoint;
  filtering happens in the adapter, auditable).
* STOOQ independent asof cross-check: keyless CSV, but currently behind a
  JavaScript anti-bot challenge unfetchable by a stdlib/no-JS client — degraded
  to a review-safe provenance step (cross_check_status='unavailable'), probed
  once so the tool records the block without hammering it per ticker.

Caches written under --out-dir (default stage1/cache/), each a deterministic
sorted JSONL + a .meta.json sidecar (endpoint, retrieved_at, params, cutoff,
asof, counts — NEVER the key; wall-clock lives only in the sidecar):

    price_grouped.jsonl   ticker_events.jsonl   cross_check.jsonl
    financials.jsonl      ipo.jsonl

Resumable: per-ticker families (financials, ticker_events) skip tickers already
cached and checkpoint to a <out>.part file after each ticker; a later FULL run
(drop --sample) extends the SAME caches with only the not-yet-fetched tickers.
The market-wide grouped-daily and IPO calls are idempotent and refetched only
when the requested set is not already covered. Each cache is WINDOW-BOUND: a
resume reuses a cached price row only when its requested_date matches the current
--cutoff/--asof, and a cached financials ticker only when its period_of_report
.lte matches the current --asof; entries fetched under a different window are
re-fetched, never silently mixed (so 5 seeded tickers can never carry an old
window while the rest carry a new one). Backoff on 429/5xx is time-based
(the 429 carries no Retry-After header): a 429 sleeps a full minute to clear the
rolling-minute window; other transient errors back off exponentially from the
pacing interval. A missing/incomplete cache entry is the adapter's problem
(it degrades to review) — this tool never silently drops a requested ticker.

``--families`` (see CACHE_FAMILIES) restricts a run to a subset of the five
caches, leaving every unselected cache and sidecar byte-untouched — the
sanctioned shape for a targeted top-up such as re-anchoring the grouped-daily
price cache at a moved cutoff (``--families price``) without letting the
market-wide IPO refetch (whose listing_date.gte tracks the new cutoff) drop
the earlier listings a derive must still enumerate as temporal exclusions.

Run from the repo root (network access + POLYGON_API_KEY required):

    python3 -m stage1.tools.fetch_polygon \\
        --universe sec/sp500_universe.csv \\
        --cutoff 2026-01-01 --asof 2026-06-30 \\
        --out-dir stage1/cache \\
        --sample AAPL,MSFT,NVDA,JPM,XOM        # omit for the full universe
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from stage1.config import require_contact_email, setting, user_agent

TOOL_VERSION = "fetch_polygon:v1"
API_HOST = "https://api.polygon.io"
USER_AGENT = user_agent()
STOOQ_URL = "https://stooq.com/q/d/l/"

# Grouped-daily step-back cap: 7 calendar days covers the longest US market
# closure (a holiday landing on a long weekend). Bounded so a genuinely dead
# feed fails loudly instead of walking back forever.
STEP_BACK_CAP = 7
# Quarters of financial history to pull per ticker. Polygon 'quarterly' omits
# fiscal-Q4 (folded into the 10-K), so ~3 rows/year; 12 rows ~= 4 years, more
# than enough to hold the last pre-cutoff quarter, the first post-cutoff
# quarter, and the shares filing on/before both anchor and asof.
FINANCIALS_LIMIT = 12
IPO_PAGE_LIMIT = 1000

ROLE_RANK = {"anchor": 0, "asof": 1}

_DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"


# --------------------------------------------------------------------------- #
# Rate pacing + HTTP
# --------------------------------------------------------------------------- #
class Pacer:
    """Enforce a minimum interval between Polygon requests (the slow tier caps
    at ~5 req/min; >=12s spacing keeps us under it). wait() blocks until the
    interval has elapsed since the last mark(); every attempt marks, so a
    retried call still respects the cadence."""

    def __init__(self, min_interval: float):
        self.min_interval = max(0.0, float(min_interval))
        self._last = None  # monotonic timestamp of the previous attempt

    def wait(self) -> None:
        if self._last is None:
            return
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def mark(self) -> None:
        self._last = time.monotonic()


def polygon_request(pacer: Pacer, url: str, api_key: str, tries: int = 6,
                    base_sleep: float = 12.0):
    """(status_code, payload). The key is sent as an Authorization: Bearer
    header so it never appears in `url` (safe to log). 200 -> (200, dict);
    404 -> (404, body) so a no-data ticker is a normal, non-retried outcome;
    429/5xx -> time-based backoff and retry (429 has no Retry-After header, so
    sleep a full rolling-minute); other 4xx (400/401/403) -> raise loudly."""
    require_contact_email()
    headers = {"User-Agent": USER_AGENT, "Authorization": f"Bearer {api_key}"}
    last_error = "no attempt made"
    for attempt in range(1, tries + 1):
        pacer.wait()
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                payload = json.load(resp)
            pacer.mark()
            return 200, payload
        except urllib.error.HTTPError as exc:
            pacer.mark()
            code = exc.code
            if code == 404:
                try:
                    body = json.load(exc)
                except (ValueError, OSError):
                    body = {}
                return 404, body if isinstance(body, dict) else {}
            if code == 429 or 500 <= code < 600:
                wait = 60.0 if code == 429 else min(120.0, base_sleep * (2 ** (attempt - 1)))
                last_error = f"HTTP {code}"
                print(f"  [{last_error}] backoff {wait:.0f}s (attempt {attempt}/{tries})",
                      file=sys.stderr, flush=True)
                time.sleep(wait)
                continue
            # 400/401/403 etc. are our bug (bad params) or a bad key — fail
            # loudly. exc's message may echo the URL, which carries NO key.
            raise RuntimeError(f"Polygon HTTP {code} (unrecoverable) for {url}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            pacer.mark()
            wait = min(120.0, base_sleep * (2 ** (attempt - 1)))
            last_error = f"{type(exc).__name__}: {exc}"
            print(f"  [{last_error}] backoff {wait:.0f}s (attempt {attempt}/{tries})",
                  file=sys.stderr, flush=True)
            time.sleep(wait)
            continue
    raise RuntimeError(f"Polygon unreachable after {tries} tries: {last_error}")


def polygon_get(pacer: Pacer, path: str, params: dict, api_key: str):
    url = API_HOST + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return polygon_request(pacer, url, api_key)


# --------------------------------------------------------------------------- #
# Pure parsers / builders (unit-tested offline, no network)
# --------------------------------------------------------------------------- #
def _as_str(v):
    return None if v is None else str(v)


def _stat(income: dict, key: str):
    """income_statement.<key>.value, tolerating any missing level."""
    if not isinstance(income, dict):
        return None
    node = income.get(key)
    if not isinstance(node, dict):
        return None
    return node.get("value")


def grouped_is_empty(payload: dict) -> bool:
    """A holiday/weekend grouped-daily response is HTTP 200 with 'results'
    ABSENT (None), not an empty array — treat missing/None/[] all as empty
    (the step-back trigger), never KeyError."""
    results = payload.get("results")
    if not isinstance(results, list):
        return True
    return len(results) == 0


def resolve_trading_day(requested_date: str, fetch_grouped, cap: int = STEP_BACK_CAP,
                        start_step: int = 0):
    """(resolved_day, step_back, payload, calls). Step the requested date back
    one calendar day at a time until grouped-daily returns rows. `fetch_grouped`
    is `day -> payload` (injected, so tests need no network).

    ``start_step`` is the first probe's offset in days: 0 (default) resolves
    the last trading day ON/before the requested date (the asof role);
    1 resolves the last trading day STRICTLY BEFORE it — the ANCHOR role's
    contract, because the benchmark window is cutoff-INCLUSIVE (SEC includes
    change_date == cutoff), so a cutoff falling on a trading day must never
    donate its own close to the 'before' side (day-0 in-window leakage). The
    probes end at requested_date - cap either way."""
    d0 = date.fromisoformat(requested_date)
    calls = 0
    for step in range(start_step, cap + 1):
        day = (d0 - timedelta(days=step)).isoformat()
        payload = fetch_grouped(day)
        calls += 1
        if not grouped_is_empty(payload):
            return day, step, payload, calls
    raise RuntimeError(
        f"grouped-daily returned no rows within {cap} days before {requested_date}"
    )


def build_price_rows(payload: dict, role: str, requested_date: str,
                     resolved_day: str, step_back: int, tickers) -> list:
    """One row per requested ticker for one resolved day. A ticker absent from
    a non-empty grouped response (delisted / no trade) gets status='missing'
    with null OHLC so the adapter routes it to review — never a silent drop."""
    results = payload.get("results") or []
    by_ticker = {}
    for r in results:
        if isinstance(r, dict) and isinstance(r.get("T"), str):
            by_ticker.setdefault(r["T"], r)
    rows = []
    for t in sorted(tickers):
        r = by_ticker.get(t)
        base = {
            "role": role,
            "requested_date": requested_date,
            "resolved_trading_day": resolved_day,
            "step_back_days": step_back,
            "ticker": t,
            "adjusted": False,
            "source": "polygon:/v2/aggs/grouped/locale/us/market/stocks",
        }
        if r is None:
            base.update({"status": "missing", "c": None, "o": None, "h": None,
                         "l": None, "v": None, "vw": None, "n": None, "t": None})
        else:
            base.update({
                "status": "present",
                "c": r.get("c"), "o": r.get("o"), "h": r.get("h"), "l": r.get("l"),
                "v": r.get("v"), "vw": r.get("vw"), "n": r.get("n"), "t": r.get("t"),
            })
        rows.append(base)
    return rows


def map_financials(ticker: str, universe_cik, payload: dict, asof_filter: str) -> list:
    """One row per quarterly filing (RAW: revenues + basic/diluted average
    shares + all dates). Zero filings / 404 -> a single status='no_data'
    sentinel so the ticker counts as fetched (resumable) and the adapter can
    route it to review."""
    results = payload.get("results") or []
    rows = []
    for res in results:
        if not isinstance(res, dict):
            continue
        fin = res.get("financials") or {}
        income = fin.get("income_statement") if isinstance(fin, dict) else None
        income = income if isinstance(income, dict) else {}
        rev = income.get("revenues") if isinstance(income.get("revenues"), dict) else {}
        rows.append({
            "ticker": ticker,
            "cik": _as_str(res.get("cik")) or _as_str(universe_cik),
            "fiscal_period": res.get("fiscal_period"),
            "fiscal_year": _as_str(res.get("fiscal_year")),
            "timeframe": res.get("timeframe"),
            "start_date": res.get("start_date"),
            "end_date": res.get("end_date"),
            "filing_date": res.get("filing_date"),
            "acceptance_datetime": res.get("acceptance_datetime"),
            "revenues": rev.get("value"),
            "revenues_unit": rev.get("unit"),
            "basic_average_shares": _stat(income, "basic_average_shares"),
            "diluted_average_shares": _stat(income, "diluted_average_shares"),
            "source_filing_url": res.get("source_filing_url"),
            "asof_filter": asof_filter,
            "status": "ok",
            "source": "polygon:/vX/reference/financials",
        })
    if not rows:
        rows.append({
            "ticker": ticker,
            "cik": _as_str(universe_cik),
            "fiscal_period": None, "fiscal_year": None, "timeframe": "quarterly",
            "start_date": None, "end_date": None, "filing_date": None,
            "acceptance_datetime": None, "revenues": None, "revenues_unit": None,
            "basic_average_shares": None, "diluted_average_shares": None,
            "source_filing_url": None, "asof_filter": asof_filter,
            "status": "no_data",
            "source": "polygon:/vX/reference/financials",
        })
    return rows


def map_events(ticker: str, universe_cik, payload: dict) -> dict:
    """One row per ticker. `results` is an OBJECT {name, composite_figi, cik,
    events[]}; each event's ticker_change.ticker is the symbol ADOPTED on that
    date. Events sorted newest-first (date desc) for a stable, API-faithful
    order. Missing object / 404 -> status='no_data' with empty events."""
    results = payload.get("results")
    if not isinstance(results, dict):
        return {
            "ticker": ticker, "name": None, "cik": _as_str(universe_cik),
            "composite_figi": None, "events": [], "status": "no_data",
            "source": "polygon:/vX/reference/tickers/{ticker}/events",
        }
    events = []
    for e in results.get("events") or []:
        if not isinstance(e, dict):
            continue
        tc = e.get("ticker_change") or {}
        events.append({
            "type": e.get("type"),
            "date": e.get("date"),
            "new_ticker": tc.get("ticker") if isinstance(tc, dict) else None,
        })
    events.sort(key=lambda x: (x.get("date") or "", x.get("new_ticker") or ""),
                reverse=True)
    return {
        "ticker": ticker,
        "name": results.get("name"),
        "cik": _as_str(results.get("cik")) or _as_str(universe_cik),
        "composite_figi": results.get("composite_figi"),
        "events": events,
        "status": "ok",
        "source": "polygon:/vX/reference/tickers/{ticker}/events",
    }


_IPO_FIELDS = (
    "ticker", "issuer_name", "listing_date", "announced_date", "last_updated",
    "primary_exchange", "security_type", "ipo_status", "shares_outstanding",
    "final_issue_price", "lowest_offer_price", "highest_offer_price",
    "max_shares_offered", "total_offer_size", "isin", "us_code",
    "currency_code", "security_description", "lot_size",
)


def map_ipo(res: dict) -> dict:
    """RAW IPO row (all discriminators kept: security_type CS vs SP=SPAC,
    primary_exchange MIC, ISIN issuer-country prefix). The CS/exchange/ISIN
    filter is applied later in the adapter, auditable."""
    row = {f: res.get(f) for f in _IPO_FIELDS}
    row["source"] = "polygon:/vX/reference/ipos"
    return row


def parse_stooq_csv(text: str):
    """Close from a Stooq daily CSV (last data row = the asof close), or None
    when the body is not CSV (the JavaScript anti-bot challenge page, a 404
    logo page, or an empty range)."""
    if not text:
        return None
    lines = [ln for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
    if len(lines) < 2:
        return None
    header = [h.strip().lower() for h in lines[0].split(",")]
    if not header or header[0] != "date" or "close" not in header:
        return None
    close_idx = header.index("close")
    fields = lines[-1].split(",")
    if close_idx >= len(fields):
        return None
    try:
        return float(fields[close_idx])
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# I/O helpers
# --------------------------------------------------------------------------- #
def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cache_identifier(out_path: Path, as_given: str) -> str:
    """LOCATION-INDEPENDENT id for a cache file, recorded in its sidecar (which
    ships with the checkout): a machine-absolute path there would leak the
    local layout and make identical fetches byte-different across machines. A
    cache in the package-default dir maps to 'stage1/cache/<name>'; any other
    destination keeps the path exactly as given on the CLI."""
    try:
        resolved = out_path.resolve()
    except OSError:
        return as_given
    if resolved.parent == _DEFAULT_CACHE_DIR:
        return f"stage1/cache/{resolved.name}"
    return as_given


def load_universe(path: Path):
    """(ticker -> {'name','cik'}, load_errors). Header rows / blank tickers are
    recorded, never silently dropped."""
    universe, errors = {}, []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for line_no, r in enumerate(reader, 2):
            ticker = (r.get("ticker") or "").strip()
            if not ticker:
                errors.append({"line": line_no, "error": "missing ticker"})
                continue
            universe[ticker] = {
                "name": (r.get("name") or "").strip(),
                "cik": (r.get("cik") or "").strip(),
            }
    return universe, errors


def load_rows_by_ticker(path: Path, key: str = "ticker") -> dict:
    """ticker -> [rows] from a previous (complete or partial) run. A torn last
    line of an interrupted append is expected and skipped."""
    out = {}
    if not path.is_file():
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get(key), str):
                out.setdefault(row[key], []).append(row)
    return out


def write_jsonl_sorted(out_path: Path, rows, sort_key) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows, key=sort_key)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for row in ordered:
            fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
    os.replace(tmp, out_path)


def build_sidecar_meta(meta: dict, cache_file_id: str, cache_sha1: str, *,
                       tool_version: str = TOOL_VERSION, user_agent: str = USER_AGENT,
                       retrieved_at=None) -> dict:
    """Assemble a cache ``.meta.json`` sidecar dict: the per-family ``meta``
    plus the provenance stamp (tool version, User-Agent, retrieval time, the
    cache file's LOCATION-INDEPENDENT id and its sha1).

    Factored out so it is defined ONCE and shared by BOTH the standalone tool's
    ``write_sidecar`` (below) and the harvest ``FinanceHarvester`` (which routes
    the returned dict through ``SnapshotWriter.add_json`` so the sidecar is
    sha1-pinned in the snapshot manifest instead of being written out-of-band).
    NEVER contains the API key.

    ``retrieved_at`` controls the wall-clock stamp:

    * ``None`` (default) — stamp the current UTC wall-clock. This is the
      standalone-tool behaviour: the committed ``stage1/cache/*.meta.json``
      sidecars carry a real, frozen retrieval time.
    * an ISO string — pin that exact value (used by tests for determinism).
    * ``False`` — OMIT the field entirely. HARVEST snapshots pass this so the
      sidecar bytes are a pure function of the fetched DATA. A harvest snapshot's
      single non-deterministic byte is ``snapshot_manifest.harvested_at`` (which
      is excluded from the derive input fingerprint); a sidecar is sha1-pinned in
      the manifest AND fingerprinted by the deriving adapter, so a wall-clock
      inside it would change the sidecar's sha1 (and the derive input
      fingerprint) on every re-harvest of byte-identical data — exactly the
      reproducible-fingerprint guarantee the harvest layer promises. Omitting it
      keeps that promise honest for finance (and every later sidecar source, e.g.
      fda) without weakening tamper detection (the sidecar is still sha1-pinned).
    """
    out = {
        **meta,
        "tool_version": tool_version,
        "user_agent": user_agent,
        "cache_file": cache_file_id,
        "cache_sha1": cache_sha1,
    }
    if retrieved_at is not False:
        out["retrieved_at"] = (
            retrieved_at or datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return out


# --------------------------------------------------------------------------- #
# Per-family sidecar META builders (shared by main() and FinanceHarvester._emit)
# --------------------------------------------------------------------------- #
# These assemble the family-specific portion of each cache's .meta.json sidecar
# (family / params / counts / and the family's extras) and are called by BOTH the
# standalone tool's main() and the harvest FinanceHarvester._emit, each merging
# its OWN base_meta ({**base_meta, **<family>_family_meta(...)}). base_meta stays
# caller-owned because it legitimately differs (the harvester records the
# LOCATION-INDEPENDENT in-snapshot universe filename, the tool the on-disk path).
# Sharing the assembly here means the tool's caches and the harvest snapshot's
# sidecars can no longer silently drift the way the per-family blocks previously
# had (e.g. the cross_check note). Pure; no network, no wall-clock.
_PRICE_ANCHOR_METHOD = (
    "last trading day strictly before cutoff via grouped-daily step-back "
    f"starting at cutoff-1 (cap {STEP_BACK_CAP} days); asof role remains "
    "on/before asof")


def price_family_meta(price_rows, tickers, resolved, missing_by_role, calls) -> dict:
    return {
        "family": "price_grouped",
        "params": {"path": "/v2/aggs/grouped/locale/us/market/stocks/{day}",
                   "adjusted": "false", "anchor_method": _PRICE_ANCHOR_METHOD},
        "resolved": resolved,
        "missing_tickers_by_role": missing_by_role,
        "counts": {"rows": len(price_rows), "tickers": len(tickers),
                   "calls_this_run": calls},
    }


def financials_family_meta(fin_rows, asof, tickers_cached, tickers_fetched, calls) -> dict:
    no_data = sorted({r["ticker"] for r in fin_rows
                      if isinstance(r, dict) and r.get("status") == "no_data"})
    return {
        "family": "financials",
        "params": {"path": "/vX/reference/financials", "timeframe": "quarterly",
                   "period_of_report_date.lte": asof, "order": "desc",
                   "sort": "filing_date", "limit": FINANCIALS_LIMIT},
        "counts": {"rows": len(fin_rows), "tickers_cached": tickers_cached,
                   "tickers_fetched_this_run": tickers_fetched,
                   "no_data_tickers": len(no_data), "calls_this_run": calls},
        "no_data_tickers": no_data,
    }


def events_family_meta(ev_rows, tickers_cached, tickers_fetched, calls) -> dict:
    with_changes = sorted({r["ticker"] for r in ev_rows
                           if isinstance(r, dict) and r.get("events")})
    return {
        "family": "ticker_events",
        "params": {"path": "/vX/reference/tickers/{ticker}/events", "types": "ticker_change"},
        "counts": {"rows": len(ev_rows), "tickers_cached": tickers_cached,
                   "tickers_fetched_this_run": tickers_fetched,
                   "tickers_with_events": len(with_changes), "calls_this_run": calls},
    }


def ipo_family_meta(ipo_rows, pages, calls, cutoff, asof) -> dict:
    return {
        "family": "ipo",
        "params": {"path": "/vX/reference/ipos", "listing_date.gte": cutoff,
                   "listing_date.lte": asof, "order": "asc", "sort": "listing_date",
                   "limit": IPO_PAGE_LIMIT},
        "counts": {"rows": len(ipo_rows), "pages": pages, "calls_this_run": calls},
        "note": "market-wide; SPAC/exchange/ISIN filtering applied in the adapter",
    }


def cross_check_family_meta(cc_rows, probe, asof_day) -> dict:
    ok = sum(1 for r in cc_rows
             if isinstance(r, dict) and r.get("cross_check_status") == "ok")
    return {
        "family": "cross_check",
        "endpoint": STOOQ_URL,
        "params": {"path": "/q/d/l/", "s": "{ticker}.us", "i": "d",
                   "asof_trading_day": asof_day},
        "probe": probe,
        "counts": {"rows": len(cc_rows), "ok": ok, "unavailable": len(cc_rows) - ok},
        "note": "independent asof cross-check; disagreement -> review, never fail. "
                "Stooq keyless CSV currently behind a JS challenge (unfetchable "
                "by a stdlib client) -> degrades to 'unavailable'.",
    }


def write_sidecar(meta_path: Path, out_path: Path, as_given: str, meta: dict) -> None:
    full = build_sidecar_meta(meta, cache_identifier(out_path, as_given), sha1_file(out_path))
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    with open(meta_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(full, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


# --------------------------------------------------------------------------- #
# Per-family fetchers
# --------------------------------------------------------------------------- #
def fetch_price(pacer, cutoff, asof, tickers, api_key, out_path):
    """Resolve the anchor (strictly BEFORE cutoff) and asof (on/before asof)
    trading days via grouped-daily step-back and cache each requested
    ticker's OHLC for both. Only the roles
    with uncovered tickers issue calls (the grouped response covers every
    ticker, so a sample cache extends to the full universe with 2-3 calls).
    Returns (rows, resolved, calls, missing_by_role)."""
    existing = {}  # (role, ticker) -> row
    resolved = {}  # role -> {resolved_trading_day, step_back_days, requested_date}
    # WINDOW BINDING (Finding 2): a cached price row is only reusable if its
    # requested_date matches the CURRENT window for its role (anchor<-cutoff,
    # asof<-asof). A row fetched under a different cutoff/asof is ignored here so
    # it is re-fetched under the current window — never silently mixed in (which
    # would let 5 seeded tickers carry an old anchor/asof while the rest carry
    # the new one, with the sidecar reporting only one window).
    role_requested = {"anchor": cutoff, "asof": asof}
    for rows in load_rows_by_ticker(out_path).values():
        for row in rows:
            role = row.get("role")
            if role not in ROLE_RANK:
                continue
            if row.get("requested_date") != role_requested[role]:
                continue
            existing[(role, row["ticker"])] = row
            resolved.setdefault(role, {
                "resolved_trading_day": row.get("resolved_trading_day"),
                "step_back_days": row.get("step_back_days"),
                "requested_date": row.get("requested_date"),
            })

    calls = 0
    missing_by_role = {}
    for role, requested_date in (("anchor", cutoff), ("asof", asof)):
        missing = [t for t in tickers if (role, t) not in existing]
        if not missing:
            continue

        def fetch_grouped(day):
            _status, payload = polygon_get(
                pacer, f"/v2/aggs/grouped/locale/us/market/stocks/{day}",
                {"adjusted": "false"}, api_key)
            return payload

        # ANCHOR contract: the 'before' close is the last trading day STRICTLY
        # BEFORE the cutoff (the window is cutoff-inclusive, so a cutoff landing
        # on a trading day must not donate its own close to the before side);
        # the asof role keeps on/before-asof (start_step 0).
        day, step, payload, c = resolve_trading_day(
            requested_date, fetch_grouped,
            start_step=1 if role == "anchor" else 0)
        calls += c
        rows = build_price_rows(payload, role, requested_date, day, step, tickers)
        for row in rows:
            existing[(role, row["ticker"])] = row
        resolved[role] = {"resolved_trading_day": day, "step_back_days": step,
                          "requested_date": requested_date}
        missing_by_role[role] = sorted(
            row["ticker"] for row in rows if row["status"] == "missing")
        print(f"[price] {role}: requested {requested_date} -> resolved {day} "
              f"(step_back {step}, {c} call(s)); {len(rows)} tickers, "
              f"{len(missing_by_role[role])} missing", file=sys.stderr, flush=True)

    rows = sorted(existing.values(),
                  key=lambda r: (r["ticker"], ROLE_RANK.get(r["role"], 9)))
    return rows, resolved, calls, missing_by_role


def harvest_per_ticker(tickers, out_path, fetch_one, sort_key, label, is_current=None):
    """Generic resumable per-ticker harvest. `fetch_one(ticker) -> (rows, calls)`.
    Tickers already present in the cache/part are skipped; each ticker's rows
    are appended to <out>.part and flushed (checkpoint), then the union of all
    cached + fetched rows is written sorted and the part removed. The union
    (not just the requested set) is preserved so a sample run's tickers survive
    a later full run.

    WINDOW BINDING (Finding 2): `is_current(rows) -> bool` (optional) marks a
    cached ticker as up-to-date for the CURRENT window. A cached ticker that is
    NOT current is excluded from `done`, so — when it is in the requested set —
    it is re-fetched and overwritten under the current window rather than reused
    with stale-window provenance. Families whose payload is window-independent
    (ticker events are full history) pass `is_current=None` and always reuse."""
    part_path = out_path.with_suffix(out_path.suffix + ".part")
    cached = load_rows_by_ticker(out_path)
    cached.update(load_rows_by_ticker(part_path))  # part is newer -> overwrite
    if is_current is None:
        done = set(cached)
    else:
        done = {t for t, rows in cached.items() if is_current(rows)}
    todo = [t for t in tickers if t not in done]
    print(f"[{label}] {len(done)} already cached, {len(todo)} to fetch",
          file=sys.stderr, flush=True)

    calls = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(part_path, "a", encoding="utf-8", newline="\n") as part:
        for i, ticker in enumerate(todo, 1):
            rows, c = fetch_one(ticker)
            calls += c
            cached[ticker] = rows
            for row in rows:
                part.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
            part.flush()
            if i % 25 == 0 or i == len(todo):
                print(f"[{label}] {i}/{len(todo)} tickers ({calls} calls this run)",
                      file=sys.stderr, flush=True)

    all_rows = [row for rows in cached.values() for row in rows]
    write_jsonl_sorted(out_path, all_rows, sort_key)
    if part_path.exists():
        part_path.unlink()
    return all_rows, calls, len(done), len(todo)


def fetch_financials(pacer, asof, tickers, universe, api_key, out_path):
    asof_filter = f"period_of_report_date.lte={asof}"

    def fetch_one(ticker):
        params = {
            "ticker": ticker,
            "timeframe": "quarterly",
            "period_of_report_date.lte": asof,
            "order": "desc",
            "sort": "filing_date",
            "limit": FINANCIALS_LIMIT,
        }
        status, payload = polygon_get(pacer, "/vX/reference/financials", params, api_key)
        cik = universe.get(ticker, {}).get("cik")
        payload = payload if status == 200 else {}
        return map_financials(ticker, cik, payload, asof_filter), 1

    def sort_key(r):
        return (r["ticker"], r.get("end_date") or "", r.get("filing_date") or "",
                r.get("acceptance_datetime") or "")

    def is_current(rows):
        # financials are pulled with period_of_report_date.lte=asof, stamped
        # into each row's asof_filter; a ticker cached under a different asof is
        # stale for this window and must be re-fetched (window binding).
        return all(isinstance(r, dict) and r.get("asof_filter") == asof_filter
                   for r in rows)

    return harvest_per_ticker(tickers, out_path, fetch_one, sort_key, "financials",
                              is_current=is_current)


def fetch_events(pacer, tickers, universe, api_key, out_path):
    def fetch_one(ticker):
        path = f"/vX/reference/tickers/{urllib.parse.quote(ticker)}/events"
        status, payload = polygon_get(pacer, path, {"types": "ticker_change"}, api_key)
        cik = universe.get(ticker, {}).get("cik")
        payload = payload if status == 200 else {}
        return [map_events(ticker, cik, payload)], 1

    return harvest_per_ticker(tickers, out_path, fetch_one,
                              lambda r: r["ticker"], "ticker_events")


def fetch_ipos(pacer, cutoff, asof, api_key):
    """ONE market-wide paginated call for the [cutoff, asof] listing window.
    Refetched each run (idempotent, a handful of calls) so it is always
    complete. Returns (rows, calls, pages)."""
    params = {
        "listing_date.gte": cutoff,
        "listing_date.lte": asof,
        "order": "asc",
        "sort": "listing_date",
        "limit": IPO_PAGE_LIMIT,
    }
    status, payload = polygon_get(pacer, "/vX/reference/ipos", params, api_key)
    rows, calls, pages = [], 1, 1
    if status == 200:
        rows.extend(map_ipo(r) for r in payload.get("results") or []
                    if isinstance(r, dict))
        next_url = payload.get("next_url")
        while next_url:
            status, payload = polygon_request(pacer, next_url, api_key)
            calls += 1
            pages += 1
            rows.extend(map_ipo(r) for r in payload.get("results") or []
                        if isinstance(r, dict))
            next_url = payload.get("next_url")
    print(f"[ipo] {len(rows)} listings over {pages} page(s), {calls} call(s)",
          file=sys.stderr, flush=True)
    return rows, calls, pages


def stooq_try(ticker, asof_day):
    """(status, close, reason). Best-effort keyless CSV. Any non-CSV body (the
    JS challenge) or network error degrades to 'unavailable' — never raises."""
    require_contact_email()
    d = asof_day.replace("-", "")
    query = urllib.parse.urlencode(
        {"s": f"{ticker.lower()}.us", "d1": d, "d2": d, "i": "d"})
    url = f"{STOOQ_URL}?{query}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        return "unavailable", None, f"network_error: {type(exc).__name__}"
    close = parse_stooq_csv(text)
    if close is None:
        return "unavailable", None, "stooq_js_challenge"
    return "ok", close, "csv"


def fetch_cross_check(tickers, asof_day):
    """Independent asof close per ticker. Probed once: if the first ticker is
    blocked (the JS challenge, per the verified report), every ticker is marked
    unavailable WITHOUT further requests; otherwise each ticker is fetched
    politely. Rows are review-safe — a cross-check never fails a record."""
    rows = []
    if not tickers or not asof_day:
        return rows, {"probe_status": "skipped", "probe_reason": "no asof day"}
    probe_status, _close, probe_reason = stooq_try(tickers[0], asof_day)
    print(f"[cross_check] stooq probe on {tickers[0]}: {probe_status} ({probe_reason})",
          file=sys.stderr, flush=True)
    for t in sorted(tickers):
        if probe_status == "ok":
            status, close, reason = stooq_try(t, asof_day)
            time.sleep(0.3)
        else:
            status, close, reason = "unavailable", None, probe_reason
        rows.append({
            "ticker": t,
            "asof_date": asof_day,
            "cross_check_status": status,
            "close": close,
            "reason": reason,
            "source": "stooq:/q/d/l",
        })
    return rows, {"probe_status": probe_status, "probe_reason": probe_reason}


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
# The five cache families a run can fetch, in step order. ``--families`` selects
# a subset so a targeted top-up touches ONLY the caches it names — REQUIRED for
# a window re-anchor: fetch_ipos refetches market-wide at listing_date.gte=
# cutoff, so re-running the full tool at a LATER cutoff (e.g. 2026-02-01) would
# silently DELETE the earlier listings (January IPOs) from the cache, removing
# them from derive enumeration instead of leaving them as audit-visible
# ``excluded:temporal_window`` rows. With ``--families price`` only the
# grouped-daily anchor/asof cache (and its sidecar) is rewritten; every other
# cache and sidecar keeps its bytes.
CACHE_FAMILIES = ("price", "financials", "events", "ipo", "cross_check")


def parse_families(value):
    """The validated set of cache families a run will fetch. ``None``/blank ->
    ALL families (the default full run, behaviour unchanged). A comma-separated
    subset is deduped and whitespace-tolerant; any unknown name raises
    ValueError (naming the offenders) so main() refuses loudly BEFORE any
    key/universe/network work. Pure and total."""
    if value is None or not str(value).strip():
        return set(CACHE_FAMILIES)
    requested = {tok.strip() for tok in str(value).split(",") if tok.strip()}
    unknown = sorted(requested - set(CACHE_FAMILIES))
    if unknown:
        raise ValueError(
            f"unknown families {unknown}; valid: {', '.join(CACHE_FAMILIES)}")
    return requested


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m stage1.tools.fetch_polygon",
        description="One-time Polygon.io fetch for the Stage-1 finance adapter.",
    )
    parser.add_argument("--universe", default="sec/sp500_universe.csv",
                        help="CSV with ticker,name,cik (default sec/sp500_universe.csv)")
    parser.add_argument("--cutoff", required=True, help="cutoff date flag, YYYY-MM-DD")
    parser.add_argument("--asof", required=True, help="asof date flag, YYYY-MM-DD")
    parser.add_argument("--out-dir", default=str(_DEFAULT_CACHE_DIR),
                        help="cache directory (default stage1/cache)")
    parser.add_argument("--sample", default=None,
                        help="comma-separated ticker subset (e.g. AAPL,MSFT,NVDA); "
                             "omit for the full universe")
    parser.add_argument("--rate-min-interval", type=float, default=12.0,
                        help="minimum seconds between Polygon calls (slow tier ~5/min)")
    parser.add_argument("--skip-stooq", action="store_true",
                        help="skip the (currently blocked) Stooq cross-check entirely")
    parser.add_argument("--families", default=None,
                        help="comma-separated subset of cache families to fetch "
                             f"({', '.join(CACHE_FAMILIES)}); omit for all. A "
                             "skipped family's cache and sidecar are left "
                             "byte-untouched (e.g. --families price re-anchors "
                             "the grouped-daily cache without letting the "
                             "market-wide IPO refetch drop pre-cutoff listings)")
    args = parser.parse_args(argv)

    for flag, value in (("--cutoff", args.cutoff), ("--asof", args.asof)):
        try:
            date.fromisoformat(value)
        except ValueError:
            print(f"error: {flag} must be YYYY-MM-DD, got {value!r}", file=sys.stderr)
            return 2

    # Validate the family selection BEFORE the key/universe checks so a typo
    # can never cost a network call (or half-touch the caches).
    try:
        families = parse_families(args.families)
    except ValueError as exc:
        print(f"error: --families {exc}", file=sys.stderr)
        return 2

    api_key = setting("POLYGON_API_KEY")
    if not api_key:
        print("error: POLYGON_API_KEY is not set (config.toml or the environment).", file=sys.stderr)
        return 2

    universe_path = Path(args.universe)
    if not universe_path.is_file():
        print(f"error: universe file not found: {universe_path}", file=sys.stderr)
        return 2
    universe, uni_errors = load_universe(universe_path)
    for err in uni_errors:
        print(f"[universe] line {err['line']}: {err['error']}", file=sys.stderr)

    if args.sample:
        tickers = sorted({t.strip().upper() for t in args.sample.split(",") if t.strip()})
        unknown = [t for t in tickers if t not in universe]
        if unknown:
            print(f"[sample] {len(unknown)} ticker(s) not in universe "
                  f"(name/cik will be null): {unknown}", file=sys.stderr)
    else:
        tickers = sorted(universe)
    if not tickers:
        print("error: no tickers requested.", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    pacer = Pacer(args.rate_min_interval)
    uni_sha1 = sha1_file(universe_path)
    base_meta = {
        "endpoint": API_HOST,
        "cutoff": args.cutoff,
        "asof": args.asof,
        "sample": tickers if args.sample else None,
        "universe_file": str(universe_path),
        "universe_sha1": uni_sha1,
        "universe_load_errors": uni_errors,
        "rate_min_interval_s": args.rate_min_interval,
    }
    skipped = [f for f in CACHE_FAMILIES if f not in families]
    print(f"[fetch] {len(tickers)} ticker(s); cutoff={args.cutoff} asof={args.asof}; "
          f"out-dir={out_dir}"
          + (f"; families={','.join(f for f in CACHE_FAMILIES if f in families)}"
             f" (skipped: {','.join(skipped)})" if skipped else ""),
          file=sys.stderr, flush=True)

    total_calls = 0
    # resolved stays {} when the price family is skipped: the cross_check step
    # then has no asof trading day and degrades (probe 'no asof day'), it never
    # guesses one.
    resolved = {}

    # ---- (1) PRICE (grouped-daily) --------------------------------------- #
    if "price" in families:
        price_out = out_dir / "price_grouped.jsonl"
        price_rows, resolved, price_calls, missing_by_role = fetch_price(
            pacer, args.cutoff, args.asof, tickers, api_key, price_out)
        total_calls += price_calls
        write_jsonl_sorted(price_out, price_rows,
                           lambda r: (r["ticker"], ROLE_RANK.get(r["role"], 9)))
        write_sidecar(out_dir / "price_grouped.meta.json", price_out, str(price_out), {
            **base_meta,
            **price_family_meta(price_rows, tickers, resolved, missing_by_role, price_calls),
        })

    # ---- (2) FINANCIALS (per ticker) ------------------------------------- #
    if "financials" in families:
        fin_out = out_dir / "financials.jsonl"
        fin_rows, fin_calls, fin_done, fin_todo = fetch_financials(
            pacer, args.asof, tickers, universe, api_key, fin_out)
        total_calls += fin_calls
        write_sidecar(out_dir / "financials.meta.json", fin_out, str(fin_out), {
            **base_meta,
            **financials_family_meta(fin_rows, args.asof, fin_done + fin_todo, fin_todo, fin_calls),
        })

    # ---- (3) TICKER EVENTS (per ticker) ---------------------------------- #
    if "events" in families:
        ev_out = out_dir / "ticker_events.jsonl"
        ev_rows, ev_calls, ev_done, ev_todo = fetch_events(
            pacer, tickers, universe, api_key, ev_out)
        total_calls += ev_calls
        write_sidecar(out_dir / "ticker_events.meta.json", ev_out, str(ev_out), {
            **base_meta,
            **events_family_meta(ev_rows, ev_done + ev_todo, ev_todo, ev_calls),
        })

    # ---- (4) IPO (market-wide) ------------------------------------------- #
    if "ipo" in families:
        ipo_out = out_dir / "ipo.jsonl"
        ipo_rows, ipo_calls, ipo_pages = fetch_ipos(pacer, args.cutoff, args.asof, api_key)
        total_calls += ipo_calls
        write_jsonl_sorted(ipo_out, ipo_rows,
                           lambda r: (r.get("listing_date") or "", r.get("ticker") or ""))
        write_sidecar(out_dir / "ipo.meta.json", ipo_out, str(ipo_out), {
            **base_meta,
            **ipo_family_meta(ipo_rows, ipo_pages, ipo_calls, args.cutoff, args.asof),
        })

    # ---- (5) STOOQ cross-check (best-effort, review-safe) ---------------- #
    if "cross_check" in families:
        cc_out = out_dir / "cross_check.jsonl"
        asof_day = (resolved.get("asof") or {}).get("resolved_trading_day")
        if args.skip_stooq:
            cc_rows = [{
                "ticker": t, "asof_date": asof_day, "cross_check_status": "unavailable",
                "close": None, "reason": "skipped_by_flag", "source": "stooq:/q/d/l",
            } for t in tickers]
            cc_probe = {"probe_status": "skipped", "probe_reason": "--skip-stooq"}
        else:
            cc_rows, cc_probe = fetch_cross_check(tickers, asof_day)
        write_jsonl_sorted(cc_out, cc_rows, lambda r: r["ticker"])
        write_sidecar(out_dir / "cross_check.meta.json", cc_out, str(cc_out), {
            **base_meta,
            **cross_check_family_meta(cc_rows, cc_probe, asof_day),
        })

    print(f"[fetch] DONE. {total_calls} Polygon call(s) this run. Caches under {out_dir}:",
          file=sys.stderr, flush=True)
    for family, name in (("price", "price_grouped"), ("financials", "financials"),
                         ("events", "ticker_events"), ("ipo", "ipo"),
                         ("cross_check", "cross_check")):
        marker = "" if family in families else "  (skipped this run; cache untouched)"
        print(f"    {out_dir / (name + '.jsonl')}{marker}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
