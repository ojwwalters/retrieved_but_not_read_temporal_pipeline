"""Command-line front door: `python3 generate_facts.py` and
`python3 -m stage1.wizard`.

With no arguments on an interactive terminal it walks through the whole
setup: cutoff date, as-of date, a checkbox source menu, credential prompts
for the sources that need them, then a plan preview and confirmation. Every
step can instead be supplied as a flag, and with `--cutoff` + `--sources`
(+ `--yes`) the wizard is fully scriptable — in CI or a pipe it never
prompts, it errors with instructions.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date

from stage1.config import contact_email, setting
from stage1.wizard import sources as src
from stage1.wizard.orchestrate import PlanError, ROOT, build_plan, execute_plan
from stage1.wizard.ui import (
    BOLD, DIM, RED, Choice, multiselect, paint, prompt_date, prompt_line,
    prompt_yes_no, use_color,
)

# Windows already generated in this repo — offered as cutoff suggestions.
KNOWN_CUTOFFS = ("2026-01-01", "2026-02-01")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generate_facts.py",
        description=(
            "Generate a fresh Stage-1 fact release: harvest each selected source "
            "for a [cutoff .. asof] window, then build its gated facts.jsonl. "
            "Run with no arguments for the interactive wizard."
        ),
        epilog=(
            "examples:\n"
            "  python3 generate_facts.py\n"
            "  python3 generate_facts.py --cutoff 2026-03-01 --sources fda,chemical --yes\n"
            "  python3 generate_facts.py --cutoff 2026-01-01 --sources sec "
            "--contact you@example.com --sample 1013871 --yes\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--cutoff", help="training-cutoff date, YYYY-MM-DD")
    parser.add_argument("--asof", help="as-of date for 'after' truth (default: today)")
    parser.add_argument("--sources",
                        help="comma-separated source names, or 'all' "
                             f"(valid: {', '.join(src.all_keys())})")
    parser.add_argument("--contact",
                        help="contact email for request User-Agents "
                             "(default: STAGE1_CONTACT_EMAIL in config.toml)")
    parser.add_argument("--sample",
                        help="small live-test subset for a SINGLE source "
                             "(sec: CIKs; finance: tickers; wiki sources: titles)")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the plan (commands, paths, reuse/resume) and exit")
    parser.add_argument("--verbose", action="store_true",
                        help="stream raw harvester output instead of the live board")
    parser.add_argument("--list-sources", action="store_true",
                        help="describe the available sources and exit")
    return parser


def parse_source_list(raw: str) -> list[str]:
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        raise PlanError("--sources is empty")
    if tokens == ["all"]:
        return src.all_keys()
    for t in tokens:
        try:
            src.spec(t)
        except KeyError as exc:
            raise PlanError(str(exc).strip('"')) from None
    return list(dict.fromkeys(tokens))


def print_source_table(out=sys.stdout) -> None:
    out.write("available sources:\n")
    for sp in src.REGISTRY:
        needs = []
        if sp.needs_contact:
            needs.append("contact email")
        if sp.env_key:
            needs.append(sp.env_key)
        extra = f"  [needs {', '.join(needs)}]" if needs else ""
        out.write(f"  {sp.key:<12} {sp.detail}\n"
                  f"  {'':<12} {sp.duration_hint}{extra}\n")


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _choices() -> list[Choice]:
    choices = []
    for sp in src.REGISTRY:
        hints = [sp.duration_hint]
        if sp.needs_contact:
            hints.append("asks for a contact email")
        if sp.env_key and not setting(sp.env_key):
            hints.append(f"asks for {sp.env_key}")
        choices.append(Choice(
            key=sp.key,
            label=sp.key,
            detail=sp.detail,
            hint="; ".join(hints),
            # Safe demo defaults: the fast, keyless sources.
            selected=sp.key in ("chemical", "fda"),
        ))
    return choices


def _preview(plan, out) -> None:
    color = use_color(out)
    out.write(paint(f"\nplan — window [{plan.cutoff} .. {plan.asof}]\n", BOLD, color))
    for job in plan.jobs:
        if job.harvest_argv is None:
            action = "build only (snapshot reused)"
        elif job.snap_state == "partial":
            action = "resume harvest, then build"
        else:
            action = "harvest, then build"
        out.write(f"  {job.spec.key:<12} {action}\n")
        if job.harvest_argv is not None:
            out.write(paint(f"  {'':<12} $ python3 {' '.join(job.harvest_argv)}\n",
                            DIM, color))
        out.write(paint(f"  {'':<12} $ python3 {' '.join(job.run_argv)}\n", DIM, color))
        out.write(paint(f"  {'':<12} {job.spec.duration_hint}\n", DIM, color))
    for note in plan.notes:
        out.write(f"  note: {note}\n")
    out.write("existing releases and snapshots are never modified; "
              "all writes go to the paths above.\n")


def main(argv: list[str] | None = None, *, root=None) -> int:
    """Run the wizard; returns a shell exit code.

    Flow: resolve the window (flags, else prompts) -> resolve sources (flags,
    else checkbox menu) -> collect credentials for the sources that need them
    -> build and preview the plan -> confirm -> execute. On a non-tty every
    "else prompt" branch becomes a hard error naming the missing flag.

    Exit codes: 0 success/aborted-by-choice, 1 a source failed, 2 bad
    arguments or an unsatisfiable plan, 130 interrupted. `root` overrides the
    repo root for tests only.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    out = sys.stdout
    root = root or ROOT

    if args.list_sources:
        print_source_table(out)
        return 0

    interactive = _interactive()
    try:
        # ---- window ----
        if args.cutoff:
            cutoff = args.cutoff
        elif interactive:
            out.write(paint("Stage-1 fact generator\n", BOLD, use_color(out)))
            cutoff = prompt_date("cutoff date (facts must post-date this)",
                                 suggestions=KNOWN_CUTOFFS)
        else:
            parser.error("--cutoff is required when not running on a terminal")
        asof = args.asof or (
            prompt_date("as-of date", default=date.today().isoformat())
            if interactive and not args.yes
            else date.today().isoformat()
        )

        # ---- sources ----
        if args.sources:
            keys = parse_source_list(args.sources)
        elif interactive:
            picked = multiselect("select sources", _choices())
            if not picked:
                out.write("nothing selected — exiting.\n")
                return 0
            keys = picked
        else:
            parser.error("--sources is required when not running on a terminal")

        # ---- credentials (prompt only when interactive) ----
        # Every harvest names a contact in its User-Agent (stage1.config).
        contact = args.contact or os.environ.get("SEC_CONTACT") or contact_email()
        selected = [src.spec(k) for k in keys]
        if not contact and interactive:
            contact = prompt_line(
                "contact email for request User-Agents",
                validate=lambda v: None if "@" in v else "needs an email address",
            )
        extra_env: dict[str, str] = {}
        if contact and contact != contact_email():
            extra_env["STAGE1_CONTACT_EMAIL"] = contact
        for sp in selected:
            if sp.env_key and not setting(sp.env_key) and interactive:
                import getpass
                value = getpass.getpass(f"{sp.env_key} (input hidden): ").strip()
                if value:
                    extra_env[sp.env_key] = value

        env = dict(os.environ)
        for sp in selected:
            if sp.env_key and setting(sp.env_key):
                env.setdefault(sp.env_key, setting(sp.env_key))
        env.update(extra_env)
        plan = build_plan(
            keys, cutoff, asof,
            root=root, contact=contact, sample=args.sample, env=env,
        )
        plan.extra_env = extra_env

        # ---- confirm and go ----
        _preview(plan, out)
        if args.dry_run:
            out.write("dry run — nothing was executed.\n")
            return 0
        if not args.yes and interactive:
            if not prompt_yes_no("start?", default=True):
                out.write("aborted — nothing was run.\n")
                return 0
        _, code = execute_plan(plan, root=root, stream=out, verbose=args.verbose)
        return code

    except PlanError as exc:
        sys.stderr.write(paint(f"error: {exc}\n", RED, use_color(sys.stderr)))
        return 2
    except KeyboardInterrupt:
        out.write("\ninterrupted — nothing needs cleaning up; rerun the same "
                  "command to resume (snapshots checkpoint their progress).\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
