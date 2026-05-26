"""Shared sudo utilities: in-TUI password prompt + subprocess wrapper."""
from __future__ import annotations

import os
import socket
import subprocess

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static


# ── modal ─────────────────────────────────────────────────────────────────────

class SudoModal(ModalScreen[str | None]):
    """Full-screen-dimming authentication dialog for sudo."""

    DEFAULT_CSS = """
    SudoModal { align: center middle; }

    #sudo-box {
        width: 56;
        height: auto;
        border: heavy $warning;
        background: $surface;
        padding: 0;
    }

    #sudo-header {
        background: $warning 20%;
        padding: 1 2;
        border-bottom: solid $warning 50%;
        align: center middle;
        height: auto;
    }

    #sudo-icon  { text-align: center; width: 1fr; }
    #sudo-title { text-align: center; width: 1fr; }
    #sudo-who   { text-align: center; width: 1fr; color: $text-muted; }

    #sudo-body  { padding: 1 2; height: auto; }

    #sudo-input-wrap { margin-top: 1; height: auto; }

    #sudo-error {
        color: $error;
        text-align: center;
        height: auto;
        margin-top: 1;
        display: none;
    }
    #sudo-error.visible { display: block; }

    #sudo-hint  { color: $text-muted; text-align: center; margin-top: 1; }

    #sudo-buttons {
        align-horizontal: center;
        height: auto;
        margin-top: 1;
        padding-bottom: 1;
    }
    #sudo-buttons Button { min-width: 14; margin: 0 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, prompt: str = "Authentication required") -> None:
        super().__init__()
        self._prompt = prompt

    def _whoami(self) -> str:
        try:
            user = os.environ.get("USER") or os.environ.get("LOGNAME") or os.getlogin()
            host = socket.gethostname()
            return f"{user}@{host}"
        except Exception:
            return "current user"

    def compose(self) -> ComposeResult:
        with Vertical(id="sudo-box"):
            with Vertical(id="sudo-header"):
                yield Static("🔒", id="sudo-icon")
                yield Static(f"[bold]{self._prompt}[/bold]", id="sudo-title")
                yield Static(self._whoami(), id="sudo-who")

            with Vertical(id="sudo-body"):
                yield Label("[dim]Password for[/dim] [bold]sudo[/bold]:")
                with Container(id="sudo-input-wrap"):
                    yield Input(
                        password=True,
                        placeholder="Enter password…",
                        id="sudo-input",
                    )
                yield Static("", id="sudo-error")
                yield Static(
                    "[dim]Enter[/dim] = authenticate   [dim]Esc[/dim] = cancel",
                    id="sudo-hint",
                )

            with Horizontal(id="sudo-buttons"):
                yield Button("Authenticate", id="ok", variant="warning")
                yield Button("Cancel", id="cancel", variant="default")

    def on_mount(self) -> None:
        self.query_one("#sudo-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        value = self.query_one("#sudo-input", Input).value
        err = self.query_one("#sudo-error", Static)
        if not value:
            err.update("Password cannot be empty.")
            err.add_class("visible")
            return
        # Quick verification: sudo -S -v just validates credentials
        result = subprocess.run(
            [_SUDO, "-S", "-v"],
            input=value + "\n",
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            err.update("Incorrect password — try again.")
            err.add_class("visible")
            inp = self.query_one("#sudo-input", Input)
            inp.value = ""
            inp.focus()
            return
        err.remove_class("visible")
        self.dismiss(value)


# ── subprocess helper ─────────────────────────────────────────────────────────

_SUDO = "/usr/bin/sudo"


def sudo_run(
    *args: str,
    password: str | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """
    Run ``sudo <args>``.

    If *password* is provided, passes it via stdin (``sudo -S``).
    Otherwise uses ``sudo -n`` (non-interactive; relies on cached credentials).
    """
    if password is not None:
        return subprocess.run(
            [_SUDO, "-S", *args],
            input=password + "\n",
            capture_output=True,
            text=True,
            check=check,
        )
    return subprocess.run(
        [_SUDO, "-n", *args],
        capture_output=True,
        text=True,
        check=check,
    )


def sudo_cached() -> bool:
    """Return True if sudo credentials are already cached (no password needed)."""
    return subprocess.run(
        [_SUDO, "-n", "true"],
        capture_output=True,
    ).returncode == 0
