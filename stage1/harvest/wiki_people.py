"""Wikipedia people-DEATHS harvester — the FIFTH harvest source (harvest_people:v1).

THE REDESIGN (owner-approved, 2026-07-22). The people benchmark is DEATHS
ONLY, and the legacy harvest reached them through a wasteful funnel: diff ALL
infobox fields across ~29,570 people/org pages -> 19 deaths. This harvester
replaces the funnel with a TARGETED death-discovery harvest that mirrors the
sports pattern (discovery via Wikidata SPARQL -> pinned-revision Wikipedia
infobox ground truth -> Wikidata corroboration cache), so
``python3 -m stage1.harvest --source wiki_people --cutoff X --asof Y`` writes a
frozen DEATH-CANDIDATE snapshot the offline wiki_people adapter derives from.

The fetch steps ``harvest()`` composes (parameterized by [cutoff, asof]):

  (a) DISCOVERY (the finder, NOT the ground truth) — Wikidata SPARQL over
      monthly windows on P570 (date of death) within [cutoff, asof], with the
      enwiki title delivered by the SAME query via schema:about (no second
      title-resolution pass and no resolution miss to guard — unlike sports).
      Every discovery failure is FATAL (a persistent SPARQL failure, or a
      window returning the LIMIT row cap after a weekly split — a truncation
      signal), mirroring the sports SPARQL / SEC FTS fatal-on-discovery rule:
      aborting beats silently under-covering the candidate set. The humans-only
      P31=Q5 clause is deliberately OMITTED (it 504-times-out WDQS); the person
      check happens at extraction (a person infobox death field), a documented
      small false-positive screen. ``--sample`` skips SPARQL and resolves each
      supplied enwiki title -> QID via pageprops (the small-live-test route;
      a resolution miss only degrades corroboration, never drops a candidate).
  (b) GROUND TRUTH — per candidate, the newest revision at/before the cutoff
      (the 'before') and at/before asof (the 'after') are fetched ONCE and the
      infobox death DATE / PLACE / CAUSE are read from each pinned revision via
      the shared, versioned ``stage1.adapters.people_death`` extractor,
      together with the deterministic BODY-PROSE length of the 'after'
      revision. Emitted as ``people_death_verified.jsonl`` (a death is a
      SINGLE-SIDED change: the before side is empty for essentially every row)
      plus the full pinned wikitext in ``people_wikitext.jsonl`` (the
      byte-anchor for the adapter's re-extraction and offline prose measure).
  (c) CORROBORATION / VANDALISM GUARD — ``people_wd_p570.jsonl``: the entity's
      cutoff + current Wikidata claim state for the DEATH properties ONLY
      (P570 date, P20 place, P509 cause, P119 resting place — a death-scoped
      slice of the people cache_format, built by the SAME imported
      fetch_wikidata_people decode machinery). The adapter requires the
      infobox death date to AGREE with P570; a vandalised fake death will not
      match and rests in review, never included. Batch-resilient like the
      sports P54 fetch: one unreachable batch degrades ONLY its own entities'
      corroboration (recorded in http_errors), never the whole universe.
  (d) RECOGNISABILITY SIGNAL — ``people_pageviews.jsonl``: the PRE-DEATH
      baseline-month pageviews per title (the calendar month before the
      cutoff, so the death spike never inflates the signal) from the Wikimedia
      REST pageviews API, cached so the adapter's modest recognisability floor
      evaluates OFFLINE. A 404 is a definite 'no data' (views 0); a persistent
      fetch failure records views null -> the adapter reviews
      (recognisability_unknown), never a silent pass/fail.

PRIMARY-SOURCE INVARIANT: the recorded before/after VALUE is the Wikipedia
infobox reading (evidence.kind='wikipedia_infobox', re-extracted by the
adapter from the pinned revisions). Wikidata corroborates and dates; it is
NEVER the recorded answer.

COVERAGE SEMANTICS — back_datable=TRUE (P570 claims, Wikipedia revisions and
REST pageviews are all permanent/reconstructable), BOTH edges pinned
(cutoff_exact AND asof_exact: the before/after infoboxes are the newest
revisions at/before THIS cutoff / THIS asof — a different window resolves
different revisions not in the snapshot), precision 'day',
window_basis='death_date_within_window;revision_snapshot_endpoints' (discovery
filters the P570 DEATH DATE into [cutoff, asof] — the death date is both the
discovery filter and the change's effective date — while the before/after
values are pinned at the two revision endpoints). A coarse (month/year) P570
straddling an edge that IS discovered routes to the adapter's window_edge
policy; a year-precision claim rendering outside every monthly window is a
documented discovery limit.

Determinism: both sidecars OMIT the wall-clock ``retrieved_at`` (the
finance-port fix); a snapshot's only wall-clock is
``snapshot_manifest.harvested_at``. Resumable (.part checkpoint per title) and
polite (descriptive UA + contact, backoff, WDQS throttle handling). NO LLM.
"""

from __future__ import annotations

import sys
import time
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

from stage1.harvest import Harvester
from stage1.harvest.snapshot import sha1_file
from stage1.harvest.sports import (
    SPARQL_ENDPOINT,
    SPARQL_LIMIT,
    _append_part,
    _load_part,
    sparql_get,
)
from stage1.adapters.people_death import (
    DEATH_PROPERTIES,
    EXTRACTOR_VERSION,
    PROSE_VERSION,
    clean_death_value,
    extract_death_fields,
    prose_chars,
)
from stage1.adapters.wiki_people import (
    DEATH_PROPERTY_WHITELIST,
    DEFAULT_POLICY as ADAPTER_DEFAULT_POLICY,
    _date_canon_to_iso,
    wd_date_from_state,
)
from stage1.normalize import get_comparator
from stage1.tools.fetch_wikidata_p54 import USER_AGENT as WD_USER_AGENT, qid_sort_key
from stage1.tools.fetch_wikidata_people import (
    BATCH_SIZE as WD_BATCH_SIZE,
    DECODER_VERSION,
    ENWIKI_API,
    QUALIFIER_WHITELIST,
    WIKIDATA_API,
    _resolve_chain,
    api_get as people_api_get,
    collect_ref_qids,
    fetch_cutoff_state,
    fetch_current_state,
    fill_labels,
    wbget_batch,
)
from stage1.tools.fetch_wiki_revisions import (
    API as ENWIKI_REV_API,
    RV_PROPS,
    USER_AGENT as ENWIKI_USER_AGENT,
    fetch_side,
)
from stage1.config import require_contact_email

TOOL_VERSION = "harvest_people:v1"

# The EXACT fixed filenames the redesigned wiki_people adapter reads by name.
CANDIDATES_FILENAME = "people_death_candidates.jsonl"
VERIFIED_FILENAME = "people_death_verified.jsonl"
WIKITEXT_CACHE_FILENAME = "people_wikitext.jsonl"
WIKITEXT_SIDECAR_FILENAME = "people_wikitext.meta.json"
WD_CACHE_FILENAME = "people_wd_p570.jsonl"
WD_SIDECAR_FILENAME = "people_wd_p570.meta.json"
PAGEVIEWS_FILENAME = "people_pageviews.jsonl"

PAGEVIEWS_ENDPOINT = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
PAGEVIEWS_PROJECT = "en.wikipedia.org"
ENWIKI_ARTICLE_PREFIX = "https://en.wikipedia.org/wiki/"

# Politeness defaults (overridable via --opt): the enwiki API tolerates ~5
# req/s; WDQS/wbgetentities is throttled far harder; the pageviews REST API is
# generous but still paced.
DEFAULT_ENWIKI_SLEEP = 0.2
DEFAULT_WD_SLEEP = 1.0
DEFAULT_PV_SLEEP = 0.2

# A discovery window is split into ~weekly sub-windows when it returns the
# WDQS row cap; a SUB-window still at the cap is fatal (truncation).
CAP_SPLIT_DAYS = 7

PV_TRIES = 6
PV_BASE_SLEEP = 2.0


def _iso(value) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def _cutoff_pin(cutoff) -> str:
    """The rvstart pin for the 'before' side: the END of the cutoff day, so
    the newest revision at/before it is the page state as of the cutoff
    (inclusive) — the LEGACY people convention (the committed
    people_wd_entities cutoff pin 2026-02-01T23:59:59Z describes the same
    instant), and the pin the Wikidata cutoff-state fetch shares so the two
    structured sources describe one moment."""
    return f"{_iso(cutoff)}T23:59:59Z"


def _asof_pin(asof) -> str:
    """The rvstart pin for the 'after' side: the END of the asof day."""
    return f"{_iso(asof)}T23:59:59Z"


def baseline_month(cutoff: date) -> str:
    """The PRE-DEATH pageview baseline month: the calendar month immediately
    before the cutoff's month ('was this person known BEFORE the death'), so
    the post-death traffic spike never inflates the recognisability signal."""
    prev = cutoff.replace(day=1) - timedelta(days=1)
    return f"{prev.year:04d}-{prev.month:02d}"


def _month_last_day(ym: str) -> int:
    y, m = map(int, ym.split("-"))
    first = date(y, m, 1)
    nxt = (first + timedelta(days=31)).replace(day=1)
    return (nxt - timedelta(days=1)).day


def death_windows(cutoff: date, asof: date):
    """The P570 discovery windows: per calendar month spanned by
    [cutoff, asof], clamped to the window edges — [start, end) date pairs with
    end exclusive (asof's day is included via end = asof + 1 day). Derived
    from the window, never hardcoded."""
    windows = []
    month_start = cutoff.replace(day=1)
    hard_end = asof + timedelta(days=1)
    while month_start <= asof:
        next_month = (month_start + timedelta(days=31)).replace(day=1)
        lo = max(month_start, cutoff)
        hi = min(next_month, hard_end)
        if lo < hi:
            windows.append((lo, hi))
        month_start = next_month
    return windows


def _death_query(lo: date, hi_exclusive: date) -> str:
    """The VERIFIED discovery query (design-tested live against WDQS): every
    person with a P570 in [lo, hi) that has an enwiki sitelink, the title
    delivered by the same query via schema:about. The humans-only P31=Q5
    variant 504-times-out and is deliberately omitted (person-ness is screened
    at extraction by the person-infobox death field)."""
    return (
        "SELECT ?person ?article ?dod WHERE {\n"
        "  ?person wdt:P570 ?dod .\n"
        f'  FILTER(?dod >= "{lo.isoformat()}T00:00:00Z"^^xsd:dateTime && '
        f'?dod < "{hi_exclusive.isoformat()}T00:00:00Z"^^xsd:dateTime)\n'
        "  ?article schema:about ?person ; "
        "schema:isPartOf <https://en.wikipedia.org/> .\n"
        f"}} LIMIT {SPARQL_LIMIT}"
    )


def title_from_article_url(url) -> str:
    """The enwiki page title of a schema:about ?article URL (unquoted,
    underscores -> spaces), or '' when the URL is not an enwiki article."""
    if not isinstance(url, str) or not url.startswith(ENWIKI_ARTICLE_PREFIX):
        return ""
    tail = url[len(ENWIKI_ARTICLE_PREFIX):]
    return urllib.parse.unquote(tail).replace("_", " ").strip()


def _percentiles(values):
    """Nearest-rank percentiles p10/p25/p50/p75/p90 (+ n) over the KNOWN int
    values of a list (percentile p = the value at 1-indexed rank
    ceil(p/100 * n) of the ascending sort — deterministic, no interpolation).
    None when nothing is known."""
    import math

    known = sorted(v for v in values
                   if isinstance(v, int) and not isinstance(v, bool))
    if not known:
        return None
    out = {}
    for p in (10, 25, 50, 75, 90):
        out[f"p{p}"] = known[max(1, math.ceil(p / 100 * len(known))) - 1]
    out["n"] = len(known)
    return out


def signal_distribution(verified_rows, candidate_rows, pageview_rows) -> dict:
    """The OBSERVED distribution of the three threshold-bearing signals across
    a snapshot's frozen rows — prose_chars (harvest-time, at
    params.prose_version), sitelinks, and pre-death baseline-month views —
    recorded into the snapshot manifest ``params.signal_distribution`` so the
    data-grounding of the derive-side thresholds is REGENERABLE and auditable
    from every harvested snapshot (the original n=120 design-session study was
    live-only and is not committed; every FULL-window harvest re-derives the
    distribution its floors should be tuned against, and
    ``stage1.tools.people_threshold_study`` recomputes it offline from any
    snapshot dir at the CURRENT prose version). Pure + deterministic
    (nearest-rank percentiles over the frozen rows)."""
    return {
        "method": "nearest_rank",
        "prose_chars": _percentiles([r.get("prose_chars") for r in verified_rows]),
        "sitelinks": _percentiles([r.get("sitelinks") for r in candidate_rows]),
        "pre_death_monthly_views": _percentiles(
            [r.get("views") for r in pageview_rows]),
    }


class PeopleHarvester(Harvester):
    source = "wiki_people"
    tool_version = TOOL_VERSION

    # -- coverage (PURE, no network) ---------------------------------------
    def coverage(self, cfg: dict) -> dict:
        cutoff = cfg["cutoff"]
        asof = cfg["asof"]
        return {
            "cutoff": _iso(cutoff),
            "asof": _iso(asof),
            # BOTH edges pinned: the before/after infobox readings are the
            # newest revisions at/before THIS cutoff / THIS asof — a different
            # window resolves different revisions not in the snapshot.
            "cutoff_exact": True,
            "asof_exact": True,
            "precision": "day",
            # Discovery filters the P570 DEATH DATE into [cutoff, asof] (the
            # death date is the discovery filter AND the change's effective
            # date); the before/after values are pinned at the two revision
            # endpoints.
            "window_basis": "death_date_within_window;revision_snapshot_endpoints",
            # P570 claims, Wikipedia revisions, and REST pageviews (2015-07
            # onward) are permanent/reconstructable — unlike FDA.
            "back_datable": True,
        }

    # -- harvest (the ONLY networked method) -------------------------------
    def harvest(self, cfg: dict, writer, ctx: dict) -> None:
        cutoff = cfg["cutoff"]
        asof = cfg["asof"]
        sample = cfg.get("sample")
        resume = bool(cfg.get("resume"))
        enwiki_sleep = float(cfg.get("enwiki_sleep", DEFAULT_ENWIKI_SLEEP))
        wd_sleep = float(cfg.get("wd_sleep", DEFAULT_WD_SLEEP))
        pv_sleep = float(cfg.get("pv_sleep", DEFAULT_PV_SLEEP))
        http_errors: list = []

        # ---- (a) DISCOVERY -> candidates ---------------------------------
        if sample:
            titles = sorted({t.strip() for t in sample if t and t.strip()})
            if not titles:
                raise LookupError("wiki_people --sample produced no titles")
            discovery = "sample_titles"
            candidates = self._discover_sample(titles, wd_sleep, http_errors)
        else:
            discovery = "wikidata_sparql_p570"
            candidates = self._discover_sparql(cutoff, asof, wd_sleep)
        print(f"[harvest people] discovery={discovery}: {len(candidates)} candidate(s)",
              file=sys.stderr, flush=True)

        # ---- (b) GROUND TRUTH: pinned-revision infobox extraction --------
        verified_by_title, wikitext_by_title = self._extract_infoboxes(
            candidates, cutoff, asof, writer.out_dir, resume, enwiki_sleep, http_errors)

        # ---- (c) CORROBORATION cache: death-scoped Wikidata states -------
        wd_rows, sitelink_counts, p570_by_qid = self._fetch_wd_cache(
            candidates, _cutoff_pin(cutoff), wd_sleep, http_errors)

        # Fill each candidate's sitelinks + P570 precision (and, on the sample
        # path, the P570 date) from the wbgetentities fetch — sitelink counts
        # come free with props=sitelinks, and the decoded P570 carries the
        # claim's own precision (SPARQL's ?dod does not).
        for cand in candidates:
            qid = cand.get("qid")
            if qid in sitelink_counts:
                cand["sitelinks"] = sitelink_counts[qid]
            p570 = p570_by_qid.get(qid)
            if p570 is not None:
                if not cand.get("p570_date"):
                    cand["p570_date"] = p570[0]
                cand["p570_precision"] = p570[1]

        # ---- (d) RECOGNISABILITY signal: pre-death baseline pageviews ----
        # Views are fetched for the redirect-RESOLVED article where discovery
        # recorded one (a redirect page's own traffic is near zero and would
        # falsely sink the recognisability floor); rows stay keyed by the
        # candidate title.
        pv_month = baseline_month(cutoff)
        fetch_titles = {c["title"]: self._fetch_title(c) for c in candidates}
        pageview_rows = self._fetch_pageviews(
            sorted(verified_by_title), pv_month, pv_sleep, http_errors, fetch_titles)

        candidate_rows = sorted(candidates, key=lambda c: str(c.get("title") or ""))
        verified_rows = list(verified_by_title.values())
        wikitext_rows = list(wikitext_by_title.values())

        stats = {
            "discovery": discovery,
            "candidates": len(candidate_rows),
            "verified": len(verified_rows),
            "with_infobox_deathdate": sum(
                1 for r in verified_rows
                if ((r.get("fields_present") or {}).get("after") or {}).get("deathdate")),
            "wd_rows": len(wd_rows),
            "pageview_rows": len(pageview_rows),
            "pageview_views_known": sum(
                1 for r in pageview_rows if isinstance(r.get("views"), int)),
            "http_errors": http_errors,
            "resumed": resume,
        }
        self._emit(writer, cfg, discovery, candidate_rows, verified_rows,
                   wikitext_rows, wd_rows, pageview_rows, pv_month,
                   enwiki_sleep, wd_sleep, pv_sleep, stats)

        part = writer.out_dir / (VERIFIED_FILENAME + ".part")
        if part.exists():
            part.unlink()

    # -- (a) discovery: sample titles -> QIDs ------------------------------
    def _discover_sample(self, titles, sleep, http_errors) -> list:
        """Resolve each --sample enwiki title to its Wikidata QID via batched
        pageprops. Non-fatal on a miss: the title is the INPUT here and
        survives a failed QID lookup — only corroboration degrades (recorded),
        never a dropped candidate (mirroring the sports sample path)."""
        resolved: dict = {}
        title_list = sorted(titles)
        for i in range(0, len(title_list), WD_BATCH_SIZE):
            batch = title_list[i:i + WD_BATCH_SIZE]
            try:
                payload = people_api_get(ENWIKI_API, {
                    "action": "query", "prop": "pageprops", "ppprop": "wikibase_item",
                    "redirects": "1", "formatversion": "2", "titles": "|".join(batch),
                })
            except RuntimeError as exc:
                http_errors.append({"stage": "resolve_qids", "titles": list(batch),
                                    "error": str(exc)})
                for t in batch:
                    resolved.setdefault(t, {"qid": None, "resolved_title": t,
                                            "title_status": "missing_title"})
                continue
            if "error" in payload:
                http_errors.append({"stage": "resolve_qids", "titles": list(batch),
                                    "error": str(payload["error"])})
                for t in batch:
                    resolved.setdefault(t, {"qid": None, "resolved_title": t,
                                            "title_status": "missing_title"})
                continue
            query = payload.get("query", {})
            norm = {e.get("from"): e.get("to") for e in query.get("normalized", [])}
            redir = {e.get("from"): e.get("to") for e in query.get("redirects", [])}
            pages = {p.get("title"): p for p in query.get("pages", []) if isinstance(p, dict)}
            for t in batch:
                final, redirected = _resolve_chain(t, norm, redir)
                page = pages.get(final)
                qid = None
                status = "missing_title"
                if isinstance(page, dict) and not page.get("missing") and not page.get("invalid"):
                    qid = (page.get("pageprops") or {}).get("wikibase_item")
                    if isinstance(qid, str) and qid:
                        status = "redirect_resolved" if redirected else "ok"
                    else:
                        qid, status = None, "no_wikibase_item"
                resolved[t] = {"qid": qid, "resolved_title": final, "title_status": status}
            time.sleep(sleep)
        missing = [t for t in title_list if not resolved.get(t, {}).get("qid")]
        if missing:
            print(f"[harvest people] {len(missing)} sample title(s) had no Wikidata QID "
                  f"(corroboration will review): {missing}", file=sys.stderr, flush=True)
        return [{
            "qid": resolved.get(t, {}).get("qid"),
            "title": t,
            "resolved_title": resolved.get(t, {}).get("resolved_title", t),
            "title_status": resolved.get(t, {}).get("title_status", "missing_title"),
            "p570_date": "",
            "p570_precision": None,
            "sitelinks": None,
        } for t in title_list]

    # -- (a) discovery: full Wikidata SPARQL P570 scan ---------------------
    def _discover_sparql(self, cutoff, asof, wd_sleep) -> list:
        """Monthly P570 windows over [cutoff, asof] (clamped at both edges),
        title delivered by the same query. FATAL on a persistent SPARQL
        failure; a window returning the row cap is split into weekly
        sub-windows, and a sub-window still at the cap aborts (a truncation
        signal — later in-window deaths were dropped)."""
        by_qid: dict = {}
        for lo, hi in death_windows(cutoff, asof):
            self._scan_window(lo, hi, wd_sleep, by_qid, allow_split=True)
        by_title: dict = {}
        for qid in sorted(by_qid, key=qid_sort_key):
            cand = by_qid[qid]
            if cand["title"] and cand["title"] not in by_title:
                by_title[cand["title"]] = cand
        return list(by_title.values())

    def _scan_window(self, lo, hi, wd_sleep, by_qid, allow_split) -> None:
        payload = self._sparql_get(_death_query(lo, hi), wd_sleep)
        if payload is None:
            raise RuntimeError(
                f"WDQS P570 discovery failed for [{lo} .. {hi}) after retries: the "
                "window's deaths are unreachable, so the snapshot would silently "
                "UNDER-COVER discovery — aborting rather than writing an incomplete "
                "snapshot (rerun when WDQS is reachable; per-title extraction resumes "
                "from its .part checkpoint)"
            )
        bindings = payload.get("results", {}).get("bindings", [])
        if len(bindings) >= SPARQL_LIMIT:
            if allow_split and (hi - lo).days > CAP_SPLIT_DAYS:
                print(f"[harvest people]   [{lo}..{hi}) hit the {SPARQL_LIMIT} cap; "
                      f"splitting into {CAP_SPLIT_DAYS}-day sub-windows",
                      file=sys.stderr, flush=True)
                sub_lo = lo
                while sub_lo < hi:
                    sub_hi = min(sub_lo + timedelta(days=CAP_SPLIT_DAYS), hi)
                    self._scan_window(sub_lo, sub_hi, wd_sleep, by_qid, allow_split=False)
                    sub_lo = sub_hi
                return
            raise RuntimeError(
                f"WDQS P570 discovery for [{lo} .. {hi}) returned the row cap "
                f"({SPARQL_LIMIT}) even after weekly splitting: the window is too large "
                "to return without truncation, so later in-window deaths were dropped and "
                "the snapshot would silently UNDER-COVER discovery — aborting rather than "
                "writing an incomplete snapshot (narrow [cutoff, asof])"
            )
        for binding in bindings:
            person = (binding.get("person", {}) or {}).get("value") or ""
            qid = person.rsplit("/", 1)[-1]
            title = title_from_article_url((binding.get("article", {}) or {}).get("value"))
            dod = ((binding.get("dod", {}) or {}).get("value") or "")[:10]
            if not qid or not title:
                continue
            by_qid.setdefault(qid, {
                "qid": qid,
                "title": title,
                "resolved_title": title,
                "title_status": "ok",
                "p570_date": dod,
                "p570_precision": None,
                "sitelinks": None,
            })
        print(f"[harvest people]   [{lo}..{hi}): {len(bindings)} P570 rows "
              f"({len(by_qid)} distinct so far)", file=sys.stderr, flush=True)

    def _sparql_get(self, query, sleep):
        """Delegates to the shared polite WDQS client (patchable in tests)."""
        return sparql_get(query, sleep)

    # -- (b) ground truth: pinned-revision death-infobox extraction --------
    @staticmethod
    def _fetch_title(cand) -> str:
        """The enwiki page to FETCH for a candidate: the redirect-RESOLVED
        title when discovery recorded one (the pinned revisions and pageviews
        must describe the person's real article — a redirect page's own
        wikitext is '#REDIRECT [[...]]' with no infobox and its own near-zero
        traffic), else the input title. The candidate's ``title`` stays the
        row KEY everywhere; the candidates row records resolved_title +
        title_status for the audit trail. The full SPARQL path always has
        title_status='ok' (schema:about delivers canonical titles), so this
        only affects the --sample route."""
        resolved = cand.get("resolved_title")
        if (cand.get("title_status") == "redirect_resolved"
                and isinstance(resolved, str) and resolved):
            return resolved
        return cand["title"]

    def _extract_infoboxes(self, candidates, cutoff, asof, out_dir, resume,
                           sleep, http_errors):
        """For each candidate title, fetch the pinned 'before' (<=cutoff end)
        and 'after' (<=asof end) revisions ONCE (by the redirect-resolved
        title where discovery recorded one), read the infobox death fields
        + the body-prose length from each, and reuse the same revision content
        for the wikitext cache. Returns (verified_by_title, wikitext_by_title).

        Resumable: a title whose .part row has NO fetch_errors AND whose
        stamped window pins MATCH this run's [cutoff, asof] pins is skipped on
        --resume; an errored or other-window row is refetched. The pin stamp
        closes the stale-checkpoint hole: without it, rerunning an interrupted
        harvest into the same out-dir after CHANGING the window would accept
        rows pinned to the OLD window while the manifest claims
        cutoff_exact/asof_exact for the new one."""
        cutoff_pin = _cutoff_pin(cutoff)
        asof_pin = _asof_pin(asof)
        pins = {"before": cutoff_pin, "after": asof_pin}
        part_path = out_dir / (VERIFIED_FILENAME + ".part")
        part_rows = _load_part(part_path) if resume else {}
        done = {t: r for t, r in part_rows.items()
                if isinstance(r.get("wikitext"), dict)
                and not r["wikitext"].get("fetch_errors")
                and r.get("pins") == pins}
        stale = sum(1 for r in part_rows.values() if r.get("pins") != pins)
        todo = [c for c in candidates if c["title"] not in done]
        print(f"[harvest people] extraction: {len(done)} resumed "
              f"({stale} checkpoint row(s) from another window ignored), "
              f"{len(todo)} to fetch (before<={cutoff_pin}, after<={asof_pin})",
              file=sys.stderr, flush=True)

        results = dict(done)
        with open(part_path, "a", encoding="utf-8", newline="\n") as part:
            for i, cand in enumerate(todo, 1):
                title = cand["title"]
                fetch_title = self._fetch_title(cand)
                before_side, before_err = self._fetch_snapshot_side(
                    fetch_title, cutoff_pin, "cutoff", sleep, http_errors)
                after_side, after_err = self._fetch_snapshot_side(
                    fetch_title, asof_pin, "current", sleep, http_errors)
                verified, wikitext = self._build_rows(
                    cand, before_side, before_err, after_side, after_err)
                results[title] = {"title": title, "pins": pins,
                                  "verified": verified, "wikitext": wikitext}
                _append_part(part, results[title])
                if i % 25 == 0 or i == len(todo):
                    print(f"[harvest people]   extracted {i}/{len(todo)} title(s)",
                          file=sys.stderr, flush=True)

        verified_by_title = {t: r["verified"] for t, r in results.items()}
        wikitext_by_title = {t: r["wikitext"] for t, r in results.items()}
        return verified_by_title, wikitext_by_title

    def _fetch_snapshot_side(self, title, date_pin, side_name, sleep, http_errors):
        """Fetch the newest revision of ``title`` at/before ``date_pin``
        (reusing fetch_wiki_revisions.fetch_side), relabelling the cache
        side's pin to the revision's own timestamp (ts_source='row') so the
        wikitext cache is pin-consistent with the recorded revision — the
        sports pattern. Returns (side | None, err | None)."""
        try:
            side, err = fetch_side(title, date_pin, "row")
        except RuntimeError as exc:
            http_errors.append({"stage": f"{side_name}_revision", "title": title,
                                "error": str(exc)})
            time.sleep(sleep)
            return None, f"fetch failed after retries: {exc}"
        time.sleep(sleep)
        if side is not None:
            side = dict(side)
            side["pinned_ts"] = side.get("ts")
        return side, err

    @staticmethod
    def _build_rows(cand, before_side, before_err, after_side, after_err):
        """One people_death_verified.jsonl row + one people_wikitext.jsonl row
        from the two pinned revisions. The recorded values are the INFOBOX
        readings (extract_death_fields + clean_death_value over the pinned
        wikitext) — never Wikidata. Pure given the fetched sides (tests drive
        it with fixture content)."""
        def read_side(side):
            if not isinstance(side, dict) or not isinstance(side.get("content"), str):
                return None
            ext = extract_death_fields(side["content"])
            out = {"infobox_found": ext["infobox_found"], "template": ext["template_name"],
                   "values": {}, "present": {}, "cleaned": {}}
            for prop in DEATH_PROPERTIES:
                field = ext["fields"][prop]
                present = bool(field["present"] and not field["blank"])
                out["present"][prop] = present
                cleaned = clean_death_value(prop, field["raw"]) if present else None
                out["cleaned"][prop] = cleaned
                out["values"][prop] = cleaned["value"] if cleaned and cleaned["ok"] else ""
            return out

        before = read_side(before_side)
        after = read_side(after_side)

        after_dd_iso = None
        after_dd_precision = None
        if after is not None and after["values"]["deathdate"]:
            parsed = get_comparator("date").parse(after["values"]["deathdate"])
            if getattr(parsed, "ok", False):
                iso = _date_canon_to_iso(parsed.canonical)
                if iso is not None:
                    after_dd_iso, after_dd_precision = iso

        verified = {
            "title": cand["title"],
            "qid": cand.get("qid"),
            "before": {
                prop: (before["values"][prop] if before is not None else "")
                for prop in DEATH_PROPERTIES
            },
            "after": {
                prop: (after["values"][prop] if after is not None else "")
                for prop in DEATH_PROPERTIES
            },
            "after_deathdate_iso": after_dd_iso,
            "after_deathdate_precision": after_dd_precision,
            "fields_present": {
                "before": {prop: bool(before is not None and before["present"][prop])
                           for prop in DEATH_PROPERTIES},
                "after": {prop: bool(after is not None and after["present"][prop])
                          for prop in DEATH_PROPERTIES},
            },
            "infobox_found": bool(after is not None and after["infobox_found"]),
            "template": after["template"] if after is not None else None,
            "prose_chars": (prose_chars(after_side["content"])
                            if isinstance(after_side, dict)
                            and isinstance(after_side.get("content"), str) else None),
            "cutoff_rev_ts": before_side.get("ts") if before_side else None,
            "cutoff_revid": before_side.get("revid") if before_side else None,
            "cur_rev_ts": after_side.get("ts") if after_side else None,
            "cur_revid": after_side.get("revid") if after_side else None,
        }
        fetch_errors = []
        if before_err:
            fetch_errors.append(f"cutoff: {before_err}")
        if after_err:
            fetch_errors.append(f"current: {after_err}")
        verified["fetch_errors"] = list(fetch_errors)
        wikitext = {
            "title": cand["title"],
            "cutoff": before_side,
            "current": after_side,
            "fetch_errors": fetch_errors,
        }
        return verified, wikitext

    # -- (c) corroboration cache: death-scoped Wikidata states -------------
    def _fetch_wd_cache(self, candidates, cutoff_pin, sleep, http_errors):
        """(wd_rows, sitelink_counts, p570_by_qid): cache_format rows for the
        candidate titles, restricted to the DEATH property whitelist; sitelink
        counts (free with props=sitelinks) and each entity's decoded current
        P570 (iso, precision_str) for the candidate fill.

        BATCH-RESILIENT (the sports P54 pattern): a failed wbgetentities batch
        or per-entity cutoff-revision fetch degrades ONLY those entities'
        corroboration (recorded in http_errors -> the adapter reviews), never
        the whole universe. The corroboration cache is a SECOND source — never
        the recorded value — so nothing here is fatal."""
        whitelist = list(DEATH_PROPERTY_WHITELIST)
        qids = sorted({c["qid"] for c in candidates if c.get("qid")}, key=qid_sort_key)
        unresolved_types: set = set()

        cutoff_states: dict = {}
        for i, qid in enumerate(qids, 1):
            try:
                state, err = fetch_cutoff_state(qid, cutoff_pin, unresolved_types, whitelist)
            except RuntimeError as exc:
                http_errors.append({"stage": "wd_cutoff_state", "qid": qid, "error": str(exc)})
                state, err = None, "cutoff_not_fetched"
            cutoff_states[qid] = (state, err)
            time.sleep(sleep)
            if i % 50 == 0 or i == len(qids):
                print(f"[harvest people]   wd cutoff states {i}/{len(qids)}",
                      file=sys.stderr, flush=True)

        current_states: dict = {}
        sitelink_counts: dict = {}
        for i in range(0, len(qids), WD_BATCH_SIZE):
            batch = qids[i:i + WD_BATCH_SIZE]
            try:
                entities = wbget_batch(batch, "info|claims|sitelinks", {}, sleep)
            except RuntimeError as exc:
                http_errors.append({"stage": "wd_current_state", "qids": list(batch),
                                    "error": str(exc)})
                for qid in batch:
                    current_states[qid] = (None, "current_not_fetched")
                continue
            for qid in batch:
                ent = entities.get(qid)
                if isinstance(ent, dict) and isinstance(ent.get("sitelinks"), dict):
                    sitelink_counts[qid] = len(ent["sitelinks"])
                state, err = fetch_current_state(qid, ent, unresolved_types, whitelist)
                current_states[qid] = (state, err)

        ref_qids: set = set()
        for state, _err in list(cutoff_states.values()) + list(current_states.values()):
            if isinstance(state, dict):
                ref_qids |= collect_ref_qids(state)
        labels: dict = {}
        ref_sorted = sorted(ref_qids, key=qid_sort_key)
        for i in range(0, len(ref_sorted), WD_BATCH_SIZE):
            batch = ref_sorted[i:i + WD_BATCH_SIZE]
            try:
                entities = wbget_batch(batch, "labels|sitelinks|aliases",
                                       {"languages": "en", "sitefilter": "enwiki"}, sleep)
            except RuntimeError as exc:
                http_errors.append({"stage": "wd_labels", "qids": list(batch),
                                    "error": str(exc)})
                continue
            for qid in batch:
                ent = entities.get(qid)
                if not isinstance(ent, dict) or "missing" in ent:
                    labels[qid] = {"qid": qid, "label": None, "sitelink": None, "aliases": []}
                    continue
                aliases = sorted(
                    a.get("value", "") for a in ent.get("aliases", {}).get("en", [])
                    if isinstance(a, dict) and a.get("value"))
                labels[qid] = {
                    "qid": qid,
                    "label": ent.get("labels", {}).get("en", {}).get("value"),
                    "sitelink": ent.get("sitelinks", {}).get("enwiki", {}).get("title"),
                    "aliases": aliases,
                }

        p570_by_qid: dict = {}
        rows = []
        for cand in candidates:
            title = cand["title"]
            qid = cand.get("qid")
            errors: list = []
            cutoff_block = None
            current_block = None
            if qid:
                state, err = cutoff_states.get(qid, (None, "cutoff_not_fetched"))
                if isinstance(state, dict):
                    for unres in sorted(fill_labels(state, labels)):
                        errors.append(f"label_unresolved:{unres}")
                    cutoff_block = state
                if err:
                    errors.append(err)
                state, err = current_states.get(qid, (None, "current_not_fetched"))
                if isinstance(state, dict):
                    for unres in sorted(fill_labels(state, labels)):
                        label_err = f"label_unresolved:{unres}"
                        if label_err not in errors:
                            errors.append(label_err)
                    current_block = state
                    p570 = wd_date_from_state(state, "P570")
                    if p570 is not None:
                        p570_by_qid[qid] = (p570[0], p570[1])
                if err:
                    errors.append(err)
            rows.append({
                "title": title,
                "resolved_title": cand.get("resolved_title", title),
                "qid": qid,
                "title_status": cand.get("title_status", "ok"),
                "cutoff": cutoff_block,
                "current": current_block,
                "errors": errors,
            })
        return rows, sitelink_counts, p570_by_qid

    # -- (d) recognisability signal: pre-death baseline pageviews ----------
    def _fetch_pageviews(self, titles, ym, sleep, http_errors,
                         fetch_titles=None) -> list:
        """One REST pageviews call per title for the baseline month ``ym``
        ('YYYY-MM'). ``fetch_titles`` maps a row-key title to the article
        actually queried (the redirect-resolved title on the sample path —
        recorded as ``fetched_title`` on the row when it differs). A 404 is a
        DEFINITE no-data outcome (views 0, note 'no_data' — some real bios
        genuinely had zero pre-death views); a persistent failure records
        views null (-> the adapter reviews as recognisability_unknown) in
        http_errors, never a silent pass/fail."""
        rows = []
        for i, title in enumerate(titles, 1):
            article = (fetch_titles or {}).get(title) or title
            views, note, error = self._fetch_monthly_views(article, ym)
            if error is not None:
                http_errors.append({"stage": "pageviews", "title": title, "error": error})
            row = {
                "title": title,
                "project": PAGEVIEWS_PROJECT,
                "access": "all-access",
                "agent": "all-agents",
                "granularity": "monthly",
                "month": ym,
                "views": views,
            }
            if article != title:
                row["fetched_title"] = article
            if note:
                row["note"] = note
            if error:
                row["error"] = error
            rows.append(row)
            time.sleep(sleep)
            if i % 50 == 0 or i == len(titles):
                print(f"[harvest people]   pageviews {i}/{len(titles)}",
                      file=sys.stderr, flush=True)
        return rows

    @staticmethod
    def _fetch_monthly_views(title, ym):
        """(views | None, note | None, error | None) for one title-month via
        the Wikimedia REST pageviews API (retry + backoff; 404 -> (0,
        'no_data', None))."""
        require_contact_email()
        import json as _json
        import urllib.error
        import urllib.request

        ym_compact = ym.replace("-", "")
        start = f"{ym_compact}0100"
        end = f"{ym_compact}{_month_last_day(ym):02d}00"
        article = urllib.parse.quote(title.replace(" ", "_"), safe="")
        url = (f"{PAGEVIEWS_ENDPOINT}/{PAGEVIEWS_PROJECT}/all-access/all-agents/"
               f"{article}/monthly/{start}/{end}")
        last_error = "no attempt made"
        for attempt in range(1, PV_TRIES + 1):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": ENWIKI_USER_AGENT})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = _json.load(resp)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    # The API 404s when the article has NO pageview data for
                    # the month (not created yet / zero traffic): a definite
                    # answer, not a fetch miss.
                    return 0, "no_data", None
                last_error = f"HTTP {exc.code}"
                wait = 30.0 if exc.code == 429 else min(30.0, PV_BASE_SLEEP * (2 ** (attempt - 1)))
                print(f"  [pageviews {last_error}] retry in {wait:.0f}s "
                      f"(attempt {attempt}/{PV_TRIES})", file=sys.stderr, flush=True)
                time.sleep(wait)
                continue
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                wait = min(30.0, PV_BASE_SLEEP * (2 ** (attempt - 1)))
                print(f"  [pageviews {last_error}] retry in {wait:.0f}s "
                      f"(attempt {attempt}/{PV_TRIES})", file=sys.stderr, flush=True)
                time.sleep(wait)
                continue
            items = payload.get("items") if isinstance(payload, dict) else None
            if isinstance(items, list) and items and isinstance(items[0], dict):
                views = items[0].get("views")
                if isinstance(views, int) and not isinstance(views, bool):
                    return views, None, None
            return 0, "empty_items", None
        return None, None, f"pageviews unreachable after {PV_TRIES} tries: {last_error}"

    # -- snapshot emission (PURE given already-fetched rows) ---------------
    def _emit(self, writer, cfg, discovery, candidate_rows, verified_rows,
              wikitext_rows, wd_rows, pageview_rows, pv_month,
              enwiki_sleep, wd_sleep, pv_sleep, stats) -> None:
        """Route the fetched rows through the SnapshotWriter: five caches via
        add_jsonl, the two .meta.json sidecars via add_json (sha1-pinned; NO
        wall-clock retrieved_at — the finance-port determinism fix), and
        params/stats onto the manifest. No network — tests drive this with
        fixture rows to assert snapshot layout."""
        out_dir = writer.out_dir

        writer.add_jsonl(CANDIDATES_FILENAME, candidate_rows)
        candidates_sha1 = sha1_file(out_dir / CANDIDATES_FILENAME)
        writer.add_jsonl(VERIFIED_FILENAME, verified_rows)
        verified_sha1 = sha1_file(out_dir / VERIFIED_FILENAME)

        writer.add_jsonl(WD_CACHE_FILENAME, wd_rows)
        wd_cache_sha1 = sha1_file(out_dir / WD_CACHE_FILENAME)
        writer.add_json(WD_SIDECAR_FILENAME,
                        self._wd_sidecar(cfg, wd_rows, candidates_sha1, wd_cache_sha1))

        writer.add_jsonl(WIKITEXT_CACHE_FILENAME, wikitext_rows)
        wt_cache_sha1 = sha1_file(out_dir / WIKITEXT_CACHE_FILENAME)
        writer.add_json(WIKITEXT_SIDECAR_FILENAME,
                        self._wikitext_sidecar(cfg, wikitext_rows, verified_sha1, wt_cache_sha1))

        writer.add_jsonl(PAGEVIEWS_FILENAME, pageview_rows)

        writer.set_params({
            "discovery": discovery,
            "sparql_endpoint": SPARQL_ENDPOINT,
            "sparql_row_cap": SPARQL_LIMIT,
            "cap_split_days": CAP_SPLIT_DAYS,
            "wikidata_endpoint": WIKIDATA_API,
            "enwiki_endpoint": ENWIKI_REV_API,
            "pageviews_endpoint": PAGEVIEWS_ENDPOINT,
            "wikidata_user_agent": WD_USER_AGENT,
            "enwiki_user_agent": ENWIKI_USER_AGENT,
            "extractor_version": EXTRACTOR_VERSION,
            "prose_version": PROSE_VERSION,
            "wd_decoder_version": DECODER_VERSION,
            "death_property_whitelist": list(DEATH_PROPERTY_WHITELIST),
            "before_pin": _cutoff_pin(cfg["cutoff"]),
            "after_pin": _asof_pin(cfg["asof"]),
            "wd_cutoff_pin": _cutoff_pin(cfg["cutoff"]),
            "discovery_windows": [[lo.isoformat(), hi.isoformat()]
                                  for lo, hi in death_windows(cfg["cutoff"], cfg["asof"])],
            "pageviews_baseline_month": pv_month,
            # The derive-side thresholds, recorded here as DOCUMENTATION of
            # the defaults the snapshot was designed for (the adapter owns and
            # manifest-records the active values; all tunable).
            "derive_default_thresholds": {
                "body_prose_min_chars": ADAPTER_DEFAULT_POLICY["body_prose_min_chars"],
                "pre_death_monthly_views_min": ADAPTER_DEFAULT_POLICY["pre_death_monthly_views_min"],
                "sitelinks_min": ADAPTER_DEFAULT_POLICY["sitelinks_min"],
            },
            # The OBSERVED distribution of the three threshold-bearing signals
            # across THIS snapshot's frozen rows — the auditable, regenerable
            # data-grounding of the thresholds above (recomputable offline at
            # any time via stage1.tools.people_threshold_study).
            "signal_distribution": signal_distribution(
                verified_rows, candidate_rows, pageview_rows),
            "enwiki_sleep_s": enwiki_sleep,
            "wd_sleep_s": wd_sleep,
            "pv_sleep_s": pv_sleep,
            "sample": sorted({c.get("title") for c in candidate_rows if c.get("title")})
            if cfg.get("sample") else None,
            "files": [CANDIDATES_FILENAME, VERIFIED_FILENAME, WD_CACHE_FILENAME,
                      WIKITEXT_CACHE_FILENAME, PAGEVIEWS_FILENAME],
        })
        writer.set_stats(stats)
        print(f"[harvest people] DONE. {stats['candidates']} candidate(s); "
              f"{stats['verified']} verified ({stats['with_infobox_deathdate']} with an "
              f"infobox death date); {stats['wd_rows']} WD row(s); "
              f"{stats['pageview_rows']} pageview row(s); "
              f"{len(stats['http_errors'])} http miss(es).", file=sys.stderr, flush=True)

    @staticmethod
    def _wd_sidecar(cfg, wd_rows, candidates_sha1, cache_sha1) -> dict:
        """The people_wd_p570.meta.json sidecar — the fetch_wikidata_people
        meta shape, death-scoped, MINUS the wall-clock retrieved_at. The
        adapter reads ``property_whitelist`` from here to assert the cache
        covers the death properties (corroboration reviews otherwise)."""
        with_qid = [r for r in wd_rows if r.get("qid")]
        rows_with_errors = sum(1 for r in wd_rows if r.get("errors"))
        return {
            "tool_version": TOOL_VERSION,
            "decoder_version": DECODER_VERSION,
            "endpoints": {"enwiki": ENWIKI_API, "wikidata": WIKIDATA_API},
            "user_agent": WD_USER_AGENT,
            "cutoff_ts": _cutoff_pin(cfg["cutoff"]),
            "property_whitelist": list(DEATH_PROPERTY_WHITELIST),
            "qualifier_whitelist": list(QUALIFIER_WHITELIST),
            "label_language": "en",
            "sitefilter": "enwiki",
            "query_params": {
                "cutoff_revision_call": {"action": "query", "prop": "revisions",
                                         "rvprop": "ids|timestamp|content",
                                         "rvslots": "main", "rvdir": "older",
                                         "rvlimit": "1",
                                         "rvstart": _cutoff_pin(cfg["cutoff"]),
                                         "batch_size": 1},
                "current_call": {"action": "wbgetentities",
                                 "props": "info|claims|sitelinks",
                                 "batch_size": WD_BATCH_SIZE},
                "label_call": {"action": "wbgetentities",
                               "props": "labels|sitelinks|aliases", "languages": "en",
                               "sitefilter": "enwiki", "batch_size": WD_BATCH_SIZE},
            },
            "candidates_file": CANDIDATES_FILENAME,
            "candidates_sha1": candidates_sha1,
            "counts": {
                "titles": len(wd_rows),
                "titles_with_qid": len(with_qid),
                "rows_with_errors": rows_with_errors,
            },
            "cache_sha1": cache_sha1,
        }

    @staticmethod
    def _wikitext_sidecar(cfg, wikitext_rows, verified_sha1, cache_sha1) -> dict:
        """The people_wikitext.meta.json sidecar — the fetch_wiki_revisions
        meta shape MINUS retrieved_at. Every pin is by the revision's own
        timestamp (ts_source='row')."""
        err_titles = sorted(t["title"] for t in wikitext_rows if t.get("fetch_errors"))
        n_ok = sum(1 for r in wikitext_rows if not r.get("fetch_errors"))
        return {
            "tool_version": TOOL_VERSION,
            "endpoint": ENWIKI_REV_API,
            "user_agent": ENWIKI_USER_AGENT,
            "query_params": {
                "action": "query", "prop": "revisions", "rvprop": RV_PROPS,
                "rvslots": "main", "rvlimit": "1", "rvdir": "older",
                "rvstart": "before=cutoff day end, after=asof day end; "
                           "cache pinned to each revision's own timestamp",
                "maxlag": "5",
            },
            "pins": {
                "cutoff": _cutoff_pin(cfg["cutoff"]),
                "current": _asof_pin(cfg["asof"]),
            },
            "verified_file": VERIFIED_FILENAME,
            "verified_sha1": verified_sha1,
            "counts": {
                "titles": len(wikitext_rows),
                "cached_rows": len(wikitext_rows),
                "rows_ok": n_ok,
                "rows_with_fetch_errors": len(wikitext_rows) - n_ok,
            },
            "fetch_error_titles": err_titles,
            "cache_sha1": cache_sha1,
        }


HARVESTER = PeopleHarvester()
