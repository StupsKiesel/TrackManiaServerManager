"""Context menu shown when Enter is pressed on an instance."""
from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Label, ListItem, ListView


@dataclass
class MenuItem:
    action: str          # action id returned via dismiss
    label: str
    enabled: bool = True


class ActionMenuScreen(ModalScreen[str | None]):
    """Modal listing actions for the selected instance. Dismisses with the action id."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    ActionMenuScreen { align: center middle; }
    #menu-box {
        width: 40;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #menu-title { padding-bottom: 1; }
    ListView { height: auto; max-height: 16; }
    ListItem.-disabled { color: $text-muted; }
    """

    def __init__(self, title: str, items: list[MenuItem]) -> None:
        super().__init__()
        self.title_text = title
        self.items = items

    def compose(self) -> ComposeResult:
        with Container(id="menu-box"):
            yield Label(f"[b]{self.title_text}[/b]", id="menu-title")
            list_items = []
            for it in self.items:
                li = ListItem(Label(it.label), id=f"item-{it.action}")
                if not it.enabled:
                    li.add_class("-disabled")
                    li.disabled = True
                list_items.append(li)
            yield ListView(*list_items, id="menu-list")

    def on_mount(self) -> None:
        lv = self.query_one(ListView)
        lv.focus()
        # Place cursor on the first enabled item
        for i, item in enumerate(self.items):
            if item.enabled:
                lv.index = i
                break

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        if not item_id.startswith("item-"):
            return
        action = item_id[len("item-"):]
        # Look up enabled state
        for it in self.items:
            if it.action == action and it.enabled:
                self.dismiss(action)
                return

    def action_cancel(self) -> None:
        self.dismiss(None)
