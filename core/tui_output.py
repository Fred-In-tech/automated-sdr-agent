"""Output half of the terminal UI: the banner, step headers, messages, tables, panels and spinner.

Split out of core/tui.py (UI inherits `TerminalOutput`, and BANNER_ART is re-exported there, so
`from core.tui import BANNER_ART` and every `ui.info(...)` keep working) to keep that module about
asking questions. Each method prints rich output on a capable console and an ASCII line otherwise.
"""

from __future__ import annotations

import contextlib
import sys
from typing import Any, Callable, Iterator, Sequence

from core.product import AUTHOR, BRAND_COLOR, DISPLAY_NAME, TAGLINE, version

ACCENT = "#38BDF8"  # banner gradient end: a lighter sky blue next to the brand blue

# Two-row half-block lettering: ~50 columns wide so it fits an 80-column terminal with a
# frame around it. Built from glyphs so both rows always line up.
_GLYPHS = {
    "A": ("▄▀█", "█▀█"), "U": ("█ █", "█▄█"), "T": ("▀█▀", " █ "), "O": ("█▀█", "█▄█"),
    "M": ("█▀▄▀█", "█ ▀ █"), "E": ("█▀▀", "██▄"), "D": ("█▀▄", "█▄▀"), "S": ("█▀▀", "▄▄█"),
    "R": ("█▀█", "█▀▄"), " ": (" ", " "),
}
BANNER_ART = tuple(" ".join(_GLYPHS[ch][row] for ch in "AUTOMATED SDR") for row in range(2))
BANNER_TITLE = "AUTOMATED SDR"


def _blend(start: str, end: str, t: float) -> str:
    """Hex colour `t` of the way from `start` to `end` (for the banner gradient)."""
    a = [int(start[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(end[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(a, b))


class _Status:
    """What `UI.spinner()` yields: `status.update("new message")`."""

    def __init__(self, update: Callable[[str], None]):
        self.update = update


class TerminalOutput:
    """Mixin for UI: everything it prints. Needs `self.console` (a rich Console, or None in plain
    mode), `self._rich` (the lazily imported rich pieces) and `self._out` (plain-mode stream)."""

    def _stream(self) -> Any:
        return self.console.file if self.console is not None else (self._out or sys.stdout)

    def _write(self, text: str = "") -> None:
        """Plain write that survives consoles which can't encode every character."""
        stream = self._stream()
        try:
            stream.write(text + "\n")
        except UnicodeEncodeError:
            encoding = getattr(stream, "encoding", None) or "ascii"
            stream.write((text + "\n").encode(encoding, "replace").decode(encoding))
        with contextlib.suppress(Exception):
            stream.flush()

    def _emit(self, plain_text: str, fancy: Callable[[], Any] | None = None) -> None:
        if self.console is None or fancy is None:
            self._write(plain_text)
            return
        try:
            self.console.print(fancy())
        except UnicodeEncodeError:
            self._write(plain_text)

    def _can_render(self, text: str) -> bool:
        encoding = self.console.encoding if self.console is not None else "ascii"
        try:
            text.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            return False
        return True

    def _sym(self, fancy: str, ascii_: str) -> str:
        return fancy if self.console is not None and self._can_render(fancy) else ascii_

    def _line(self, symbol: str, symbol_style: str, msg: str, msg_style: str = "") -> Any:
        return self._rich.Text.assemble((symbol + " ", symbol_style), (msg, msg_style))

    def banner(self) -> None:
        """Welcome header: framed gradient art in fancy mode, two plain lines otherwise."""
        if self.console is None:
            self._write(f"{DISPLAY_NAME}  v{version()}")
            self._write(TAGLINE)
            self._write()
            return
        self._emit(f"{DISPLAY_NAME}  v{version()}\n{TAGLINE}\n", self._banner_panel)
        self.console.print()

    def _banner_panel(self) -> Any:
        r = self._rich
        width = max(len(line) for line in BANNER_ART)
        if self.console.width >= width + 10 and self._can_render("".join(BANNER_ART)):
            title = r.Text()
            for row, line in enumerate(BANNER_ART):
                for col, ch in enumerate(line):
                    title.append(ch, style=f"bold {_blend(BRAND_COLOR, ACCENT, col / (width - 1))}" if ch != " " else "")
                if row < len(BANNER_ART) - 1:
                    title.append("\n")
        else:
            title = r.Text(BANNER_TITLE, style=f"bold {BRAND_COLOR}")
        dot = self._sym("·", "-")
        byline = r.Text.assemble(("by ", "dim"), (AUTHOR, "bold"), (f"  {dot}  v{version()}", "dim"))
        body = r.Group(title, byline, r.Text(""), r.Text(TAGLINE))
        return r.Panel(body, box=r.box.ROUNDED, border_style=BRAND_COLOR, padding=(1, 3), expand=False)

    def step(self, n: int, total: int, title: str, hint: str = "") -> None:
        """Section header, e.g. "Step 2 of 8 · Your audience"."""
        if self.console is None:
            self._write()
            self._write(f"[Step {n}/{total}] {title}")
            if hint:
                self._write(f"  {hint}")
            return
        r = self._rich
        dot = self._sym("·", "-")
        self.console.print()
        heading = r.Text.assemble((f"Step {n} of {total}", f"bold {BRAND_COLOR}"), (f"  {dot}  ", "dim"),
                                  (title, "bold"))
        self._emit(f"[Step {n}/{total}] {title}", lambda: r.Rule(heading, align="left", style=BRAND_COLOR))
        if hint:
            self._emit(f"  {hint}", lambda: r.Text(f"  {hint}", style="dim"))

    def echo(self, msg: str = "") -> None:
        """Print text as-is (no prefix, no markup parsing)."""
        self._emit(msg, (lambda: self._rich.Text(msg)) if self.console is not None else None)

    def info(self, msg: str) -> None:
        self._emit(f"  {msg}", lambda: self._line(self._sym("•", "*"), BRAND_COLOR, msg))

    def success(self, msg: str) -> None:
        self._emit(f"OK: {msg}", lambda: self._line(self._sym("✓", "+"), "bold green", msg))

    def warn(self, msg: str) -> None:
        self._emit(f"Warning: {msg}", lambda: self._line("!", "bold yellow", msg, "yellow"))

    def error(self, msg: str) -> None:
        self._emit(f"Error: {msg}", lambda: self._line(self._sym("✗", "x"), "bold red", msg, "red"))

    def _hint(self, hint: str | None) -> None:
        if hint:
            self._emit(f"  {hint}", lambda: self._rich.Text(f"  {hint}", style="dim"))

    def _retry(self, msg: str) -> None:
        self._emit(f"  ! {msg}", lambda: self._line("  !", "bold yellow", msg, "yellow"))

    def _echo_answer(self, label: str, shown: str) -> None:
        """Show which answer from the file was used, so agent transcripts make sense."""
        self._emit(f"  {label}: {shown}",
                   lambda: self._rich.Text.assemble((f"  {self._sym('›', '>')} ", BRAND_COLOR),
                                                    (f"{label}: ", "dim"), (shown, "")))

    def summary(self, title: str, rows: Sequence[tuple[str, str]]) -> None:
        """Two-column recap (setting, value) — e.g. before writing config."""
        width = max((len(str(k)) for k, _v in rows), default=0)
        plain_lines = [title]
        for key, value in rows:
            lines = str(value).splitlines() or [""]
            plain_lines.append(f"  {str(key).ljust(width)} : {lines[0]}")
            plain_lines += [f"  {' ' * width}   {extra}" for extra in lines[1:]]
        plain_text = "\n".join(plain_lines)
        if self.console is None:
            self._write(plain_text)
            return

        def table() -> Any:
            r = self._rich
            grid = r.Table(title=r.Text(title, style="bold"), title_justify="left", box=r.box.ROUNDED,
                           border_style=BRAND_COLOR, show_header=False, padding=(0, 1))
            grid.add_column(style="dim", no_wrap=True)
            grid.add_column()
            for key, value in rows:
                grid.add_row(r.Text(str(key)), r.Text(str(value)))
            return grid

        self._emit(plain_text, table)

    def panel(self, body: str, title: str | None = None) -> None:
        """A framed block of text, e.g. a sample email."""
        indented = "\n".join(f"  {line}" for line in body.splitlines())
        plain_text = (f"== {title} ==\n" if title else "") + indented + "\n"
        if self.console is None:
            self._write(plain_text)
            return
        r = self._rich
        self._emit(plain_text, lambda: r.Panel(r.Text(body), title=r.Text(title, style="bold") if title else None,
                                               title_align="left", box=r.box.ROUNDED, border_style="dim",
                                               padding=(1, 2), expand=False))

    @contextlib.contextmanager
    def spinner(self, msg: str) -> Iterator[Any]:
        """`with ui.spinner("Checking your login") as status: ...` — animated on a real
        terminal, a single "msg..." line otherwise. `status.update(msg)` changes the text."""
        if self.console is None or not self.console.is_terminal:
            self._emit(f"{msg}...", (lambda: self._rich.Text(f"{msg}...")) if self.console is not None else None)
            yield _Status(lambda new: self._write(f"  {new}..."))
            return
        spin = "dots" if self._can_render("⠋") else "line"
        with self.console.status(self._rich.Text(msg), spinner=spin, spinner_style=BRAND_COLOR) as status:
            yield _Status(lambda new: status.update(self._rich.Text(new)))
