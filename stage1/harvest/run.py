"""Stage-1 HARVEST runner: the ONLY networked step.

Mirrors stage1/run.py's arg style. Builds cfg, calls the harvester's pure
coverage() for the manifest coverage block, runs the networked harvest(...),
then SnapshotWriter.finalize() writes snapshot_manifest.json. The output is a
FROZEN snapshot dir that a later `python3 -m stage1.run --source <s> --data-dir
<snapshot>` derives from — deterministically and offline.

    python3 -m stage1.harvest --source sec \\
        --cutoff 2026-01-01 --asof 2026-06-30 \\
        --universe sp500_universe.csv \\
        --out-dir stage1/snapshots/sec_2026-01-01_2026-06-30 \\
        [--max-rps 8] [--roles CEO,CFO] [--resume] \\
        [--sample 1013871,1324404]      # small live test over specific CIKs

Dates are required flags; there is no hardcoded date default. The contact
address for request User-Agents comes from config.toml (STAGE1_CONTACT_EMAIL);
--contact overrides it for one run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

from stage1.config import require_contact_email
from stage1.harvest import get_harvester
from stage1.harvest.snapshot import SnapshotWriter


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python3 -m stage1.harvest",
        description="Harvest a frozen Stage-1 snapshot for one source (the only networked step).",
    )
    parser.add_argument("--source", required=True, help="harvester name, e.g. 'sec'")
    parser.add_argument("--cutoff", required=True, help="earliest EVENT date to fetch, YYYY-MM-DD")
    parser.add_argument("--asof", required=True, help="latest EVENT date + as-of evidence pin, YYYY-MM-DD")
    parser.add_argument("--universe", default=None, help="scope csv (e.g. sec/sp500_universe.csv)")
    parser.add_argument("--out-dir", required=True, help="snapshot output directory")
    parser.add_argument("--contact", default=None,
                        help="contact email for request User-Agents "
                             "(default: STAGE1_CONTACT_EMAIL from config.toml)")
    parser.add_argument("--max-rps", type=float, default=8.0,
                        help="max requests/sec (capped at 8; default 8)")
    parser.add_argument("--roles", default="CEO,CFO",
                        help="cert roles to verify (default CEO,CFO — only these sign SOX certs)")
    parser.add_argument("--resume", action="store_true",
                        help="continue an interrupted harvest from its .part checkpoints")
    parser.add_argument("--sample", default=None,
                        help="comma-separated subset for a small live test (SEC: CIKs via the "
                             "per-CIK submissions path; finance: TICKERS) instead of the full universe")
    parser.add_argument("--opt", action="append", default=None, metavar="KEY=VALUE",
                        help="source-specific harvester option, repeatable, folded into cfg with "
                             "true/false/none/int/float coerced (e.g. finance: "
                             "--opt rate_min_interval=12 --opt skip_stooq=true). A generic channel "
                             "for a harvester's own documented knobs so the framework is not "
                             "SEC-shaped; cannot override a reserved flag (source/cutoff/asof/…).")
    return parser.parse_args(argv)


# The cfg keys the reserved CLI flags own; --opt may not shadow any of them
# (use the dedicated flag). Kept next to build_cfg so the two stay in lockstep.
_RESERVED_CFG_KEYS = frozenset({
    "source", "cutoff", "asof", "universe", "out_dir", "contact", "max_rps",
    "roles", "resume", "sample",
})


def _coerce_opt_value(raw: str):
    """Type-coerce a raw ``--opt`` string so a harvester's typed knob works from
    the CLI: 'true'/'false' -> bool (so ``skip_stooq=false`` is FALSY, not a
    non-empty truthy string), 'none'/'null' -> None, an int/float literal ->
    that number, else the string verbatim. Pure and total."""
    low = raw.strip().lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("none", "null"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def parse_opts(opt_list) -> dict:
    """Fold repeated ``--opt KEY=VALUE`` into a dict of coerced values. A missing
    '=', an empty key, or a reserved key exits 2 loudly rather than silently
    dropping or shadowing a knob."""
    opts: dict = {}
    for item in opt_list or []:
        if "=" not in item:
            raise SystemExit(f"error: --opt must be KEY=VALUE, got {item!r}")
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            raise SystemExit(f"error: --opt has an empty key: {item!r}")
        if key in _RESERVED_CFG_KEYS:
            raise SystemExit(
                f"error: --opt {key!r} would shadow a reserved cfg key; use the --{key} flag")
        opts[key] = _coerce_opt_value(value)
    return opts


def _parse_iso_date(value: str, arg_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"error: {arg_name} must be a valid YYYY-MM-DD date, got {value!r}")


def build_cfg(args: argparse.Namespace) -> dict:
    sample = None
    if args.sample:
        sample = [c.strip() for c in args.sample.split(",") if c.strip()]
    cfg = {
        "source": args.source,
        "cutoff": _parse_iso_date(args.cutoff, "--cutoff"),
        "asof": _parse_iso_date(args.asof, "--asof"),
        "universe": Path(args.universe) if args.universe else None,
        "out_dir": Path(args.out_dir),
        "contact": args.contact,
        "max_rps": args.max_rps,
        "roles": [r.strip() for r in args.roles.split(",") if r.strip()],
        "resume": args.resume,
        "sample": sample,
    }
    # Source-specific knobs (--opt KEY=VALUE) ride in last, on their own keys —
    # a generic channel so a harvester's documented options (e.g. finance's
    # rate_min_interval / skip_stooq) are settable from the framework CLI, not
    # only when driven from Python. Reserved keys are rejected in parse_opts.
    cfg.update(parse_opts(getattr(args, "opt", None)))
    return cfg


def run(cfg: dict) -> int:
    if cfg["cutoff"] > cfg["asof"]:
        print(f"error: --cutoff {cfg['cutoff']} is after --asof {cfg['asof']}", file=sys.stderr)
        return 2
    harvester = get_harvester(cfg["source"])
    writer = SnapshotWriter(cfg["out_dir"], cfg["source"], harvester.tool_version)
    coverage = harvester.coverage(cfg)
    writer.set_coverage(coverage)
    ctx: dict = {}
    harvester.harvest(cfg, writer, ctx)
    manifest = writer.finalize()

    print(f"\nsnapshot -> {cfg['out_dir']}")
    print(f"  harvested_at: {manifest['harvested_at']}")
    print(f"  coverage:     {json.dumps(manifest['coverage'], sort_keys=True)}")
    print(f"  scope:        {json.dumps(manifest.get('scope', {}), sort_keys=True)}")
    print("  files:")
    for name in sorted(manifest.get("files", {})):
        meta = manifest["files"][name]
        print(f"    {name}: rows={meta['rows']} bytes={meta['bytes']} sha1={meta['sha1'][:12]}…")
    print(f"  fetch_stats:  {json.dumps(manifest.get('fetch_stats', {}), sort_keys=True)}")
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    # One contact for every request this run makes. Harvester modules build
    # their User-Agents at import, which get_harvester defers until run(), so
    # the override is in place before any of them load.
    if args.contact:
        os.environ["STAGE1_CONTACT_EMAIL"] = args.contact
    args.contact = require_contact_email()
    cfg = build_cfg(args)
    try:
        return run(cfg)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except NotImplementedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
