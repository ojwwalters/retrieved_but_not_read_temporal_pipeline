"""Wikipedia sports-transfer harvester — the THIRD harvest source (Wikipedia).

Ports the legacy overnight harvest (wikipedia/sports/sports_harvest.py) plus the
two standalone enrichment tools (stage1.tools.fetch_wikidata_p54 /
fetch_wiki_revisions) into the harvest framework, so
``python3 -m stage1.harvest --source sports --cutoff X --asof Y`` writes a FROZEN
snapshot the OFFLINE sports adapter (stage1/adapters/sports.py) derives from
unchanged. It composes THREE fetch steps, parameterized by [cutoff, asof]:

  (a) DISCOVERY (the finder, NOT the ground truth) — Wikidata SPARQL over an
      occupation whitelist (P106) x monthly windows on the P54 start-date
      qualifier (P580), yielding the players who changed team in-window. Emitted
      as sports_candidates.jsonl {qid, title, sport_hint, team_new_wd, date}. For
      a small live test the SPARQL scan is replaced by direct enwiki title ->
      Wikidata QID resolution over the --sample titles (no full discovery).

  (b) THE GROUND TRUTH — per candidate, the CURRENT-CLUB INFOBOX value is
      extracted from the player's PINNED Wikipedia revisions: the newest revision
      at/before the cutoff (the 'before' club) and at/before asof (the 'after'
      club), via the shared, versioned stage1.wikitext / stage1.adapters.
      sports_infobox extractor. This infobox value — never a Wikidata value — is
      the recorded before/after, so a question ("according to Wikipedia, what club
      does X play for") has defensible ground truth. Emitted as
      sports_verified.jsonl (old_club/new_club + the pinned revision timestamps).

  (c) THE CORROBORATION / RECOVERY CACHES — the P54 enrichment cache
      (sports_wd_p54.jsonl, Wikidata as the corroboration + event-dating source
      ONLY) and the pinned-revision wikitext cache (sports_wikitext.jsonl, the
      full wikitext of the SAME two revisions used in (b), so the adapter's
      versioned re-extraction is byte-anchored) — each with its .meta.json
      sidecar. All API code is IMPORTED from the two fetch tools (fetch_players /
      fetch_teams / build_cache_rows / api_get / fetch_side), never duplicated.

PRIMARY-SOURCE INVARIANT (owner-confirmed): the recorded before/after VALUE is
the Wikipedia infobox value (evidence.kind='wikipedia_infobox', produced by the
adapter from these pinned revisions). Wikidata (P54) corroborates and dates the
change; it is NEVER the recorded answer. ``old_club``/``new_club`` here come
ONLY from ``extract_current_club`` over the pinned revision wikitext.

COVERAGE SEMANTICS — cutoff_exact AND asof_exact (like finance, stricter than
SEC). A sports snapshot pins TWO specific revisions per player: the newest at/
before ``cutoff`` (before) and at/before ``asof`` (after). A derive whose cutoff
or asof differs would resolve to DIFFERENT revisions not in the snapshot, so
``coverage()`` sets BOTH ``cutoff_exact=True`` and ``asof_exact=True`` and the
shared ``check_coverage`` refuses any derive whose cutoff != coverage.cutoff
(rule 4b) or asof != coverage.asof (rule 4). Sports is fully BACK-DATABLE
(Wikipedia revisions + Wikidata history are permanent), so a different [cutoff,
asof] simply pins different revisions and produces a different valid snapshot.

Determinism: the harvest sidecars OMIT the wall-clock ``retrieved_at`` (the
finance-port fix) so a re-harvest of byte-identical data yields a reproducible
derive input fingerprint; a snapshot's only wall-clock is
``snapshot_manifest.harvested_at``. NO LLM anywhere.
"""

from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from pathlib import Path

from stage1.harvest import Harvester
from stage1.harvest.snapshot import sha1_file
from stage1.adapters.sports_infobox import EXTRACTOR_VERSION, extract_current_club
from stage1.tools.fetch_wikidata_p54 import (
    API as WD_API,
    BATCH_SIZE as WD_BATCH_SIZE,
    USER_AGENT as WD_USER_AGENT,
    api_get as wd_api_get,
    build_cache_rows as build_p54_cache_rows,
    fetch_players,
    fetch_teams,
    qid_sort_key,
)
from stage1.tools.fetch_wiki_revisions import (
    API as ENWIKI_API,
    FALLBACK_CURRENT_TS,
    FALLBACK_CUTOFF_TS,
    RV_PROPS,
    USER_AGENT as ENWIKI_USER_AGENT,
    api_get as enwiki_api_get,
    fetch_side,
)
from stage1.config import require_contact_email

TOOL_VERSION = "harvest_sports:v1"

# The EXACT fixed filenames the sports adapter reads by name.
CANDIDATES_FILENAME = "sports_candidates.jsonl"
VERIFIED_FILENAME = "sports_verified.jsonl"
WD_CACHE_FILENAME = "sports_wd_p54.jsonl"
WIKITEXT_CACHE_FILENAME = "sports_wikitext.jsonl"
WD_SIDECAR_FILENAME = "sports_wd_p54.meta.json"
WIKITEXT_SIDECAR_FILENAME = "sports_wikitext.meta.json"

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

# Occupation whitelist (sport -> P106 occupation QID), ported verbatim from the
# legacy sports_harvest.SPORTS. Scoping by an INDEXED occupation makes the P580
# date-range SPARQL tractable (a global P580 filter times WDQS out).
DEFAULT_OCCUPATIONS = {
    "football": "Q937857",      # association football player
    "nfl": "Q19204627",         # American football player
    "nba": "Q3665646",          # basketball player
    "mlb": "Q10871364",         # baseball player
    "nhl": "Q11774891",         # ice hockey player
}

# Politeness defaults (overridable via --opt); the enwiki API tolerates ~5 req/s,
# WDQS/wbgetentities is throttled far harder (the legacy harvest self-paced).
DEFAULT_ENWIKI_SLEEP = 0.2
DEFAULT_WD_SLEEP = 1.0

# WDQS backoff (ported from the legacy sports_harvest.http_json): cap at 90s so a
# recovering endpoint is retried often; a 429 throttle waits ~70s.
SPARQL_TRIES = 8
SPARQL_BASE_SLEEP = 5.0

# The per-sport-month P580 discovery row cap. A sport-month returning EXACTLY the
# cap is a truncation signal (WDQS returned the ceiling, so later in-window
# players were dropped) and is FATAL — mirroring the SEC harvester's FTS
# pagination-cap abort — rather than silently under-covering discovery.
SPARQL_LIMIT = 5000


def _iso(value) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def _cutoff_pin(cutoff) -> str:
    """The rvstart pin for the 'before' side: the START of the cutoff day, so the
    newest revision at/before it is the page state entering the cutoff (matches
    the legacy CUTOFF constant 2026-02-01T00:00:00Z)."""
    return f"{_iso(cutoff)}T00:00:00Z"


def _asof_pin(asof) -> str:
    """The rvstart pin for the 'after' side: the END of the asof day, so the
    newest revision at/before it is the page state as of asof (inclusive)."""
    return f"{_iso(asof)}T23:59:59Z"


def _infobox_club(result) -> str:
    """The infobox club string from an extract_current_club result, or '' when
    the side has NO DISPLAYED value (unreadable / no article / a blank-or-absent
    current-club field). This is the PRIMARY-SOURCE value: it is the Wikipedia
    infobox reading, never Wikidata. A loan-annotated club is still the displayed
    club (the adapter's loan gate screens it).

    An 'unattached' state with an EXPLICIT free-agency marker
    ('currentclub = Free agent'/'Retired'/...) DID display a literal infobox
    value, so that marker (extract_current_club's detail['marker'], which is the
    cleaned infobox field reading, NOT a Wikidata label) is preserved as the
    recorded value — otherwise the harvest would empty a real infobox reading,
    defeating the downstream ``sports_free_agent`` gate (which keys on the
    literal after value) and diverging from the committed release. Only an
    unattached state WITHOUT a displayed marker (reason 'field_blank' /
    'field_absent' — the field is blank or absent), and every 'no_value' status,
    map to '' (there is no infobox value to record)."""
    if not isinstance(result, dict):
        return ""
    if result.get("status") == "club" and isinstance(result.get("club"), str):
        return result["club"]
    if result.get("status") == "unattached" and result.get("reason") == "explicit_marker":
        marker = (result.get("detail") or {}).get("marker")
        if isinstance(marker, str) and marker:
            return marker
    return ""


def sparql_get(query, sleep):
    """GET the WDQS SPARQL endpoint with the legacy backoff (cap 90s; a 429
    throttle waits ~70s). Returns the parsed JSON, or None after exhausting
    retries (the caller aborts loudly). Module-level so the people death
    harvester reuses the SAME polite WDQS client instead of duplicating it."""
    require_contact_email()
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    url = SPARQL_ENDPOINT + "?" + urllib.parse.urlencode({"query": query})
    headers = {"User-Agent": WD_USER_AGENT, "Accept": "application/sparql-results+json"}
    for attempt in range(SPARQL_TRIES):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.load(resp)
            time.sleep(sleep)
            return payload
        except urllib.error.HTTPError as exc:
            wait = 70.0 if exc.code == 429 else min(90.0, SPARQL_BASE_SLEEP * (2 ** attempt))
            print(f"  [WDQS HTTP {exc.code}] backoff {wait:.0f}s (attempt {attempt + 1})",
                  file=sys.stderr, flush=True)
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            wait = min(90.0, SPARQL_BASE_SLEEP * (2 ** attempt))
            print(f"  [WDQS {type(exc).__name__}] backoff {wait:.0f}s (attempt {attempt + 1})",
                  file=sys.stderr, flush=True)
            time.sleep(wait)
    return None


def months_in_window(lo: date, hi: date):
    """Every YYYY-MM spanned by [lo, hi] inclusive — the P580 months SPARQL
    discovery queries. Derived from the window, never hardcoded."""
    out = []
    y, m = lo.year, lo.month
    while (y, m) <= (hi.year, hi.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _next_month(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    return f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"


def discovery_months(cutoff: date, asof: date):
    """The P580 months discovery scans: from the month BEFORE the cutoff (the
    corroboration window's lower edge, prev_month_start) through asof's month, so
    a harvest-edge change dated in the cutoff-1 month is still discovered."""
    prev = (cutoff.replace(day=1) - timedelta(days=1))
    return months_in_window(prev.replace(day=1), asof)


# --------------------------------------------------------------------------- #
# Resume checkpoint helpers (a combined .part keyed by title so a title's
# verified row and its wikitext row stay in lockstep).
# --------------------------------------------------------------------------- #
def _load_part(path: Path) -> dict:
    import json
    out: dict = {}
    if not path.is_file():
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue  # torn last line of an interrupted append
            if isinstance(row, dict) and isinstance(row.get("title"), str):
                out[row["title"]] = row
    return out


def _append_part(part_fh, row) -> None:
    import json
    part_fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
    part_fh.flush()


class SportsHarvester(Harvester):
    source = "sports"
    tool_version = TOOL_VERSION

    # -- coverage (PURE, no network) ---------------------------------------
    def coverage(self, cfg: dict) -> dict:
        cutoff = cfg["cutoff"]
        asof = cfg["asof"]
        return {
            "cutoff": _iso(cutoff),
            "asof": _iso(asof),
            # BOTH edges pinned: 'before' is the newest revision at/before THIS
            # cutoff, 'after' the newest at/before THIS asof — a different cutoff/
            # asof resolves to different revisions that are not in the snapshot.
            "cutoff_exact": True,
            "asof_exact": True,
            "precision": "day",
            # [cutoff, asof] bounds the two pinned revision snapshots (endpoints),
            # not the change's effective date (which lands between them).
            "window_basis": "revision_snapshot_endpoints",
        }

    # -- harvest (the ONLY networked method) -------------------------------
    def harvest(self, cfg: dict, writer, ctx: dict) -> None:
        cutoff = cfg["cutoff"]
        asof = cfg["asof"]
        sample = cfg.get("sample")
        resume = bool(cfg.get("resume"))
        enwiki_sleep = float(cfg.get("enwiki_sleep", DEFAULT_ENWIKI_SLEEP))
        wd_sleep = float(cfg.get("wd_sleep", DEFAULT_WD_SLEEP))
        http_errors: list = []

        # ---- (a) DISCOVERY -> candidates -------------------------------------
        if sample:
            titles = sorted({t.strip() for t in sample if t and t.strip()})
            if not titles:
                raise LookupError("sports --sample produced no titles")
            discovery = "sample_titles"
            candidates = self._discover_sample(titles, enwiki_sleep, http_errors)
        else:
            occupations = self._resolve_occupations(cfg)
            discovery = "wikidata_sparql"
            candidates = self._discover_sparql(cutoff, asof, occupations,
                                               enwiki_sleep, wd_sleep)
        print(f"[harvest sports] discovery={discovery}: {len(candidates)} candidate(s)",
              file=sys.stderr, flush=True)

        # ---- (b) GROUND TRUTH: pinned-revision infobox extraction ------------
        verified_by_title, wikitext_by_title = self._extract_infoboxes(
            candidates, cutoff, asof, writer.out_dir, resume, enwiki_sleep, http_errors)

        # Backfill each candidate's sport_hint from the extracted sport when the
        # finder did not supply one (the sample path has no SPARQL occupation).
        for cand in candidates:
            if not cand.get("sport_hint"):
                v = verified_by_title.get(cand["title"]) or {}
                cand["sport_hint"] = v.get("sport")

        # ---- (c) CORROBORATION cache: Wikidata P54 --------------------------
        qids = sorted({c["qid"] for c in candidates if c.get("qid")}, key=qid_sort_key)
        p54_rows = self._fetch_p54(qids, wd_sleep, http_errors)

        candidate_rows = sorted(candidates, key=lambda c: str(c.get("title") or ""))
        verified_rows = list(verified_by_title.values())
        wikitext_rows = list(wikitext_by_title.values())

        stats = {
            "discovery": discovery,
            "candidates": len(candidate_rows),
            "verified": len(verified_rows),
            "changed": sum(1 for r in verified_rows if r.get("changed")),
            "p54_players": len(p54_rows),
            "http_errors": http_errors,
            "resumed": resume,
        }
        self._emit(writer, cfg, discovery, candidate_rows, verified_rows,
                   wikitext_rows, p54_rows, enwiki_sleep, wd_sleep, stats)

        # Drop the resume checkpoint now the frozen files are written.
        part = writer.out_dir / (VERIFIED_FILENAME + ".part")
        if part.exists():
            part.unlink()

    # -- (a) discovery: sample titles -> QIDs ------------------------------
    def _discover_sample(self, titles, sleep, http_errors) -> list:
        """Resolve each --sample enwiki title to its Wikidata QID via the enwiki
        pageprops API (wikibase_item). Candidate finder fields (team_new_wd/date)
        are left empty: the SPARQL finder is skipped for a small live test, and
        the P54 cache — not the finder — corroborates and dates the change."""
        resolved = self._resolve_qids_from_titles(titles, sleep, http_errors)
        candidates = []
        for title in titles:
            candidates.append({
                "qid": resolved.get(title),
                "title": title,
                "sport_hint": None,
                "team_new_wd": "",
                "date": "",
            })
        missing = [t for t in titles if not resolved.get(t)]
        if missing:
            print(f"[harvest sports] {len(missing)} sample title(s) had no Wikidata QID "
                  f"(corroboration will review): {missing}", file=sys.stderr, flush=True)
        return candidates

    def _resolve_qids_from_titles(self, titles, sleep, http_errors) -> dict:
        """{enwiki title: wikidata QID or None} via prop=pageprops (batched).
        Normalization + redirect maps re-key the API's canonical title back to
        the requested one; an unresolved title yields None (not an error)."""
        out: dict = {}
        titles = sorted(set(titles))
        for i in range(0, len(titles), WD_BATCH_SIZE):
            batch = titles[i:i + WD_BATCH_SIZE]
            try:
                payload = enwiki_api_get({
                    "action": "query",
                    "prop": "pageprops",
                    "ppprop": "wikibase_item",
                    "titles": "|".join(batch),
                    "redirects": "1",
                })
            except RuntimeError as exc:
                for t in batch:
                    out.setdefault(t, None)
                http_errors.append({"stage": "resolve_qids", "titles": batch, "error": str(exc)})
                continue
            query = payload.get("query", {}) if isinstance(payload, dict) else {}
            norm = {n.get("from"): n.get("to") for n in query.get("normalized", [])}
            redir = {r.get("from"): r.get("to") for r in query.get("redirects", [])}
            pages = {p.get("title"): p for p in query.get("pages", []) if isinstance(p, dict)}
            for req in batch:
                final = redir.get(norm.get(req, req), norm.get(req, req))
                page = pages.get(final)
                qid = None
                if isinstance(page, dict):
                    qid = (page.get("pageprops") or {}).get("wikibase_item")
                out[req] = qid if isinstance(qid, str) and qid else None
            time.sleep(sleep)
        return out

    # -- (a) discovery: full Wikidata SPARQL scan --------------------------
    @staticmethod
    def _resolve_occupations(cfg) -> dict:
        """The {sport: occupation QID} map to scan. --opt occupations=football,nba
        restricts to a subset of the whitelist; absent -> the full whitelist."""
        override = cfg.get("occupations")
        if isinstance(override, str) and override.strip():
            wanted = {s.strip().lower() for s in override.split(",") if s.strip()}
            picked = {s: q for s, q in DEFAULT_OCCUPATIONS.items() if s in wanted}
            if picked:
                return picked
        return dict(DEFAULT_OCCUPATIONS)

    def _discover_sparql(self, cutoff, asof, occupations, enwiki_sleep, wd_sleep) -> list:
        """Wikidata SPARQL over occupation x month windows on the P54 P580
        start-date qualifier (ported from legacy phase-1). Returns candidate
        dicts {qid, title, sport_hint, team_new_wd, date}, deduped by title
        (first occurrence wins). A persistent SPARQL failure for a sport-month is
        FATAL: keeping the other months would silently under-cover discovery."""
        by_title: dict = {}
        info: dict = {}  # player_qid -> {team_qid, date, sport}
        for sport, occ in sorted(occupations.items()):
            for ym in discovery_months(cutoff, asof):
                query = (
                    "SELECT ?player ?team ?start WHERE {\n"
                    f"  ?player wdt:P106 wd:{occ} .\n"
                    "  ?player p:P54 ?st . ?st ps:P54 ?team ; pq:P580 ?start .\n"
                    f'  FILTER(?start >= "{ym}-01"^^xsd:dateTime && '
                    f'?start < "{_next_month(ym)}-01"^^xsd:dateTime) }} LIMIT {SPARQL_LIMIT}'
                )
                payload = self._sparql_get(query, wd_sleep)
                if payload is None:
                    raise RuntimeError(
                        f"WDQS SPARQL discovery failed for {sport} {ym} after retries: the "
                        "sport-month's players are unreachable, so the snapshot would silently "
                        "UNDER-COVER discovery — aborting rather than writing an incomplete "
                        "snapshot (rerun when WDQS is reachable; per-title extraction resumes "
                        "from its .part checkpoint)"
                    )
                bindings = payload.get("results", {}).get("bindings", [])
                if len(bindings) >= SPARQL_LIMIT:
                    raise RuntimeError(
                        f"WDQS SPARQL discovery for {sport} {ym} returned the row cap "
                        f"({SPARQL_LIMIT}): the sport-month is too large to return without "
                        "truncation, so later in-window players were dropped and the snapshot "
                        "would silently UNDER-COVER discovery — aborting rather than writing an "
                        "incomplete snapshot (narrow the [cutoff, asof] window, or restrict "
                        "--opt occupations= to fewer sports per run, so every sport-month "
                        "returns fewer than the cap)"
                    )
                for binding in bindings:
                    pqid = binding["player"]["value"].rsplit("/", 1)[-1]
                    tqid = binding["team"]["value"].rsplit("/", 1)[-1]
                    start = (binding.get("start", {}).get("value") or "")[:10]
                    info.setdefault(pqid, {"team_qid": tqid, "date": start, "sport": sport})
                print(f"[harvest sports]   {sport} {ym}: {len(bindings)} P54 rows",
                      file=sys.stderr, flush=True)
        # Resolve player + team QIDs to enwiki titles / labels (wbgetentities,
        # NOT subject to the WDQS throttle).
        entities = self._resolve_entities(
            set(info) | {v["team_qid"] for v in info.values()}, wd_sleep)
        for pqid, meta in info.items():
            title = entities.get(pqid, {}).get("title")
            if not title or title in by_title:
                continue
            by_title[title] = {
                "qid": pqid,
                "title": title,
                "sport_hint": meta["sport"],
                "team_new_wd": entities.get(meta["team_qid"], {}).get("label", ""),
                "date": meta["date"],
            }
        return list(by_title.values())

    def _sparql_get(self, query, sleep):
        """Delegates to the module-level :func:`sparql_get` (kept as a method
        so tests patching ``SportsHarvester._sparql_get`` keep working)."""
        return sparql_get(query, sleep)

    def _resolve_entities(self, qids, sleep) -> dict:
        """{QID: {'title': enwiki title, 'label': en label}} via batched
        wbgetentities (props=sitelinks|labels). Reuses the P54 tool's api_get.

        Title resolution is an integral part of SPARQL discovery: a discovered
        player with no resolved enwiki title has no candidate row (``title`` is
        the ground-truth key). A persistent wbgetentities failure for a batch is
        therefore FATAL — mirroring the sport-month SPARQL abort in
        ``_discover_sparql`` and the SEC harvester's fatal-on-discovery rule —
        rather than swallowing the batch and silently DROPPING those players from
        candidates (the derive coverage check never inspects fetch_stats, so an
        under-covered discovery would look complete). The sibling sample path
        (``_resolve_qids_from_titles``) is deliberately non-fatal instead: there
        the enwiki title is the INPUT and survives a failed QID lookup, so only
        corroboration degrades — no candidate is lost."""
        out: dict = {}
        qids = sorted({q for q in qids if q}, key=qid_sort_key)
        for i in range(0, len(qids), WD_BATCH_SIZE):
            batch = qids[i:i + WD_BATCH_SIZE]
            try:
                payload = wd_api_get({
                    "action": "wbgetentities",
                    "ids": "|".join(batch),
                    "props": "sitelinks|labels",
                    "sitefilter": "enwiki",
                    "languages": "en",
                })
            except RuntimeError as exc:
                raise RuntimeError(
                    f"wbgetentities title/label resolution failed for a batch of "
                    f"{len(batch)} QID(s) after retries ({exc}): a discovered player's "
                    "enwiki title could not be resolved, so that player would be silently "
                    "DROPPED from candidates and the snapshot would UNDER-COVER discovery — "
                    "aborting rather than writing an incomplete snapshot (rerun when Wikidata "
                    "is reachable; per-title extraction resumes from its .part checkpoint)"
                ) from exc
            for qid, ent in (payload.get("entities", {}) if isinstance(payload, dict) else {}).items():
                if not isinstance(ent, dict):
                    continue
                out[qid] = {
                    "title": ent.get("sitelinks", {}).get("enwiki", {}).get("title"),
                    "label": ent.get("labels", {}).get("en", {}).get("value", ""),
                }
            time.sleep(sleep)
        return out

    # -- (b) ground truth: pinned-revision infobox extraction --------------
    def _extract_infoboxes(self, candidates, cutoff, asof, out_dir, resume,
                           sleep, http_errors):
        """For each candidate title, fetch the pinned 'before' (<=cutoff) and
        'after' (<=asof) revisions ONCE, extract the current-club infobox value
        from each (the PRIMARY before/after), and reuse the same revision content
        for the wikitext cache. Returns (verified_by_title, wikitext_by_title).

        Resumable: a title whose .part row has NO fetch_errors AND whose stamped
        window pins MATCH this run's [cutoff, asof] pins is skipped on --resume;
        an errored or other-window row is refetched. The pin stamp closes the
        stale-checkpoint hole (shared with the people harvester): without it,
        rerunning an interrupted harvest into the same out-dir after CHANGING
        the window would accept rows pinned to the OLD window while the
        manifest claims cutoff_exact/asof_exact for the new one."""
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
        print(f"[harvest sports] extraction: {len(done)} resumed "
              f"({stale} checkpoint row(s) from another window ignored), "
              f"{len(todo)} to fetch (before<={cutoff_pin}, after<={asof_pin})",
              file=sys.stderr, flush=True)

        results = dict(done)
        with open(part_path, "a", encoding="utf-8", newline="\n") as part:
            for i, cand in enumerate(todo, 1):
                title = cand["title"]
                before_side, before_err = self._fetch_snapshot_side(
                    title, cutoff_pin, "cutoff", sleep, http_errors)
                after_side, after_err = self._fetch_snapshot_side(
                    title, asof_pin, "current", sleep, http_errors)
                verified, wikitext = self._build_rows(
                    cand, before_side, before_err, after_side, after_err)
                results[title] = {"title": title, "pins": pins,
                                  "verified": verified, "wikitext": wikitext}
                _append_part(part, results[title])
                if i % 25 == 0 or i == len(todo):
                    print(f"[harvest sports]   extracted {i}/{len(todo)} title(s)",
                          file=sys.stderr, flush=True)

        verified_by_title = {t: r["verified"] for t, r in results.items()}
        wikitext_by_title = {t: r["wikitext"] for t, r in results.items()}
        return verified_by_title, wikitext_by_title

    def _fetch_snapshot_side(self, title, date_pin, side_name, sleep, http_errors):
        """Fetch the newest revision of ``title`` at/before ``date_pin`` (reusing
        fetch_wiki_revisions.fetch_side), then RELABEL the cache side's pin to the
        revision's own timestamp — exactly the row that fetch_wiki_revisions would
        produce if handed that timestamp, and what the verified row records as the
        revision timestamp, so the adapter's cache-pin cross-check passes
        (ts_source='row', pinned_ts == recorded rev ts). Returns (side|None, err)."""
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

    def _build_rows(self, cand, before_side, before_err, after_side, after_err):
        """Build one sports_verified.jsonl row and one sports_wikitext.jsonl row
        from the two pinned revisions. old_club/new_club are the INFOBOX values
        (extract_current_club over the pinned wikitext) — never Wikidata."""
        before_result = extract_current_club(before_side.get("content")) if before_side else None
        after_result = extract_current_club(after_side.get("content")) if after_side else None
        old_club = _infobox_club(before_result)
        new_club = _infobox_club(after_result)
        sport = ((after_result or {}).get("sport") or (before_result or {}).get("sport"))
        gt_field = ((after_result or {}).get("field") or (before_result or {}).get("field"))
        changed = bool(old_club and new_club
                       and old_club.strip().lower() != new_club.strip().lower())
        cutoff_rev_ts = before_side.get("ts") if before_side else None
        cur_rev_ts = after_side.get("ts") if after_side else None

        verified = {
            "title": cand["title"],
            "sport": sport,
            "changed": changed,
            "team_new_wd": cand.get("team_new_wd", ""),
            "wd_date": cand.get("date", ""),
            "gt_field": gt_field,
            "old_club": old_club,
            "new_club": new_club,
            "cutoff_rev_ts": cutoff_rev_ts,
            "cur_rev_ts": cur_rev_ts,
            "views": 0,
        }
        fetch_errors = []
        if before_err:
            fetch_errors.append(f"cutoff: {before_err}")
        if after_err:
            fetch_errors.append(f"current: {after_err}")
        wikitext = {
            "title": cand["title"],
            "cutoff": before_side,
            "current": after_side,
            "fetch_errors": fetch_errors,
        }
        return verified, wikitext

    # -- (c) corroboration cache: Wikidata P54 -----------------------------
    def _fetch_p54(self, qids, sleep, http_errors) -> list:
        """The P54 enrichment cache rows for the candidate player QIDs, built by
        the SHARED fetch_players / fetch_teams / build_cache_rows from the P54
        tool (no duplicated API code). The corroboration cache is a SECOND source
        (never the recorded value), so a Wikidata miss degrades corroboration to
        review rather than crashing.

        BATCH-RESILIENT: each WD_BATCH_SIZE chunk is fetched INDEPENDENTLY (the
        shared fetch_players / fetch_teams already batch at WD_BATCH_SIZE, so a
        single-chunk call is one API batch), so one unreachable batch loses
        corroboration ONLY for its own players/teams — recorded per-chunk in
        http_errors — instead of discarding every successfully-fetched player's
        statements on the first bad batch. Chunk boundaries and the final
        sorted-by-QID join are identical to the all-at-once call, so a clean run
        yields byte-identical cache rows."""
        if not qids:
            return []
        players: dict = {}
        for i in range(0, len(qids), WD_BATCH_SIZE):
            chunk = qids[i:i + WD_BATCH_SIZE]
            try:
                players.update(fetch_players(chunk, sleep))
            except RuntimeError as exc:
                http_errors.append({"stage": "p54_players", "qids": list(chunk),
                                    "error": str(exc)})
                print(f"[harvest sports] P54 player fetch failed for a batch of "
                      f"{len(chunk)} ({exc}); those players' corroboration degrades to "
                      "review", file=sys.stderr, flush=True)
        team_qids = sorted({s["team_qid"] for p in players.values()
                            for s in p["statements"] if s["team_qid"]}, key=qid_sort_key)
        teams: dict = {}
        for i in range(0, len(team_qids), WD_BATCH_SIZE):
            chunk = team_qids[i:i + WD_BATCH_SIZE]
            try:
                teams.update(fetch_teams(chunk, sleep))
            except RuntimeError as exc:
                http_errors.append({"stage": "p54_teams", "qids": list(chunk),
                                    "error": str(exc)})
                print(f"[harvest sports] P54 team fetch failed for a batch of "
                      f"{len(chunk)} ({exc}); those teams' labels are unresolved (affected "
                      "records may review)", file=sys.stderr, flush=True)
        return build_p54_cache_rows(players, teams)

    # -- snapshot emission (PURE given already-fetched rows) ---------------
    def _emit(self, writer, cfg, discovery, candidate_rows, verified_rows,
              wikitext_rows, p54_rows, enwiki_sleep, wd_sleep, stats) -> None:
        """Route the fetched rows through the SnapshotWriter: the four caches via
        add_jsonl, the two .meta.json sidecars via add_json (sha1-pinned; NO
        wall-clock retrieved_at, so a re-harvest of identical data yields a
        reproducible derive fingerprint), and params/stats onto the manifest. No
        network — a test drives this with fixture rows to assert snapshot layout.
        """
        cutoff_iso = _iso(cfg["cutoff"])
        asof_iso = _iso(cfg["asof"])
        out_dir = writer.out_dir

        # (a) candidates + (b) verified — the finder output and the infobox GT.
        writer.add_jsonl(CANDIDATES_FILENAME, candidate_rows)
        candidates_sha1 = sha1_file(out_dir / CANDIDATES_FILENAME)
        writer.add_jsonl(VERIFIED_FILENAME, verified_rows)
        verified_sha1 = sha1_file(out_dir / VERIFIED_FILENAME)

        # (c) P54 corroboration cache + its sidecar (retrieved_at OMITTED).
        writer.add_jsonl(WD_CACHE_FILENAME, p54_rows)
        wd_cache_sha1 = sha1_file(out_dir / WD_CACHE_FILENAME)
        writer.add_json(WD_SIDECAR_FILENAME,
                        self._wd_sidecar(p54_rows, candidates_sha1, wd_cache_sha1))

        # (c) pinned-revision wikitext cache + its sidecar (retrieved_at OMITTED).
        writer.add_jsonl(WIKITEXT_CACHE_FILENAME, wikitext_rows)
        wt_cache_sha1 = sha1_file(out_dir / WIKITEXT_CACHE_FILENAME)
        writer.add_json(WIKITEXT_SIDECAR_FILENAME,
                        self._wikitext_sidecar(wikitext_rows, verified_sha1, wt_cache_sha1))

        writer.set_params({
            "discovery": discovery,
            "sparql_endpoint": SPARQL_ENDPOINT,
            "wikidata_endpoint": WD_API,
            "enwiki_endpoint": ENWIKI_API,
            "wikidata_user_agent": WD_USER_AGENT,
            "enwiki_user_agent": ENWIKI_USER_AGENT,
            "extractor_version": EXTRACTOR_VERSION,
            "occupation_whitelist": DEFAULT_OCCUPATIONS,
            "before_pin": _cutoff_pin(cfg["cutoff"]),
            "after_pin": _asof_pin(cfg["asof"]),
            "enwiki_sleep_s": enwiki_sleep,
            "wd_sleep_s": wd_sleep,
            "sample": sorted({t for c in candidate_rows for t in [c.get("title")] if t})
            if cfg.get("sample") else None,
            "files": [CANDIDATES_FILENAME, VERIFIED_FILENAME, WD_CACHE_FILENAME,
                      WIKITEXT_CACHE_FILENAME],
        })
        writer.set_stats(stats)
        print(f"[harvest sports] DONE. {stats['candidates']} candidate(s); "
              f"{stats['verified']} verified ({stats['changed']} changed); "
              f"{stats['p54_players']} P54 player(s); {len(stats['http_errors'])} http miss(es).",
              file=sys.stderr, flush=True)

    @staticmethod
    def _wd_sidecar(p54_rows, candidates_sha1, cache_sha1) -> dict:
        """The sports_wd_p54.meta.json sidecar — the fetch_wikidata_p54 meta
        shape MINUS the wall-clock retrieved_at (finance-port determinism fix).
        The adapter surfaces tool_version/endpoint/candidates_file/candidates_sha1/
        cache_sha1 into the manifest; the rest is provenance."""
        ok = [r for r in p54_rows if r.get("status") == "ok"]
        missing = sorted((r["player_qid"] for r in p54_rows if r.get("status") != "ok"),
                         key=qid_sort_key)
        redirected = sorted((r["player_qid"] for r in ok
                             if r.get("resolved_qid") != r.get("player_qid")), key=qid_sort_key)
        n_statements = sum(len(r.get("statements") or []) for r in p54_rows)
        team_qids = {s.get("team_qid") for r in p54_rows for s in (r.get("statements") or [])
                     if s.get("team_qid")}
        return {
            "tool_version": TOOL_VERSION,
            "endpoint": WD_API,
            "user_agent": WD_USER_AGENT,
            "query_params": {
                "player_call": {"action": "wbgetentities", "props": "claims",
                                "batch_size": WD_BATCH_SIZE},
                "team_call": {"action": "wbgetentities",
                              "props": "labels|aliases|sitelinks", "languages": "en",
                              "sitefilter": "enwiki", "batch_size": WD_BATCH_SIZE},
            },
            "candidates_file": CANDIDATES_FILENAME,
            "candidates_sha1": candidates_sha1,
            "candidates_load_errors": [],
            "counts": {
                "players_requested": len(p54_rows),
                "players_ok": len(ok),
                "players_missing": len(missing),
                "players_redirected": len(redirected),
                "p54_statements": n_statements,
                "teams_resolved": len(team_qids),
            },
            "missing_player_qids": missing,
            "redirected_player_qids": redirected,
            "cache_sha1": cache_sha1,
        }

    @staticmethod
    def _wikitext_sidecar(wikitext_rows, verified_sha1, cache_sha1) -> dict:
        """The sports_wikitext.meta.json sidecar — the fetch_wiki_revisions meta
        shape MINUS retrieved_at. The harvester always pins by the revision's own
        timestamp (ts_source='row'), so no fallback pin is used; the fallback
        CONSTANTS are recorded for provenance parity with the standalone tool."""
        err_titles = sorted(t["title"] for t in wikitext_rows if t.get("fetch_errors"))
        n_ok = sum(1 for r in wikitext_rows if not r.get("fetch_errors"))
        return {
            "tool_version": TOOL_VERSION,
            "endpoint": ENWIKI_API,
            "user_agent": ENWIKI_USER_AGENT,
            "query_params": {
                "action": "query", "prop": "revisions", "rvprop": RV_PROPS,
                "rvslots": "main", "rvlimit": "1", "rvdir": "older",
                "rvstart": "before=cutoff day start, after=asof day end; "
                           "cache pinned to each revision's own timestamp",
                "maxlag": "5",
            },
            "fallback_pins": {
                "cutoff": FALLBACK_CUTOFF_TS,
                "current": FALLBACK_CURRENT_TS,
                "used_this_run": 0,
            },
            "verified_file": VERIFIED_FILENAME,
            "verified_sha1": verified_sha1,
            "verified_load_errors": [],
            "counts": {
                "titles": len(wikitext_rows),
                "cached_rows": len(wikitext_rows),
                "rows_ok": n_ok,
                "rows_with_fetch_errors": len(wikitext_rows) - n_ok,
            },
            "fetch_error_titles": err_titles,
            "cache_sha1": cache_sha1,
        }


HARVESTER = SportsHarvester()
