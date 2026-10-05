#!/usr/bin/env python3
"""fetch_openfda_rmc.py -- openFDA Recent-Major-Changes (RMC) discovery.

The faithful stage-1 PORT of the legacy ``drugs/fda1_rmc_harvest.py`` (which is
read-only). It discovers real post-cutoff drug-label section changes via
openFDA's ``recent_major_changes`` field: RMC names the SECTION and the DATE
(MM/YYYY) of each major labeling change, and the label itself carries the
CURRENT text of that section (the ground-truth 'after' value). This module is
the DISCOVERY half of the FDA harvest; the Wayback 'before' half lives in
``stage1.tools.fetch_dailymed_history``.

Parameterized by [cutoff, asof] (no hardcoded 2026): the openFDA query filters
on ``effective_time`` and the kept RMC changes are those whose change MONTH
intersects [cutoff, asof] (RMC dates are MM/YYYY -> month precision).

The pure builders (SEC_RE parsing, subsection narrowing, dedup, top-300 load)
are unit-testable offline; the networked fetchers (``fetch_records`` full-scan,
``fetch_records_by_setids`` sample) are exercised only in a live sample run. The
FDA harvester (``stage1.harvest.fda``) IMPORTS these functions — it does not
duplicate the openFDA API code — so the standalone CLI and the harvest snapshot
cannot silently drift.

openFDA needs no key (moderate anonymous use is allowed). This is a public data
API, never an LLM call.
"""

from __future__ import annotations

import argparse
import calendar
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path
from stage1.config import require_contact_email, user_agent

TOOL_VERSION = "fetch_openfda_rmc:v1"
BASE = "https://api.fda.gov/drug/label.json"
USER_AGENT = user_agent()
SNIPPET_CAP = 1200            # mirror the legacy fda1_rmc_harvest [:1200]
PAGE_SIZE = 1000             # openFDA max page
MAX_SKIP = 5000             # openFDA caps skip+limit at ~26k; the legacy walked to 5000

# RMC section label -> candidate openFDA fields holding that section's text
# (ported verbatim from fda1_rmc_harvest.SECTION_FIELDS).
SECTION_FIELDS = {
    "boxed warning":            ["boxed_warning"],
    "contraindications":        ["contraindications"],
    "warnings and precautions": ["warnings_and_cautions", "warnings_and_precautions", "warnings"],
    "warnings":                 ["warnings", "warnings_and_cautions"],
    "dosage and administration": ["dosage_and_administration"],
    "adverse reactions":        ["adverse_reactions"],
    "indications and usage":    ["indications_and_usage"],
    "indications":              ["indications_and_usage"],
    "drug interactions":        ["drug_interactions"],
    "use in specific populations": ["use_in_specific_populations"],
}

# RMC free-text -> (section title, subsection number(s), MM, YYYY). Ported verbatim.
SEC_RE = re.compile(
    r'(Boxed Warning|Contraindications|Warnings and Precautions|Warnings|'
    r'Dosage and Administration|Adverse Reactions|Indications and Usage|Indications|'
    r'Drug Interactions|Use in Specific Populations)'
    r'\s*\(?\s*([\d.,\s]*?)\)?\s*(\d{1,2})/(\d{4})', re.I)


class OpenFdaError(Exception):
    """A request that could not be completed after all retries. Callers catch
    this specifically (never a bare except): a FULL-scan discovery failure is
    fatal (aborting beats silently under-covering the candidate SET, which the
    derive coverage check cannot see), while a per-set_id SAMPLE miss is
    recorded and skipped."""


# ---------------------------------------------------------------------------
# Pure helpers (no network) -- these are what the unit tests exercise.
# ---------------------------------------------------------------------------
def subsection_text(field_text: str, num: str) -> str:
    """Extract subsection ``num`` (e.g. '5.6') text up to the next sibling, else
    ''. Copied verbatim (behaviourally) from fda1_rmc_harvest.subsection_text so
    the 'after' text is narrowed to the same subsection here and in the Wayback
    'before' tool."""
    if not num:
        return ""
    num = num.strip().split(",")[0].strip()      # first number if a list
    if not re.match(r'^\d+\.\d+$', num):
        return ""
    nxt = re.escape(num.rsplit(".", 1)[0]) + r'\.\d+'
    m = re.search(rf'(\b{re.escape(num)}\b.*?)(?:\b{nxt}\b|\Z)', field_text, re.S)
    return re.sub(r'\s+', ' ', m.group(1)).strip() if m else ""


def load_top300(path) -> dict:
    """{generic_lower: rank} from the top-300 reference CSV (col2 = generic name,
    rank column). A missing file yields {} (the ``known`` flag then degrades to
    False for every row — never a crash), mirroring the legacy behaviour. This
    list is OPTIONAL stratification metadata; FDA has no gating universe."""
    top: dict = {}
    p = Path(path)
    if not p.is_file():
        return top
    import csv
    with open(p, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            g = (row.get("col2") or "").strip().lower()
            rank = row.get("rank")
            if g:
                top[g] = rank
    return top


def change_month_in_window(month: int, year: int, cutoff: date, asof: date) -> bool:
    """Does the RMC change MONTH [first day, last day] intersect [cutoff, asof]?
    RMC dates are MM/YYYY (month precision), so an in-window change is one whose
    whole month overlaps the day-precision window — inclusive at the edges so a
    month straddling a bound is KEPT as a candidate (the FDA adapter's
    precision-aware temporal gate then routes an edge month to review, never a
    silent drop). Pure and total; an out-of-range month returns False."""
    if not (1 <= month <= 12):
        return False
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last >= cutoff and first <= asof


def rmc_rows_from_records(records, top: dict, cutoff: date, asof: date) -> list:
    """Build the deduped RMC rows from raw openFDA label records. For each label,
    parse every RMC change, keep those whose change month intersects
    [cutoff, asof], pull the CURRENT section text (the 'after') and narrow it to
    the changed subsection. Dedup by (generic, section, section_num), preferring a
    row that actually captured section text. Pure (no network); the row shape
    matches the legacy fda_rmc_2026.jsonl exactly."""
    rows = []
    for r in records:
        if not isinstance(r, dict):
            continue
        of = r.get("openfda", {}) or {}
        gen = (of.get("generic_name", [""]) or [""])[0].strip().lower()
        brand = (of.get("brand_name", [""]) or [""])[0].strip()
        setid = r.get("set_id", "") or ""
        eff = r.get("effective_time", "") or ""
        rmc = " ".join(r.get("recent_major_changes", []) or [])
        for sec, num, mm, yy in SEC_RE.findall(rmc):
            month, year = int(mm), int(yy)
            if not change_month_in_window(month, year, cutoff, asof):
                continue
            sec_l = sec.lower().strip()
            field_text = ""
            for fld in SECTION_FIELDS.get(sec_l, []):
                if r.get(fld):
                    field_text = " ".join(r[fld]) if isinstance(r[fld], list) else str(r[fld])
                    break
            field_text = re.sub(r'\s+', ' ', field_text).strip()
            sub = subsection_text(field_text, num)
            # a real subsection has prose; a bare "5.6 )" cross-ref does not
            snippet = (sub if len(sub) > 60 else field_text)[:SNIPPET_CAP]
            num_norm = re.sub(r'\s+', '', num.strip()).rstrip(",")
            rows.append({
                "generic": gen, "brand": brand, "set_id": setid,
                "effective_time": eff, "section": sec.strip(),
                "section_num": num_norm, "change_date": f"{month:02d}/{year}",
                "known": gen in top, "rank": top.get(gen, ""),
                "section_text": snippet,
            })
    uniq: dict = {}
    for row in rows:
        key = (row["generic"], row["section"].lower(), row["section_num"])
        if key not in uniq or (not uniq[key]["section_text"] and row["section_text"]):
            uniq[key] = row
    return list(uniq.values())


def build_query_full(cutoff: date, asof: date) -> str:
    """The openFDA search for the FULL discovery scan: labels that carry an RMC
    field AND whose ``effective_time`` (the label's own revision date) is in
    [cutoff, asof]. Literal '+' joins terms (openFDA search syntax), so this
    string is concatenated into the URL WITHOUT url-encoding the '+'."""
    lo = cutoff.strftime("%Y%m%d")
    hi = asof.strftime("%Y%m%d")
    return f"_exists_:recent_major_changes+AND+effective_time:[{lo}+TO+{hi}]"


# ---------------------------------------------------------------------------
# Network (retry/backoff). Only the fetchers + main() reach these.
# ---------------------------------------------------------------------------
def getj(url: str, *, timeout: float = 60.0, tries: int = 4,
         base_sleep: float = 2.0, verbose: bool = True):
    """Parsed openFDA JSON. A 404 (openFDA's 'no results') returns an empty
    result set — a normal outcome, not an error. A persistent transient failure
    raises OpenFdaError after ``tries`` attempts (never a bare except)."""
    require_contact_email()
    last = "no attempt"
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"results": [], "meta": {"results": {"total": 0}}}
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt < tries - 1:
            wait = min(60.0, base_sleep * (2 ** attempt))
            if verbose:
                print(f"    [openfda {last}] retry in {wait:.0f}s "
                      f"({attempt + 1}/{tries})", file=sys.stderr, flush=True)
            time.sleep(wait)
    raise OpenFdaError(f"openFDA unreachable after {tries} tries ({last}): {url}")


def fetch_records(query: str, *, sleep: float = 0.5, tries: int = 4,
                  timeout: float = 60.0, page_size: int = PAGE_SIZE,
                  max_skip: int = MAX_SKIP):
    """(records, requests): the FULL paginated openFDA scan for ``query``.

    TWO failure modes are BOTH fatal (aborting beats silently UNDER-COVERING the
    candidate SET, which the derive coverage check never inspects — mirroring the
    SEC FTS / sports SPARQL fatal-on-discovery rule):

      * a persistent transient failure mid-pagination (``getj`` raises);
      * the PAGINATION CAP is hit with results still beyond it — the page at the
        last allowed ``skip`` comes back FULL (``len(batch) == page_size``), so
        more matching labels exist past ``max_skip + page_size``. The legacy
        break-on-short-page loop returned the truncated prefix silently; here it
        raises like SEC's ``FTS_FROM_CAP`` abort. Narrow ``[cutoff, asof]`` so the
        window pages within the cap.

    ``meta.results.total`` (openFDA's match count) is surfaced in the abort
    message when present, but the decision rests on the observed full-page-at-cap
    condition so it is correct even if ``meta`` is absent."""
    records: list = []
    requests = 0
    for skip in range(0, max_skip + 1, page_size):
        url = f"{BASE}?search={query}&limit={page_size}&skip={skip}"
        payload = getj(url, timeout=timeout, tries=tries)
        requests += 1
        batch = payload.get("results", []) if isinstance(payload, dict) else []
        records.extend(batch)
        if len(batch) < page_size:
            break
        # The page is FULL. If the NEXT page would exceed the cap, more labels
        # match than we can page — abort rather than return a truncated set.
        if skip + page_size > max_skip:
            total = None
            if isinstance(payload, dict):
                total = ((payload.get("meta") or {}).get("results") or {}).get("total")
            total_note = f"; openFDA reports meta.results.total={total}" if total else ""
            raise OpenFdaError(
                f"openFDA RMC discovery exceeds the pagination cap: fetched {len(records)} "
                f"label(s) through skip={skip} (limit {page_size}) and the final page is FULL, "
                f"so more labels match beyond skip={max_skip + page_size}{total_note}. Returning "
                "the truncated prefix would silently UNDER-COVER the candidate SET (the derive "
                "coverage check cannot see fetch_stats) — aborting rather than writing an "
                f"incomplete snapshot. Narrow [cutoff, asof] so the window pages within the cap. "
                f"query={query!r}"
            )
        time.sleep(sleep)
    return records, requests


def fetch_records_by_setids(set_ids, *, sleep: float = 0.5, tries: int = 4,
                            timeout: float = 60.0):
    """(records, requests, errors): the SAMPLE path — fetch the CURRENT label for
    each requested set_id directly (one query per id), so a small live test never
    runs the full RMC scan. A per-set_id miss is RECORDED and skipped (that drug
    simply produces no candidate) rather than aborting — the sample is a live
    smoke test, not a completeness-critical full harvest."""
    records: list = []
    requests = 0
    errors: list = []
    for sid in set_ids:
        url = f"{BASE}?search=set_id:%22{urllib.parse.quote(sid, safe='')}%22&limit=1"
        try:
            payload = getj(url, timeout=timeout, tries=tries)
        except OpenFdaError as exc:
            errors.append({"stage": "openfda_setid", "set_id": sid, "error": str(exc)})
            continue
        requests += 1
        batch = payload.get("results", []) if isinstance(payload, dict) else []
        if not batch:
            errors.append({"stage": "openfda_setid", "set_id": sid,
                           "error": "no label returned for set_id"})
        records.extend(batch)
        time.sleep(sleep)
    return records, requests, errors


# ---------------------------------------------------------------------------
# Standalone CLI (parity with the legacy tool; the harvester is the usual entry).
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m stage1.tools.fetch_openfda_rmc",
        description="Discover post-cutoff FDA label section changes via openFDA RMC.",
    )
    parser.add_argument("--cutoff", default="2026-01-01", help="window start YYYY-MM-DD")
    parser.add_argument("--asof", default="2026-12-31", help="window end YYYY-MM-DD")
    parser.add_argument("--top300", default="../0_prior_work/drugs/drug_universe_top300.csv",
                        help="top-300 reference csv (defines the optional 'known' flag)")
    parser.add_argument("--out", default="fda_rmc_2026.jsonl", help="output jsonl")
    parser.add_argument("--sample", default="", help="comma-separated set_ids (testing)")
    parser.add_argument("--sleep", type=float, default=0.5)
    parser.add_argument("--tries", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    cutoff = date.fromisoformat(args.cutoff)
    asof = date.fromisoformat(args.asof)
    top = load_top300(args.top300)
    print(f"top-300 generics: {len(top)}", file=sys.stderr)

    sample = [s.strip() for s in args.sample.split(",") if s.strip()]
    if sample:
        records, requests, errors = fetch_records_by_setids(
            sample, sleep=args.sleep, tries=args.tries, timeout=args.timeout)
        for e in errors:
            print(f"[warn] {e}", file=sys.stderr)
    else:
        records, requests = fetch_records(
            build_query_full(cutoff, asof), sleep=args.sleep, tries=args.tries,
            timeout=args.timeout)
    print(f"labels pulled: {len(records)} ({requests} requests)", file=sys.stderr)

    rows = rmc_rows_from_records(records, top, cutoff, asof)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        for row in sorted(rows, key=lambda r: (r["set_id"], r["section"], r["section_num"])):
            fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"post-cutoff section changes: {len(rows)} across "
          f"{len({r['generic'] for r in rows})} drugs -> {args.out}", file=sys.stderr)
    for s, c in Counter(r["section"] for r in rows).most_common():
        print(f"   {c:5d}  {s}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
