"""Interactive front door for the Stage-1 pipeline.

`python3 generate_facts.py` (or `python3 -m stage1.wizard`) walks a user
through generating a fresh fact release: pick a cutoff/as-of window, tick the
sources to include, and watch each one harvest and build with live progress.

The wizard is a WRAPPER, never a re-implementation: it composes the two
existing entry points (`python3 -m stage1.harvest`, `python3 -m stage1.run`)
as subprocesses with exactly the arguments documented in README.md.
It imports nothing from the pipeline packages, so PIPELINE_VERSION, the gate
ledger, and every committed release are untouched by construction; all of its
writes land in fresh `<source>_<cutoff>_<asof>` directories and it refuses to
overwrite anything that already holds facts.
"""

from __future__ import annotations

WIZARD_VERSION = "wizard:v1"
