from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

from textual.app import App, SystemCommand
from textual.screen import Screen

from .. import __version__
from .. import paths
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
        self._cleanup_stale_state()
        self.push_screen(MainScreen())
        self.run_worker(self._check_for_update, thread=True, exclusive=True, name="update-check")

    def _cleanup_stale_state(self) -> None:
        """After a host reboot/crash, drop any leftover supervisor state so
        instances aren't shown as 'running' when the processes are long gone."""
        from .. import supervisor
        from ..instances.service import MariaDBInstance
        supervisor.prune_stale_sessions()
        try:
            mdb = MariaDBInstance(self.cfg)
            pid = mdb._read_pid()
            if pid is not None and not mdb._pid_alive(pid):
                try:
                    mdb._pid_file().unlink()
                except FileNotFoundError:
                    pass
        except Exception:
            pass

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

    def action_screenshot(
        self, filename: str | None = None, path: str | None = None
    ) -> None:
        """Save an SVG screenshot to ~/.tmsm/screenshots/ by default.

        Textual's built-in action calls ``deliver_screenshot`` which uses a
        terminal file-transfer escape sequence; almost no terminal (Windows
        Terminal, gnome-terminal, xterm, tmux, …) supports it, so the
        screenshot silently disappears. We generate the SVG with
        ``export_screenshot`` and write it ourselves under TMSM_HOME so it
        always works and is easy to find.
        """
        target_dir = paths.HOME / "screenshots" if path is None else Path(path)
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.notify(f"Cannot create {target_dir}: {e}",
                        severity="error", timeout=10)
            return
        if filename is None:
            filename = f"tmsm-{datetime.now().strftime('%Y%m%d-%H%M%S')}.svg"
        out = target_dir / filename
        try:
            svg = self.export_screenshot(title=str(self.title))
            out.write_text(svg, encoding="utf-8")
        except Exception as e:
            self.notify(f"Screenshot failed: {e}", severity="error", timeout=10)
            return
        self.notify(f"Screenshot saved: {out}", title="Screenshot", timeout=8)

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        """Replace Textual's built-in 'Screenshot' command (which uses the
        terminal file-delivery escape sequence and silently fails on every
        common terminal) with one that writes the SVG to disk directly."""
        for cmd in super().get_system_commands(screen):
            if cmd.title.lower().startswith("screenshot"):
                continue
            yield cmd
        yield SystemCommand(
            "Save screenshot",
            f"Save an SVG screenshot under {paths.HOME / 'screenshots'}",
            lambda: self.set_timer(0.1, self.action_screenshot),
        )
