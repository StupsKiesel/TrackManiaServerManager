from __future__ import annotations

from textual.app import App

from ..config import load as load_config
from .main_screen import MainScreen


class TmsmApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "tmsm — TrackMania Server Manager"

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("?", "help", "Help"),
    ]

    def on_mount(self) -> None:
        self.cfg = load_config()
        self.push_screen(MainScreen())

    def action_help(self) -> None:
        self.notify(
            "↑/↓ select · Enter actions · n new · R refresh · ? help · q quit",
            title="Keys",
            timeout=8,
        )
