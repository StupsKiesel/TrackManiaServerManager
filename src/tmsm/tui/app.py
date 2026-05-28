from __future__ import annotations

from textual.app import App

from .. import __version__
from ..config import load as load_config
from .main_screen import MainScreen


class TmsmApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "tmsm — TrackMania Server Manager"
    SUB_TITLE = f"v{__version__}"

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("?", "help", "Help"),
    ]

    restart_pending: bool = False
    update_available: bool = False

    def on_mount(self) -> None:
        self.cfg = load_config()
        self.push_screen(MainScreen())
        self.run_worker(self._check_for_update, thread=True, exclusive=True, name="update-check")

    def _check_for_update(self) -> None:
        from .. import updater
        avail = updater.check_update_available()
        self.call_from_thread(self._set_update_available, avail)

    def _set_update_available(self, avail: bool) -> None:
        self.update_available = avail
        if avail:
            self.notify(
                "A tmsm update is available. Press [b]u[/b] to install it.",
                title="Update available", timeout=10,
            )
        # Refresh footer so the Update binding shows/hides accordingly.
        screen = self.screen
        if hasattr(screen, "refresh_bindings"):
            screen.refresh_bindings()

    def action_help(self) -> None:
        self.notify(
            "↑/↓ select · Enter actions · n new · R refresh · ? help · q quit",
            title="Keys",
            timeout=8,
        )
