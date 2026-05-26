"""systemctl service manager screen."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static


from .sudo_helper import SudoModal, sudo_cached, sudo_run


# ── helpers ───────────────────────────────────────────────────────────────────

@dataclass
class ServiceEntry:
    unit: str
    load: str
    active: str
    sub: str
    description: str

    @property
    def is_running(self) -> bool:
        return self.active == "active" and self.sub == "running"

    @property
    def is_enabled(self) -> bool:
        r = subprocess.run(
            ["systemctl", "is-enabled", "--quiet", self.unit],
            capture_output=True,
        )
        return r.returncode == 0


def _fetch_services() -> list[ServiceEntry]:
    r = subprocess.run(
        [
            _SYSTEMCTL, "list-units",
            "--type=service",
            "--all",
            "--no-pager",
            "--plain",
            "--no-legend",
        ],
        capture_output=True, text=True,
    )
    entries: list[ServiceEntry] = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        unit = parts[0]
        load = parts[1]
        active = parts[2]
        sub = parts[3]
        description = parts[4] if len(parts) > 4 else ""
        # skip transient/scope/socket units that sneak in
        if not unit.endswith(".service"):
            continue
        entries.append(ServiceEntry(unit, load, active, sub, description))
    return entries


_SYSTEMCTL = "/usr/bin/systemctl"
_JOURNALCTL = "/usr/bin/journalctl"


def _systemctl(action: str, unit: str, password: str | None = None) -> tuple[bool, str]:
    r = sudo_run(_SYSTEMCTL, action, unit, password=password)
    out = (r.stderr or r.stdout).strip()
    return r.returncode == 0, out


def _journal(unit: str) -> None:
    """Open journalctl output in $PAGER (suspends TUI — caller must handle)."""
    subprocess.run(["sudo", "journalctl", "-u", unit, "--no-pager", "-n", "200"])


# ── screen ────────────────────────────────────────────────────────────────────

class SystemctlScreen(Screen):
    """Browse and manage systemd services."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q",      "app.pop_screen", "Back"),
        Binding("R",      "refresh",        "Refresh"),
        Binding("s",      "start",          "Start"),
        Binding("S",      "stop",           "Stop"),
        Binding("r",      "restart",        "Restart"),
        Binding("e",      "enable",         "Enable"),
        Binding("d",      "disable",        "Disable"),
        Binding("j",      "journal",        "Journal"),
        Binding("/",      "filter",         "Filter"),
    ]

    DEFAULT_CSS = """
    SystemctlScreen #svc-wrap { padding: 0 1; }
    SystemctlScreen #svc-filter { padding: 1 1 0 1; color: $text-muted; }
    SystemctlScreen #svc-help  { padding: 0 1 1 1; color: $text-muted; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._services: list[ServiceEntry] = []
        self._filter: str = ""
        self._password: str | None = None  # cached sudo password

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="svc-filter")
        with Container(id="svc-wrap"):
            yield DataTable(id="svc-table", cursor_type="row", zebra_stripes=True)
        yield Static(
            "s=start  S=stop  r=restart  e=enable  d=disable  j=journal  /=filter  R=refresh  Esc=back",
            id="svc-help",
        )
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one(DataTable)
        t.add_columns("Unit", "Load", "Active", "Sub", "Description")
        self.refresh_services()
        self.set_interval(5.0, self.refresh_services)
        # Prime sudo password in background (needed for start/stop/etc.)
        self.call_after_refresh(self._prime_sudo)

    def _prime_sudo(self) -> None:
        cached_pw: str | None = getattr(self.app, "sudo_password", None)
        if cached_pw is not None:
            self._password = cached_pw
            return
        if sudo_cached():
            return
        def _got(pw: str | None) -> None:
            if pw is None:
                return
            self._password = pw
            self.app.sudo_password = pw  # type: ignore[attr-defined]
        self.app.push_screen(SudoModal(), _got)

    # ── data ──────────────────────────────────────────────────────────────────

    def refresh_services(self) -> None:
        self._services = _fetch_services()
        self._repopulate()

    def _visible(self) -> list[ServiceEntry]:
        if not self._filter:
            return self._services
        f = self._filter.lower()
        return [s for s in self._services if f in s.unit.lower() or f in s.description.lower()]

    def _repopulate(self) -> None:
        t = self.query_one(DataTable)
        prev = t.cursor_row if t.row_count else 0
        t.clear()
        for s in self._visible():
            if s.active == "active":
                active_str = f"[green]{s.active}[/green]"
            elif s.active == "failed":
                active_str = f"[red]{s.active}[/red]"
            else:
                active_str = f"[dim]{s.active}[/dim]"

            sub_str = f"[green]{s.sub}[/green]" if s.sub == "running" else s.sub

            t.add_row(
                s.unit.removesuffix(".service"),
                s.load,
                active_str,
                sub_str,
                s.description,
            )
        if t.row_count:
            t.move_cursor(row=min(prev, t.row_count - 1))

        ftext = f" Filter: [b]{self._filter}[/b]" if self._filter else " All services"
        self.query_one("#svc-filter", Static).update(ftext)

    def _selected(self) -> ServiceEntry | None:
        t = self.query_one(DataTable)
        visible = self._visible()
        if not t.row_count or not visible:
            return None
        return visible[t.cursor_row]

    # ── actions ───────────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self.refresh_services()

    def _run(self, action: str) -> None:
        svc = self._selected()
        if svc is None:
            return
        ok, msg = _systemctl(action, svc.unit, password=self._password)
        if ok:
            self.notify(f"{action.capitalize()}ed {svc.unit}")
        else:
            self.notify(msg or f"{action} failed", severity="error", timeout=10)
        self.refresh_services()

    def action_start(self) -> None:
        self._run("start")

    def action_stop(self) -> None:
        svc = self._selected()
        if svc is None:
            return
        from .confirm import ConfirmScreen
        unit = svc.unit

        def _do(confirmed: bool) -> None:
            if not confirmed:
                return
            ok, msg = _systemctl("stop", unit, password=self._password)
            if ok:
                self.notify(f"Stopped {unit}")
            else:
                self.notify(msg or "stop failed", severity="error", timeout=10)
            self.refresh_services()

        self.app.push_screen(
            ConfirmScreen(f"Stop {unit}?", title="Stop service"),
            _do,
        )

    def action_restart(self) -> None:
        self._run("restart")

    def action_enable(self) -> None:
        self._run("enable")

    def action_disable(self) -> None:
        svc = self._selected()
        if svc is None:
            return
        from .confirm import ConfirmScreen
        unit = svc.unit

        def _do(confirmed: bool) -> None:
            if not confirmed:
                return
            ok, msg = _systemctl("disable", unit, password=self._password)
            if ok:
                self.notify(f"Disabled {unit}")
            else:
                self.notify(msg or "disable failed", severity="error", timeout=10)
            self.refresh_services()

        self.app.push_screen(
            ConfirmScreen(f"Disable {unit}?", title="Disable service"),
            _do,
        )

    def action_journal(self) -> None:
        svc = self._selected()
        if svc is None:
            return
        with self.app.suspend():
            try:
                subprocess.run(
                    ["sudo", _JOURNALCTL, "-u", svc.unit, "--no-pager", "-n", "200"]
                )
                input("\n-- Press Enter to return --")
            except (FileNotFoundError, KeyboardInterrupt):
                pass

    def action_filter(self) -> None:
        from textual.screen import ModalScreen as _Modal
        from textual.app import ComposeResult as _CR
        from textual.widgets import Input as _Input
        from textual.binding import Binding as _B
        from textual.containers import Container as _C
        from textual.widgets import Label as _L

        class _FilterModal(_Modal[str | None]):
            DEFAULT_CSS = """
            _FilterModal { align: center middle; }
            #fb { width: 50; height: auto; padding: 1 2;
                  border: round $accent; background: $surface; }
            """
            BINDINGS = [_B("escape", "cancel", "Cancel")]

            def compose(self) -> _CR:
                with _C(id="fb"):
                    yield _L("[b]Filter services[/b] [dim](leave blank to clear)[/dim]")
                    yield _Input(placeholder="e.g. nginx", id="fi")

            def on_mount(self) -> None:
                self.query_one(_Input).focus()

            def on_input_submitted(self, e: _Input.Submitted) -> None:
                self.dismiss(e.value.strip())

            def action_cancel(self) -> None:
                self.dismiss(None)

        def _apply(value: str | None) -> None:
            if value is None:
                return
            self._filter = value
            self._repopulate()

        self.app.push_screen(_FilterModal(), _apply)
