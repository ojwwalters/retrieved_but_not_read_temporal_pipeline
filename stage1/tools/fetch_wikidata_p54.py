"""One-time Wikidata P54 enrichment fetch for the sports adapter.

The legacy sports harvest (wikipedia/sports/sports_harvest.py) kept only ONE
P54 (member of sports team) binding per player and discarded the team QID and
the P580/P582 date PRECISION integers. The Stage-1 sports adapter needs all
of that to corroborate the Wikipedia infobox value against Wikidata offline
(replacing the legacy LLM tier-3 web-verify gate), so this tool fetches it
once and freezes it into a deterministic cache the pipeline can replay.

For every player QID in sports_candidates.jsonl it retrieves ALL P54 claims
(action=wbgetentities, props=claims, batches of <=50 ids) and, for every team
QID those claims target, the English label, English aliases, and the enwiki
sitelink title (props=labels|aliases|sitelinks). The enwiki sitelink title is
the primary comparison key downstream — it lives in the same namespace as the
infobox wikilink target the harvest extracted.

Outputs:

* cache (default stage1/cache/sports_wd_p54.jsonl): one JSON object per
  player, sorted by numeric QID, sorted keys — byte-identical for identical
  API data. Row shape:
      {"player_qid": "Q485444", "status": "ok"|"missing",
       "resolved_qid": "Q485444",       # differs from player_qid on redirect
       "statements": [{"team_qid", "team_enwiki_title", "team_label_en",
                       "team_aliases_en", "p580": {"time", "precision"}|null,
                       "p582": {...}|null, "rank", "snaktype"}, ...]}
  p580/p582 keep the raw Wikidata time string AND the wikibase precision
  integer (11=day, 10=month, 9=year) that the legacy SPARQL harvest threw
  away. Statements are sorted by (p580 time, p582 time, team_qid, rank).
* sidecar (default stage1/cache/sports_wd_p54.meta.json): retrieval
  timestamp, endpoint, query params, input file sha1, and counts. The
  timestamp lives ONLY here — the cache itself stays deterministic.

The pipeline itself never calls this tool or the network: the sports adapter
only READS the cache, and a missing/incomplete cache entry surfaces as a
'review' disposition via gate evidence, never a crash.

Run from the repo root (network access to www.wikidata.org required):

    python3 -m stage1.tools.fetch_wikidata_p54 \
        --candidates wikipedia/sports/sports_candidates.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from stage1.config import require_contact_email, user_agent

TOOL_VERSION = "fetch_wikidata_p54:v1"

# Wikidata's replication-lag gate. 5s is the guidance for BULK bots and stays the default
# so the original frozen cache remains reproducible by the original command. It bounds
# AVAILABILITY, not content — the API returns identical data at any setting — so a small
# interactive top-up (the 43 control players added 2026-08-11, when wdqs sat at 11-15s lag
# for an extended period) may raise it without affecting determinism.
MAXLAG = "5"
API = "https://www.wikidata.org/w/api.php"
USER_AGENT = user_agent()
PLAYER_PROPS = "claims"
TEAM_PROPS = "labels|aliases|sitelinks"
BATCH_SIZE = 50
_QID_RE = re.compile(r"^Q\d+$")

_DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"


def qid_sort_key(qid: str):
    return (int(qid[1:]), qid)


def api_get(params: dict, tries: int = 8, base_sleep: float = 2.0) -> dict:
    """GET the Wikidata API with retry/backoff. Raises RuntimeError after
    exhausting retries — this is a one-time online tool, so failing loudly is
    correct (the offline pipeline never runs this code path)."""
    require_contact_email()
    query = urllib.parse.urlencode({**params, "format": "json", "maxlag": MAXLAG})
    url = f"{API}?{query}"
    last_error = "no attempt made"
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=90) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as exc:
            wait = 45.0 if exc.code == 429 else min(60.0, base_sleep * (2 ** (attempt - 1)))
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
                wait = float(payload["error"].get("lag", 5)) + 2.0
                print(f"  [maxlag] retry in {wait:.0f}s (attempt {attempt}/{tries})",
                      file=sys.stderr, flush=True)
                time.sleep(wait)
                continue
            raise RuntimeError(f"Wikidata API error for {params.get('ids', '')[:80]}: {last_error}")
        return payload
    raise RuntimeError(f"Wikidata API unreachable after {tries} tries: {last_error}")


def load_candidate_qids(path: Path):
    """(sorted unique player QIDs, load_errors). Malformed lines are recorded,
    never silently skipped."""
    qids = set()
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
            qid = row.get("qid") if isinstance(row, dict) else None
            if not isinstance(qid, str) or not _QID_RE.match(qid):
                errors.append({"line": line_no, "error": f"missing/malformed qid: {qid!r}"})
                continue
            qids.add(qid)
    return sorted(qids, key=qid_sort_key), errors


def _time_value(snak) -> dict | None:
    """{'time', 'precision'} from a qualifier snak, or None."""
    if not isinstance(snak, dict):
        return None
    value = snak.get("datavalue", {}).get("value")
    if not isinstance(value, dict) or "time" not in value:
        return None
    return {"time": value.get("time"), "precision": value.get("precision")}


def extract_p54_statements(entity: dict) -> list:
    """All P54 statements of one player entity, as plain sorted dicts."""
    statements = []
    for claim in entity.get("claims", {}).get("P54", []):
        if not isinstance(claim, dict):
            continue
        mainsnak = claim.get("mainsnak", {})
        snaktype = mainsnak.get("snaktype", "value")
        team_qid = None
        if snaktype == "value":
            target = mainsnak.get("datavalue", {}).get("value")
            if isinstance(target, dict):
                team_qid = target.get("id")
        qualifiers = claim.get("qualifiers", {})
        p580_list = qualifiers.get("P580", [])
        p582_list = qualifiers.get("P582", [])
        statements.append(
            {
                "team_qid": team_qid,
                "snaktype": snaktype,
                "rank": claim.get("rank", "normal"),
                "p580": _time_value(p580_list[0]) if p580_list else None,
                "p582": _time_value(p582_list[0]) if p582_list else None,
            }
        )
    statements.sort(
        key=lambda s: (
            (s["p580"] or {}).get("time") or "",
            (s["p582"] or {}).get("time") or "",
            s["team_qid"] or "",
            s["rank"],
        )
    )
    return statements


def fetch_players(qids, sleep: float):
    """player_qid -> {'status', 'resolved_qid', 'statements'} via batched
    wbgetentities. Redirected ids are resolved through the API's redirect
    mapping; deleted/missing ids get status 'missing' with no statements."""
    players = {}
    batches = [qids[i:i + BATCH_SIZE] for i in range(0, len(qids), BATCH_SIZE)]
    for n, batch in enumerate(batches, 1):
        payload = api_get({
            "action": "wbgetentities",
            "ids": "|".join(batch),
            "props": PLAYER_PROPS,
        })
        entities = payload.get("entities", {})
        for requested in batch:
            entity = entities.get(requested)
            resolved = requested
            if entity is None:
                # redirect: the entity is keyed by its target id
                for ent in entities.values():
                    if isinstance(ent, dict) and ent.get("id") != requested:
                        redirect = ent.get("redirects", {})
                        if redirect.get("from") == requested:
                            entity, resolved = ent, ent.get("id", requested)
                            break
            elif isinstance(entity, dict) and entity.get("id") not in (None, requested):
                resolved = entity["id"]
            if not isinstance(entity, dict) or "missing" in entity:
                players[requested] = {"status": "missing", "resolved_qid": requested,
                                      "statements": []}
                continue
            players[requested] = {
                "status": "ok",
                "resolved_qid": resolved,
                "statements": extract_p54_statements(entity),
            }
        print(f"[players] batch {n}/{len(batches)} done ({len(players)} players)",
              file=sys.stderr, flush=True)
        time.sleep(sleep)
    return players


def fetch_teams(team_qids, sleep: float):
    """team_qid -> {'label_en', 'aliases_en', 'enwiki_title'}."""
    teams = {}
    qids = sorted(team_qids, key=qid_sort_key)
    batches = [qids[i:i + BATCH_SIZE] for i in range(0, len(qids), BATCH_SIZE)]
    for n, batch in enumerate(batches, 1):
        payload = api_get({
            "action": "wbgetentities",
            "ids": "|".join(batch),
            "props": TEAM_PROPS,
            "languages": "en",
            "sitefilter": "enwiki",
        })
        for qid, entity in payload.get("entities", {}).items():
            if not isinstance(entity, dict) or "missing" in entity:
                teams[qid] = {"label_en": "", "aliases_en": [], "enwiki_title": ""}
                continue
            label = entity.get("labels", {}).get("en", {}).get("value", "")
            aliases = sorted(
                a.get("value", "")
                for a in entity.get("aliases", {}).get("en", [])
                if isinstance(a, dict) and a.get("value")
            )
            title = entity.get("sitelinks", {}).get("enwiki", {}).get("title", "")
            resolved_id = entity.get("id", qid)
            info = {"label_en": label, "aliases_en": aliases, "enwiki_title": title}
            teams[resolved_id] = info
            if resolved_id != qid:
                teams[qid] = info  # redirected team qid resolves to the same info
        # any batch member the response did not cover at all
        for qid in batch:
            teams.setdefault(qid, {"label_en": "", "aliases_en": [], "enwiki_title": ""})
        print(f"[teams] batch {n}/{len(batches)} done ({len(teams)} teams)",
              file=sys.stderr, flush=True)
        time.sleep(sleep)
    return teams


def build_cache_rows(players, teams):
    """[player cache row], sorted by numeric QID, joining resolved team info
    into every P54 statement — the exact rows written to sports_wd_p54.jsonl.

    Pure (no I/O). Extracted so the standalone tool AND the sports harvester
    (stage1.harvest.sports) build byte-identical cache rows from the same
    fetch_players / fetch_teams output — the tool's cache and a harvest
    snapshot's P54 cache cannot silently drift."""
    rows = []
    for qid in sorted(players, key=qid_sort_key):
        info = players[qid]
        statements = []
        for s in info["statements"]:
            team = teams.get(s["team_qid"] or "", {})
            statements.append(
                {
                    "team_qid": s["team_qid"],
                    "team_enwiki_title": team.get("enwiki_title", ""),
                    "team_label_en": team.get("label_en", ""),
                    "team_aliases_en": list(team.get("aliases_en", [])),
                    "p580": s["p580"],
                    "p582": s["p582"],
                    "rank": s["rank"],
                    "snaktype": s["snaktype"],
                }
            )
        rows.append(
            {
                "player_qid": qid,
                "status": info["status"],
                "resolved_qid": info["resolved_qid"],
                "statements": statements,
            }
        )
    return rows


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    global MAXLAG
    parser = argparse.ArgumentParser(
        prog="python3 -m stage1.tools.fetch_wikidata_p54",
        description="One-time Wikidata P54 enrichment fetch for the sports adapter.",
    )
    parser.add_argument(
        "--candidates",
        default="../0_prior_work/wikipedia/sports/sports_candidates.jsonl",
        help="sports_candidates.jsonl with one player qid per row",
    )
    parser.add_argument(
        "--out",
        default=str(_DEFAULT_CACHE_DIR / "sports_wd_p54.jsonl"),
        help="cache output path (default stage1/cache/sports_wd_p54.jsonl)",
    )
    parser.add_argument(
        "--meta",
        default=str(_DEFAULT_CACHE_DIR / "sports_wd_p54.meta.json"),
        help="sidecar metadata path (default stage1/cache/sports_wd_p54.meta.json)",
    )
    parser.add_argument("--sleep", type=float, default=1.0,
                        help="seconds to sleep between API calls (politeness)")
    parser.add_argument("--maxlag", default=MAXLAG,
                        help="Wikidata maxlag seconds (default 5, the bulk-bot guidance; "
                             "raise only for small interactive top-ups). Bounds "
                             "availability, not content")
    args = parser.parse_args(argv)
    MAXLAG = str(args.maxlag)

    candidates_path = Path(args.candidates)
    if not candidates_path.is_file():
        print(f"error: candidates file not found: {candidates_path}", file=sys.stderr)
        return 2

    qids, load_errors = load_candidate_qids(candidates_path)
    for err in load_errors:
        print(f"[candidates] line {err['line']}: {err['error']}", file=sys.stderr)
    print(f"[fetch] {len(qids)} unique player QIDs from {candidates_path}",
          file=sys.stderr, flush=True)

    players = fetch_players(qids, args.sleep)
    team_qids = {
        s["team_qid"]
        for p in players.values()
        for s in p["statements"]
        if s["team_qid"]
    }
    print(f"[fetch] {len(team_qids)} distinct team QIDs to resolve", file=sys.stderr, flush=True)
    teams = fetch_teams(team_qids, args.sleep)

    # ---- join team info into the statements and write the sorted cache ----
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = build_cache_rows(players, teams)
    n_statements = sum(len(r["statements"]) for r in rows)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")

    missing = sorted(
        (q for q, p in players.items() if p["status"] != "ok"), key=qid_sort_key
    )
    redirected = sorted(
        (q for q, p in players.items() if p["status"] == "ok" and p["resolved_qid"] != q),
        key=qid_sort_key,
    )
    meta = {
        "tool_version": TOOL_VERSION,
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "endpoint": API,
        "user_agent": USER_AGENT,
        "query_params": {
            "player_call": {"action": "wbgetentities", "props": PLAYER_PROPS,
                            "batch_size": BATCH_SIZE},
            "team_call": {"action": "wbgetentities", "props": TEAM_PROPS,
                          "languages": "en", "sitefilter": "enwiki",
                          "batch_size": BATCH_SIZE},
        },
        "candidates_file": str(candidates_path),
        "candidates_sha1": sha1_file(candidates_path),
        "candidates_load_errors": load_errors,
        "counts": {
            "players_requested": len(qids),
            "players_ok": sum(1 for p in players.values() if p["status"] == "ok"),
            "players_missing": len(missing),
            "players_redirected": len(redirected),
            "p54_statements": n_statements,
            "teams_resolved": len(team_qids),
        },
        "missing_player_qids": missing,
        "redirected_player_qids": redirected,
        "cache_file": cache_identifier(out_path, args.out),
        "cache_sha1": sha1_file(out_path),
    }
    meta_path = Path(args.meta)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    with open(meta_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")

    print(f"[fetch] wrote {len(players)} players / {n_statements} P54 statements -> {out_path}",
          file=sys.stderr, flush=True)
    print(f"[fetch] sidecar -> {meta_path}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
