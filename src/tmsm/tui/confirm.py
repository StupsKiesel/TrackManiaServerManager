"""Simple yes/no confirmation modal."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Label


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("n", "cancel", "No"),
        Binding("y", "ok", "Yes"),
    ]

    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen > #confirm-box {
        width: auto;
        min-width: 40;
        max-width: 72;
        height: auto;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    ConfirmScreen.-destructive > #confirm-box { border: round $error; }
    ConfirmScreen #confirm-msg {
        width: auto;
        max-width: 68;
        padding: 0 0 1 0;
    }
    ConfirmScreen #confirm-buttons {
        width: 100%;
        height: auto;
        align-horizontal: right;
    }
    ConfirmScreen #confirm-buttons Button { margin: 0 0 0 2; min-width: 10; }
    """

    def __init__(self, message: str, *, title: str = "Confirm",
                 ok_label: str = "Yes", cancel_label: str = "No",
                 destructive: bool = False) -> None:
        super().__init__()
        self.message = message
        self.title_text = title
        self.ok_label = ok_label
        self.cancel_label = cancel_label
        self.destructive = destructive

    def compose(self) -> ComposeResult:
        box = Container(id="confirm-box")
        box.border_title = self.title_text
        with box:
            yield Label(self.message, id="confirm-msg")
            with Horizontal(id="confirm-buttons"):
                yield Button(self.cancel_label, id="cancel")
                ok_variant = "error" if self.destructive else "primary"
                yield Button(self.ok_label, id="ok", variant=ok_variant)

    def on_mount(self) -> None:
        if self.destructive:
            self.add_class("-destructive")
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "ok")

    def action_ok(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
