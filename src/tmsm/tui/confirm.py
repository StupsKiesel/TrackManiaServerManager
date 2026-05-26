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
    #confirm-box {
        width: 60;
        height: auto;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }
    #confirm-buttons { padding-top: 1; align-horizontal: center; }
    Button { margin: 0 1; }
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
        with Container(id="confirm-box"):
            yield Label(f"[b]{self.title_text}[/b]")
            yield Label(self.message)
            with Horizontal(id="confirm-buttons"):
                ok_variant = "error" if self.destructive else "primary"
                yield Button(self.ok_label, id="ok", variant=ok_variant)
                yield Button(self.cancel_label, id="cancel")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "ok")

    def action_ok(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
