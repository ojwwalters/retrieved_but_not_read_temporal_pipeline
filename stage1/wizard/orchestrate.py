"""Plan and execute a wizard run.

Planning turns (window, sources, credentials) into per-source jobs with
precomputed argument vectors and directory states; execution drives the two
pipeline entry points as subprocesses with live progress and ends with a
summary of what each release contains.

Safety contract — the reason this module exists:

* NEVER deletes or overwrites. A release directory already holding
  `facts.jsonl`/`manifest.json` is refused at plan time with the exact path;
  the committed `dev-*` releases can never be named by the
  `<source>_<cutoff>_<asof>` scheme and are guarded again at write time.
* An existing complete snapshot for the same window is REUSED (same window,
  same wanted bytes); an interrupted one is RESUMED through the harvester's
  own `--resume` checkpoints. Neither is ever clobbered.
* One source failing does not abort the rest; failures are reported at the
  end with the tail of their log and the command already primed to resume.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Mapping

from stage1.wizard import sources as src
from stage1.wizard.progress import PhaseTracker, format_eta
from stage1.wizard.ui import (
    BOLD, CYAN, DIM, GREEN, RED, YELLOW, LiveBoard, bar, cursor_hidden, paint,
    use_ansi,
)

ROOT = Path(__file__).resolve().parents[2]


class PlanError(Exception):
    """A problem the user must fix; the message is the whole story."""


def parse_iso(raw: str, what: str) -> str:
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        raise PlanError(f"{what} {raw!r} is not a valid YYYY-MM-DD date") from None


def snapshot_state(snap_dir: Path) -> str:
    """'complete' (manifest present) / 'partial' (files but no manifest) / 'absent'."""
    if (snap_dir / "snapshot_manifest.json").is_file():
        return "complete"
    if snap_dir.is_dir() and any(snap_dir.iterdir()):
        return "partial"
    return "absent"


def release_state(rel_dir: Path) -> str:
    """'occupied' (holds facts) / 'remnant' (stray files from a failed run) / 'absent'."""
    if (rel_dir / "facts.jsonl").is_file() or (rel_dir / "manifest.json").is_file():
        return "occupied"
    if rel_dir.is_dir() and any(rel_dir.iterdir()):
        return "remnant"
    return "absent"


def assert_safe_out(rel: Path) -> None:
    """Belt-and-braces: wizard output may never target a dev-* release."""
    if rel.name == "dev" or rel.name.startswith("dev-"):
        raise PlanError(f"refusing to write into committed release directory {rel}")


@dataclass
class SourceJob:
    spec: src.SourceSpec
    cutoff: str
    asof: str
    snap_state: str                     # absent | partial | complete
    harvest_argv: list[str] | None      # None when the snapshot is reused
    run_argv: list[str]

    @property
    def snap_rel(self) -> str:
        return str(src.snapshot_rel(self.spec.key, self.cutoff, self.asof))

    @property
    def release_rel(self) -> str:
        return str(src.release_rel(self.spec.key, self.cutoff, self.asof))

    @property
    def harvest_log_rel(self) -> str:
        return str(src.harvest_log_rel(self.spec.key, self.cutoff, self.asof))


@dataclass
class Plan:
    cutoff: str
    asof: str
    jobs: list[SourceJob]
    notes: list[str] = field(default_factory=list)
    extra_env: dict[str, str] = field(default_factory=dict)  # e.g. a typed-in key


def build_plan(
    keys: list[str],
    cutoff: str,
    asof: str,
    *,
    root: Path = ROOT,
    contact: str | None = None,
    sample: str | None = None,
    env: Mapping[str, str] = os.environ,
    today: date | None = None,
) -> Plan:
    """Validate everything up front and return an executable Plan.

    Raises PlanError — with the complete list of problems, not just the
    first — for anything the user must fix: bad/reversed/future dates,
    unknown sources, missing credentials, or a release directory that
    already holds facts. After this returns, execution performs no further
    user-facing checks.
    """
    cutoff = parse_iso(cutoff, "--cutoff")
    asof = parse_iso(asof, "--asof")
    if cutoff > asof:
        raise PlanError(f"--cutoff {cutoff} is after --asof {asof}")
    today = today or date.today()
    if asof > today.isoformat():
        raise PlanError(
            f"--asof {asof} is in the future — 'after' truth cannot be observed yet"
        )

    requested = list(dict.fromkeys(keys))
    for key in requested:
        try:
            src.spec(key)
        except KeyError as exc:
            raise PlanError(str(exc)) from None
    # Execute in registry order (fastest first), whatever order was typed.
    ordered = [s for s in src.REGISTRY if s.key in requested]
    if not ordered:
        raise PlanError("no sources selected")
    if sample and len(ordered) != 1:
        raise PlanError("--sample applies to exactly one source per run")

    problems = []
    for sp in ordered:
        for need in src.missing_prereqs(sp, contact=contact, env=env):
            problems.append(f"{sp.key}: needs {need}")
    if problems:
        raise PlanError("missing prerequisites:\n  " + "\n  ".join(problems))

    notes: list[str] = []
    jobs: list[SourceJob] = []
    for sp in ordered:
        snap_dir = root / src.snapshot_rel(sp.key, cutoff, asof)
        rel_dir = root / src.release_rel(sp.key, cutoff, asof)
        assert_safe_out(rel_dir)
        if release_state(rel_dir) == "occupied":
            raise PlanError(
                f"release {rel_dir} already contains facts — refusing to overwrite.\n"
                "Move it aside yourself if you really want to regenerate this window."
            )
        state = snapshot_state(snap_dir)
        if state == "complete":
            harvest = None
            notes.append(f"{sp.key}: reusing the complete snapshot at {snap_dir}")
        else:
            resume = state == "partial"
            if resume:
                notes.append(f"{sp.key}: resuming the interrupted harvest in {snap_dir}")
            harvest = src.harvest_argv(
                sp, cutoff, asof, contact=contact, sample=sample, resume=resume
            )
        jobs.append(SourceJob(sp, cutoff, asof, state, harvest, src.run_argv(sp, cutoff, asof)))
    return Plan(cutoff, asof, jobs, notes)


# -------------------------------------------------------------- execution


@dataclass
class StepResult:
    rc: int
    seconds: float
    tail: list[str]


@dataclass
class SourceResult:
    key: str
    ok: bool = False
    stage_failed: str | None = None     # harvest | build
    harvest: StepResult | None = None
    build: StepResult | None = None
    counts: dict | None = None
    release: str = ""


def run_subprocess(argv, *, root: Path, env: dict, on_line, tee: Path | None) -> StepResult:
    """Run one pipeline command, streaming merged output line-by-line.

    Harvest progress arrives on stderr with flush=True, so stderr is merged
    into stdout and the child runs unbuffered.
    """
    start = time.monotonic()
    tail: deque[str] = deque(maxlen=40)
    if tee:
        tee.parent.mkdir(parents=True, exist_ok=True)
    tee_fh = tee.open("a", encoding="utf-8") if tee else None
    proc = subprocess.Popen(
        [sys.executable, "-u", *argv],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            if tee_fh:
                tee_fh.write(raw)
            for piece in raw.replace("\r", "\n").split("\n"):
                if piece.strip():
                    tail.append(piece.rstrip())
                    on_line(piece)
        rc = proc.wait()
    except KeyboardInterrupt:
        # The tty already delivered SIGINT to the child (same process group);
        # give its checkpoint writers a moment, then make sure it is gone.
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=5)
        raise
    finally:
        if tee_fh:
            tee_fh.close()
    return StepResult(rc, time.monotonic() - start, list(tail))


def read_counts(rel_dir: Path) -> dict | None:
    """Disposition tallies from a built release, or None when unreadable."""
    try:
        manifest = json.loads((rel_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    disp = manifest.get("counts", {}).get("dispositions", {})
    return {
        "records": manifest.get("counts", {}).get("records", 0),
        "included": disp.get("included", 0),
        "review": disp.get("review", 0),
        "excluded": sum(v for k, v in disp.items() if k.startswith("excluded")),
    }


def _row(job: SourceJob, status: str, body: str, *, color: bool) -> str:
    glyphs = {
        "waiting": paint("·", DIM, color),
        "running": paint(">", CYAN, color),
        "done": paint("ok", GREEN, color),
        "failed": paint("XX", RED, color),
    }
    return f"{glyphs[status]:>2} {job.spec.key:<12} {body}"


def _progress_body(stage: str, tracker: PhaseTracker, *, color: bool) -> str:
    if tracker.cur is not None and tracker.total is not None:
        eta = tracker.eta_seconds()
        eta_txt = f"  ETA {format_eta(eta)}" if eta else ""
        label = (tracker.label or "")[:60]
        return (f"{stage:<8}{bar(tracker.cur, tracker.total)} "
                f"{tracker.cur}/{tracker.total}  {paint(label, DIM, color)}{eta_txt}")
    line = tracker.last_line[:70] if tracker.last_line else "starting"
    return f"{stage:<8}{paint(line, DIM, color)}"


def _counts_body(counts: dict, release: str, *, color: bool) -> str:
    parts = [f"{counts['included']} included"]
    if counts["review"]:
        parts.append(f"{counts['review']} review")
    if counts["excluded"]:
        parts.append(f"{counts['excluded']} excluded")
    return (f"{counts['records']} records -> {' · '.join(parts)}  "
            f"{paint(release, DIM, color)}")


def execute_plan(
    plan: Plan,
    *,
    root: Path = ROOT,
    stream=sys.stdout,
    verbose: bool = False,
    runner=run_subprocess,
) -> tuple[list[SourceResult], int]:
    """Run every job in the plan (harvest then build, sequentially) and
    return (per-source results, shell exit code).

    Sequential on purpose: the harvesters carry their own per-host rate
    limits, and parallel sources would multiply load on shared hosts
    (Wikipedia/Wikidata). A failed source is reported and skipped past, so
    one flaky upstream never wastes the others' completed work; `runner` is
    injectable for tests.
    """
    child_env = dict(os.environ)
    child_env.update(plan.extra_env)
    board = LiveBoard(stream, ansi=use_ansi(stream) and not verbose)
    color = board.color
    results: list[SourceResult] = []

    for job in plan.jobs:
        board.set_row(job.spec.key, _row(job, "waiting", "waiting", color=color))
    board.render(force=True)

    with cursor_hidden(stream, board.ansi):
        for job in plan.jobs:
            result = SourceResult(key=job.spec.key, release=job.release_rel)
            results.append(result)

            def on_line_for(stage: str, tracker: PhaseTracker):
                def on_line(line: str) -> None:
                    if verbose:
                        stream.write(f"    {job.spec.key}: {line.rstrip()}\n")
                    if tracker.feed(line):
                        board.set_row(
                            job.spec.key,
                            _row(job, "running",
                                 _progress_body(stage, tracker, color=color),
                                 color=color),
                        )
                        board.render()
                return on_line

            # ---- harvest (skipped when the snapshot is complete) ----
            if job.harvest_argv is None:
                board.set_row(job.spec.key,
                              _row(job, "running",
                                   f"harvest snapshot reused ({job.snap_rel})",
                                   color=color))
                board.render(force=True)
            else:
                tracker = PhaseTracker()
                board.set_row(job.spec.key,
                              _row(job, "running", "harvest starting", color=color))
                board.render(force=True)
                result.harvest = runner(
                    job.harvest_argv,
                    root=root,
                    env=child_env,
                    on_line=on_line_for("harvest", tracker),
                    tee=root / job.harvest_log_rel,
                )
                if result.harvest.rc != 0:
                    result.stage_failed = "harvest"
                    board.set_row(job.spec.key,
                                  _row(job, "failed",
                                       f"harvest exited {result.harvest.rc} "
                                       f"(log: {job.harvest_log_rel})",
                                       color=color))
                    board.render(force=True)
                    continue

            # ---- build the release (offline, deterministic) ----
            rel_dir = root / job.release_rel
            assert_safe_out(rel_dir)
            rel_dir.mkdir(parents=True, exist_ok=True)
            tracker = PhaseTracker()
            board.set_row(job.spec.key,
                          _row(job, "running", "build   applying gates", color=color))
            board.render(force=True)
            result.build = runner(
                job.run_argv,
                root=root,
                env=child_env,
                on_line=on_line_for("build", tracker),
                tee=rel_dir / "build.log",
            )
            if result.build.rc != 0:
                result.stage_failed = "build"
                board.set_row(job.spec.key,
                              _row(job, "failed",
                                   f"build exited {result.build.rc} "
                                   f"(log: {job.release_rel}/build.log)",
                                   color=color))
                board.render(force=True)
                continue

            result.counts = read_counts(rel_dir)
            if result.counts is None:
                result.stage_failed = "build"
                board.set_row(job.spec.key,
                              _row(job, "failed",
                                   f"build wrote no readable manifest in {job.release_rel}",
                                   color=color))
                board.render(force=True)
                continue
            result.ok = True
            board.set_row(job.spec.key,
                          _row(job, "done",
                               _counts_body(result.counts, job.release_rel, color=color),
                               color=color))
            board.render(force=True)

    board.close()
    _print_summary(results, plan, stream)
    return results, 0 if all(r.ok for r in results) else 1


def _print_summary(results: list[SourceResult], plan: Plan, stream) -> None:
    color = use_ansi(stream) and not os.environ.get("NO_COLOR")
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    stream.write("\n")
    stream.write(paint(
        f"Done: {len(ok)}/{len(results)} source(s) built for "
        f"[{plan.cutoff} .. {plan.asof}].\n", BOLD, color))
    for r in ok:
        c = r.counts or {}
        stream.write(f"  {r.key:<12} {c.get('included', 0):>5} included  "
                     f"{r.release}/facts.jsonl\n")
    for r in failed:
        step = r.harvest if r.stage_failed == "harvest" else r.build
        stream.write(paint(f"  {r.key:<12} FAILED during {r.stage_failed}\n", RED, color))
        for line in (step.tail if step else [])[-8:]:
            stream.write(f"      {line}\n")
    if failed:
        stream.write(
            "\nRerun the same command to retry — completed snapshots are reused and\n"
            "interrupted harvests resume from their checkpoints.\n"
        )
