"""Prompt for a zip path / URL to update an existing Discord bot install."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static


class BotUpdateScreen(ModalScreen[str | None]):
    """Dismisses with the user-entered source (zip path or URL) or None on cancel."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    BotUpdateScreen { align: center middle; }
    #bot-update-box {
        width: 80;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    Label { padding: 1 0 0 0; }
    Input { width: 100%; }
    #hint { color: $text-muted; padding-top: 0; }
    Horizontal { height: auto; padding-top: 1; align-horizontal: right; }
    Button { margin-left: 1; }
    """

    def __init__(self, bot_name: str) -> None:
        super().__init__()
        self.bot_name = bot_name
        self.source_input = Input(
            placeholder="path to .zip OR https://… URL",
            id="bot-update-source",
        )

    def compose(self) -> ComposeResult:
        with Container(id="bot-update-box"):
            yield Static(f"[b]Update Discord bot '{self.bot_name}'[/b]")
            yield Static(
                "Files in the zip overwrite the install. Files in the install "
                "dir that are not in the zip (e.g. .env, .venv) are kept.",
                id="hint",
            )
            yield Label("Zip path or URL:")
            yield self.source_input
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Update", id="ok", variant="primary")

    def on_mount(self) -> None:
        self.source_input.focus()

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "ok":
            self._submit()

    def _submit(self) -> None:
        value = self.source_input.value.strip()
        if not value:
            self.notify("Provide a zip path or URL.", severity="warning")
            return
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)
