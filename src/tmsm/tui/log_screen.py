"""Live log viewer for an instance's log file."""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, RichLog, Static


# Maximum bytes loaded from existing log on open (avoid blowing up memory on huge logs).
_INITIAL_TAIL_BYTES = 256 * 1024


class LogScreen(Screen):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q",      "close", "Close"),
        Binding("f",      "toggle_follow", "Follow"),
        Binding("c",      "clear_view", "Clear view"),
        Binding("g",      "scroll_top", "Top"),
        Binding("G",      "scroll_bottom", "Bottom"),
    ]

    DEFAULT_CSS = """
    #log-header { padding: 0 1; }
    #log-status { padding: 0 1; color: $text-muted; }
    RichLog { height: 1fr; background: $surface; }
    """

    def __init__(self, path: Path, title: str | None = None) -> None:
        super().__init__()
        self.path = path
        self.title_text = title or path.name
        self._offset = 0          # byte position last read
        self._follow = True
        self._timer = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(f"[b]{self.title_text}[/b]\n[dim]{self.path}[/dim]", id="log-header")
        yield RichLog(highlight=False, markup=False, wrap=False,
                      auto_scroll=True, id="logview")
        yield Static(self._status_text(), id="log-status")
        yield Footer()

    def _status_text(self) -> str:
        return f"Following: {'on' if self._follow else 'off'}    [dim](f to toggle, c to clear, g/G top/bottom)[/dim]"

    def on_mount(self) -> None:
        view = self.query_one(RichLog)
        if not self.path.is_file():
            view.write(f"(log file not found: {self.path})")
            self.query_one("#log-status", Static).update("File missing. Will appear when created.")
            self._offset = 0
        else:
            self._load_initial()
        # Poll for new bytes 4×/sec.
        self._timer = self.set_interval(0.25, self._poll)

    def _load_initial(self) -> None:
        view = self.query_one(RichLog)
        try:
            size = self.path.stat().st_size
            start = max(0, size - _INITIAL_TAIL_BYTES)
            with self.path.open("rb") as f:
                f.seek(start)
                data = f.read()
            self._offset = size
        except OSError as e:
            view.write(f"(error reading log: {e})")
            return
        if start > 0:
            view.write(f"(... truncated: showing last {len(data)} bytes of {size})")
        text = data.decode("utf-8", errors="replace")
        # Drop a leading partial line when we truncated mid-line.
        if start > 0 and "\n" in text:
            text = text[text.index("\n") + 1 :]
        for line in text.splitlines():
            view.write(line)

    def _poll(self) -> None:
        if not self._follow:
            return
        try:
            if not self.path.is_file():
                return
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self._offset:
            # File was rotated/truncated — reload from the new start.
            self._offset = 0
            self.query_one(RichLog).write("(log rotated — reloaded)")
        if size == self._offset:
            return
        try:
            with self.path.open("rb") as f:
                f.seek(self._offset)
                chunk = f.read(size - self._offset)
            self._offset = size
        except OSError:
            return
        text = chunk.decode("utf-8", errors="replace")
        view = self.query_one(RichLog)
        for line in text.splitlines():
            view.write(line)

    def action_close(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        self.dismiss(None)

    def action_toggle_follow(self) -> None:
        self._follow = not self._follow
        view = self.query_one(RichLog)
        view.auto_scroll = self._follow
        self.query_one("#log-status", Static).update(self._status_text())

    def action_clear_view(self) -> None:
        self.query_one(RichLog).clear()

    def action_scroll_top(self) -> None:
        view = self.query_one(RichLog)
        view.auto_scroll = False
        self._follow = False
        view.scroll_home(animate=False)
        self.query_one("#log-status", Static).update(self._status_text())

    def action_scroll_bottom(self) -> None:
        self._follow = True
        view = self.query_one(RichLog)
        view.auto_scroll = True
        view.scroll_end(animate=False)
        self.query_one("#log-status", Static).update(self._status_text())
