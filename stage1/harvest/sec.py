"""SEC Item 5.02 officer-change harvester — the reference harvester.

Ports the legacy two-step SEC harvest under stage1/harvest/ (a faithful
REWRITE, not an import of sec/ code) into deterministic, parameterized,
resumable, polite code that writes a frozen snapshot the offline SEC adapter
(stage1/adapters/sec.py) reads unchanged.

It produces the three snapshot files the adapter consumes, parameterized by
[cutoff, asof] + the S&P 500 universe:

FILE 1 — sec_5502_2026.jsonl  (port of sec/sec1_harvest.py)
  8-K Item 5.02 filings whose filer CIK is in the universe and whose EVENT date
  (period_ending / reportDate) falls in [cutoff, asof]. Two discovery paths:
    * FTS (default, full-universe modern window): EDGAR full-text search month
      by month, q="Item 5.02", forms=8-K, paginated by `from`; ABORTS loudly if
      a month slice hits the ~10k `from` cap rather than truncating.
    * submissions API per CIK (used for --sample, and the cap-free back-dating
      route): data.sec.gov/submissions/CIK{cik10}.json, keeping 8-K filings with
      "5.02" in items and reportDate in-window.
  Then the primary document body is fetched, cleaned, and the Item 5.02
  narrative isolated + trimmed, with senior/appt/depart regex flags.

FILE 2 — sec_verified.jsonl  (port of sec/sec_verify.py; the NO-LLM ground truth)
  For the CEO/CFO subset of File-1 rows, before/after officer names are read
  DETERMINISTICALLY from consecutive SOX s302 certifications (Exhibit 31.1=CEO /
  31.2=CFO) located by CONTENT: after = signer of the latest 10-Q/10-K filed
  on/before asof; before = signer of the latest filed strictly before the event.
  An unreadable cert leaves the side empty (honest), never guessed. Because the
  'after' is pinned at asof, coverage.asof_exact = True.

FILE 3 — sp500_universe.csv  copied verbatim into the snapshot (self-contained).

None of this uses an LLM anywhere.
"""

from __future__ import annotations

import csv
import hashlib
import html
import re
import sys
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

from stage1.harvest import Harvester
from stage1.harvest.http import EdgarClient, HttpError
from stage1.harvest.snapshot import sha1_file

TOOL_VERSION = "harvest_sec:v1"

FTS_ENDPOINT = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_ENDPOINT = "https://data.sec.gov/submissions/CIK{cik10}.json"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

FTS_PAGE = 100
# EDGAR FTS refuses `from` beyond ~10000; abort a slice that would exceed it
# rather than silently truncating (narrow to week slices for a huge window).
FTS_FROM_CAP = 10000

# EDGAR FTS windows by FILING date, but we KEEP hits by EVENT date. An 8-K is
# filed AFTER its event (statutory deadline: 4 business days). So an in-window
# event near `asof` can be FILED in a later month than asof's, and querying only
# the window's own filing months would silently miss it. We therefore extend the
# FTS filing-month range past asof by this many days — a generous margin over the
# 4-business-day deadline (weekends/holidays/short amendments) that adds at most
# one trailing month — so coverage.asof is an honest promise for the FTS path.
FTS_FILING_LAG_DAYS = 20

RAW_FILENAME = "sec_5502_2026.jsonl"
VERIFIED_FILENAME = "sec_verified.jsonl"
UNIVERSE_FILENAME = "sp500_universe.csv"

DEFAULT_ROLES = ["CEO", "CFO"]

# ---------------------------------------------------------------------------
# Regexes ported verbatim from the legacy pipeline.
# File-1 senior-officer prefilter flags (sec1_harvest.py):
SENIOR = re.compile(
    r"\b(chief executive officer|chief financial officer|chief operating officer|"
    r"\bC\.?E\.?O\.?\b|\bC\.?F\.?O\.?\b|\bC\.?O\.?O\.?\b|president)\b", re.I)
APPT = re.compile(
    r"\b(appoint|elect|nam(?:e|ed|ing)|hir(?:e|ed|ing)|promot|succeed)\b", re.I)
DEPART = re.compile(
    r"\b(resign|retir|depart|terminat|step(?:ping|s|ped)?\s+down|"
    r"separation|transition|remov)\b", re.I)

# File-2 candidate detection (sec2_curate.py): role + incoming/outgoing names.
ROLES = [
    ("CEO",       r"chief\s+executive\s+officer|\bC\.?E\.?O\.?\b"),
    ("CFO",       r"chief\s+financial\s+officer|\bC\.?F\.?O\.?\b"),
    ("COO",       r"chief\s+operating\s+officer|\bC\.?O\.?O\.?\b"),
    ("President", r"\bpresident\b"),
    ("Chair",     r"chair(?:man|person|woman|)\b"),
]
ACTION = re.compile(
    r"appoint|elect|nam(?:e|ed|ing)|promot|hir(?:e|ed|ing)|resign|retir|"
    r"step(?:ping|s|ped)?\s+down|depart|termin|transition|succeed|separation|"
    r"will\s+become|assume|to\s+serve\s+as", re.I)
NM = r"(?:Mr\.|Ms\.|Mrs\.|Dr\.)?\s*([A-Z][A-Za-z'\-]+(?:\s+(?:[A-Z]\.|[A-Z][A-Za-z'\-]+)){1,3})"
INC_PATS = [re.compile(p, re.I | re.M) for p in [
    r"(?:appointed|named|elected|promoted|hired|designated)\s+" + NM + r"\s+(?:as|to)\b",
    NM + r"\s+(?:as|to\s+serve\s+as|will\s+serve\s+as|will\s+become|was\s+appointed|"
         r"has\s+been\s+appointed|will\s+be\s+appointed)\s+(?:the\s+)?(?:new\s+)?(?:company'?s\s+)?(?:interim\s+)?"
         r"(?:chief|president|chair)",
    NM + r"\s+will\s+succeed\b",
]]
OUT_PATS = [re.compile(p, re.I | re.M) for p in [
    r"(?:resignation|retirement|departure|separation)\s+of\s+" + NM,
    NM + r"\s+(?:notified|informed|has\s+notified|will\s+retire|is\s+retiring|will\s+resign|"
         r"is\s+resigning|resigned|retired|has\s+resigned|has\s+retired|will\s+step\s+down|"
         r"is\s+stepping\s+down|stepped\s+down|will\s+depart|is\s+departing|will\s+leave)",
    r"succeed(?:s|ing|ed)?\s+" + NM,
    r"replac(?:e|es|ing|ed)\s+" + NM,
]]
_STOP = {"the", "board", "company", "inc", "corporation", "directors", "director", "officer"}

# Cert reading (sec_verify.py).
CEO_PHRASES = ("principal executive officer", "chief executive officer")
CFO_PHRASES = ("principal financial officer", "chief financial officer")
_I_CERTIFY = re.compile(r"\bI,\s+([A-Z][A-Za-z.'\- ]+?)\s*,(?:[^,]*,){0,3}\s*certify", re.I)


# ---------------------------------------------------------------------------
# Pure text helpers (unit-testable offline, no network).
# ---------------------------------------------------------------------------
def clean_html(html_text: str) -> str:
    """Decode entities twice (filings double-encode &amp;#160;), strip tags,
    collapse whitespace. Port of sec1_harvest.clean — used for File-1
    section_text so it reproduces the legacy extraction byte-for-byte."""
    t = html_text or ""
    for _ in range(2):
        t = html.unescape(t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def clean_cert(t: str) -> str:
    """Cert-body cleaner (port of sec_verify.clean): additionally folds
    zero-width / nbsp characters to spaces before collapsing, so 'I, <name>,
    certify' still matches when a filer pads it with &nbsp;."""
    t = t or ""
    for _ in range(2):
        t = html.unescape(t)
    t = re.sub(r"[​‌‍⁠﻿\xa0  ]", " ", re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"\s+", " ", t).strip()


def section_502(clean_text: str) -> str:
    """Isolate the longest Item 5.02 narrative from cleaned text. Port of
    sec1_harvest.section_502 (must run on cleaned text: entities/tags sit
    between 'Item' and '5.02' in raw HTML)."""
    best = ""
    for m in re.finditer(r"Item\s*5\.02", clean_text, re.I):
        seg = clean_text[m.start():]
        nxt = re.search(r"(Item\s*[0-9]\.[0-9]{2}|SIGNATURES?\b|Pursuant to the requirements)", seg[8:])
        seg = seg[:(nxt.start() + 8) if nxt else 2000]
        if len(seg) > len(best):
            best = seg
    return best or clean_text[:2000]


def normalize_name(s: str) -> str:
    """Collapse whitespace, strip stray leading/trailing punctuation. Port of
    sec_verify.normalize_name."""
    s = re.sub(r"\s+", " ", s or "").strip()
    return s.strip(" ,").rstrip(".").strip()


def detect_role(text: str) -> str:
    """Highest-priority principal role appearing within 100 chars of an action
    verb; 'other' when none does. Port of sec2_curate.detect_role."""
    for role, pat in ROLES:
        for m in re.finditer(pat, text, re.I):
            w = text[max(0, m.start() - 100): m.end() + 100]
            if ACTION.search(w):
                return role
    return "other"


def _first(pats, text: str) -> str:
    """First name captured by the first pattern whose match survives the legacy
    filters (first word not in _STOP, >=2 words). Port of sec2_curate._first."""
    for p in pats:
        m = p.search(text)
        if m:
            nm = re.sub(r"\s+", " ", m.group(1)).strip(" ,.")
            if nm and nm.split()[0].lower() not in _STOP and len(nm.split()) >= 2:
                return nm
    return ""


def pad_cik(cik) -> str:
    return f"{int(cik):010d}"


def change_id(cik10: str, event_date: str, adsh: str) -> str:
    """sha1('cik10|event_date|adsh')[:10] — the adapter's legacy join key
    (cik zero-padded, matching sec2_curate.change_id over the padded raw)."""
    return hashlib.sha1(f"{cik10}|{event_date}|{adsh}".encode()).hexdigest()[:10]


def months_in_window(cutoff: date, asof: date):
    """Every YYYY-MM spanned by [cutoff, asof], inclusive — derived from the
    window, never hardcoded."""
    out = []
    y, m = cutoff.year, cutoff.month
    while (y, m) <= (asof.year, asof.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def discovery_filing_months(cutoff: date, asof: date):
    """The YYYY-MM FILING months FTS discovery must query for events in
    [cutoff, asof]. Extends the upper bound to the month of asof + the 8-K
    filing lag so a late-filed in-window event (event at/near asof, filed the
    next month) is still fetched; the event-date filter c_iso <= ev <= a_iso
    then bounds what is KEPT. Lower bound stays at cutoff's month: an 8-K is
    never filed before its event, so a filing for an event >= cutoff is filed
    in cutoff's month or later."""
    return months_in_window(cutoff, asof + timedelta(days=FTS_FILING_LAG_DAYS))


def build_5502_row(cik10, ticker, company, event_date, file_date, form, adsh, doc, section_text):
    """One File-1 row in the exact shape the adapter reads."""
    seg = section_502(section_text)
    seg = seg[:1800]
    return {
        "cik": cik10,
        "ticker": ticker,
        "company": company,
        "event_date": event_date,
        "file_date": file_date,
        "form": form,
        "adsh": adsh,
        "doc_url": doc,
        "section_text": seg,
        "senior": bool(SENIOR.search(seg)),
        "is_appt": bool(APPT.search(seg)),
        "is_depart": bool(DEPART.search(seg)),
    }


# ---------------------------------------------------------------------------
# I/O helpers.
# ---------------------------------------------------------------------------
def load_universe(path: Path) -> dict:
    """{cik10: (ticker, name)} from the S&P 500 universe csv. Rows without a
    numeric cik are skipped (they could never match a padded CIK)."""
    uni = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            raw = (row.get("cik") or "").strip()
            try:
                uni[pad_cik(raw)] = ((row.get("ticker") or "").strip(),
                                     (row.get("name") or "").strip())
            except (TypeError, ValueError):
                continue
    return uni


def _load_part(path: Path, key: str) -> dict:
    """Reload a resume checkpoint (.part jsonl): {row[key]: row}. A torn last
    line of an interrupted append is skipped."""
    import json
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
                out[row[key]] = row
    return out


def _append_part(part_fh, row) -> None:
    import json
    part_fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
    part_fh.flush()


# ---------------------------------------------------------------------------
# The harvester.
# ---------------------------------------------------------------------------
class SecHarvester(Harvester):
    source = "sec"
    tool_version = TOOL_VERSION

    # -- coverage (pure, no network) ---------------------------------------

    def coverage(self, cfg: dict) -> dict:
        cutoff = cfg["cutoff"]
        asof = cfg["asof"]
        cov = {
            "cutoff": cutoff.isoformat() if isinstance(cutoff, date) else str(cutoff),
            "asof": asof.isoformat() if isinstance(asof, date) else str(asof),
            # the 'after' cert is the latest 10-Q/10-K filed on/before asof, so
            # the snapshot is only soundly re-derivable at its EXACT asof.
            "asof_exact": True,
            "precision": "day",
            # HONEST window semantics: discovery keeps filings by EVENT date
            # (period_ending / reportDate), so [cutoff, asof] bounds EVENT dates.
            # The SEC adapter dates a change by its 8-K EFFECTIVE date, which can
            # LEAD the event date (a change announced pre-cutoff but effective
            # in-window). Such a change has a pre-cutoff event date and is NOT
            # harvested, so this coverage window does not promise it. Recording
            # the basis keeps the coverage guarantee and the harvest filter
            # describing the same thing; effective-date completeness near the
            # lower edge is a documented limit (lead time is unbounded, so no
            # finite lower-margin can guarantee it).
            "window_basis": "event_date",
        }
        scope = self._scope_digest(cfg.get("universe"))
        if scope is not None:
            cov["scope"] = scope
        return cov

    @staticmethod
    def _scope_digest(universe_path):
        if not universe_path:
            return None
        path = Path(universe_path)
        if not path.is_file():
            return None
        uni = load_universe(path)
        return {
            "kind": "sp500_universe",
            "file": UNIVERSE_FILENAME,
            "sha1": sha1_file(path),
            "size": len(uni),
        }

    # -- harvest (the only networked method) --------------------------------

    def harvest(self, cfg: dict, writer, ctx: dict) -> None:
        cutoff = cfg["cutoff"]
        asof = cfg["asof"]
        universe_path = cfg.get("universe")
        if not universe_path or not Path(universe_path).is_file():
            raise LookupError(
                "the sec harvester requires --universe pointing at the S&P 500 csv"
            )
        universe = load_universe(Path(universe_path))
        roles = [r.strip().upper() for r in (cfg.get("roles") or DEFAULT_ROLES) if r.strip()]
        sample = cfg.get("sample")
        resume = bool(cfg.get("resume"))
        client = EdgarClient(cfg.get("contact"), max_rps=cfg.get("max_rps", 8))

        stats = {"discovery": None, "fts_total_hits": 0, "universe_5502_deduped": 0,
                 "resumed": resume}

        # ---- FILE 1: discovery -------------------------------------------
        if sample:
            sample_ciks = sorted({pad_cik(c) for c in sample})
            stats["discovery"] = "submissions"
            stats["sample_ciks"] = sample_ciks
            cands = self._discover_submissions(client, sample_ciks, universe, cutoff, asof)
        else:
            stats["discovery"] = "fts"
            cands, fts_hits = self._discover_fts(client, universe, cutoff, asof)
            stats["fts_total_hits"] = fts_hits
        stats["universe_5502_deduped"] = len(cands)
        print(f"[harvest sec] {len(cands)} universe post-cutoff 5.02 filing(s) discovered "
              f"via {stats['discovery']}", file=sys.stderr, flush=True)

        # ---- FILE 1: bodies (resumable) ----------------------------------
        file1_rows = self._fetch_bodies(client, cands, writer.out_dir, resume)

        # ---- FILE 2: verify CEO/CFO via consecutive SOX certs ------------
        file2_rows = self._verify(client, file1_rows, roles, asof, writer.out_dir, resume)

        # ---- emit snapshot files -----------------------------------------
        writer.add_scope_file(UNIVERSE_FILENAME, universe_path, "sp500_universe", len(universe))
        writer.add_jsonl(RAW_FILENAME, file1_rows)
        writer.add_jsonl(VERIFIED_FILENAME, file2_rows)

        # remove resume checkpoints now that the frozen files are written
        for name in (RAW_FILENAME, VERIFIED_FILENAME):
            part = writer.out_dir / (name + ".part")
            if part.exists():
                part.unlink()

        confirmed = sum(1 for r in file2_rows if r["verified"] == "confirmed")
        unconfirmed = sum(1 for r in file2_rows if r["verified"] == "unconfirmed")
        writer.set_params({
            "fts_endpoint": FTS_ENDPOINT,
            "submissions_endpoint": SUBMISSIONS_ENDPOINT,
            "archives_base": ARCHIVES_BASE,
            "user_agent": client.user_agent,
            # the EVENT-date window's own months (what [cutoff, asof] spans) …
            "months_queried": months_in_window(cutoff, asof),
            # … and, for the FTS path, the FILING months actually queried
            # (extended past asof by the filing lag so late-filed in-window
            # events are captured; None when discovery is per-CIK submissions).
            "fts_discovery_filing_months": (
                discovery_filing_months(cutoff, asof)
                if stats["discovery"] == "fts" else None),
            "fts_filing_lag_days": FTS_FILING_LAG_DAYS,
            "rate_max_rps": client.max_rps,
            "cert_roles": roles,
            "cert_filing_forms": ["10-Q", "10-K"],
            "discovery": stats["discovery"],
        })
        writer.set_stats({
            "fts_total_hits": stats["fts_total_hits"],
            "universe_5502_deduped": stats["universe_5502_deduped"],
            "verify": {"roles": roles, "confirmed": confirmed, "unconfirmed": unconfirmed},
            "http_errors": client.errors,
            "resumed": resume,
        })
        print(f"[harvest sec] File 1: {len(file1_rows)} filings; File 2: {len(file2_rows)} "
              f"verify rows ({confirmed} confirmed / {unconfirmed} unconfirmed); "
              f"{len(client.errors)} http miss(es)", file=sys.stderr, flush=True)

    # -- discovery: FTS ----------------------------------------------------

    def _discover_fts(self, client, universe, cutoff, asof):
        """Month-by-month EDGAR full-text search over the FILING months that can
        hold an in-window event (discovery_filing_months: [cutoff, asof + lag]),
        keeping hits by EVENT date. Returns (candidates, total_hits). Dedup by
        (cik, event_date), preferring the original 8-K over amendments."""
        seen: dict = {}
        total_hits = 0
        c_iso, a_iso = cutoff.isoformat(), asof.isoformat()
        for month in discovery_filing_months(cutoff, asof):
            hits = self._fts_month(client, month)
            total_hits += len(hits)
            kept = 0
            for h in hits:
                s = h.get("_source") or {}
                if "5.02" not in (s.get("items") or []):
                    continue
                ciks = s.get("ciks") or []
                if not ciks:
                    continue
                cik = pad_cik(ciks[0])
                if cik not in universe:
                    continue
                ev = s.get("period_ending", "") or ""
                if not (c_iso <= ev <= a_iso):  # window by EVENT date, never file_date
                    continue
                adsh = s.get("adsh")
                _id = h.get("_id") or ""
                doc = _id.split(":", 1)[1] if ":" in _id else ""
                cand = {
                    "cik": cik, "ticker": universe[cik][0], "company": universe[cik][1],
                    "event_date": ev, "file_date": s.get("file_date", "") or "",
                    "form": s.get("form", "8-K") or "8-K", "adsh": adsh, "doc": doc,
                }
                key = (cik, ev)
                if key not in seen or (seen[key]["form"] != "8-K" and cand["form"] == "8-K"):
                    seen[key] = cand
                kept += 1
            print(f"[harvest sec]   {month}: {len(hits)} hits -> {kept} universe/in-window 5.02",
                  file=sys.stderr, flush=True)
        return list(seen.values()), total_hits

    def _fts_month(self, client, month):
        """All 8-K Item-5.02 hits filed in a YYYY-MM (paginated by `from`).
        ABORTS if the slice would exceed the ~10k `from` cap (refuses to
        truncate a too-large month — narrow to week slices instead).

        A persistent HTTP failure mid-pagination is ALSO fatal: keeping pages
        1..N-1 while page N is unreachable would silently under-cover the month
        (the missing later-page candidates are unidentifiable, and the derive
        coverage check never inspects fetch_stats), so we RAISE — like the cap —
        rather than write an incomplete snapshot. A 404 / empty batch is a normal
        end-of-results and is NOT an error (get_json returns {} on 404)."""
        start, end = f"{month}-01", f"{month}-31"
        hits, frm = [], 0
        while True:
            if frm >= FTS_FROM_CAP:
                raise RuntimeError(
                    f"FTS pagination cap ({FTS_FROM_CAP}) reached for {month}: the slice is too "
                    "large to page without truncation — narrow the window (e.g. week slices)"
                )
            params = urllib.parse.urlencode({
                "q": '"Item 5.02"', "forms": "8-K",
                "startdt": start, "enddt": end, "from": frm,
            })
            try:
                d = client.get_json(f"{FTS_ENDPOINT}?{params}")
            except HttpError as exc:
                raise RuntimeError(
                    f"FTS discovery failed for {month} at from={frm} after retries ({exc}): the "
                    "month's later pages are unreachable, so the snapshot would silently "
                    "UNDER-COVER this month's in-window events — aborting rather than writing an "
                    "incomplete snapshot (rerun once EDGAR is reachable; discovery restarts, while "
                    "bodies/verify resume from their .part checkpoints)"
                ) from exc
            batch = ((d.get("hits", {}) or {}).get("hits", [])) if isinstance(d, dict) else []
            if not batch:
                break
            hits.extend(batch)
            if len(batch) < FTS_PAGE:
                break
            frm += FTS_PAGE
        return hits

    # -- discovery: submissions API per CIK --------------------------------

    def _discover_submissions(self, client, ciks, universe, cutoff, asof):
        """Cap-free per-CIK discovery over data.sec.gov submissions `recent`.
        Keeps 8-K filings with '5.02' in items and reportDate in-window. Used for
        --sample and as the back-dating route (v1 reads `recent` only)."""
        c_iso, a_iso = cutoff.isoformat(), asof.isoformat()
        seen: dict = {}
        for cik in ciks:
            url = SUBMISSIONS_ENDPOINT.format(cik10=cik)
            try:
                sub = client.get_json(url)
            except HttpError as exc:
                # A per-CIK discovery failure would silently drop ALL of that
                # filer's in-window filings — a completeness gap the coverage
                # check cannot see. Abort loudly rather than under-cover a CIK.
                raise RuntimeError(
                    f"submissions discovery failed for CIK {cik} after retries ({exc}): that "
                    "filer's filings could not be listed, so the snapshot would silently "
                    "UNDER-COVER this CIK — aborting rather than writing an incomplete snapshot "
                    "(rerun once EDGAR is reachable)"
                ) from exc
            recent = (((sub or {}).get("filings") or {}).get("recent") or {})
            forms = recent.get("form") or []
            accns = recent.get("accessionNumber") or []
            fdates = recent.get("filingDate") or []
            rdates = recent.get("reportDate") or []
            items = recent.get("items") or []
            docs = recent.get("primaryDocument") or []
            ticker, company = universe.get(cik, ("", ""))
            n = min(len(forms), len(accns), len(fdates), len(rdates), len(items), len(docs))
            for i in range(n):
                if forms[i] not in ("8-K", "8-K/A"):
                    continue
                if "5.02" not in (items[i] or ""):
                    continue
                ev = rdates[i] or ""
                if not (c_iso <= ev <= a_iso):
                    continue
                cand = {
                    "cik": cik, "ticker": ticker, "company": company,
                    "event_date": ev, "file_date": fdates[i] or "",
                    "form": forms[i], "adsh": accns[i], "doc": docs[i] or "",
                }
                key = (cik, ev)
                if key not in seen or (seen[key]["form"] != "8-K" and cand["form"] == "8-K"):
                    seen[key] = cand
        return list(seen.values())

    # -- File 1: body fetch ------------------------------------------------

    def _fetch_bodies(self, client, cands, out_dir, resume):
        """Fetch each candidate's primary document, isolate the 5.02 narrative,
        build the File-1 row. Resumable: completed rows are checkpointed by adsh
        to <RAW>.part and skipped on --resume."""
        part_path = out_dir / (RAW_FILENAME + ".part")
        done = _load_part(part_path, "adsh") if resume else {}
        rows_by_adsh = dict(done)
        todo = [c for c in cands if c.get("adsh") not in rows_by_adsh]
        print(f"[harvest sec] bodies: {len(done)} resumed, {len(todo)} to fetch",
              file=sys.stderr, flush=True)
        with open(part_path, "a", encoding="utf-8", newline="\n") as part:
            for i, c in enumerate(todo, 1):
                adsh = c["adsh"]
                acc = (adsh or "").replace("-", "")
                url = f"{ARCHIVES_BASE}/{int(c['cik'])}/{acc}/{c['doc']}"
                try:
                    body = client.get_text(url)
                except HttpError as exc:
                    client.record_miss("body", url, exc)
                    body = ""
                row = build_5502_row(
                    c["cik"], c["ticker"], c["company"], c["event_date"], c["file_date"],
                    c["form"], adsh, url, clean_html(body))
                rows_by_adsh[adsh] = row
                _append_part(part, row)
                if i % 25 == 0 or i == len(todo):
                    print(f"[harvest sec]   fetched {i}/{len(todo)} bodies",
                          file=sys.stderr, flush=True)
        # deterministic order for downstream verify iteration
        return [rows_by_adsh[a] for a in sorted(rows_by_adsh)]

    # -- File 2: SOX-cert verification -------------------------------------

    def _verify(self, client, file1_rows, roles, asof, out_dir, resume):
        """For each File-1 row whose detected role is in `roles` and that named a
        person, read before/after from consecutive SOX certs. Resumable by
        change_id."""
        asof_iso = asof.isoformat() if isinstance(asof, date) else str(asof)
        role_set = set(roles)
        part_path = out_dir / (VERIFIED_FILENAME + ".part")
        done = _load_part(part_path, "change_id") if resume else {}
        results = dict(done)

        # build the candidate list (CEO/CFO subset with a name found)
        candidates = []
        for r in file1_rows:
            body = re.sub(r"\s+", " ", r.get("section_text") or "")
            role = detect_role(body)
            incoming = _first(INC_PATS, body)
            outgoing = _first(OUT_PATS, body)
            keep = role != "other" and (bool(incoming) or bool(outgoing))
            if not (keep and role in role_set):
                continue
            cid = change_id(r["cik"], r["event_date"], r["adsh"])
            candidates.append({
                "change_id": cid, "cik": r["cik"], "ticker": r["ticker"],
                "company": r["company"], "role": role, "event_date": r["event_date"],
                "adsh": r["adsh"], "outgoing": outgoing,
                "question": f"As of today, who is the {role} of {r['company']} ({r['ticker']})?",
            })

        todo = [c for c in candidates if c["change_id"] not in results]
        print(f"[harvest sec] verify: {len(candidates)} CEO/CFO candidate(s), "
              f"{len(done)} resumed, {len(todo)} to verify (asof={asof_iso})",
              file=sys.stderr, flush=True)
        with open(part_path, "a", encoding="utf-8", newline="\n") as part:
            for i, c in enumerate(todo, 1):
                row = self._verify_one(client, c, asof_iso)
                results[c["change_id"]] = row
                _append_part(part, row)
                if i % 15 == 0 or i == len(todo):
                    conf = sum(1 for x in results.values() if x["verified"] == "confirmed")
                    print(f"[harvest sec]   verified {i}/{len(todo)} (confirmed so far {conf})",
                          file=sys.stderr, flush=True)
        return self._cleanup(list(results.values()))

    def _verify_one(self, client, c, asof_iso):
        role = c["role"]
        before = after = url = sdate = ""
        peris = self._periodics(client, c["cik"])
        lp = next((p for p in peris if p[1] <= asof_iso), None)         # current holder
        pp = next((p for p in peris if p[1] < c["event_date"]), None)   # prior holder
        if lp:
            cs = self._cert_signer(client, c["cik"], lp[0], role)
            if cs:
                after, url, _ = cs
                sdate = lp[1]
        if pp:
            pcs = self._cert_signer(client, c["cik"], pp[0], role)
            if pcs:
                before = pcs[0]
        drift = bool(before and after and before.split()[-1].lower() != after.split()[-1].lower())
        verified = "confirmed" if (before and after) else "unconfirmed"
        return {
            "change_id": c["change_id"], "ticker": c["ticker"], "company": c["company"],
            "role": role, "event_date": c["event_date"],
            "before": before, "gt_answer": after,
            "gt_source": "sox_cert" if after else "",
            "before_source": "sox_cert" if before else "",
            "source_url": url, "source_date": sdate,
            "genuine_drift": drift,
            "regex_outgoing": (c.get("outgoing") or "").strip(),
            "verified": verified,
            "question": c.get("question", ""),
            "stale_looks_like": before,
        }

    def _periodics(self, client, cik):
        """All 10-Q/10-K filings as (accession_nodash, filingDate), newest first.
        Port of sec_verify.periodics (reads `recent`)."""
        url = SUBMISSIONS_ENDPOINT.format(cik10=pad_cik(cik))
        try:
            sub = client.get_json(url)
        except HttpError as exc:
            client.record_miss("periodics", url, exc)
            return []
        recent = (((sub or {}).get("filings") or {}).get("recent") or {})
        forms = recent.get("form") or []
        accns = recent.get("accessionNumber") or []
        fdates = recent.get("filingDate") or []
        out = [(accns[i].replace("-", ""), fdates[i])
               for i in range(min(len(forms), len(accns), len(fdates)))
               if forms[i] in ("10-Q", "10-K")]
        return sorted(out, key=lambda x: x[1], reverse=True)

    def _cert_signer(self, client, cik, acc, role):
        """Find the SOX cert for `role` by CONTENT: an 'I, <name>, certify'
        opening whose principal-officer phrase matches the role unambiguously.
        Port of sec_verify.cert_signer. Returns (name, url, snippet) or None."""
        base = f"{ARCHIVES_BASE}/{int(cik)}/{acc}"
        try:
            idx = client.get_json(base + "/index.json")
        except HttpError as exc:
            client.record_miss("cert_index", base + "/index.json", exc)
            return None
        items = (((idx or {}).get("directory") or {}).get("item") or [])
        files = [it["name"] for it in items
                 if isinstance(it, dict) and str(it.get("name", "")).lower().endswith((".htm", ".html"))]
        want = CEO_PHRASES if role == "CEO" else CFO_PHRASES
        other = CFO_PHRASES if role == "CEO" else CEO_PHRASES
        files.sort(key=lambda f: 0 if re.search(r"ex.?31|cert", f, re.I) else 1)
        for fn in files:
            try:
                raw = client.get_text(base + "/" + fn)
            except HttpError as exc:
                client.record_miss("cert_body", base + "/" + fn, exc)
                continue
            if not raw or len(raw) > 60000:  # certs are small; skip the main doc/big exhibits
                continue
            t = clean_cert(raw)
            m = _I_CERTIFY.search(t)
            if not m:
                continue
            tl = t.lower()
            if any(p in tl for p in want) and not any(p in tl for p in other):
                return normalize_name(m.group(1)), base + "/" + fn, t[m.start(): m.start() + 160]
        return None

    @staticmethod
    def _cleanup(results):
        """Port of sec_verify.cleanup: normalise names, then collapse duplicate
        CHANGES — the same (ticker, role, before->after) reported in more than
        one 8-K — keeping the earliest event. Non-drift rows keep their own
        change_id so distinct filings are never merged."""
        for r in results:
            for f in ("before", "gt_answer", "stale_looks_like"):
                r[f] = normalize_name(r.get(f, ""))
        seen, deduped = set(), []
        for r in sorted(results, key=lambda x: (x.get("event_date", ""), x.get("change_id", ""))):
            key = ((r["ticker"], r["role"], r["before"].lower(), r["gt_answer"].lower())
                   if r.get("genuine_drift") else r["change_id"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(r)
        return deduped


HARVESTER = SecHarvester()
