"""Prompt for a (Track|Mania)Exchange map ID to add to a server."""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from ..instances.server import GameServerInstance, GameType
from ..maps import exchange_host


class AddMapScreen(ModalScreen[tuple[str, Path] | None]):
    """Dismisses with (map_id_input, matchsettings_path) or None on cancel."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    AddMapScreen { align: center middle; }
    #add-map-box {
        width: 70;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    Label { padding: 1 0 0 0; }
    Input, Select { width: 100%; }
    #hint { color: $text-muted; padding-top: 0; }
    Horizontal { height: auto; padding-top: 1; align-horizontal: right; }
    Button { margin-left: 1; }
    """

    def __init__(self, inst: GameServerInstance) -> None:
        super().__init__()
        self.inst = inst
        self._matchsettings = self._discover_matchsettings()
        self.id_input = Input(placeholder="e.g. 12345 or https://trackmania.exchange/maps/12345",
                              id="map-id")
        opts = [(p.name, str(p)) for p in self._matchsettings]
        self.ms_select: Select = Select(
            options=opts,
            value=opts[0][1] if opts else Select.BLANK,
            allow_blank=False,
            id="ms-select",
        )

    def _discover_matchsettings(self) -> list[Path]:
        ms_dir = self.inst.server_dir() / "UserData" / "Maps" / "MatchSettings"
        if not ms_dir.is_dir():
            return []
        return sorted(ms_dir.glob("*.txt"))

    def compose(self) -> ComposeResult:
        host = exchange_host(self.inst.meta.game)
        title = "Trackmania 2020" if self.inst.meta.game is GameType.TM2020 else "ManiaPlanet"
        with Container(id="add-map-box"):
            yield Static(f"[b]Add map to '{self.inst.name}'[/b]  ({title})")
            yield Static(f"Source: [b]{host}[/b]", id="hint")
            yield Label("Map ID or URL:")
            yield self.id_input
            yield Label("MatchSettings file to update:")
            if self._matchsettings:
                yield self.ms_select
            else:
                yield Static(
                    "[red]No MatchSettings/*.txt file found in this server.[/red]",
                    id="no-ms",
                )
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Add", id="ok", variant="primary",
                             disabled=not self._matchsettings)

    def on_mount(self) -> None:
        self.id_input.focus()

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "ok":
            self._submit()

    def _submit(self) -> None:
        raw = self.id_input.value.strip()
        if not raw:
            self.notify("Enter a map ID or URL.", severity="warning")
            return
        if not self._matchsettings:
            self.notify("No MatchSettings file to add the map to.", severity="error")
            return
        ms_value = str(self.ms_select.value)
        self.dismiss((raw, Path(ms_value)))

    def action_cancel(self) -> None:
        self.dismiss(None)
