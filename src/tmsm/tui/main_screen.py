from __future__ import annotations

from typing import List

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from .. import supervisor
from ..instances import Instance, Kind, discover_all
from ..supervisor import Status
from .stats_screen import StatsScreen


STATUS_DOT = {
    Status.RUNNING: "[green]●[/green]",
    Status.STOPPED: "[grey50]○[/grey50]",
    Status.CRASHED: "[red]✗[/red]",
}


class MainScreen(Screen):
    BINDINGS = [
        Binding("enter", "open_menu", "Actions"),
        Binding("R", "refresh", "Refresh"),
        Binding("n", "new", "New"),
        Binding("s", "screens",    "Screens"),
        Binding("t", "stats",       "Stats"),
        Binding("f", "ufw",         "UFW"),
        Binding("y", "systemctl",   "Services"),
        Binding("a", "addons",      "Addons"),
        Binding("l", "tmsm_logs",   "App logs"),
        Binding("d", "diagnostics", "Diagnose"),
        Binding("u", "update_tmsm", "Update tmsm"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.instances: List[Instance] = []
        # At most one pool can have auto-attach enabled at a time (session-only).
        self._auto_attach_pool: str | None = None
        # Last observed pool runtime state: name -> (running, pid).
        self._pool_state: dict[str, tuple[bool, int | None]] = {}
        # Whether we've seen the selected auto-attach pool go down and are
        # waiting to auto-attach when it comes back.
        self._auto_attach_armed: dict[str, bool] = {}
        # Guard against re-entrant auto-attach attempts while already attaching.
        self._auto_attach_busy: bool = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="table-wrap"):
            table = DataTable(id="instances", cursor_type="row", zebra_stripes=True)
            yield table
        yield Vertical(Static("Select an instance.", id="details-body"), id="details")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("", "Name", "Kind", "Status", "Port", "XMLRPC", "Account", "Link", "Screen", "Mem", "Command")
        self.refresh_instances()
        self.set_interval(2.0, self.refresh_instances)

    # --- data refresh ---

    def refresh_instances(self) -> None:
        self.instances = discover_all(self.app.cfg)  # type: ignore[attr-defined]
        table = self.query_one(DataTable)
        prev_row = table.cursor_row if table.row_count else 0
        prev_scroll_x = table.scroll_x
        table.clear()
        next_pool_state: dict[str, tuple[bool, int | None]] = {}
        auto_attach_target: Instance | None = None
        for inst in self.instances:
            st = inst.status()
            dot = STATUS_DOT.get(st.status, "?")
            port = self._port_for(inst)
            link = self._link_for(inst)
            mem = f"{st.mem_mb:.0f}M" if st.mem_mb is not None else "—"
            table.add_row(
                dot, inst.name, inst.kind.value, st.status.value,
                port, inst.xmlrpc_port_str(), inst.account_name(),
                link, inst.screen_session(), mem, inst.cmd_summary(),
            )

            if inst.kind is Kind.POOL:
                running_now = st.status is Status.RUNNING
                next_pool_state[inst.name] = (running_now, st.pid)

                # Auto-attach trigger for automatic restart/reload:
                # - arm when pool is observed down
                # - fire when armed pool comes up again
                # - also fire on in-place PID change while still running
                if (
                    self._auto_attach_pool == inst.name
                    and not self._auto_attach_busy
                    and auto_attach_target is None
                ):
                    prev = self._pool_state.get(inst.name)
                    if not running_now:
                        self._auto_attach_armed[inst.name] = True
                    if prev is not None:
                        prev_running, prev_pid = prev
                        restarted = False
                        if running_now and self._auto_attach_armed.get(inst.name, False):
                            restarted = True
                        elif prev_running and running_now:
                            if prev_pid is not None and st.pid is not None and prev_pid != st.pid:
                                restarted = True
                        elif (not prev_running) and running_now:
                            restarted = True
                        if restarted:
                            self._auto_attach_armed[inst.name] = False
                            auto_attach_target = inst

        self._pool_state = next_pool_state
        if table.row_count:
            table.move_cursor(row=min(prev_row, table.row_count - 1))
        table.scroll_x = prev_scroll_x
        self.update_details()

        if auto_attach_target is not None:
            self._auto_attach_busy = True
            try:
                self.notify(
                    f"Auto Attach: attaching to {auto_attach_target.name} after automatic restart.",
                    severity="information",
                    timeout=6,
                )
                self._attach_instance(auto_attach_target, automatic=True, refresh_after=False)
            finally:
                self._auto_attach_busy = False

    def _port_for(self, inst: Instance) -> str:
        if inst.kind is Kind.SERVER:
            return str(inst.meta.game_port)  # type: ignore[attr-defined]
        if inst.kind is Kind.SERVICE and inst.name == "mariadb":
            return str(self.app.cfg.mariadb.port)  # type: ignore[attr-defined]
        return "—"

    def _link_for(self, inst: Instance) -> str:
        if inst.kind is Kind.SERVER:
            p = inst.meta.linked_pool  # type: ignore[attr-defined]
            return f"← {p}" if p else "—"
        if inst.kind is Kind.POOL:
            t = inst.meta.target_server  # type: ignore[attr-defined]
            return f"→ {t}" if t else "—"
        return "—"

    # --- details pane ---

    def _selected(self) -> Instance | None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        return self.instances[table.cursor_row]

    def update_details(self) -> None:
        body = self.query_one("#details-body", Static)
        inst = self._selected()
        if inst is None:
            body.update("No instances yet. Press [b]n[/b] to create one.")
            return
        lines = [f"[b]{inst.name}[/b]   ([dim]{inst.kind.value}[/dim])", ""]
        for k, v in inst.detail_rows():
            lines.append(f"  [dim]{k:14s}[/dim] {v}")
        lines.append("")
        lines.append("[dim]Press [b]Enter[/b] for actions.[/dim]")
        body.update("\n".join(lines))

    def on_data_table_row_highlighted(self, _event: DataTable.RowHighlighted) -> None:
        self.update_details()

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        self.action_open_menu()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "update_tmsm":
            # Hide entirely until the background check confirms an update.
            return True if getattr(self.app, "update_available", False) else False
        return True

    # --- actions ---

    def action_refresh(self) -> None:
        self.refresh_instances()

    def action_start(self) -> None:
        inst = self._selected()
        if inst is None:
            return
        try:
            pid = inst.start()
            self.notify(f"Started {inst.name} (pid {pid})")
        except Exception as e:
            self.notify(f"Start failed: {e}", severity="error")
        self.refresh_instances()

    def action_stop(self) -> None:
        inst = self._selected()
        if inst is None:
            return
        if inst.stop():
            self.notify(f"Stopped {inst.name}")
        else:
            self.notify(f"{inst.name} was not running")
        self.refresh_instances()

    def action_restart(self) -> None:
        inst = self._selected()
        if inst is None:
            return
        try:
            pid = inst.restart()
            self.notify(f"Restarted {inst.name} (pid {pid})")
        except Exception as e:
            self.notify(f"Restart failed: {e}", severity="error")
        self.refresh_instances()

    def action_new(self) -> None:
        from .wizard import WizardScreen
        self.app.push_screen(WizardScreen(), lambda _: self.refresh_instances())

    def action_screens(self) -> None:
        from .screens_screen import ScreensScreen
        self.app.push_screen(ScreensScreen(), lambda _: self.refresh_instances())

    def action_stats(self) -> None:
        self.app.push_screen(StatsScreen())

    def action_ufw(self) -> None:
        from .ufw_screen import UfwScreen
        self.app.push_screen(UfwScreen())

    def action_systemctl(self) -> None:
        from .systemctl_screen import SystemctlScreen
        self.app.push_screen(SystemctlScreen())

    def action_addons(self) -> None:
        from .assets_screen import AssetsScreen
        self.app.push_screen(AssetsScreen())

    def action_tmsm_logs(self) -> None:
        from .tmsm_logs_screen import TmsmLogsScreen
        self.app.push_screen(TmsmLogsScreen())

    def action_diagnostics(self) -> None:
        from .diagnostics_screen import DiagnosticsScreen
        self.app.push_screen(DiagnosticsScreen(), lambda _: self.refresh_instances())

    def action_update_tmsm(self) -> None:
        from .install_screen import InstallScreen
        from .confirm import ConfirmScreen
        from .. import updater

        app = self.app

        def runner(log):
            updater.update_tmsm(log)
            app.restart_pending = True  # only set if update succeeded

        def after(_r) -> None:
            if getattr(app, "restart_pending", False):
                app.exit()

        def _launch() -> None:
            self.app.push_screen(
                InstallScreen(title="Updating tmsm from git", runner=runner),
                after,
            )

        dirty = updater.get_uncommitted_changes()
        if not dirty:
            _launch()
            return

        # Truncate long lists so the modal stays readable.
        lines = [ln for ln in dirty.splitlines() if ln.strip()]
        shown = lines[:20]
        more = len(lines) - len(shown)
        change_list = "\n".join(shown)
        if more > 0:
            change_list += f"\n... and {more} more"
        msg = (
            "The source checkout has local uncommitted changes "
            "(e.g. saved widget preset CSVs):\n\n"
            f"{change_list}\n\n"
            "Discard them and continue updating? "
            "This runs `git checkout -- .` and cannot be undone."
        )

        def _on_confirm(ok: bool | None) -> None:
            if not ok:
                return
            try:
                updater.discard_uncommitted_changes()
            except Exception as exc:
                self.notify(f"Discard failed: {exc}", severity="error")
                return
            _launch()

        self.app.push_screen(
            ConfirmScreen(
                msg,
                title="Discard local changes?",
                ok_label="Discard & update",
                cancel_label="Cancel",
                destructive=True,
            ),
            _on_confirm,
        )

    def action_open_menu(self) -> None:
        from .action_menu import ActionMenuScreen, MenuItem
        inst = self._selected()
        if inst is None:
            return
        running = inst.is_running
        is_db_target = inst.kind in (Kind.POOL, Kind.SERVICE) or (
            inst.kind is Kind.BOT and bool(getattr(getattr(inst, "meta", None), "db_name", ""))
        )
        is_server = inst.kind is Kind.SERVER
        is_bot = inst.kind is Kind.BOT
        items = [
            MenuItem("start",   "▶  Start",        enabled=not running),
            MenuItem("stop",    "■  Stop",         enabled=running),
            MenuItem("restart", "↻  Restart",      enabled=running),
            MenuItem("attach",  "⇆  Attach (screen)", enabled=running),
            MenuItem("logs",    "≡  View logs"),
            MenuItem("edit",    "✎  Edit config"),
            MenuItem("db_tool",       "⛁  Open DB tool",      enabled=is_db_target),
            MenuItem(
                "update",
                "⤓  Update bot (from zip / URL)" if is_bot else "⤓  Update server",
                enabled=(is_server or is_bot) and not running,
            ),
            MenuItem("add_map",       "＋  Add map (from Exchange)", enabled=is_server),
            MenuItem("open_location", "📂  Open location (mc)"),
            MenuItem("delete",        "✗  Delete",            enabled=not running),
        ]
        if inst.kind is Kind.POOL:
            on = self._auto_attach_pool == inst.name
            items.insert(
                4,
                MenuItem(
                    "auto_attach_toggle",
                    f"⟳  Auto Attach ({'ON' if on else 'OFF'})",
                    enabled=True,
                ),
            )
        title = f"{inst.name}  ({inst.kind.value})"

        def _dispatch(action: str | None) -> None:
            if not action:
                return
            handler = getattr(self, f"action_{action}", None)
            if handler:
                handler()

        self.app.push_screen(ActionMenuScreen(title, items), _dispatch)

    def action_auto_attach_toggle(self) -> None:
        inst = self._selected()
        if inst is None or inst.kind is not Kind.POOL:
            self.notify("Auto Attach is only available for PyPlanet pools.", severity="warning")
            return

        if self._auto_attach_pool == inst.name:
            self._auto_attach_pool = None
            self._auto_attach_armed.pop(inst.name, None)
            self.notify(f"Auto Attach OFF for {inst.name}", severity="information", timeout=5)
            return

        prev = self._auto_attach_pool
        if prev and prev != inst.name:
            self._auto_attach_armed.pop(prev, None)
        self._auto_attach_pool = inst.name
        st = inst.status()
        self._pool_state[inst.name] = (st.status is Status.RUNNING, st.pid)
        self._auto_attach_armed[inst.name] = False
        if prev and prev != inst.name:
            self.notify(
                f"Auto Attach moved from {prev} to {inst.name}",
                severity="information",
                timeout=6,
            )
        else:
            self.notify(f"Auto Attach ON for {inst.name}", severity="information", timeout=5)

    def _attach_instance(self, inst: Instance, *, automatic: bool, refresh_after: bool) -> None:
        import subprocess
        if not inst.is_running:
            self.notify(f"{inst.name} is not running.", severity="warning")
            return
        if inst.kind is Kind.SERVICE and inst.name == "mariadb":
            self.notify(
                "MariaDB runs as a background daemon — use the DB tool (g) "
                "or tail the error log instead.",
                severity="information", timeout=8,
            )
            return
        cmd = supervisor.attach_command(inst.name)
        with self.app.suspend():
            try:
                subprocess.run(cmd)
            except FileNotFoundError:
                pass
        # Keep auto-attach enabled after attach sessions return so it can keep
        # following future automatic restarts (e.g. restart-app dev reload).
        if refresh_after:
            self.refresh_instances()

    def action_edit(self) -> None:
        inst = self._selected()
        if inst is None:
            return
        files = inst.editable_files()
        if not files:
            self.notify(
                f"No editable config files found for {inst.name}.",
                severity="warning",
            )
            return
        from .edit_screen import EditScreen, FilePickerScreen

        def _open(path) -> None:
            if path is None:
                return
            label = next((lbl for lbl, p in files if p == path), path.name)
            self.app.push_screen(EditScreen(path, label=label))

        if len(files) == 1:
            _open(files[0][1])
        else:
            self.app.push_screen(
                FilePickerScreen(f"Edit config — {inst.name}", files),
                _open,
            )

    def action_logs(self) -> None:
        inst = self._selected()
        if inst is None:
            return
        from .log_screen import LogScreen
        from .edit_screen import FilePickerScreen

        primary = [(f"tmsm capture ({inst.log_file().name})", inst.log_file())]
        extras = inst.extra_log_files()
        choices = primary + extras

        def _open(path) -> None:
            if path is None:
                return
            label = next((lbl for lbl, p in choices if p == path), path.name)
            self.app.push_screen(LogScreen(path, title=f"{inst.name} — {label}"))

        if len(choices) == 1:
            _open(choices[0][1])
        else:
            self.app.push_screen(
                FilePickerScreen(f"View logs — {inst.name}", choices),
                _open,
            )

    def action_attach(self) -> None:
        inst = self._selected()
        if inst is None:
            return
        self._attach_instance(inst, automatic=False, refresh_after=True)

    def action_db_tool(self) -> None:
        inst = self._selected()
        is_bot_with_db = (
            inst is not None
            and inst.kind is Kind.BOT
            and bool(getattr(getattr(inst, "meta", None), "db_name", ""))
        )
        if inst is None or (inst.kind not in (Kind.POOL, Kind.SERVICE) and not is_bot_with_db):
            self.notify("Select a pool, the mariadb service, or a bot with a database.",
                        severity="warning")
            return
        from .. import dbtool
        err = dbtool.launch(inst, self.app)
        if err:
            self.notify(err, severity="error", timeout=10)
        self.refresh_instances()

    def action_open_location(self) -> None:
        import subprocess
        inst = self._selected()
        if inst is None:
            return
        with self.app.suspend():
            try:
                subprocess.run(["mc", "-S", "modarin256", str(inst.root)])
            except FileNotFoundError:
                pass
        self.refresh_instances()

    def action_update(self) -> None:
        from .install_screen import InstallScreen
        from ..installers import server as server_installer
        from ..installers import bot as bot_installer
        from .bot_update_screen import BotUpdateScreen
        inst = self._selected()
        if inst is None or inst.kind not in (Kind.SERVER, Kind.BOT):
            self.notify("Update is only available for game servers and Discord bots.",
                        severity="warning")
            return
        if inst.is_running:
            self.notify(f"Stop {inst.name} before updating.", severity="warning")
            return
        name = inst.name

        if inst.kind is Kind.BOT:
            def on_source(source: str | None, _n=name) -> None:
                if not source:
                    return
                title = f"Updating bot '{_n}'"

                def runner(log, _src=source):
                    bot_installer.update_bot(_n, _src, log)

                self.app.push_screen(
                    InstallScreen(title=title, runner=runner),
                    lambda _r: self.refresh_instances(),
                )

            self.app.push_screen(BotUpdateScreen(name), on_source)
            return

        title = f"Updating server '{name}'"

        def runner(log):
            server_installer.update_server(name, log)

        self.app.push_screen(
            InstallScreen(title=title, runner=runner),
            lambda _r: self.refresh_instances(),
        )

    def action_add_map(self) -> None:
        from pathlib import Path
        from .add_map_screen import AddMapScreen
        from .install_screen import InstallScreen
        from .. import maps as maps_mod

        inst = self._selected()
        if inst is None or inst.kind is not Kind.SERVER:
            self.notify("Add map is only available for game servers.", severity="warning")
            return

        server_inst = inst  # captured below; narrow type for the closure

        def on_prompt(result: tuple[str, Path] | None) -> None:
            if result is None:
                return
            raw_id, ms_path = result
            title = f"Adding map {raw_id} to '{server_inst.name}'"

            def runner(log):
                maps_mod.add_map_from_exchange(server_inst, raw_id, ms_path, log)

            self.app.push_screen(
                InstallScreen(title=title, runner=runner),
                lambda _r: self.refresh_instances(),
            )

        self.app.push_screen(AddMapScreen(inst), on_prompt)

    def action_delete(self) -> None:
        from .confirm import ConfirmScreen
        from .install_screen import InstallScreen
        from ..installers import bot as bot_installer
        from ..installers import pyplanet as pyplanet_installer
        from ..installers import server as server_installer
        from ..installers import mariadb as mariadb_installer

        inst = self._selected()
        if inst is None:
            return
        if inst.is_running:
            self.notify(f"Stop {inst.name} before deleting.", severity="warning")
            return
        if inst.kind is Kind.SERVICE and inst.name != "mariadb":
            self.notify(f"{inst.name} cannot be deleted.", severity="warning")
            return

        name = inst.name
        kind = inst.kind

        def confirmed(yes: bool | None) -> None:
            if not yes:
                return
            title = f"Deleting {kind.value} '{name}'"

            def runner(log):
                if kind is Kind.SERVER:
                    server_installer.delete_server(name, log)
                elif kind is Kind.POOL:
                    pyplanet_installer.delete_pool(name, log)
                elif kind is Kind.BOT:
                    bot_installer.delete_bot(name, log)
                elif kind is Kind.SERVICE and name == "mariadb":
                    mariadb_installer.delete_mariadb(log)

            self.app.push_screen(
                InstallScreen(title=title, runner=runner),
                lambda _r: self.refresh_instances(),
            )

            self.app.push_screen(
                InstallScreen(title=title, runner=runner),
                lambda _r: self.refresh_instances(),
            )

        if kind is Kind.SERVICE and name == "mariadb":
            msg = (
                f"Permanently delete MariaDB?\n"
                f"This removes the install, the entire datadir, and all pool databases.\n"
                f"PyPlanet pools will lose their data."
            )
        else:
            msg = (
                f"Permanently delete {kind.value} '{name}'?\n"
                f"This removes its directory and all data."
            )
        self.app.push_screen(
            ConfirmScreen(msg, title="Delete instance",
                          ok_label="Delete", cancel_label="Cancel",
                          destructive=True),
            confirmed,
        )
