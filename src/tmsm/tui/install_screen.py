"""Modal that runs an install function in a worker thread, streaming logs."""
from __future__ import annotations

import datetime as _dt
import re
import traceback
from typing import Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Button, Label, RichLog

from .. import paths

Runner = Callable[[Callable[[str], None]], None]


class InstallScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close_if_done", "Close")]

    DEFAULT_CSS = """
    InstallScreen { align: center middle; }
    #box {
        width: 90%;
        height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    RichLog { height: 1fr; border: round $primary; }
    #logpath { color: $text-muted; }
    Button { margin-top: 1; }
    """

    def __init__(self, title: str, runner: Runner) -> None:
        super().__init__()
        self.title_text = title
        self.runner = runner
        self._done = False
        logs_dir = paths.LOGS_DIR
        logs_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", title).strip("-").lower() or "install"
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log_path = logs_dir / f"{ts}-{slug}.log"
        self._log_fh = self.log_path.open("w", encoding="utf-8", buffering=1)

    def compose(self) -> ComposeResult:
        with Container(id="box"):
            yield Label(f"[b]{self.title_text}[/b]")
            yield Label(f"log: {self.log_path}", id="logpath")
            yield RichLog(id="log", highlight=False, markup=False, wrap=False, auto_scroll=True)
            yield Button("Working…", id="close", disabled=True)

    def on_mount(self) -> None:
        self.run_worker(self._work, thread=True, exclusive=True, name="install")

    def _log(self, msg: str) -> None:
        try:
            self._log_fh.write(msg.rstrip("\n") + "\n")
        except Exception:
            pass
        log = self.query_one("#log", RichLog)
        self.app.call_from_thread(log.write, msg)

    def _work(self) -> None:
        try:
            self.runner(self._log)
            self._log("")
            self._log("--- done ---")
        except Exception as e:
            self._log("")
            self._log(f"ERROR: {e}")
            self._log(traceback.format_exc())
        finally:
            self._done = True
            try:
                self._log_fh.flush()
                self._log_fh.close()
            except Exception:
                pass
            self.app.call_from_thread(self._mark_done)

    def _mark_done(self) -> None:
        btn = self.query_one("#close", Button)
        btn.label = "Close"
        btn.disabled = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close" and self._done:
            self.dismiss(None)

    def action_close_if_done(self) -> None:
        if self._done:
            self.dismiss(None)
