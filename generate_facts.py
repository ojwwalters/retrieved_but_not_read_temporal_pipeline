#!/usr/bin/env python3
"""Generate a fresh Stage-1 fact release — the one-command entry point.

    python3 generate_facts.py

walks you through it: pick a training-cutoff date, tick the sources to
include (FDA labels, sports transfers, people deaths, SEC officers, finance,
chemical), and watch each one harvest and build with live progress bars.
Fully scriptable too:

    python3 generate_facts.py --cutoff 2026-03-01 --sources fda,chemical --yes

Existing releases and snapshots are never modified: every run writes only to
fresh stage1/snapshots/<source>_<cutoff>_<asof>/ and
stage1/releases/<source>_<cutoff>_<asof>/ directories. Stdlib only — no pip
installs. Details: README.md.
"""

import sys

if sys.version_info < (3, 11):
    sys.stderr.write(
        "generate_facts.py needs Python 3.11+ (this is %d.%d). On macOS try "
        "'python3.11' or 'brew install python@3.11'.\n"
        % (sys.version_info[0], sys.version_info[1])
    )
    sys.exit(1)


def _main():
    """Hand off to the wizard package once the interpreter is known-good.

    The import happens inside this function so the version check above is
    the only code an old interpreter ever parses. The repo root is put on
    sys.path first, so `python3 /path/to/generate_facts.py` works from any
    working directory, not just a checkout-root shell.
    """
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from stage1.wizard.cli import main

    return main()


if __name__ == "__main__":
    sys.exit(_main())
