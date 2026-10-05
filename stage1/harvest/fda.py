"""FDA drug-label section-change harvester — the FIFTH source (non-back-datable).

Ports the legacy two-step FDA extraction into the harvest framework so
``python3 -m stage1.harvest --source fda --cutoff X --asof Y`` writes a FROZEN
snapshot the OFFLINE fda adapter (``stage1/adapters/fda.py``) derives from
unchanged. Like finance/sports it is a THIN ORCHESTRATOR that IMPORTS, never
duplicates, the two standalone fetch tools — the openFDA API code lives in
``stage1.tools.fetch_openfda_rmc`` and the Wayback API code in
``stage1.tools.fetch_dailymed_history``; the harvester only composes them and
routes their outputs through the ``SnapshotWriter``. It emits the files the fda
adapter reads by name:

    fda_dailymed_history.jsonl   (the before/after history cache, adapter input)
    fda_dailymed_history.meta.json (its .meta.json sidecar; retrieved_at OMITTED)
    fda_rmc_2026.jsonl           (the RMC provenance: effective_time + known)
    drug_universe_top300.csv     (OPTIONAL reference that defined 'known'; copied
                                  in when present — never a gate)
    snapshot_manifest.json  .stage1_snapshot

harvest() composes TWO steps, parameterized by [cutoff, asof]:

  (a) RMC DISCOVERY (openFDA) — ``fetch_openfda_rmc`` finds labels with an RMC
      field whose changes fall in-window and pulls the CURRENT section text (the
      ground-truth 'after'). Full-scan for a real window; per-set_id for
      ``--sample`` (a small live test that NEVER runs the full RMC scan). Produces
      the RMC rows (the fda_rmc_2026.jsonl shape).

  (b) WAYBACK 'BEFORE' FETCH — ``fetch_dailymed_history.process_group`` recovers
      the pre-change section text from the Internet Archive's DailyMed snapshots
      (the latest statuscode-200 text/html capture before the change month). One
      CDX + one snapshot fetch per drug (set_id-grouped), gzip-aware, resumable
      via a scratch .part OUTSIDE the snapshot. Produces the history-cache rows
      (before + after per changed section).

The 'before' evidence is the Wayback DailyMed archive (the adapter emits
``evidence.kind='dailymed_archive'``); the 'after' is the current openFDA/DailyMed
label. FDA is a SINGLE authoritative source (the label) — there is NO
corroboration gate.

COVERAGE SEMANTICS — CONTEMPORANEOUS, back_datable=False. FDA is the ONE
non-back-datable source: openFDA's Recent Major Changes only surfaces changes on
CURRENT labels (you cannot reconstruct 'what were the RMC changes as of a past
window'), and the 'after' is the contemporaneous current label. The Internet
Archive 'before' fetch IS back-datable, but the candidate SET from RMC is not, so
the whole harvest is only sound near the window it ran for. ``coverage()`` records
``back_datable=False`` honestly, and — because the 'after' is the harvest-time
current label and the candidate set is discovered for THIS exact window — sets
BOTH ``cutoff_exact=True`` and ``asof_exact=True`` so a re-derive at a WIDER or
SHIFTED [cutoff, asof] is refused loudly (rules 4/4b), with ONE sanctioned
exception: ``cutoff_narrowable=True`` (owner decision 2026-07-23, benchmark
window alignment). FDA's before/after evidence is PER-CHANGE — every harvested
change row carries its own Wayback pre-change 'before' and asof-pinned 'after' —
so the evidence a derive at a LATER cutoff needs is a pure subset of what this
snapshot holds; the early candidates stay enumerated and become audit-visible
``excluded:temporal_window`` rows, never silent drops. Narrowing relaxes NOTHING
else: a cutoff earlier than coverage.cutoff stays refused (rule 2/4b) and asof
must still EQUAL coverage.asof (rule 4). RMC change dates are MM/YYYY, so
``precision='month'`` and
``window_basis='rmc_change_month_within_effective_time_window'``. That basis is
DELIBERATELY explicit: the candidate SET is discovered by the openFDA query
``_exists_:recent_major_changes AND effective_time:[cutoff TO asof]`` (the
label's own revision date), then the kept changes are those whose RMC MONTH
intersects [cutoff, asof]. So a genuine in-window RMC change on a label that was
re-revised AFTER asof (a later ``effective_time``) is NOT returned by the query
and cannot even be recorded as a miss — an inherent openFDA limitation (it serves
only current labels) and precisely why ``back_datable=False``. The window_basis
names BOTH bounds rather than overstating that it bounds RMC months alone.

Determinism: the sidecar OMITS the wall-clock ``retrieved_at`` (the finance-port
fix) so a re-harvest of byte-identical data yields a reproducible derive input
fingerprint; a snapshot's only wall-clock is ``snapshot_manifest.harvested_at``.
NO LLM anywhere; the ground truth is a deterministic before/after text diff.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, OrderedDict
from datetime import date
from pathlib import Path

from stage1.harvest import Harvester
from stage1.harvest.snapshot import sha1_file
from stage1.tools.fetch_openfda_rmc import (
    BASE as OPENFDA_ENDPOINT,
    TOOL_VERSION as RMC_TOOL_VERSION,
    USER_AGENT as OPENFDA_USER_AGENT,
    OpenFdaError,
    build_query_full,
    fetch_records,
    fetch_records_by_setids,
    load_top300,
    rmc_rows_from_records,
)
from stage1.tools.fetch_dailymed_history import (
    CDX_ENDPOINT,
    FULL_SECTION_CAP,
    MIN_SNAPSHOT_DAY,
    SNIPPET_CAP,
    TERMINAL_STATUSES,
    USER_AGENT as WAYBACK_USER_AGENT,
    WAYBACK_BASE,
    load_cached,
    make_cache_row,
    process_group,
    row_key,
)

TOOL_VERSION = "harvest_fda:v1"

# The EXACT fixed filenames the fda adapter reads by name.
HISTORY_FILENAME = "fda_dailymed_history.jsonl"
HISTORY_META_FILENAME = "fda_dailymed_history.meta.json"
RMC_FILENAME = "fda_rmc_2026.jsonl"
# The optional top-300 reference that defined the 'known' flag; copied in for a
# self-contained snapshot (the adapter does not read it — 'known' is baked into
# the RMC rows — so it is a NON-gating provenance scope).
TOP300_FILENAME = "drug_universe_top300.csv"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_TOP300_PATH = REPO_ROOT / "drugs" / TOP300_FILENAME

# Wayback politeness defaults (mirror the standalone tool; overridable via --opt).
DEFAULT_WAYBACK_SLEEP = 1.5
DEFAULT_WAYBACK_TRIES = 6
DEFAULT_WAYBACK_TIMEOUT = 120.0
# openFDA politeness defaults (moderate anonymous use; overridable via --opt).
DEFAULT_OPENFDA_SLEEP = 0.5
DEFAULT_OPENFDA_TRIES = 4
DEFAULT_OPENFDA_TIMEOUT = 60.0


def _iso(value) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


class FdaHarvester(Harvester):
    source = "fda"
    tool_version = TOOL_VERSION

    # -- coverage (PURE, no network) ---------------------------------------
    def coverage(self, cfg: dict) -> dict:
        """The contemporaneous, non-back-datable coverage descriptor.

        back_datable=False states plainly that the candidate SET (openFDA RMC on
        CURRENT labels) and the 'after' (the current label) cannot be
        reconstructed at a past window. cutoff_exact + asof_exact PIN the window:
        the candidate set is discovered for THIS [cutoff, asof] and the 'after' is
        the harvest-time label, so a re-derive at a wider or shifted window is
        refused (rules 4/4b) — EXCEPT that cutoff_narrowable=True sanctions a
        derive cutoff LATER than coverage.cutoff (owner decision 2026-07-23:
        the benchmark cutoff moved to 2026-02-01 after this source was harvested
        at 2026-01-01). That narrowing is evidence-complete because FDA's
        before/after evidence is PER-CHANGE (each row carries its own Wayback
        'before' and asof-pinned 'after'), so a narrower window is a pure subset
        whose early candidates become audit-visible temporal exclusions. asof
        must still EQUAL coverage.asof (rule 4) and an earlier cutoff stays
        refused. precision='month' reflects the MM/YYYY RMC change dates."""
        return {
            "cutoff": _iso(cfg["cutoff"]),
            "asof": _iso(cfg["asof"]),
            "back_datable": False,
            # a re-derive is only sound at the harvested window — refuse a
            # different cutoff or asof (the candidate set + contemporaneous
            # 'after' were fetched for THIS exact window)...
            "cutoff_exact": True,
            "asof_exact": True,
            # ...EXCEPT a LATER derive cutoff (sanctioned narrowing, owner
            # decision 2026-07-23): before/after evidence is per-change, so a
            # narrower window is an evidence-complete subset — its excluded
            # early candidates stay enumerated as temporal-exclusion audit rows.
            # asof equality is still required; an earlier cutoff is still refused.
            "cutoff_narrowable": True,
            "precision": "month",
            # [cutoff, asof] bounds the RMC CHANGE months of the candidate set
            # (MM/YYYY, month precision) AND — the honest part — the label's own
            # effective_time (the discovery query is
            # effective_time:[cutoff TO asof]). A basis that named only the RMC
            # month would overstate coverage: an in-window change on a label
            # re-revised after asof is not in the query result and cannot be
            # recorded as a miss. This is inherent to openFDA (current-labels
            # only) and the reason back_datable=False. Not a pinned revision or
            # trading day.
            "window_basis": "rmc_change_month_within_effective_time_window",
        }

    # -- harvest (the ONLY networked method) -------------------------------
    def harvest(self, cfg: dict, writer, ctx: dict) -> None:
        cutoff = cfg["cutoff"]
        asof = cfg["asof"]
        sample = cfg.get("sample")
        resume = bool(cfg.get("resume"))
        of_sleep = float(cfg.get("openfda_sleep", DEFAULT_OPENFDA_SLEEP))
        of_tries = int(cfg.get("openfda_tries", DEFAULT_OPENFDA_TRIES))
        of_timeout = float(cfg.get("openfda_timeout", DEFAULT_OPENFDA_TIMEOUT))
        wb_sleep = float(cfg.get("wayback_sleep", DEFAULT_WAYBACK_SLEEP))
        wb_tries = int(cfg.get("wayback_tries", DEFAULT_WAYBACK_TRIES))
        wb_timeout = float(cfg.get("wayback_timeout", DEFAULT_WAYBACK_TIMEOUT))
        http_errors: list = []

        # Optional top-300 reference (defines the 'known' flag; never gates). A
        # cfg override wins; else the repo default; a missing file degrades every
        # 'known' to False with a warning — never a crash.
        top_path = self._resolve_top300(cfg)
        top = load_top300(top_path) if top_path else {}
        if not top:
            print(f"[harvest fda] top-300 reference not found (known -> False for all): "
                  f"{top_path}", file=sys.stderr, flush=True)

        # ---- (a) RMC DISCOVERY (openFDA) ---------------------------------
        # requested_sample = the sample set_ids AS REQUESTED (verbatim, normalized
        # + sorted), recorded in the manifest so a requested drug that yields no
        # in-window change is still visible as requested-but-empty — never silently
        # replaced by the candidate-producing subset. None for a full scan.
        requested_sample = None
        if sample:
            set_ids = sorted({s.strip() for s in sample if s and s.strip()})
            if not set_ids:
                raise LookupError("fda --sample produced no set_ids")
            requested_sample = set_ids
            discovery = "openfda_setids"
            openfda_query = list(set_ids)
            records, of_requests, of_errors = fetch_records_by_setids(
                set_ids, sleep=of_sleep, tries=of_tries, timeout=of_timeout)
            http_errors.extend(of_errors)
        else:
            discovery = "openfda_rmc_full"
            openfda_query = build_query_full(cutoff, asof)
            records, of_requests = fetch_records(
                openfda_query, sleep=of_sleep, tries=of_tries, timeout=of_timeout)
        rmc_rows = rmc_rows_from_records(records, top, cutoff, asof)
        print(f"[harvest fda] discovery={discovery}: {len(records)} label(s) -> "
              f"{len(rmc_rows)} in-window RMC change(s) via {of_requests} openFDA call(s)",
              file=sys.stderr, flush=True)

        if sample:
            got = {r["set_id"] for r in rmc_rows}
            missing = [s for s in set_ids if s not in got]
            if missing:
                print(f"[harvest fda] {len(missing)} sample set_id(s) produced no in-window "
                      f"RMC change (no candidate): {missing}", file=sys.stderr, flush=True)

        # ---- (b) WAYBACK 'BEFORE' FETCH (resumable) ----------------------
        history_rows, wb_requests, transient = self._fetch_before(
            rmc_rows, writer.out_dir, resume, wb_sleep, wb_tries, wb_timeout, http_errors)

        params = {
            "openfda_sleep_s": of_sleep, "openfda_tries": of_tries,
            "openfda_timeout_s": of_timeout,
            "wayback_sleep_s": wb_sleep, "wayback_tries": wb_tries,
            "wayback_timeout_s": wb_timeout,
        }
        self._emit(writer, cfg, discovery, openfda_query, requested_sample, rmc_rows,
                   history_rows, of_requests, wb_requests, transient, top_path, top,
                   params, http_errors)

    # -- (b) Wayback before-fetch: set_id-grouped, resumable ---------------
    def _fetch_before(self, rmc_rows, out_dir, resume, sleep, tries, timeout, http_errors):
        """For every RMC row, recover the pre-change 'before' section from the
        Internet Archive via ``process_group`` (one CDX + one snapshot per drug).
        Returns (history_rows, wayback_requests, transient_count).

        Resumable like finance/sports: the resume ``.part`` checkpoint is written
        to a SCRATCH dir OUTSIDE the snapshot (SnapshotWriter is the sole writer of
        snapshot bytes), and a group whose section rows are all TERMINAL is skipped
        on --resume. A row left in a transient state (a Wayback timeout — NOT a
        true miss) keeps its status, is recorded in http_errors, and the scratch is
        preserved so a later --resume retries it; only an all-terminal run cleans
        the scratch up."""
        groups: "OrderedDict[str, list]" = OrderedDict()
        for r in rmc_rows:
            groups.setdefault(r["set_id"], []).append(r)

        scratch = out_dir.parent / (".harvest_scratch_" + out_dir.name)
        scratch.mkdir(parents=True, exist_ok=True)
        part_path = scratch / (HISTORY_FILENAME + ".part")

        cached = load_cached(part_path) if resume else {}

        def group_done(sid):
            return all(
                cached.get((sid, r["section"], r["section_num"]), {}).get("status")
                in TERMINAL_STATUSES
                for r in groups[sid]
            )

        todo = [sid for sid in groups if not group_done(sid)]
        print(f"[harvest fda] before-fetch: {len(groups)} set_id(s), "
              f"{len(groups) - len(todo)} resumed, {len(todo)} to fetch",
              file=sys.stderr, flush=True)

        produced: dict = {}
        wb_requests = 0
        with open(part_path, "a", encoding="utf-8", newline="\n") as part:
            for i, sid in enumerate(todo, 1):
                rows, n_req = process_group(sid, groups[sid], sleep=sleep,
                                            tries=tries, timeout=timeout)
                wb_requests += n_req
                for r in rows:
                    produced[row_key(r)] = r
                    cached[row_key(r)] = r
                    part.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
                part.flush()
                if i % 10 == 0 or i == len(todo):
                    print(f"[harvest fda]   before-fetched {i}/{len(todo)} set_id(s) "
                          f"({wb_requests} archive request(s))", file=sys.stderr, flush=True)

        # Assemble one history row per RMC row: freshly produced > resumed cache >
        # a 'pending' placeholder (never dropped). Record every non-terminal row.
        final: dict = {}
        transient = 0
        for sid, rows in groups.items():
            for r in rows:
                k = (sid, r["section"], r["section_num"])
                if k in produced:
                    final[k] = produced[k]
                elif k in cached:
                    final[k] = cached[k]
                else:
                    final[k] = make_cache_row(
                        r, before_text=None, before_full=None, snap_ts=None,
                        snap_url=None, snapshot_count=0, extract_method="none",
                        status="pending")
                if final[k].get("status") not in TERMINAL_STATUSES:
                    transient += 1
                    http_errors.append({
                        "stage": "wayback_before", "set_id": sid,
                        "section": r.get("section"), "section_num": r.get("section_num"),
                        "status": final[k].get("status")})

        # Cleanup: only when every row is terminal (a clean, complete harvest);
        # otherwise keep the scratch .part so a later --resume retries the
        # transient rows. Best-effort — a cleanup failure must not fail the harvest.
        if transient == 0:
            try:
                if part_path.exists():
                    part_path.unlink()
                if scratch.is_dir() and not any(scratch.iterdir()):
                    scratch.rmdir()
            except OSError as exc:
                print(f"[harvest fda] scratch cleanup skipped: {exc}",
                      file=sys.stderr, flush=True)
        else:
            print(f"[harvest fda] {transient} row(s) left transient (recorded in "
                  f"fetch_stats.http_errors); scratch kept at {scratch} for --resume",
                  file=sys.stderr, flush=True)

        return list(final.values()), wb_requests, transient

    # -- snapshot emission (PURE given already-fetched rows) ---------------
    def _emit(self, writer, cfg, discovery, openfda_query, requested_sample, rmc_rows,
              history_rows, of_requests, wb_requests, transient, top_path, top,
              params, http_errors) -> None:
        """Route the fetched rows through the SnapshotWriter: the history + RMC
        caches via add_jsonl, the history sidecar via add_json (sha1-pinned; NO
        wall-clock retrieved_at), the optional top-300 reference via
        add_scope_file, and params/stats onto the manifest. No network — a test
        drives this with fixture rows to assert snapshot layout + determinism.

        ``requested_sample`` is the sample AS REQUESTED (or None for a full scan);
        it is what ``params['sample']`` records, so a requested drug with no
        in-window change stays visible. The candidate-producing set_ids (which may
        be a strict subset of the request) are reported separately in
        ``fetch_stats.candidate_set_ids``."""
        out_dir = writer.out_dir
        status_breakdown = dict(sorted(Counter(r.get("status") for r in history_rows).items()))
        candidate_set_ids = sorted({r["set_id"] for r in rmc_rows})

        # history cache (before/after) + RMC provenance (effective_time + known)
        writer.add_jsonl(HISTORY_FILENAME, history_rows)
        cache_sha1 = sha1_file(out_dir / HISTORY_FILENAME)
        writer.add_jsonl(RMC_FILENAME, rmc_rows)
        rmc_sha1 = sha1_file(out_dir / RMC_FILENAME)

        # the history cache's .meta.json sidecar — retrieved_at OMITTED so a
        # re-harvest of identical data yields a reproducible derive fingerprint;
        # cache_sha1 pins the file the writer just wrote (adapter re-checks it).
        writer.add_json(HISTORY_META_FILENAME, self._history_sidecar(
            rmc_rows, history_rows, cache_sha1, rmc_sha1, status_breakdown,
            of_requests + wb_requests, params))

        # the top-300 reference (optional; defines 'known') — copied verbatim as a
        # NON-gating provenance scope so the snapshot is self-describing about what
        # list produced 'known'. FDA has no fixed universe, so this never gates.
        top_id = None
        top_sha1 = None
        if top_path and Path(top_path).is_file():
            writer.add_scope_file(TOP300_FILENAME, top_path, "drug_top300_reference", len(top))
            top_id = TOP300_FILENAME
            top_sha1 = writer._files[TOP300_FILENAME]["sha1"]

        writer.set_params({
            "openfda_endpoint": OPENFDA_ENDPOINT,
            "openfda_user_agent": OPENFDA_USER_AGENT,
            "rmc_tool_version": RMC_TOOL_VERSION,
            "cdx_endpoint": CDX_ENDPOINT,
            "wayback_base": WAYBACK_BASE,
            "wayback_user_agent": WAYBACK_USER_AGENT,
            "discovery": discovery,
            "openfda_query": openfda_query,
            "min_snapshot_day": MIN_SNAPSHOT_DAY,
            "snippet_cap": SNIPPET_CAP,
            "full_section_cap": FULL_SECTION_CAP,
            # CONTEMPORANEOUS: the candidate set + 'after' are not reconstructable
            # at a past window (see coverage.back_datable). Echoed here for audit.
            "back_datable": False,
            "top300_reference": top_id,
            "top300_sha1": top_sha1,
            # the sample AS REQUESTED (verbatim), NOT the candidate-producing
            # subset — a requested drug with no in-window change stays visible.
            "sample": requested_sample,
            "files": [HISTORY_FILENAME, RMC_FILENAME],
        })
        writer.set_stats({
            "discovery": discovery,
            "rmc_rows": len(rmc_rows),
            "set_ids": len(candidate_set_ids),
            # the set_ids that actually produced an in-window candidate (a strict
            # subset of requested_sample when some requested drugs were empty).
            "candidate_set_ids": candidate_set_ids,
            "history_rows": len(history_rows),
            "status_breakdown": status_breakdown,
            "openfda_requests": of_requests,
            "wayback_requests": wb_requests,
            "transient_rows": transient,
            "http_errors": http_errors,
            "resumed": bool(cfg.get("resume")),
        })
        ok = status_breakdown.get("ok", 0)
        print(f"[harvest fda] DONE. {len(rmc_rows)} RMC change(s) across "
              f"{len(candidate_set_ids)} drug(s); {len(history_rows)} history row(s) "
              f"({ok} ok); {len(http_errors)} http miss(es).", file=sys.stderr, flush=True)

    @staticmethod
    def _history_sidecar(rmc_rows, history_rows, cache_sha1, rmc_sha1,
                         status_breakdown, requests_this_run, params) -> dict:
        """The fda_dailymed_history.meta.json sidecar — the fetch_dailymed_history
        meta shape MINUS the wall-clock retrieved_at (the finance-port determinism
        fix). The fda adapter surfaces tool_version/cache_sha1/rmc_sha1/rmc_file/
        status_breakdown/params/counts into the manifest and cross-checks
        cache_sha1 against the history cache the writer wrote."""
        return {
            "tool_version": TOOL_VERSION,
            "cdx_endpoint": CDX_ENDPOINT,
            "wayback_base": WAYBACK_BASE,
            "user_agent": WAYBACK_USER_AGENT,
            "params": {
                "min_snapshot_day": MIN_SNAPSHOT_DAY,
                "snapshot_selection": "latest statuscode-200 text/html capture "
                                      "before the change month's first day",
                "snippet_cap": SNIPPET_CAP,
                "full_section_cap": FULL_SECTION_CAP,
                "sleep": params.get("wayback_sleep_s"),
                "tries": params.get("wayback_tries"),
                "timeout": params.get("wayback_timeout_s"),
            },
            # self-contained in-snapshot RMC id (never a machine-absolute path)
            "rmc_file": RMC_FILENAME,
            "rmc_sha1": rmc_sha1,
            "rmc_load_errors": [],
            "counts": {
                "rmc_rows": len(rmc_rows),
                "set_ids": len({r["set_id"] for r in rmc_rows}),
                "cached_rows": len(history_rows),
                "requests_this_run": requests_this_run,
            },
            "status_breakdown": status_breakdown,
            "cache_file": HISTORY_FILENAME,
            "cache_sha1": cache_sha1,
        }

    # -- top-300 resolution -------------------------------------------------
    @staticmethod
    def _resolve_top300(cfg: dict):
        """The top-300 reference path: a cfg override (--opt top300=PATH) wins,
        else the repo default. Returns a Path or None (None -> 'known' degrades to
        False, warned)."""
        override = cfg.get("top300")
        if isinstance(override, str) and override.strip():
            return Path(override.strip())
        if DEFAULT_TOP300_PATH.is_file():
            return DEFAULT_TOP300_PATH
        return None


HARVESTER = FdaHarvester()
