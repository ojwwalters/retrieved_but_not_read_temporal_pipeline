"""Turn the harvesters' human-facing stderr lines into fractions and ETAs.

The harvesters narrate for humans (`[harvest people]   extracted 12/500
title(s)`); rather than teach the frozen pipeline a machine progress
protocol, the wizard reads the same lines a human would. All heuristics live
here, pinned by tests to verbatim sample lines from each harvester — if a
harvester's wording drifts, a pinned test says so, and at runtime the wizard
degrades to showing the raw line without a bar. It must never render a wrong
bar from a date-like token (`07/2026`), and it must never crash on any line.
"""

from __future__ import annotations

import re
import time
from collections import deque

# A counter is `N/M` not embedded in a longer number/date/path token.
_COUNTER = re.compile(r"(?<![\d./-])(\d{1,7})\s*/\s*(\d{1,7})(?![\d./-])")
_HARVEST_PREFIX = re.compile(r"^\[harvest [^\]]+\]\s*")
_WS = re.compile(r"\s+")


def parse_counter(line: str) -> tuple[int, int] | None:
    """Extract (current, total) from a progress line, or None.

    Takes the LAST counter in the line (the finest-grained one when several
    appear, e.g. `batch 2/5: fetched 120/600`). A match is rejected when
    current > total, and when it looks like a month/year date: total in
    1900..2100 with current <= 12 (`07/2026`). A real `15/2026 rows` counter
    still parses because current exceeds 12.
    """
    result = None
    for m in _COUNTER.finditer(line):
        cur, total = int(m.group(1)), int(m.group(2))
        if cur > total or total == 0:
            continue
        if 1900 <= total <= 2100 and cur <= 12:
            continue
        result = (cur, total)
    return result


def clean_label(line: str) -> str:
    """A display label: harvest prefix stripped, whitespace collapsed."""
    return _WS.sub(" ", _HARVEST_PREFIX.sub("", line)).strip()


def phase_key(line: str) -> str:
    """A stable identity for the phase a counter line belongs to.

    Two lines that differ only in their counters are the same phase
    (`extracted 12/500 title(s)` == `extracted 13/500 title(s)`), so the
    ETA window survives across updates but resets on a genuine phase change.
    """
    return _COUNTER.sub("N/N", clean_label(line))


def format_eta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


class PhaseTracker:
    """Progress state for one running step, fed one output line at a time."""

    # ETA needs a little history before it is worth showing.
    _MIN_SAMPLES = 3
    _MIN_SPAN_SECONDS = 1.0

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.label: str | None = None      # label of the current counter phase
        self.cur: int | None = None
        self.total: int | None = None
        self.last_line: str = ""           # latest cleaned line, counter or not
        self._key: str | None = None
        self._samples: deque[tuple[float, int]] = deque(maxlen=60)

    def feed(self, line: str) -> bool:
        """Consume one output line; True when the visible state changed."""
        cleaned = clean_label(line)
        if not cleaned:
            return False
        changed = cleaned != self.last_line
        self.last_line = cleaned
        counter = parse_counter(line)
        if counter is None:
            return changed
        cur, total = counter
        key = phase_key(line)
        if key != self._key or total != self.total or (
            self.cur is not None and cur < self.cur
        ):
            # New phase, resized total, or a resumed/restarted counter:
            # the old rate no longer describes this work.
            self._samples.clear()
            self._key = key
        self.label = cleaned
        self.cur, self.total = cur, total
        self._samples.append((self._clock(), cur))
        return True

    def eta_seconds(self) -> float | None:
        if self.cur is None or self.total is None or len(self._samples) < self._MIN_SAMPLES:
            return None
        (t0, c0), (t1, c1) = self._samples[0], self._samples[-1]
        span, done = t1 - t0, c1 - c0
        if span < self._MIN_SPAN_SECONDS or done <= 0:
            return None
        return (self.total - self.cur) * span / done
