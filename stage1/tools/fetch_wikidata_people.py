"""One-time Wikidata entity-history enrichment fetch for the people adapter.

The Stage-1 people port replaces the legacy Opus significance gate with typed
per-field comparators plus a SECOND structured source: the Wikidata entity's
claim state as of the study cutoff versus its current state. Pinned at the same
instant the infobox "before" value was captured (the newest enwiki revision at
or before 2026-02-01T23:59:59Z), Wikidata gives typed before/after values with
qualifiers (P580/P582 term dates, P585 point-in-time), date precision, and
rank — corroborating (or disputing) the infobox diff offline, LLM-free.

For every unique page title in the people change set (wikipedia/people/
changes.csv + changes_tail.csv, plus any xlsx-only gold title) this tool:

  1. resolves the enwiki title -> Wikidata QID via batched pageprops
     (action=query, prop=pageprops, ppprop=wikibase_item, redirects=1, 50
     titles/req), walking the normalized + redirect chains and distinguishing
     a genuine redirect (title_status="redirect_resolved") from mere
     underscore->space normalization (still "ok");
  2. fetches the entity JSON pinned at the cutoff (per-entity prop=revisions,
     rvstart=<cutoff>, rvdir=older, rvlimit=1 -> the state as of the pin) and
     at current (batched wbgetentities, props=info|claims, lastrevid pins each
     row);
  3. decodes ONLY the whitelisted properties' statements to plain typed values
     (time with (time,precision) authoritative and a derived date_precision;
     entityid; quantity; monolingualtext; string) with rank, snaktype, and
     whitelisted qualifiers preserved in SOURCE ORDER;
  4. resolves every referenced entity-id (positions, parties, places, spouses,
     employers, quantity units) to its English label + enwiki SITELINK title
     (the primary name-match key, same namespace as the infobox wikilink) via
     batched wbgetentities (props=labels|sitelinks|aliases, 50 ids/req),
     deduped globally across both pins;
  5. records redirects/merges/missing explicitly, never guessing.

Outputs (deterministic; byte-identical across runs and checkouts for identical
API data):

  * cache (default stage1/cache/people_wd_entities.jsonl): one JSON object per
    QUERIED title (a title with no QID still gets a row), rows SORTED by numeric
    QID then title, each written with json.dumps(sort_keys=True,
    ensure_ascii=False). Row shape is the recon cache_format contract:
        {"title", "resolved_title", "qid"|null, "title_status",
         "cutoff": STATE_BLOCK|null, "current": STATE_BLOCK|null, "errors":[...]}
  * sidecar (default stage1/cache/people_wd_entities.meta.json): retrieval
    timestamp, endpoints, query params, cutoff pin, the shared property +
    qualifier whitelists, input file sha1s, counts, and error lists. Wall-clock
    data lives ONLY here — the cache itself stays deterministic.

Resumable: title resolution, per-entity cutoff states, batched current states,
and referenced-QID labels are each persisted to a .part file next to --out and
skipped on restart. The sorted final cache is assembled and written atomically
at the end. Polite: descriptive UA with contact, maxlag=5 with backoff, retry
on transient HTTP/network errors, a request-rate cap, and a poison-batch
handler (a single deleted id makes wbgetentities return zero entities with a
top-level no-such-entity error, so the bad id is dropped and the batch retried).

The pipeline itself never calls this tool or the network: the people adapter
only READS the cache, and a missing cache row / missing state / null qid
surfaces as a 'review' disposition via gate evidence, never a crash and never a
silent include.

Run from the repo root (network access to en.wikipedia.org + www.wikidata.org
required):

    python3 -m stage1.tools.fetch_wikidata_people
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from stage1.config import require_contact_email, user_agent

TOOL_VERSION = "fetch_wikidata_people:v1"
DECODER_VERSION = "wd_decode:v1"
ENWIKI_API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
USER_AGENT = user_agent()

# The study cutoff: the infobox "before" value is the newest enwiki revision at
# or before 2026-02-01T23:59:59Z (2_Infobox_Diff.py: cutoff_iso = cutoff +
# 'T23:59:59Z'); pinning the Wikidata state at the SAME instant makes the two
# structured sources describe the same moment. Overridable on the CLI.
DEFAULT_CUTOFF_TS = "2026-02-01T23:59:59Z"

BATCH_SIZE = 50

# Properties stored (the SHARED contract point: the adapter asserts this cache
# whitelist is a superset of what its comparators need, else review/refetch —
# never a silent 'no statement'). Grouped by the legacy field-class taxonomy.
PROPERTY_WHITELIST = sorted({
    # MONEY (companies; Wikidata rarely models/keeps these, but whitelist anyway)
    "P2139",  # total revenue
    "P2137",  # total equity
    "P2226",  # market capitalization
    "P2295",  # net profit
    "P2403",  # total assets
    "P3362",  # operating income
    # COUNT
    "P1128",  # employees (P585 point-in-time)
    "P2124",  # member count
    # LEAD (people/orgs directing an org)
    "P112",   # founded by
    "P127",   # owned by
    "P169",   # chief executive officer
    "P488",   # chairperson
    "P1037",  # director / manager
    # POS (positions/office; P580/P582/P768 qualifiers) + party
    "P39",    # position held
    "P102",   # member of political party
    # DATE
    "P569",   # date of birth
    "P570",   # date of death
    "P571",   # inception
    "P1619",  # date of official opening
    # PLACE
    "P19",    # place of birth
    "P20",    # place of death
    "P27",    # country of citizenship (nationality folds here)
    "P119",   # place of burial
    "P159",   # headquarters location
    # STAT
    "P31",    # instance of (type, loose)
    "P241",   # military branch (allegiance, partial)
    "P452",   # industry
    "P509",   # cause of death
    "P576",   # dissolved/abolished/demolished date
    "P749",   # parent organization
    "P1196",  # manner of death
    "P1366",  # replaced by (fate)
    "P1454",  # legal form (type, partial proxy)
    # GENERIC/OTHER (meaningful, clean Wikidata property; head-only in gold)
    "P26",    # spouse
    "P40",    # child
    "P69",    # educated at
    "P551",   # residence
})

# Qualifier properties kept on whitelisted statements. Term dates live here (no
# standalone termstart property): P580/P582 qualify a P39 statement; P585 dates
# a P1128/financial snapshot; P768 is the electoral district; P1366 the next
# officeholder / replaced-by.
QUALIFIER_WHITELIST = sorted({"P580", "P582", "P585", "P768", "P1366"})
_QUAL_SET = set(QUALIFIER_WHITELIST)
_PROP_SET = set(PROPERTY_WHITELIST)

_QID_RE = re.compile(r"^Q\d+$")
_SHEET_RE = re.compile(r"xl/worksheets/sheet\d+\.xml$")
_SS_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

_DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"

# Wikibase time-precision integers -> coarse bucket. Sub-precision digits of the
# raw time STRING are garbage and inconsistent, so (time, precision) is the only
# authority; the precision-aware date comparator narrows further downstream.
_PRECISION_LABEL = {11: "day", 10: "month", 9: "year"}


# --------------------------------------------------------------------------- #
# polite HTTP                                                                  #
# --------------------------------------------------------------------------- #
_MAXLAG = "5"  # server-lag threshold sent on every request; overridable via --maxlag


def api_get(api: str, params: dict, tries: int = 8, base_sleep: float = 2.0,
            maxlag_tries: int = 60) -> dict:
    """GET a MediaWiki/Wikibase API with maxlag handling and retry/backoff.

    Two independent budgets, because they are different failure modes:

    * TRANSIENT errors (network, timeout, HTTP 5xx/429, unparseable body) get
      `tries` attempts with exponential backoff, then RuntimeError — a genuinely
      unreachable endpoint should fail loudly (one-time online tool).
    * maxlag (a 200-status {"error":{"code":"maxlag"}} body — the server telling
      a well-behaved client to come back later; on www.wikidata.org the
      query-service replication lag counts against it and can persist for
      MINUTES) is NOT a failure: it gets its own generous `maxlag_tries` budget
      with a lag-proportional, 60s-capped wait, so a long lag window costs time
      (the tool is polite + resumable), never the run. maxlag must NOT be
      stripped in production.

    Returns the decoded payload for ANY non-maxlag outcome, INCLUDING a
    top-level {"error": ...} that the caller must inspect (wbgetentities signals
    a deleted id via a top-level no-such-entity error that must be handled, not
    raised)."""
    require_contact_email()
    query = urllib.parse.urlencode({**params, "format": "json", "maxlag": _MAXLAG})
    url = f"{api}?{query}"
    last_error = "no request completed"
    transient = 0
    maxlag = 0
    while transient < tries and maxlag < maxlag_tries:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=90) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as exc:
            transient += 1
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if retry_after and str(retry_after).isdigit():
                wait = float(retry_after) + 1.0
            elif exc.code == 429:
                wait = 45.0
            else:
                wait = min(60.0, base_sleep * (2 ** (transient - 1)))
            last_error = f"HTTP {exc.code}"
            print(f"  [{last_error}] retry in {wait:.0f}s (transient {transient}/{tries})",
                  file=sys.stderr, flush=True)
            time.sleep(wait)
            continue
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                http.client.HTTPException, ConnectionError) as exc:
            # HTTPException covers IncompleteRead: a truncated chunked body
            # surfaces during json.load's read, not at urlopen, so it is not
            # wrapped in URLError — same transient class per the docstring.
            transient += 1
            wait = min(60.0, base_sleep * (2 ** (transient - 1)))
            last_error = f"{type(exc).__name__}: {exc}"
            print(f"  [{last_error}] retry in {wait:.0f}s (transient {transient}/{tries})",
                  file=sys.stderr, flush=True)
            time.sleep(wait)
            continue
        if isinstance(payload, dict) and "error" in payload:
            code = payload["error"].get("code", "")
            if code == "maxlag":
                maxlag += 1
                lag = float(payload["error"].get("lag", 5) or 5)
                wait = min(60.0, lag + 2.0)
                last_error = f"maxlag lag={lag:.0f}"
                print(f"  [maxlag] lag={lag:.0f} retry in {wait:.0f}s "
                      f"(maxlag {maxlag}/{maxlag_tries})", file=sys.stderr, flush=True)
                time.sleep(wait)
                continue
        return payload
    raise RuntimeError(
        f"{api} gave up after {transient} transient + {maxlag} maxlag retries: {last_error}")


def qid_sort_key(qid: str):
    return (int(qid[1:]), qid)


def row_sort_key(row: dict):
    """Cache order: QID rows by numeric QID then title, then null-QID rows by
    title. Stable and location-independent."""
    qid = row.get("qid")
    if isinstance(qid, str) and _QID_RE.match(qid):
        return (0, int(qid[1:]), row.get("title", ""))
    return (1, 0, row.get("title", ""))


# --------------------------------------------------------------------------- #
# input title loading                                                         #
# --------------------------------------------------------------------------- #
def load_csv_titles(path: Path):
    """(set of titles, errors). The 'title' column is the enwiki page title
    EXACTLY as it appears in the change set (underscored) — the join key."""
    titles = set()
    errors = []
    old_limit = csv.field_size_limit()
    csv.field_size_limit(50_000_000)
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None or "title" not in reader.fieldnames:
                errors.append({"file": str(path), "error": "no 'title' column"})
                return titles, errors
            for line_no, row in enumerate(reader, 2):
                title = row.get("title")
                if isinstance(title, str) and title:
                    titles.add(title)
                else:
                    errors.append({"file": str(path), "line": line_no,
                                   "error": f"missing/empty title: {title!r}"})
    finally:
        csv.field_size_limit(old_limit)
    return titles, errors


def load_xlsx_titles(path: Path):
    """(set of titles from the 'Title' column of the first sheet, errors).
    xlsx is zip+xml; read stdlib, read-only. Handles shared-string and inline
    cells; the tail workbook's absolute sheet target is tolerated by matching
    the worksheet path with a regex."""
    titles = set()
    errors = []
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            shared = []
            if "xl/sharedStrings.xml" in names:
                root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in root.findall(f"{_SS_NS}si"):
                    shared.append("".join(t.text or "" for t in si.iter(f"{_SS_NS}t")))
            sheets = sorted(n for n in names if _SHEET_RE.match(n))
            if not sheets:
                errors.append({"file": str(path), "error": "no worksheet found"})
                return titles, errors
            sheet = ET.fromstring(zf.read(sheets[0]))
            header = []
            for i, rowel in enumerate(sheet.iter(f"{_SS_NS}row")):
                cells = []
                for c in rowel.findall(f"{_SS_NS}c"):
                    t = c.get("t")
                    v = c.find(f"{_SS_NS}v")
                    isr = c.find(f"{_SS_NS}is")
                    if t == "s" and v is not None:
                        idx = int(v.text)
                        cells.append(shared[idx] if 0 <= idx < len(shared) else "")
                    elif t == "inlineStr" and isr is not None:
                        cells.append("".join(x.text or "" for x in isr.iter(f"{_SS_NS}t")))
                    elif v is not None:
                        cells.append(v.text or "")
                    else:
                        cells.append("")
                if i == 0:
                    header = cells
                    continue
                if not cells or "Title" not in header:
                    continue
                ti = header.index("Title")
                if ti < len(cells) and cells[ti]:
                    titles.add(cells[ti])
    except (zipfile.BadZipFile, ET.ParseError, OSError, ValueError) as exc:
        errors.append({"file": str(path), "error": f"{type(exc).__name__}: {exc}"})
    return titles, errors


# --------------------------------------------------------------------------- #
# title -> QID resolution                                                     #
# --------------------------------------------------------------------------- #
def _resolve_chain(title: str, norm: dict, redir: dict):
    """(final_title, redirect_happened): walk normalization then redirect edges
    for one queried title. Normalization (underscore->space) is NOT a redirect;
    only a query.redirects edge sets redirect_happened."""
    cur = title
    redirected = False
    for _ in range(10):  # cap: guards any pathological chain
        if cur in norm and norm[cur] != cur:
            cur = norm[cur]
            continue
        if cur in redir and redir[cur] != cur:
            cur = redir[cur]
            redirected = True
            continue
        break
    return cur, redirected


def resolve_titles(titles, sleep: float, part_path: Path):
    """title -> {"resolved_title", "qid"|None, "title_status", "errors"} via
    batched enwiki pageprops. Resumable: previously resolved titles are loaded
    from part_path and skipped."""
    resolved = _load_part(part_path, key="title")
    todo = sorted(t for t in titles if t not in resolved)
    print(f"[titles] {len(resolved)} already resolved, {len(todo)} to resolve "
          f"(~{(len(todo) + BATCH_SIZE - 1) // BATCH_SIZE} pageprops batches)",
          file=sys.stderr, flush=True)
    batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
    with open(part_path, "a", encoding="utf-8", newline="\n") as part:
        for n, batch in enumerate(batches, 1):
            payload = api_get(ENWIKI_API, {
                "action": "query", "prop": "pageprops", "ppprop": "wikibase_item",
                "redirects": "1", "formatversion": "2", "titles": "|".join(batch),
            })
            if "error" in payload:
                raise RuntimeError(f"enwiki pageprops error: {payload['error']}")
            query = payload.get("query", {})
            norm = {e["from"]: e["to"] for e in query.get("normalized", [])}
            redir = {e["from"]: e["to"] for e in query.get("redirects", [])}
            pages_by_title = {}
            for pg in query.get("pages", []):
                if isinstance(pg, dict) and isinstance(pg.get("title"), str):
                    pages_by_title[pg["title"]] = pg
            for title in batch:
                final, redirected = _resolve_chain(title, norm, redir)
                page = pages_by_title.get(final)
                errors = []
                if page is None:
                    status, qid = "missing_title", None
                    errors.append("title_not_in_response")
                elif page.get("invalid"):
                    status, qid = "missing_title", None
                    errors.append(f"invalid_title:{page.get('invalidreason', '')}")
                elif page.get("missing"):
                    status, qid = "missing_title", None
                else:
                    pp = page.get("pageprops") or {}
                    qid = pp.get("wikibase_item")
                    if not (isinstance(qid, str) and _QID_RE.match(qid)):
                        status, qid = "no_wikibase_item", None
                    elif redirected:
                        status = "redirect_resolved"
                    else:
                        status = "ok"
                rec = {"title": title, "resolved_title": final, "qid": qid,
                       "title_status": status, "errors": errors}
                resolved[title] = rec
                part.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")
            part.flush()
            print(f"[titles] batch {n}/{len(batches)} ({len(resolved)} resolved)",
                  file=sys.stderr, flush=True)
            time.sleep(sleep)
    return resolved


# --------------------------------------------------------------------------- #
# value decoding                                                              #
# --------------------------------------------------------------------------- #
def _calendar_tail(model) -> str:
    if isinstance(model, str) and "/" in model:
        return model.rsplit("/", 1)[-1]
    return model if isinstance(model, str) else ""


def _unit_tail(unit) -> str:
    if isinstance(unit, str) and unit.startswith("http") and "/" in unit:
        return unit.rsplit("/", 1)[-1]
    return unit if isinstance(unit, str) else "1"


def decode_datavalue(datavalue):
    """Wikidata datavalue -> DECODED_VALUE dict (recon contract), or an
    'unresolved' marker for an unknown datavalue type (adapter -> review, never
    a guess). entityid label/sitelink/aliases are left null here and filled in
    the later batched label pass."""
    if not isinstance(datavalue, dict):
        return {"type": "unknown", "unresolved": True, "raw": datavalue}
    dvtype = datavalue.get("type")
    value = datavalue.get("value")
    if dvtype == "time" and isinstance(value, dict):
        precision = value.get("precision")
        return {
            "type": "time",
            "time": value.get("time"),
            "precision": precision,
            "date_precision": _PRECISION_LABEL.get(precision, "coarser"),
            "calendarmodel": _calendar_tail(value.get("calendarmodel")),
        }
    if dvtype == "wikibase-entityid" and isinstance(value, dict):
        return {
            "type": "entityid",
            "id": value.get("id"),
            "label": None,
            "sitelink": None,
            "aliases": None,
        }
    if dvtype == "quantity" and isinstance(value, dict):
        return {
            "type": "quantity",
            "amount": value.get("amount"),
            "unit": _unit_tail(value.get("unit")),
            "unit_label": None,
            "upperBound": value.get("upperBound"),
            "lowerBound": value.get("lowerBound"),
        }
    if dvtype == "monolingualtext" and isinstance(value, dict):
        return {"type": "monolingualtext", "text": value.get("text"),
                "language": value.get("language")}
    if dvtype == "string":
        return {"type": "string", "value": value}
    return {"type": dvtype, "unresolved": True, "raw": value}


def decode_snak(snak):
    """(decoded_value_or_None, snaktype, unresolved_type_or_None). A value snak
    -> DECODED_VALUE; novalue/somevalue -> (None, snaktype, None)."""
    if not isinstance(snak, dict):
        return None, "value", None
    snaktype = snak.get("snaktype", "value")
    if snaktype != "value":
        return None, snaktype, None
    decoded = decode_datavalue(snak.get("datavalue"))
    unresolved = decoded.get("type") if decoded.get("unresolved") else None
    return decoded, "value", unresolved


def decode_statement(claim, unresolved_types: set):
    """One claim -> STATEMENT dict (rank, snaktype, value, whitelisted
    qualifiers in source order)."""
    mainsnak = claim.get("mainsnak", {})
    value, snaktype, unres = decode_snak(mainsnak)
    if unres:
        unresolved_types.add(unres)
    qualifiers = {}
    src_quals = claim.get("qualifiers", {})
    if isinstance(src_quals, dict):
        for qprop in QUALIFIER_WHITELIST:
            snaks = src_quals.get(qprop)
            if not isinstance(snaks, list):
                continue
            decoded_list = []
            for snak in snaks:
                qval, qsnaktype, qunres = decode_snak(snak)
                if qunres:
                    unresolved_types.add(qunres)
                if qsnaktype == "value" and qval is not None:
                    decoded_list.append(qval)
                else:
                    decoded_list.append({"snaktype": qsnaktype, "value": None})
            if decoded_list:
                qualifiers[qprop] = decoded_list
    return {"rank": claim.get("rank", "normal"), "snaktype": snaktype,
            "value": value, "qualifiers": qualifiers}


def decode_claims(entity_claims, unresolved_types: set, whitelist=None) -> dict:
    """Entity's claims dict -> {prop: [STATEMENT,...]} for whitelisted props
    only, statement list order = source order (NOT re-sorted).

    ``whitelist`` (optional, an iterable of property ids) narrows the stored
    properties — the people DEATH harvester passes the four death properties
    so its cache is a death-scoped slice; None keeps the full
    PROPERTY_WHITELIST (the legacy tool behavior, byte-unchanged)."""
    out = {}
    if not isinstance(entity_claims, dict):
        return out
    for prop in (PROPERTY_WHITELIST if whitelist is None else sorted(whitelist)):
        claims = entity_claims.get(prop)
        if not isinstance(claims, list):
            continue
        statements = [decode_statement(c, unresolved_types)
                      for c in claims if isinstance(c, dict)]
        if statements:
            out[prop] = statements
    return out


def _empty_state(revid=None, ts=None):
    return {"revid": revid, "ts": ts, "exists": False, "is_redirect": False,
            "redirect_to": None, "claims": {}}


def state_from_entity(entity_dict, revid, ts, unresolved_types: set, whitelist=None):
    """A parsed Wikidata entity dict (from revision content OR wbgetentities)
    -> STATE_BLOCK. Branches on a redirect stub {entity, redirect} (no claims
    key) — its empty claims must never read as 'no value'. ``whitelist``
    narrows the stored properties (see decode_claims); None = legacy full set."""
    if not isinstance(entity_dict, dict):
        return _empty_state(revid, ts), None
    if "redirect" in entity_dict:
        target = entity_dict.get("redirect")
        state = {"revid": revid, "ts": ts, "exists": True, "is_redirect": True,
                 "redirect_to": target, "claims": {}}
        return state, target
    claims = decode_claims(entity_dict.get("claims", {}), unresolved_types, whitelist)
    state = {"revid": revid, "ts": ts, "exists": True, "is_redirect": False,
             "redirect_to": None, "claims": claims}
    return state, None


# --------------------------------------------------------------------------- #
# cutoff (per-entity pinned revision) + current (batched) fetches             #
# --------------------------------------------------------------------------- #
def fetch_cutoff_state(qid: str, cutoff_ts: str, unresolved_types: set, whitelist=None):
    """STATE_BLOCK for the newest Wikidata revision of `qid` at or before
    cutoff_ts (rvstart inclusive, rvdir=older, rvlimit=1). exists=False when the
    entity page is missing or predates the pin. ``whitelist`` narrows the
    stored properties (see decode_claims); None = legacy full set."""
    payload = api_get(WIKIDATA_API, {
        "action": "query", "prop": "revisions", "formatversion": "2",
        "titles": qid, "rvstart": cutoff_ts, "rvdir": "older", "rvlimit": "1",
        "rvprop": "ids|timestamp|content", "rvslots": "main",
    })
    if "error" in payload:
        raise RuntimeError(f"wikidata revisions error for {qid}: {payload['error']}")
    pages = payload.get("query", {}).get("pages", [])
    if not pages:
        return _empty_state(), "cutoff_no_pages"
    page = pages[0]
    if page.get("missing") or page.get("invalid"):
        return _empty_state(), "cutoff_entity_missing"
    revs = page.get("revisions") or []
    if not revs:
        return _empty_state(), "cutoff_predates_entity"
    rev = revs[0]
    revid = rev.get("revid")
    ts = rev.get("timestamp")
    slot = rev.get("slots", {}).get("main", {})
    if slot.get("texthidden") or "content" not in slot:
        return _empty_state(revid, ts), f"cutoff_content_hidden:{revid}"
    try:
        entity = json.loads(slot["content"])
    except (ValueError, TypeError):
        return _empty_state(revid, ts), f"cutoff_content_unparseable:{revid}"
    state, redirect_to = state_from_entity(entity, revid, ts, unresolved_types, whitelist)
    if redirect_to:
        return state, f"cutoff_state_is_redirect:{redirect_to}"
    return state, None


def wbget_batch(ids, props: str, extra: dict, sleep: float):
    """id -> entity dict for a batch, tolerating the poison-batch failure: a
    single deleted id makes wbgetentities return ZERO entities with a top-level
    no-such-entity error, so the reported id is marked missing and the batch is
    retried until it succeeds. Redirected ids resolve server-side to their
    target; deleted ids get {"missing": True, "id": id}."""
    remaining = list(ids)
    out = {}
    guard = 0
    while remaining:
        guard += 1
        if guard > len(ids) + 5:
            for i in remaining:
                out.setdefault(i, {"missing": True, "id": i})
            break
        payload = api_get(WIKIDATA_API, {
            "action": "wbgetentities", "ids": "|".join(remaining),
            "props": props, **extra,
        })
        if "error" in payload:
            err = payload["error"]
            if err.get("code") == "no-such-entity":
                bad = err.get("id")
                if bad in remaining:
                    out[bad] = {"missing": True, "id": bad}
                    remaining = [i for i in remaining if i != bad]
                    continue
                # unidentifiable culprit: fall back to per-id so one bad id
                # cannot hide the rest.
                if len(remaining) > 1:
                    half = max(1, len(remaining) // 2)
                    chunks = [remaining[:half], remaining[half:]]
                    remaining = []
                    for chunk in chunks:
                        out.update(wbget_batch(chunk, props, extra, sleep))
                    break
                out[remaining[0]] = {"missing": True, "id": remaining[0]}
                remaining = []
                continue
            raise RuntimeError(f"wbgetentities error: {err}")
        entities = payload.get("entities", {})
        for requested in remaining:
            ent = entities.get(requested)
            if ent is None:
                # possibly redirected: entity keyed under its target id
                for cand in entities.values():
                    if (isinstance(cand, dict)
                            and cand.get("redirects", {}).get("from") == requested):
                        ent = cand
                        break
            out[requested] = ent if isinstance(ent, dict) else {"missing": True, "id": requested}
        remaining = []
        time.sleep(sleep)
    return out


def fetch_current_state(qid: str, entity, unresolved_types: set, whitelist=None):
    """STATE_BLOCK for the CURRENT entity (from a wbgetentities response),
    pinned by lastrevid. A redirect (id != requested) is recorded, not
    followed. ``whitelist`` narrows the stored properties (see decode_claims);
    None = legacy full set."""
    if not isinstance(entity, dict) or "missing" in entity:
        return _empty_state(), "current_entity_missing"
    revid = entity.get("lastrevid")
    ts = entity.get("modified")
    resolved_id = entity.get("id")
    if isinstance(resolved_id, str) and resolved_id != qid:
        return ({"revid": revid, "ts": ts, "exists": True, "is_redirect": True,
                 "redirect_to": resolved_id, "claims": {}},
                f"current_state_is_redirect:{resolved_id}")
    state, redirect_to = state_from_entity(entity, revid, ts, unresolved_types, whitelist)
    if redirect_to:
        return state, f"current_state_is_redirect:{redirect_to}"
    return state, None


# --------------------------------------------------------------------------- #
# referenced-entity label resolution                                          #
# --------------------------------------------------------------------------- #
def iter_values(state: dict):
    """Yield every DECODED_VALUE (main + qualifier) carrying a 'type' key."""
    for statements in state.get("claims", {}).values():
        for st in statements:
            val = st.get("value")
            if isinstance(val, dict) and "type" in val:
                yield val
            for qvals in st.get("qualifiers", {}).values():
                for qv in qvals:
                    if isinstance(qv, dict) and "type" in qv:
                        yield qv


def collect_ref_qids(state: dict) -> set:
    """Q-ids referenced by a state's entityid values and quantity units (the
    ones needing a label/sitelink lookup)."""
    qids = set()
    for val in iter_values(state):
        if val.get("type") == "entityid":
            qid = val.get("id")
            if isinstance(qid, str) and _QID_RE.match(qid):
                qids.add(qid)
        elif val.get("type") == "quantity":
            unit = val.get("unit")
            if isinstance(unit, str) and _QID_RE.match(unit):
                qids.add(unit)
    return qids


def fill_labels(state: dict, labels: dict) -> set:
    """Bake resolved labels into a state's entityid + quantity-unit values.
    Returns the set of referenced Q-ids with NO label at all (deleted items ->
    'label_unresolved' errors)."""
    unresolved = set()
    for val in iter_values(state):
        if val.get("type") == "entityid":
            qid = val.get("id")
            info = labels.get(qid)
            if info is None:
                if isinstance(qid, str) and _QID_RE.match(qid):
                    unresolved.add(qid)
                val["label"], val["sitelink"], val["aliases"] = None, None, []
            else:
                val["label"] = info.get("label")
                val["sitelink"] = info.get("sitelink")
                val["aliases"] = list(info.get("aliases", []))
                if info.get("label") is None and info.get("sitelink") is None:
                    unresolved.add(qid)
        elif val.get("type") == "quantity":
            unit = val.get("unit")
            if isinstance(unit, str) and _QID_RE.match(unit):
                info = labels.get(unit)
                val["unit_label"] = info.get("label") if info else None
    return unresolved


def resolve_labels(qids, sleep: float, part_path: Path):
    """qid -> {"label", "sitelink", "aliases"} via batched wbgetentities
    (props=labels|sitelinks|aliases). Resumable via part_path."""
    resolved = _load_part(part_path, key="qid")
    todo = sorted((q for q in qids if q not in resolved), key=qid_sort_key)
    print(f"[labels] {len(resolved)} already resolved, {len(todo)} to resolve "
          f"(~{(len(todo) + BATCH_SIZE - 1) // BATCH_SIZE} batches)",
          file=sys.stderr, flush=True)
    batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
    with open(part_path, "a", encoding="utf-8", newline="\n") as part:
        for n, batch in enumerate(batches, 1):
            entities = wbget_batch(
                batch, "labels|sitelinks|aliases",
                {"languages": "en", "sitefilter": "enwiki"}, sleep,
            )
            for qid in batch:
                ent = entities.get(qid)
                if not isinstance(ent, dict) or "missing" in ent:
                    rec = {"qid": qid, "label": None, "sitelink": None, "aliases": []}
                else:
                    label = ent.get("labels", {}).get("en", {}).get("value")
                    sitelink = ent.get("sitelinks", {}).get("enwiki", {}).get("title")
                    aliases = sorted(
                        a.get("value", "")
                        for a in ent.get("aliases", {}).get("en", [])
                        if isinstance(a, dict) and a.get("value")
                    )
                    rec = {"qid": qid, "label": label, "sitelink": sitelink,
                           "aliases": aliases}
                resolved[qid] = rec
                part.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")
            part.flush()
            if n % 10 == 0 or n == len(batches):
                print(f"[labels] batch {n}/{len(batches)} ({len(resolved)} resolved)",
                      file=sys.stderr, flush=True)
    return resolved


# --------------------------------------------------------------------------- #
# resumable part files                                                        #
# --------------------------------------------------------------------------- #
def _load_part(path: Path, key: str) -> dict:
    """key-value -> row from a previous run's append-only part file. A torn last
    line from an interrupted append is expected and skipped."""
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
            k = row.get(key) if isinstance(row, dict) else None
            if isinstance(k, str) and k:
                out[k] = row
    return out


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cache_identifier(out_path: Path, as_given: str) -> str:
    """LOCATION-INDEPENDENT identifier for the cache file recorded in the
    sidecar: a machine-absolute path there would leak the local layout and make
    identical fetches byte-different across checkouts. The package-default dir
    maps to its repo-relative id; any other destination keeps the CLI path."""
    try:
        resolved = out_path.resolve()
    except OSError:
        return as_given
    if resolved.parent == _DEFAULT_CACHE_DIR:
        return f"stage1/cache/{resolved.name}"
    return as_given


# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m stage1.tools.fetch_wikidata_people",
        description="One-time Wikidata entity-history enrichment fetch for the people adapter.",
    )
    parser.add_argument("--changes", action="append", default=None,
                        help="changes CSV(s) with a 'title' column (repeatable). "
                             "Default: the head + tail people change sets.")
    parser.add_argument("--xlsx", action="append", default=None,
                        help="gold xlsx(es) whose 'Title' column adds any "
                             "xlsx-only titles (repeatable).")
    parser.add_argument("--cutoff-ts", default=DEFAULT_CUTOFF_TS,
                        help=f"Wikidata cutoff pin (rvstart, inclusive, rvdir=older). "
                             f"Default {DEFAULT_CUTOFF_TS} (the infobox before-snapshot).")
    parser.add_argument("--out", default=str(_DEFAULT_CACHE_DIR / "people_wd_entities.jsonl"),
                        help="cache output path (default stage1/cache/people_wd_entities.jsonl)")
    parser.add_argument("--meta", default=str(_DEFAULT_CACHE_DIR / "people_wd_entities.meta.json"),
                        help="sidecar metadata path")
    parser.add_argument("--sleep", type=float, default=0.2,
                        help="seconds to sleep between API requests (politeness)")
    parser.add_argument("--limit", type=int, default=0,
                        help="if >0, only process the first N titles (smoke test)")
    parser.add_argument("--maxlag", default="5",
                        help="server-lag threshold sent on every request "
                             "(default 5, the polite standard; raise slightly to "
                             "5-10 to grind through a persistently-lagged cluster).")
    args = parser.parse_args(argv)

    global _MAXLAG
    _MAXLAG = str(args.maxlag)

    # the wikipedia study moved to 0_prior_work/ at the repo root, one level above 1_temporal_facts/
    repo_root = Path(__file__).resolve().parents[3] / "0_prior_work"
    changes = args.changes or [
        str(repo_root / "wikipedia/people/changes.csv"),
        str(repo_root / "wikipedia/people/changes_tail.csv"),
    ]
    xlsxes = args.xlsx or [
        str(repo_root / "wikipedia/people/significant_changes_final.xlsx"),
        str(repo_root / "wikipedia/people/significant_changes_tail.xlsx"),
    ]

    # ---- load the universe of titles ----
    titles = set()
    input_files = []
    load_errors = []
    for path_str in changes:
        path = Path(path_str)
        if not path.is_file():
            print(f"error: changes file not found: {path}", file=sys.stderr)
            return 2
        got, errs = load_csv_titles(path)
        titles |= got
        load_errors.extend(errs)
        input_files.append({"path": str(path), "sha1": sha1_file(path),
                            "titles": len(got), "kind": "changes_csv"})
    xlsx_extra = set()
    for path_str in xlsxes:
        path = Path(path_str)
        if not path.is_file():
            print(f"warning: xlsx not found (skipped): {path}", file=sys.stderr)
            continue
        got, errs = load_xlsx_titles(path)
        xlsx_extra |= (got - titles)
        titles |= got
        load_errors.extend(errs)
        input_files.append({"path": str(path), "sha1": sha1_file(path),
                            "titles": len(got), "kind": "gold_xlsx"})
    titles = sorted(titles)
    if args.limit and args.limit > 0:
        titles = titles[:args.limit]
    print(f"[fetch] {len(titles)} unique titles "
          f"({len(xlsx_extra)} xlsx-only) from {len(input_files)} input files",
          file=sys.stderr, flush=True)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    titles_part = out_path.with_suffix(out_path.suffix + ".titles.part")
    cutoff_part = out_path.with_suffix(out_path.suffix + ".cutoff.part")
    current_part = out_path.with_suffix(out_path.suffix + ".current.part")
    labels_part = out_path.with_suffix(out_path.suffix + ".labels.part")

    # ---- phase 1: resolve titles -> QIDs ----
    title_info = resolve_titles(titles, args.sleep, titles_part)
    unique_qids = sorted(
        {rec["qid"] for rec in title_info.values()
         if isinstance(rec.get("qid"), str) and _QID_RE.match(rec["qid"])},
        key=qid_sort_key,
    )
    print(f"[fetch] {len(unique_qids)} unique QIDs to fetch "
          f"(dedup of {sum(1 for r in title_info.values() if r.get('qid'))} qid-bearing titles)",
          file=sys.stderr, flush=True)

    unresolved_types = set()

    # ---- phase 2: cutoff states (per-entity pinned revision) ----
    cutoff_states = _load_part(cutoff_part, key="qid")
    cutoff_todo = [q for q in unique_qids if q not in cutoff_states]
    print(f"[cutoff] {len(cutoff_states)} cached, {len(cutoff_todo)} to fetch "
          f"(1 revision request each)", file=sys.stderr, flush=True)
    with open(cutoff_part, "a", encoding="utf-8", newline="\n") as part:
        for i, qid in enumerate(cutoff_todo, 1):
            state, err = fetch_cutoff_state(qid, args.cutoff_ts, unresolved_types)
            rec = {"qid": qid, "state": state, "error": err}
            cutoff_states[qid] = rec
            part.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")
            part.flush()
            time.sleep(args.sleep)
            if i % 100 == 0 or i == len(cutoff_todo):
                print(f"[cutoff] {i}/{len(cutoff_todo)}", file=sys.stderr, flush=True)

    # ---- phase 3: current states (batched wbgetentities) ----
    current_states = _load_part(current_part, key="qid")
    current_todo = [q for q in unique_qids if q not in current_states]
    print(f"[current] {len(current_states)} cached, {len(current_todo)} to fetch "
          f"(~{(len(current_todo) + BATCH_SIZE - 1) // BATCH_SIZE} batches)",
          file=sys.stderr, flush=True)
    with open(current_part, "a", encoding="utf-8", newline="\n") as part:
        batches = [current_todo[i:i + BATCH_SIZE]
                   for i in range(0, len(current_todo), BATCH_SIZE)]
        for n, batch in enumerate(batches, 1):
            entities = wbget_batch(batch, "info|claims", {}, args.sleep)
            for qid in batch:
                state, err = fetch_current_state(qid, entities.get(qid), unresolved_types)
                rec = {"qid": qid, "state": state, "error": err}
                current_states[qid] = rec
                part.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")
            part.flush()
            if n % 10 == 0 or n == len(batches):
                print(f"[current] batch {n}/{len(batches)}", file=sys.stderr, flush=True)

    # ---- phase 4: referenced-entity labels ----
    ref_qids = set()
    for rec in cutoff_states.values():
        ref_qids |= collect_ref_qids(rec["state"])
    for rec in current_states.values():
        ref_qids |= collect_ref_qids(rec["state"])
    print(f"[labels] {len(ref_qids)} distinct referenced QIDs to resolve",
          file=sys.stderr, flush=True)
    labels = resolve_labels(ref_qids, args.sleep, labels_part)

    # ---- phase 5: bake labels, assemble sorted cache ----
    for rec in cutoff_states.values():
        rec["label_unresolved"] = sorted(fill_labels(rec["state"], labels))
    for rec in current_states.values():
        rec["label_unresolved"] = sorted(fill_labels(rec["state"], labels))

    # Rescan every assembled state for unknown datavalue types so the sidecar's
    # unresolved list is complete even on a RESUMED run (where decode happened
    # in an earlier process and did not populate unresolved_types this run).
    for rec in list(cutoff_states.values()) + list(current_states.values()):
        for val in iter_values(rec["state"]):
            if val.get("unresolved"):
                unresolved_types.add(val.get("type"))

    rows = []
    for title in titles:
        info = title_info.get(title)
        if info is None:  # only when --limit changed mid-resume; resolve empty
            info = {"resolved_title": title, "qid": None,
                    "title_status": "missing_title", "errors": ["not_resolved"]}
        qid = info.get("qid")
        errors = list(info.get("errors", []))
        cutoff_block = None
        current_block = None
        if qid:
            crec = cutoff_states.get(qid)
            if crec is not None:
                cutoff_block = crec["state"]
                if crec.get("error"):
                    errors.append(crec["error"])
                for u in crec.get("label_unresolved", []):
                    errors.append(f"label_unresolved:{u}")
            else:
                errors.append("cutoff_not_fetched")
            urec = current_states.get(qid)
            if urec is not None:
                current_block = urec["state"]
                if urec.get("error"):
                    errors.append(urec["error"])
                for u in urec.get("label_unresolved", []):
                    lbl = f"label_unresolved:{u}"
                    if lbl not in errors:
                        errors.append(lbl)
            else:
                errors.append("current_not_fetched")
        rows.append({
            "title": title,
            "resolved_title": info.get("resolved_title", title),
            "qid": qid,
            "title_status": info.get("title_status", "missing_title"),
            "cutoff": cutoff_block,
            "current": current_block,
            "errors": errors,
        })
    rows.sort(key=row_sort_key)

    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
    os.replace(tmp_path, out_path)

    # ---- counts + sidecar ----
    status_counts = {}
    for row in rows:
        status_counts[row["title_status"]] = status_counts.get(row["title_status"], 0) + 1
    cutoff_missing = sum(1 for r in cutoff_states.values()
                         if not r["state"]["exists"])
    cutoff_redirect = sum(1 for r in cutoff_states.values()
                          if r["state"]["is_redirect"])
    current_missing = sum(1 for r in current_states.values()
                          if not r["state"]["exists"])
    current_redirect = sum(1 for r in current_states.values()
                           if r["state"]["is_redirect"])
    rows_with_errors = sum(1 for r in rows if r["errors"])
    missing_titles = sorted(r["title"] for r in rows
                            if r["title_status"] == "missing_title")
    no_wb = sorted(r["title"] for r in rows
                   if r["title_status"] == "no_wikibase_item")
    redirect_titles = sorted(r["title"] for r in rows
                             if r["title_status"] == "redirect_resolved")
    labels_missing = sorted((q for q, r in labels.items()
                             if r.get("label") is None and r.get("sitelink") is None),
                            key=qid_sort_key)

    meta = {
        "tool_version": TOOL_VERSION,
        "decoder_version": DECODER_VERSION,
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "current_asof": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cutoff_ts": args.cutoff_ts,
        "maxlag": _MAXLAG,
        "endpoints": {"enwiki": ENWIKI_API, "wikidata": WIKIDATA_API},
        "user_agent": USER_AGENT,
        "property_whitelist": PROPERTY_WHITELIST,
        "qualifier_whitelist": QUALIFIER_WHITELIST,
        "label_language": "en",
        "sitefilter": "enwiki",
        "query_params": {
            "qid_call": {"action": "query", "prop": "pageprops",
                         "ppprop": "wikibase_item", "redirects": "1",
                         "formatversion": "2", "batch_size": BATCH_SIZE},
            "cutoff_revision_call": {"action": "query", "prop": "revisions",
                                     "rvprop": "ids|timestamp|content",
                                     "rvslots": "main", "rvdir": "older",
                                     "rvlimit": "1", "rvstart": args.cutoff_ts,
                                     "formatversion": "2", "maxlag": _MAXLAG},
            "current_call": {"action": "wbgetentities", "props": "info|claims",
                             "batch_size": BATCH_SIZE, "maxlag": _MAXLAG},
            "label_call": {"action": "wbgetentities",
                           "props": "labels|sitelinks|aliases", "languages": "en",
                           "sitefilter": "enwiki", "batch_size": BATCH_SIZE},
        },
        "input_files": input_files,
        "load_errors": load_errors,
        "unresolved_datavalue_types": sorted(unresolved_types),
        "counts": {
            "titles": len(rows),
            "resolved_qids": len(unique_qids),
            "title_status": status_counts,
            "xlsx_only_titles": len(xlsx_extra),
            "cutoff_states_missing": cutoff_missing,
            "cutoff_states_redirect": cutoff_redirect,
            "current_states_missing": current_missing,
            "current_states_redirect": current_redirect,
            "referenced_qids": len(ref_qids),
            "labels_resolved": len(labels) - len(labels_missing),
            "labels_missing": len(labels_missing),
            "rows_with_errors": rows_with_errors,
        },
        "missing_titles": missing_titles,
        "no_wikibase_item_titles": no_wb,
        "redirect_resolved_titles": redirect_titles,
        "labels_unresolved_qids": labels_missing,
        "cache_file": cache_identifier(out_path, args.out),
        "cache_sha1": sha1_file(out_path),
    }
    meta_path = Path(args.meta)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    with open(meta_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")

    # ---- drop part files only after a clean full assembly ----
    for p in (titles_part, cutoff_part, current_part, labels_part):
        if p.exists():
            p.unlink()

    print(f"[fetch] wrote {len(rows)} rows "
          f"({len(unique_qids)} QIDs, {len(missing_titles)} missing titles, "
          f"{len(no_wb)} no-wikibase, {len(redirect_titles)} redirect) -> {out_path}",
          file=sys.stderr, flush=True)
    print(f"[fetch] sidecar -> {meta_path}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
