"""Terminal primitives (stdlib only): styling, prompts, a checkbox menu, and
the live status board.

Interaction logic is separated from terminal I/O so it is testable without a
terminal: the checkbox menu is a pure reducer over keypresses (`apply_key`),
the plain fallback parser is a pure function (`parse_plain_selection`), and
both the menu and the board render to lists of strings before anything is
written. Raw-mode key reading needs a POSIX tty (termios); everywhere else —
pipes, CI, Windows — the wizard degrades to numbered-input prompts and plain
heartbeat lines, never to a crash.
"""

from __future__ import annotations

import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

BOLD, DIM, RED, GREEN, YELLOW, CYAN = "1", "2", "31", "32", "33", "36"


def use_ansi(stream) -> bool:
    """Cursor-control capable: an interactive POSIX tty that isn't dumb."""
    if os.environ.get("TERM") == "dumb" or os.name != "posix":
        return False
    return hasattr(stream, "isatty") and stream.isatty()


def use_color(stream) -> bool:
    return use_ansi(stream) and not os.environ.get("NO_COLOR")


def paint(text: str, code: str, enabled: bool) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if enabled else text


_ESC = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def visible_len(text: str) -> int:
    """Printable columns `text` occupies: escape sequences take up none."""
    return len(_ESC.sub("", text))


def clip_visible(text: str, width: int) -> str:
    """Cut `text` to `width` columns, leaving its escape sequences intact.

    Everything repainted in place here — the checkbox menu and the status
    board — addresses the screen in logical lines (`\x1b[NF` moves up N).
    A row wider than the window wraps, the terminal has then drawn more
    lines than were written, and every later repaint lands in the wrong
    place. Clipping first is what keeps the arithmetic true.
    """
    if width <= 0 or visible_len(text) <= width:
        return text
    budget, shown, styled, out, i = max(0, width - 1), 0, False, [], 0
    while i < len(text) and shown < budget:
        esc = _ESC.match(text, i)
        if esc:
            out.append(esc.group())
            styled = True
            i = esc.end()
            continue
        out.append(text[i])
        shown += 1
        i += 1
    out.append("\u2026")
    if styled:
        out.append("\x1b[0m")
    return "".join(out)


def stream_width(stream, default: int = 80) -> int:
    """Window width for `stream`, falling back to $COLUMNS then `default`."""
    try:
        return os.get_terminal_size(stream.fileno()).columns
    except (AttributeError, OSError, ValueError):
        pass
    try:
        return int(os.environ.get("COLUMNS", ""))
    except ValueError:
        return default


def bar(cur: int, total: int, width: int = 22) -> str:
    if total <= 0:
        return "[" + "-" * width + "]"
    filled = max(0, min(width, round(width * cur / total)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


@contextmanager
def cursor_hidden(stream, enabled: bool):
    if not enabled:
        yield
        return
    try:
        stream.write("\x1b[?25l")
        stream.flush()
        yield
    finally:
        stream.write("\x1b[?25h")
        stream.flush()


# ---------------------------------------------------------------- prompts


def prompt_line(label, *, default=None, validate=None, input_fn=input, out=sys.stdout):
    """Ask until `validate` (message-or-None) accepts; empty picks `default`."""
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            raw = input_fn(f"{label}{suffix}: ").strip()
        except EOFError:
            raise KeyboardInterrupt from None
        if not raw and default is not None:
            raw = default
        problem = validate(raw) if validate else None
        if problem is None and raw:
            return raw
        out.write(f"  {problem or 'a value is required'}\n")


def validate_iso_date(raw: str) -> str | None:
    from datetime import date

    try:
        date.fromisoformat(raw)
        return None
    except ValueError:
        return f"{raw!r} is not a valid YYYY-MM-DD date"


def prompt_date(label, *, default=None, suggestions=(), input_fn=input, out=sys.stdout):
    if suggestions:
        out.write(f"  (e.g. {', '.join(suggestions)})\n")
    return prompt_line(
        label, default=default, validate=validate_iso_date, input_fn=input_fn, out=out
    )


def prompt_yes_no(question, *, default=False, input_fn=input) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        raw = input_fn(f"{question} [{hint}] ").strip().lower()
    except EOFError:
        raise KeyboardInterrupt from None
    if not raw:
        return default
    return raw in ("y", "yes")


# ---------------------------------------------------------- checkbox menu


@dataclass
class Choice:
    key: str
    label: str
    detail: str = ""
    hint: str = ""            # e.g. "will ask for POLYGON_API_KEY"
    blocked: str | None = None  # reason this row cannot be selected
    selected: bool = False


@dataclass
class MenuState:
    choices: list[Choice]
    cursor: int = 0


def apply_key(state: MenuState, key: str) -> str:
    """Pure reducer: mutate `state` per keypress; return continue/accept/abort."""
    n = len(state.choices)
    if key == "up":
        state.cursor = (state.cursor - 1) % n
    elif key == "down":
        state.cursor = (state.cursor + 1) % n
    elif key == "space":
        c = state.choices[state.cursor]
        if not c.blocked:
            c.selected = not c.selected
    elif key == "all":
        for c in state.choices:
            if not c.blocked:
                c.selected = True
    elif key == "none":
        for c in state.choices:
            c.selected = False
    elif key == "enter":
        return "accept"
    elif key in ("q", "ctrl-c"):
        return "abort"
    return "continue"


def render_menu(state: MenuState, *, color: bool, width: int | None = None) -> list[str]:
    lines = []
    for i, c in enumerate(state.choices):
        pointer = ">" if i == state.cursor else " "
        box = "[x]" if c.selected else "[ ]"
        if c.blocked:
            body = f"{c.label:<16} {c.detail}  — unavailable: {c.blocked}"
            line = f"{pointer}  -  {paint(body, DIM, color)}"
        else:
            note = f"  ({c.hint})" if c.hint else ""
            body = f"{c.label:<16} {c.detail}{note}"
            if i == state.cursor:
                body = paint(body, BOLD, color)
            line = f"{pointer} {box} {body}"
        lines.append(clip_visible(line, width) if width else line)
    return lines


def parse_plain_selection(raw: str, choices: list[Choice]) -> list[str] | str:
    """Parse `1,3` / `fda chemical` / `all` into keys, or an error message."""
    raw = raw.strip().lower()
    if not raw:
        return "nothing selected — enter numbers or source names (or 'all')"
    available = [c for c in choices if not c.blocked]
    if raw == "all":
        return [c.key for c in available]
    picked: list[str] = []
    for token in raw.replace(",", " ").split():
        chosen = None
        if token.isdigit():
            idx = int(token)
            if not 1 <= idx <= len(choices):
                return f"{token} is out of range 1..{len(choices)}"
            chosen = choices[idx - 1]
        else:
            for c in choices:
                if c.key.lower() == token:
                    chosen = c
                    break
            if chosen is None:
                return f"unknown source {token!r}"
        if chosen.blocked:
            return f"{chosen.key} is unavailable: {chosen.blocked}"
        if chosen.key not in picked:
            picked.append(chosen.key)
    return picked


def _read_key(stdin) -> str:
    ch = stdin.read(1)
    if ch == "\x1b":
        rest = stdin.read(2)
        if rest == "[A":
            return "up"
        if rest == "[B":
            return "down"
        return "other"
    return {
        " ": "space",
        "\r": "enter",
        "\n": "enter",
        "a": "all",
        "n": "none",
        "q": "q",
        "\x03": "ctrl-c",
        "\x04": "ctrl-c",
    }.get(ch, "other")


def _multiselect_tty(title, state: MenuState, out) -> list[str] | None:
    import termios
    import tty

    color = use_color(out)
    out.write(paint(title, BOLD, color) + "\n")
    out.write(paint("  arrows move · space toggles · a all · n none · enter starts · q quits\n",
                    DIM, color))
    drawn = 0
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        with cursor_hidden(out, True):
            while True:
                if drawn:
                    out.write(f"\x1b[{drawn}F")
                lines = render_menu(state, color=color, width=stream_width(out))
                for line in lines:
                    out.write("\x1b[2K" + line + "\n")
                drawn = len(lines)
                out.flush()
                action = apply_key(state, _read_key(sys.stdin))
                if action == "accept":
                    return [c.key for c in state.choices if c.selected]
                if action == "abort":
                    return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _multiselect_plain(title, state: MenuState, out, input_fn) -> list[str] | None:
    out.write(f"{title}\n")
    for i, c in enumerate(state.choices, 1):
        if c.blocked:
            out.write(f"  {i}. {c.label:<16} {c.detail}  — unavailable: {c.blocked}\n")
        else:
            note = f"  ({c.hint})" if c.hint else ""
            out.write(f"  {i}. {c.label:<16} {c.detail}{note}\n")
    while True:
        try:
            raw = input_fn("select sources (numbers or names, 'all'): ")
        except EOFError:
            return None
        result = parse_plain_selection(raw, state.choices)
        if isinstance(result, list):
            return result
        out.write(f"  {result}\n")


def multiselect(title, choices: list[Choice], *, out=sys.stdout,
                input_fn=input) -> list[str] | None:
    """Returns selected keys in menu order, or None when aborted."""
    state = MenuState(choices=choices)
    if use_ansi(out) and sys.stdin.isatty():
        try:
            picked = _multiselect_tty(title, state, out)
        except (ImportError, OSError):
            picked = _multiselect_plain(title, state, out, input_fn)
    else:
        picked = _multiselect_plain(title, state, out, input_fn)
    if picked is not None:
        order = {c.key: i for i, c in enumerate(choices)}
        picked = sorted(dict.fromkeys(picked), key=order.__getitem__)
    return picked


# ------------------------------------------------------------- live board


class LiveBoard:
    """A block of one-line statuses, repainted in place on a tty.

    Off-tty it prints a plain line whenever a row's text changes, throttled
    to one heartbeat per row per `heartbeat` seconds, so CI logs stay short
    but alive.
    """

    def __init__(self, stream=sys.stdout, *, ansi=None, min_interval=0.1,
                 heartbeat=5.0, clock=time.monotonic):
        self.stream = stream
        self.ansi = use_ansi(stream) if ansi is None else ansi
        self.color = self.ansi and not os.environ.get("NO_COLOR")
        self._min_interval = min_interval
        self._heartbeat = heartbeat
        self._clock = clock
        self._rows: dict[str, str] = {}
        self._order: list[str] = []
        self._drawn = 0
        self._last_paint = 0.0
        self._last_emit: dict[str, tuple[float, str]] = {}

    def set_row(self, key: str, text: str) -> None:
        if key not in self._rows:
            self._order.append(key)
        self._rows[key] = text

    def render(self, force: bool = False) -> None:
        now = self._clock()
        if self.ansi:
            if not force and now - self._last_paint < self._min_interval:
                return
            self._last_paint = now
            if self._drawn:
                self.stream.write(f"\x1b[{self._drawn}F")
            width = stream_width(self.stream)
            for key in self._order:
                self.stream.write("\x1b[2K" + clip_visible(self._rows[key], width) + "\n")
            self._drawn = len(self._order)
            self.stream.flush()
            return
        for key in self._order:
            text = self._rows[key]
            last_t, last_text = self._last_emit.get(key, (0.0, None))
            if text != last_text and (force or now - last_t >= self._heartbeat):
                self.stream.write(text + "\n")
                self._last_emit[key] = (now, text)
        self.stream.flush()

    def close(self) -> None:
        self.render(force=True)
