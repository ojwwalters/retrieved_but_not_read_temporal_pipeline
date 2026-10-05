"""Finance (Polygon.io / "Massive") harvester — the SECOND harvest source.

Ports the standalone ``stage1.tools.fetch_polygon`` into the harvest framework,
proving the SEC-established pattern generalises cleanly. It emits the five cache
files (plus their ``.meta.json`` sidecars) the OFFLINE finance adapter
(``stage1/adapters/finance.py``) reads by name, so a harvested snapshot dir IS a
valid ``--data-dir`` with zero adapter change:

    price_grouped.jsonl   financials.jsonl   ticker_events.jsonl
    ipo.jsonl             cross_check.jsonl        (+ .meta.json each)
    sp500_universe.csv    snapshot_manifest.json   .stage1_snapshot

SHARED-FETCH REFACTOR (no duplicated API code). ALL Polygon HTTP / parsing /
resume logic lives in ``stage1.tools.fetch_polygon`` and is IMPORTED here — the
per-family fetch functions (``fetch_price``, ``fetch_financials``,
``fetch_events``, ``fetch_ipos``, ``fetch_cross_check``), the ``Pacer``, the
universe loader, the sidecar assembler (``build_sidecar_meta``). This harvester
only orchestrates them and routes their outputs through the ``SnapshotWriter``
(``add_jsonl`` for each cache, ``add_json`` for each sidecar, ``add_scope_file``
for the universe) so every file is sha1-pinned in the manifest instead of being
written out-of-band. The standalone ``python3 -m stage1.tools.fetch_polygon`` CLI
keeps working unchanged; both call the SAME functions.

THE API KEY LIVES ONLY HERE. ``harvest()`` is the only networked method; it
reads ``POLYGON_API_KEY`` from the environment or config.toml (failing clearly if absent) and
sends it as an ``Authorization: Bearer`` header inside fetch_polygon — it never
enters a URL, a log line, a snapshot file, or the manifest. Derive stays offline.

COVERAGE SEMANTICS — cutoff_exact AND asof_exact (honest pinning). A finance
snapshot fetches grouped-daily CLOSES for exactly TWO resolved trading days: the
anchor (last trading day on/before ``cutoff``) and the ``asof`` trading day. It
is therefore PINNED to the exact ``cutoff`` and ``asof`` flags: a derive whose
cutoff or asof differs would resolve to a DIFFERENT anchor/asof trading day whose
close is simply not in the snapshot. So ``coverage()`` records ``cutoff`` and
``asof`` with BOTH ``cutoff_exact=True`` and ``asof_exact=True``; the shared
``check_coverage`` refuses any derive whose cutoff != coverage.cutoff (rule 4b,
added alongside SEC's asof rule 4) or asof != coverage.asof. This is stricter
than SEC (which only pins asof and allows a NARROWER cutoff) because SEC's
evidence is an archive scan over [cutoff, asof] while finance's is two fixed
endpoint closes. A finance derive can NEVER silently reuse a snapshot fetched for
a different window.

NO LLM anywhere; the ground truth is deterministic arithmetic in the adapter.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

from stage1.config import setting
from stage1.harvest import Harvester
from stage1.harvest.snapshot import sha1_file
from stage1.tools.fetch_polygon import (
    API_HOST,
    FINANCIALS_LIMIT,
    IPO_PAGE_LIMIT,
    STEP_BACK_CAP,
    STOOQ_URL,
    USER_AGENT,
    Pacer,
    build_sidecar_meta,
    cross_check_family_meta,
    events_family_meta,
    fetch_cross_check,
    fetch_events,
    fetch_financials,
    fetch_ipos,
    fetch_price,
    financials_family_meta,
    ipo_family_meta,
    load_universe,
    price_family_meta,
)

TOOL_VERSION = "harvest_finance:v1"

# The EXACT fixed filenames the finance adapter reads by name.
PRICE_FILENAME = "price_grouped.jsonl"
FINANCIALS_FILENAME = "financials.jsonl"
EVENTS_FILENAME = "ticker_events.jsonl"
IPO_FILENAME = "ipo.jsonl"
CROSS_CHECK_FILENAME = "cross_check.jsonl"
PRICE_VERIFY_FILENAME = "price_verify.jsonl"
UNIVERSE_FILENAME = "sp500_universe.csv"

# The committed price-verification reference: the finance source's frozen split
# adjustment for the authoritative in-window splits (grouped vs split-adjusted
# current-basis close per endpoint, verdict split_adjusted; plus verified
# non-split real_kept movers). It lives beside the package caches the adapter reads and is copied
# VERBATIM into every snapshot so the snapshot is self-contained and
# FinancePriceVerifyGate's bad-tick adjudication travels with the harvested
# window — it is a rule-5b REQUIRED input (see FinanceAdapter.snapshot_inputs).
# Copying (rather than re-deriving) keeps harvest offline and byte-deterministic,
# and means a re-harvest cannot silently drop the guard. When the reference is
# absent the harvester writes NOTHING: the resulting snapshot then fails the
# coverage check (rule 5b) at derive time rather than deriving with the
# verification silently missing ('missing input -> refuse, never silent include').
PRICE_VERIFY_REFERENCE = (
    Path(__file__).resolve().parent.parent / "cache" / PRICE_VERIFY_FILENAME)

# Slow Polygon tier caps at ~5 req/min; >=12s spacing keeps us under it. A cfg
# override (``rate_min_interval``) is honoured, mostly for tests.
DEFAULT_RATE_MIN_INTERVAL = 12.0


def _iso(value) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def _sidecar_name(cache_name: str) -> str:
    """price_grouped.jsonl -> price_grouped.meta.json (the name the adapter's
    _load_sidecar computes)."""
    return cache_name[: -len(".jsonl")] + ".meta.json"


class FinanceHarvester(Harvester):
    source = "finance"
    tool_version = TOOL_VERSION

    # -- coverage (PURE, no network) ---------------------------------------
    def coverage(self, cfg: dict) -> dict:
        cutoff = cfg["cutoff"]
        asof = cfg["asof"]
        cov = {
            "cutoff": _iso(cutoff),
            "asof": _iso(asof),
            # BOTH edges are pinned: the grouped-daily anchor close is fetched
            # for the resolved trading day of THIS cutoff, and the asof close for
            # the resolved trading day of THIS asof. A wider/narrower cutoff or
            # asof needs a different close that is not in the snapshot, so a
            # sound re-derive must use the snapshot's EXACT cutoff and asof.
            "cutoff_exact": True,
            "asof_exact": True,
            "precision": "day",
            # [cutoff, asof] bounds the endpoint TRADING DAYS the closes are
            # sampled at (anchor <= cutoff, asof) plus the [cutoff, asof] date
            # window that financials/ticker-events/IPO rows are filtered to.
            "window_basis": "trading_day_endpoints",
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
        universe, _errors = load_universe(path)
        return {
            "kind": "sp500_universe",
            "file": UNIVERSE_FILENAME,
            "sha1": sha1_file(path),
            "size": len(universe),
        }

    # -- harvest (the ONLY networked method) -------------------------------
    def harvest(self, cfg: dict, writer, ctx: dict) -> None:
        cutoff_iso = _iso(cfg["cutoff"])
        asof_iso = _iso(cfg["asof"])

        universe_path = cfg.get("universe")
        if not universe_path or not Path(universe_path).is_file():
            raise LookupError(
                "the finance harvester requires --universe pointing at the S&P 500 csv"
            )
        universe, uni_errors = load_universe(Path(universe_path))
        for err in uni_errors:
            print(f"[harvest finance] universe line {err['line']}: {err['error']}",
                  file=sys.stderr, flush=True)

        # The Polygon key touches the network ONLY here; fetch_polygon sends it
        # as an Authorization: Bearer header (never in a URL). It is read into a
        # local, never stored on the instance, never written to any snapshot
        # file/sidecar/manifest, never printed.
        api_key = setting("POLYGON_API_KEY")
        if not api_key:
            raise LookupError(
                "POLYGON_API_KEY is not set (config.toml or the environment): the finance harvester cannot "
                "fetch (the key touches the network only in harvest and never enters a snapshot)"
            )

        sample = cfg.get("sample")
        if sample:
            tickers = sorted({t.strip().upper() for t in sample if t and t.strip()})
            unknown = [t for t in tickers if t not in universe]
            if unknown:
                print(f"[harvest finance] {len(unknown)} sample ticker(s) not in universe "
                      f"(name/cik will be null): {unknown}", file=sys.stderr, flush=True)
        else:
            tickers = sorted(universe)
        if not tickers:
            raise LookupError("no tickers requested (empty universe and no --sample)")

        rate = cfg.get("rate_min_interval", DEFAULT_RATE_MIN_INTERVAL)
        pacer = Pacer(rate)
        out_dir = writer.out_dir
        skip_stooq = bool(cfg.get("skip_stooq"))

        # The per-ticker fetchers (fetch_financials / fetch_events, via
        # harvest_per_ticker) write their cache + a resumable .part CHECKPOINT to
        # the path they are handed. We point them at a SCRATCH dir OUTSIDE the
        # snapshot (a hidden sibling of out_dir) so the SnapshotWriter is the sole
        # writer of snapshot bytes: no intermediate/.part file ever sits inside the
        # snapshot dir, and an interruption before finalize() leaves the snapshot
        # dir with NO cache files (rather than partial ones a later --data-dir would
        # derive with no coverage enforcement). The scratch dir persists across an
        # interrupted run so --resume still skips already-fetched tickers, and is
        # removed once _emit has routed every row through the writer.
        scratch = out_dir.parent / (".harvest_scratch_" + out_dir.name)
        scratch.mkdir(parents=True, exist_ok=True)

        print(f"[harvest finance] {len(tickers)} ticker(s); cutoff={cutoff_iso} "
              f"asof={asof_iso}; out-dir={out_dir}", file=sys.stderr, flush=True)

        # ---- (1) PRICE (grouped-daily; resolves anchor + asof trading days) --
        # fetch_price only READS its path (for resume) and RETURNS rows — it never
        # writes — so pointing it at the snapshot dir leaves no partial file.
        price_rows, resolved, price_calls, missing_by_role = fetch_price(
            pacer, cutoff_iso, asof_iso, tickers, api_key, out_dir / PRICE_FILENAME)

        # ---- (2) FINANCIALS (per ticker; revenue + avg shares) ---------------
        fin_rows, fin_calls, fin_done, fin_todo = fetch_financials(
            pacer, asof_iso, tickers, universe, api_key, scratch / FINANCIALS_FILENAME)

        # ---- (3) TICKER EVENTS (per ticker; symbol/name changes) -------------
        ev_rows, ev_calls, ev_done, ev_todo = fetch_events(
            pacer, tickers, universe, api_key, scratch / EVENTS_FILENAME)

        # ---- (4) IPO (one market-wide paginated call for [cutoff, asof]) -----
        ipo_rows, ipo_calls, ipo_pages = fetch_ipos(pacer, cutoff_iso, asof_iso, api_key)

        # ---- (5) STOOQ cross-check (best-effort, review-safe, keyless) -------
        asof_day = (resolved.get("asof") or {}).get("resolved_trading_day")
        if skip_stooq:
            cc_rows = [{
                "ticker": t, "asof_date": asof_day, "cross_check_status": "unavailable",
                "close": None, "reason": "skipped_by_flag", "source": "stooq:/q/d/l",
            } for t in tickers]
            cc_probe = {"probe_status": "skipped", "probe_reason": "skip_stooq"}
        else:
            cc_rows, cc_probe = fetch_cross_check(tickers, asof_day)

        results = {
            "price": {"rows": price_rows, "resolved": resolved,
                      "missing_by_role": missing_by_role, "calls": price_calls},
            "financials": {"rows": fin_rows, "cached": fin_done + fin_todo,
                           "fetched_this_run": fin_todo, "calls": fin_calls},
            "ticker_events": {"rows": ev_rows, "cached": ev_done + ev_todo,
                              "fetched_this_run": ev_todo, "calls": ev_calls},
            "ipo": {"rows": ipo_rows, "pages": ipo_pages, "calls": ipo_calls},
            "cross_check": {"rows": cc_rows, "probe": cc_probe, "asof_day": asof_day},
        }
        self._emit(writer, cfg, tickers, universe, uni_errors, universe_path, rate, results)

        # Every row is now routed through the writer, so the scratch checkpoints
        # are redundant — remove them so a completed snapshot leaves no sibling
        # clutter. (An interrupted run never reaches here, so its scratch dir
        # survives for --resume.) Best-effort: a cleanup failure must not fail an
        # otherwise-complete harvest.
        try:
            for name in (FINANCIALS_FILENAME, EVENTS_FILENAME):
                for leftover in (scratch / name, scratch / (name + ".part")):
                    if leftover.exists():
                        leftover.unlink()
            if scratch.is_dir() and not any(scratch.iterdir()):
                scratch.rmdir()
        except OSError as exc:
            print(f"[harvest finance] scratch cleanup skipped: {exc}",
                  file=sys.stderr, flush=True)

    # -- snapshot emission (PURE given already-fetched results) -------------
    def _emit(self, writer, cfg, tickers, universe, uni_errors, universe_path,
              rate, results) -> None:
        """Route the fetched rows through the SnapshotWriter: each cache via
        add_jsonl, each sidecar via add_json (sha1-pinned), the universe via
        add_scope_file, and the params/stats onto the manifest. No network — a
        test can drive this with fixture ``results`` to assert snapshot layout.
        """
        cutoff_iso = _iso(cfg["cutoff"])
        asof_iso = _iso(cfg["asof"])
        sample = cfg.get("sample")
        uni_path = Path(universe_path)
        uni_sha1 = sha1_file(uni_path) if uni_path.is_file() else None

        # base per-family sidecar meta (the shape the legacy fetch tool writes;
        # universe_file is the SELF-CONTAINED in-snapshot name, never an absolute
        # path, so the snapshot is location-independent). NO api key.
        base_meta = {
            "endpoint": API_HOST,
            "cutoff": cutoff_iso,
            "asof": asof_iso,
            "sample": sorted(tickers) if sample else None,
            "universe_file": UNIVERSE_FILENAME,
            "universe_sha1": uni_sha1,
            "universe_load_errors": uni_errors,
            "rate_min_interval_s": rate,
        }

        # the universe file, copied verbatim -> also the primary scope digest
        writer.add_scope_file(UNIVERSE_FILENAME, universe_path, "sp500_universe", len(universe))

        # Each family's per-family sidecar meta is assembled by the SHARED builder
        # in fetch_polygon (the same one main() uses) so the standalone tool's
        # caches and the harvest snapshot's sidecars cannot silently drift; the
        # harvester only merges its own base_meta and reads back the derived
        # summaries (no_data / with_events / cross-check ok) for set_stats.
        price = results["price"]
        resolved = price["resolved"]
        self._emit_family(writer, PRICE_FILENAME, price["rows"], {
            **base_meta,
            **price_family_meta(price["rows"], tickers, resolved,
                                price["missing_by_role"], price["calls"]),
        })

        fin = results["financials"]
        fin_meta = {
            **base_meta,
            **financials_family_meta(fin["rows"], asof_iso, fin["cached"],
                                     fin["fetched_this_run"], fin["calls"]),
        }
        self._emit_family(writer, FINANCIALS_FILENAME, fin["rows"], fin_meta)
        fin_no_data = fin_meta["no_data_tickers"]

        ev = results["ticker_events"]
        ev_meta = {
            **base_meta,
            **events_family_meta(ev["rows"], ev["cached"], ev["fetched_this_run"], ev["calls"]),
        }
        self._emit_family(writer, EVENTS_FILENAME, ev["rows"], ev_meta)
        ev_with_events = ev_meta["counts"]["tickers_with_events"]

        ipo = results["ipo"]
        self._emit_family(writer, IPO_FILENAME, ipo["rows"], {
            **base_meta,
            **ipo_family_meta(ipo["rows"], ipo["pages"], ipo["calls"], cutoff_iso, asof_iso),
        })

        cc = results["cross_check"]
        cc_meta = {
            **base_meta,
            **cross_check_family_meta(cc["rows"], cc["probe"], cc["asof_day"]),
        }
        self._emit_family(writer, CROSS_CHECK_FILENAME, cc["rows"], cc_meta)
        cc_ok = cc_meta["counts"]["ok"]

        # the frozen price-verification reference, copied verbatim (rule-5b input)
        self._emit_price_verify(writer)

        total_calls = sum(results[f]["calls"] for f in
                          ("price", "financials", "ticker_events", "ipo"))

        writer.set_params({
            "endpoint": API_HOST,
            "stooq_endpoint": STOOQ_URL,
            "user_agent": USER_AGENT,
            "rate_min_interval_s": rate,
            "step_back_cap_days": STEP_BACK_CAP,
            "financials_limit": FINANCIALS_LIMIT,
            "ipo_page_limit": IPO_PAGE_LIMIT,
            "families": ["price_grouped", "financials", "ticker_events", "ipo", "cross_check"],
            "sample": sorted(tickers) if sample else None,
            # the resolved anchor + asof trading days the closes were sampled at
            # (provenance; the coverage enforcement is on the cutoff/asof FLAGS,
            # which deterministically resolve to these same trading days).
            "resolved_trading_days": {
                role: (info or {}).get("resolved_trading_day")
                for role, info in resolved.items()},
        })
        writer.set_stats({
            "total_polygon_calls": total_calls,
            "price": {"calls": price["calls"], "resolved": resolved,
                      "missing_by_role": price["missing_by_role"]},
            "financials": {"rows": len(fin["rows"]), "tickers_cached": fin["cached"],
                           "tickers_fetched_this_run": fin["fetched_this_run"],
                           "no_data_tickers": fin_no_data, "calls": fin["calls"]},
            "ticker_events": {"rows": len(ev["rows"]), "tickers_cached": ev["cached"],
                              "tickers_fetched_this_run": ev["fetched_this_run"],
                              "tickers_with_events": ev_with_events, "calls": ev["calls"]},
            "ipo": {"rows": len(ipo["rows"]), "pages": ipo["pages"], "calls": ipo["calls"]},
            "cross_check": {"probe": cc["probe"], "ok": cc_ok,
                            "unavailable": len(cc["rows"]) - cc_ok, "asof_day": cc["asof_day"]},
            "universe_load_errors": uni_errors,
        })
        print(f"[harvest finance] DONE. {total_calls} Polygon call(s) this run; "
              f"{len(tickers)} ticker(s).", file=sys.stderr, flush=True)

    def _emit_family(self, writer, cache_name, rows, family_meta) -> None:
        """Write one cache (deterministic sorted jsonl, sha1-pinned) plus its
        .meta.json sidecar (also sha1-pinned) through the writer. The sidecar's
        cache_sha1 is computed from the file the writer just wrote, so it matches
        the manifest's recorded sha1.

        ``retrieved_at=False`` OMITS the wall-clock from the sidecar: a harvest
        snapshot's only non-deterministic byte is ``snapshot_manifest.harvested_at``
        (excluded from the derive input fingerprint). Because the sidecar is
        sha1-pinned in the manifest AND fingerprinted by the deriving finance
        adapter (its sha1 in ``input_files``, its keys in ``extra_input_meta``), a
        wall-clock here would change the sidecar bytes on every re-harvest of
        byte-identical Polygon data and so change the derive input fingerprint —
        defeating the reproducible-fingerprint guarantee. Omitting it makes the
        sidecar a pure function of the fetched data while keeping it sha1-pinned
        (tamper detection is unaffected)."""
        writer.add_jsonl(cache_name, rows)
        cache_sha1 = sha1_file(writer.out_dir / cache_name)
        sidecar = build_sidecar_meta(family_meta, cache_name, cache_sha1,
                                     tool_version=self.tool_version, retrieved_at=False)
        writer.add_json(_sidecar_name(cache_name), sidecar)

    def _emit_price_verify(self, writer) -> None:
        """Copy the committed price-verification reference sidecar VERBATIM into
        the snapshot (see PRICE_VERIFY_REFERENCE). It carries no wall-clock (the
        curated verdict rows are pure data), so it is a deterministic, sha1-pinned
        rule-5b input. When the reference is absent, emit nothing so the snapshot
        loudly fails coverage at derive time rather than deriving with the bad-tick
        guard silently missing."""
        ref = PRICE_VERIFY_REFERENCE
        if not ref.is_file():
            return
        rows = sum(1 for line in ref.read_text(encoding="utf-8").splitlines() if line.strip())
        writer.add_file(PRICE_VERIFY_FILENAME, ref, rows=rows)


HARVESTER = FinanceHarvester()
