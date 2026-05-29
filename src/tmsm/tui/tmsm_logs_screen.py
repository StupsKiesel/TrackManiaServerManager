"""Browse and manage files in the tmsm app logs directory (~/.tmsm/logs/)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from .. import paths
from .confirm import ConfirmScreen
from .log_screen import LogScreen


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "K", "M", "G"):
        if size < 1024 or unit == "G":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}T"


class TmsmLogsScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q",      "back", "Back"),
        Binding("enter",  "open", "Open"),
        Binding("R",      "refresh", "Refresh"),
        Binding("d",      "delete_one", "Delete"),
        Binding("C",      "clear_all", "Clear all"),
    ]

    DEFAULT_CSS = """
    #logs-header { padding: 0 1; }
    #logs-empty  { padding: 1 2; color: $text-muted; }
    DataTable    { height: 1fr; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.files: list[Path] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(
            f"[b]tmsm app logs[/b]   [dim]{paths.LOGS_DIR}[/dim]",
            id="logs-header",
        )
        with Container():
            yield DataTable(id="logs-table", cursor_type="row", zebra_stripes=True)
            yield Static("", id="logs-empty")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Name", "Size", "Modified")
        self.refresh_list()

    def refresh_list(self) -> None:
        table = self.query_one(DataTable)
        empty = self.query_one("#logs-empty", Static)
        prev_row = table.cursor_row if table.row_count else 0
        table.clear()
        self.files = []
        if paths.LOGS_DIR.is_dir():
            try:
                self.files = sorted(
                    (p for p in paths.LOGS_DIR.iterdir() if p.is_file()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
            except OSError as e:
                empty.update(f"[red]Error listing {paths.LOGS_DIR}: {e}[/red]")
                return
        for p in self.files:
            try:
                st = p.stat()
                size = _human_size(st.st_size)
                mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                size, mtime = "?", "?"
            table.add_row(p.name, size, mtime)
        if self.files:
            empty.update("")
            table.move_cursor(row=min(prev_row, table.row_count - 1))
        else:
            empty.update("[dim]No log files yet.[/dim]")

    def _selected(self) -> Path | None:
        table = self.query_one(DataTable)
        if not table.row_count or table.cursor_row < 0:
            return None
        return self.files[table.cursor_row]

    # --- actions ---

    def action_back(self) -> None:
        self.dismiss(None)

    def action_refresh(self) -> None:
        self.refresh_list()

    def action_open(self) -> None:
        p = self._selected()
        if p is None:
            return
        self.app.push_screen(LogScreen(p, title=p.name))

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        self.action_open()

    def action_delete_one(self) -> None:
        p = self._selected()
        if p is None:
            return

        def after(ok: bool | None) -> None:
            if not ok:
                return
            try:
                p.unlink()
                self.notify(f"Deleted {p.name}")
            except OSError as e:
                self.notify(f"Delete failed: {e}", severity="error")
            self.refresh_list()

        self.app.push_screen(
            ConfirmScreen(
                f"Delete log file '{p.name}'?",
                title="Delete log",
                ok_label="Delete",
                destructive=True,
            ),
            after,
        )

    def action_clear_all(self) -> None:
        if not self.files:
            self.notify("No log files to clear.", severity="information")
            return
        count = len(self.files)

        def after(ok: bool | None) -> None:
            if not ok:
                return
            removed = 0
            errors: list[str] = []
            for p in list(self.files):
                try:
                    p.unlink()
                    removed += 1
                except OSError as e:
                    errors.append(f"{p.name}: {e}")
            if errors:
                self.notify(
                    f"Removed {removed}/{count}. Errors: {'; '.join(errors[:3])}"
                    + (" …" if len(errors) > 3 else ""),
                    severity="warning", timeout=8,
                )
            else:
                self.notify(f"Cleared {removed} log file(s).")
            self.refresh_list()

        self.app.push_screen(
            ConfirmScreen(
                f"Delete ALL {count} file(s) in {paths.LOGS_DIR}?",
                title="Clear all logs",
                ok_label="Delete all",
                destructive=True,
            ),
            after,
        )
