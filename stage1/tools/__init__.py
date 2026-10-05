"""Offline analysis tools for Stage-1 releases (regression diffs, audits).

Tools read release artifacts (facts.jsonl, manifest.json) and legacy pipeline
outputs; they never write into releases except for their own named reports,
and they follow the same determinism rules as the pipeline: stable ordering,
no timestamps in report bodies.
"""
