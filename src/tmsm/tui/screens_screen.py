from __future__ import annotations

import subprocess

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from .. import supervisor
from .confirm import ConfirmScreen


class ScreensScreen(Screen):
    """Lists every running GNU screen session and lets the user attach or kill it."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q", "app.pop_screen", "Back"),
        Binding("R", "refresh", "Refresh"),
        Binding("enter", "attach", "Attach"),
        Binding("a", "attach", "Attach"),
        Binding("k", "kill", "Kill"),
        Binding("delete", "kill", "Kill"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[supervisor.ScreenSession] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="screens-wrap"):
            yield Static("Running screen sessions", id="screens-title")
            yield DataTable(id="screens", cursor_type="row", zebra_stripes=True)
            yield Static(
                "Enter/a = attach   k/Del = kill   R = refresh   Esc = back\n"
                "Detach from an attached session with Ctrl-A then d.",
                id="screens-help",
            )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Session", "Screen PID", "Inner PID", "Managed by tmsm")
        self.refresh_sessions()
        self.set_interval(2.0, self.refresh_sessions)

    def refresh_sessions(self) -> None:
        table = self.query_one(DataTable)
        prev = table.cursor_row if table.row_count else 0
        self.sessions = supervisor.list_all_sessions()
        table.clear()
        for s in self.sessions:
            table.add_row(
                s.session,
                str(s.screen_pid),
                "—" if s.inner_pid is None else str(s.inner_pid),
                "yes" if s.managed else "no",
            )
        if table.row_count:
            table.move_cursor(row=min(prev, table.row_count - 1))

    def _selected(self) -> supervisor.ScreenSession | None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        return self.sessions[table.cursor_row]

    def action_refresh(self) -> None:
        self.refresh_sessions()

    def action_attach(self) -> None:
        s = self._selected()
        if s is None:
            return
        cmd = supervisor.attach_command_raw(s.session)
        with self.app.suspend():
            try:
                subprocess.run(cmd)
            except FileNotFoundError:
                pass
        self.refresh_sessions()

    def action_kill(self) -> None:
        s = self._selected()
        if s is None:
            return

        msg = (
            f"Kill screen session [b]{s.session}[/b] (screen pid {s.screen_pid})?\n"
            f"This terminates the wrapped process."
        )
        if not s.managed:
            msg += "\n[b]Not managed by tmsm[/b] — proceed with caution."

        def confirmed(yes: bool | None) -> None:
            if not yes:
                return
            try:
                killed = supervisor.kill_session(s.session)
            except Exception as e:
                self.notify(f"Kill failed: {e}", severity="error")
                return
            if killed:
                self.notify(f"Killed {s.session}")
            else:
                self.notify(f"{s.session} was not running")
            self.refresh_sessions()

        self.app.push_screen(
            ConfirmScreen(
                msg, title="Kill screen session",
                ok_label="Kill", cancel_label="Cancel", destructive=True,
            ),
            confirmed,
        )

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        self.action_attach()
