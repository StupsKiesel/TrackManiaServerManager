"""New-instance wizard."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from .. import paths
from ..instances import discover_all
from ..instances.base import Kind
from ..instances.server import GameType
from ..installers import mariadb as mariadb_installer
from ..installers import pyplanet as pp_installer
from ..installers import server as server_installer
from .install_screen import InstallScreen


KIND_OPTIONS = [
    ("Trackmania 2020 server", "tm2020"),
    ("ManiaPlanet server", "maniaplanet"),
    ("PyPlanet (shared install)", "pyplanet"),
    ("PyPlanet pool", "pool"),
    ("MariaDB (portable, shared)", "mariadb"),
]


class ServerCredentialsScreen(ModalScreen[tuple[str, str] | None]):
    BINDINGS = [Binding("escape", "app.pop_screen", "Cancel")]

    DEFAULT_CSS = """
    ServerCredentialsScreen { align: center middle; }
    #creds-box {
        width: 78;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    Label { padding: 1 0 0 0; }
    Input { width: 100%; }
    #helper { color: $text-muted; }
    Horizontal { height: auto; padding-top: 1; align-horizontal: right; }
    Button { margin-left: 1; }
    """

    def __init__(self, server_name: str) -> None:
        super().__init__()
        self.server_name = server_name
        self.name_input = Input(
            placeholder="Trackmania server login",
            id="server-login",
        )
        self.password_input = Input(
            placeholder="Trackmania server password",
            password=True,
            id="server-password",
        )

    def compose(self) -> ComposeResult:
        with Container(id="creds-box"):
            yield Static(f"[b]Server credentials — {self.server_name}[/b]")
            yield Label(
                "Go to trackmania.com and create a server login and insert your server credentials here.",
                id="helper",
            )
            yield Label("Server login name:")
            yield self.name_input
            yield Label("Server login password:")
            yield self.password_input
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    def on_mount(self) -> None:
        self.name_input.focus()

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id == "save":
            self._submit()

    def _submit(self) -> None:
        login = self.name_input.value.strip()
        password = self.password_input.value
        if not login:
            self.app.notify("Server login is required.", severity="error")
            return
        self.dismiss((login, password))


def _xml_escape(value: str) -> str:
    out = str(value or "")
    out = out.replace("&", "&amp;")
    out = out.replace('"', "&quot;")
    out = out.replace("'", "&apos;")
    out = out.replace("<", "&lt;")
    out = out.replace(">", "&gt;")
    return out


def _write_masterserver_credentials(server_name: str, login: str, password: str) -> Path:
    cfg_path = paths.SERVERS_DIR / server_name / "server" / "UserData" / "Config" / "dedicated_cfg.txt"
    if not cfg_path.exists():
        raise FileNotFoundError(f"Missing dedicated config: {cfg_path}")

    text = cfg_path.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"(<masterserver_account>)(.*?)(</masterserver_account>)", text, flags=re.S)
    if not m:
        raise ValueError("Could not find <masterserver_account> block in dedicated_cfg.txt")

    block = m.group(2)
    login_esc = _xml_escape(login)
    password_esc = _xml_escape(password)

    block_new = re.sub(
        r"<login>\s*.*?\s*</login>",
        f"<login>{login_esc}</login>",
        block,
        count=1,
        flags=re.S,
    )
    block_new = re.sub(
        r"<password>\s*.*?\s*</password>",
        f"<password>{password_esc}</password>",
        block_new,
        count=1,
        flags=re.S,
    )

    updated = text[: m.start(2)] + block_new + text[m.end(2):]
    cfg_path.write_text(updated, encoding="utf-8")
    return cfg_path


class WizardScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "app.pop_screen", "Cancel")]

    DEFAULT_CSS = """
    WizardScreen { align: center middle; }
    #wizard-box {
        width: 70;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    Label { padding: 1 0 0 0; }
    Input, Select { width: 100%; }
    Horizontal { height: auto; padding-top: 1; align-horizontal: right; }
    Button { margin-left: 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._launched = False
        self.kind_select: Select = Select(
            options=KIND_OPTIONS,
            id="kind",
            allow_blank=False,
            value="tm2020",
        )
        self.name_input = Input(placeholder="name (lowercase, [a-z0-9_-])", id="name")
        self.target_select: Select = Select(options=[], id="target", allow_blank=True,
                                            prompt="(no servers yet)")

    def compose(self) -> ComposeResult:
        with Container(id="wizard-box"):
            yield Static("[b]New instance[/b]")
            yield Label("Type:")
            yield self.kind_select
            yield Label("Name:")
            yield self.name_input
            yield Label("Attach to server (pool only):")
            yield self.target_select
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Create", id="create", variant="primary")

    def on_mount(self) -> None:
        self._refresh_targets()
        self._update_visibility()

    def _refresh_targets(self) -> None:
        servers = [i for i in discover_all(self.app.cfg) if i.kind is Kind.SERVER]  # type: ignore[attr-defined]
        if servers:
            self.target_select.set_options([(s.name, s.name) for s in servers])
        else:
            self.target_select.set_options([])

    def _update_visibility(self) -> None:
        kind = self.kind_select.value
        self.target_select.display = (kind == "pool")
        self.name_input.display = kind not in ("pyplanet", "mariadb")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select is self.kind_select:
            self._update_visibility()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "create":
            self._submit()

    # --- submit logic ---

    def _submit(self) -> None:
        kind = str(self.kind_select.value)

        if kind == "pyplanet":
            self._launch("Install PyPlanet (shared)", pp_installer.install_pyplanet)
            return

        if kind == "mariadb":
            self._launch("Install MariaDB (portable)", mariadb_installer.install_mariadb)
            return

        name = (self.name_input.value or "").strip()
        if not name or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            self.app.notify("Name must match [a-z0-9_-] and start alphanumeric",
                            severity="error")
            return

        if kind in ("tm2020", "maniaplanet"):
            game = GameType.TM2020 if kind == "tm2020" else GameType.MANIAPLANET

            def runner(log, _n=name, _g=game):
                server_installer.install_server(_n, _g, log)

            wizard = self
            app = self.app

            def after_install(ok: bool) -> None:
                cfg_candidate = (
                    paths.SERVERS_DIR
                    / name
                    / "server"
                    / "UserData"
                    / "Config"
                    / "dedicated_cfg.txt"
                )
                if not ok and not cfg_candidate.exists():
                    app.notify(
                        "Install failed; skipping credentials prompt.",
                        severity="error",
                    )
                    wizard.dismiss(None)
                    return
                if not ok and cfg_candidate.exists():
                    app.notify(
                        "Install reported an error, but server files exist. You can still save credentials.",
                        severity="warning",
                    )

                def on_creds(res: tuple[str, str] | None) -> None:
                    try:
                        if res is None:
                            app.notify(
                                "Server installed. Credentials were not saved.",
                                severity="warning",
                            )
                        else:
                            login_name, login_password = res
                            try:
                                cfg_path = _write_masterserver_credentials(
                                    name, login_name, login_password
                                )
                                app.notify(
                                    f"Saved server credentials in {cfg_path}",
                                    severity="information",
                                )
                            except Exception as e:
                                app.notify(
                                    f"Failed to write credentials: {e}",
                                    severity="error",
                                )
                    finally:
                        wizard.dismiss(None)

                app.push_screen(ServerCredentialsScreen(name), on_creds)

            self._launch(f"Install {kind} server '{name}'", runner, after_install=after_install)
            return

        if kind == "pool":
            target = self.target_select.value
            if not target or target is Select.BLANK:
                self.app.notify("Pick a target server first.", severity="error")
                return
            try:
                xmlrpc_port, super_pw = _read_target_server(str(target))
            except Exception as e:
                self.app.notify(f"Could not read target server: {e}", severity="error")
                return
            def runner(log, _n=name, _t=str(target), _pw=super_pw, _p=xmlrpc_port):
                pp_installer.create_pool(_n, _t, _pw, _p, log)
            self._launch(f"Create PyPlanet pool '{name}'", runner)
            return

    def _launch(self, title: str, runner, *, after_install=None) -> None:
        if self._launched:
            return
        self._launched = True

        wizard = self
        app = self.app

        def on_install_done(result) -> None:
            ok = bool(result)
            if callable(after_install):
                try:
                    after_install(ok)
                    return
                except Exception as e:
                    app.notify(f"Post-install step failed: {e}", severity="error")
            wizard.dismiss(None)

        # Keep the wizard alive as the host modal; push install screen on top.
        # This avoids races where dismissing the wizard first drops the next
        # screen in the stack lifecycle.
        app.push_screen(InstallScreen(title, runner), on_install_done)


def _read_target_server(name: str) -> tuple[int, str]:
    root = paths.SERVERS_DIR / name
    with (root / "instance.toml").open("rb") as f:
        data = tomllib.load(f)
    xmlrpc_port = int(data.get("xmlrpc_port", 5000))
    super_pw = ""
    ded = root / "server" / "UserData" / "Config" / "dedicated_cfg.txt"
    if ded.exists():
        text = ded.read_text(errors="ignore")
        m = re.search(r"<name>\s*SuperAdmin\s*</name>\s*<password>([^<]*)</password>", text)
        if m:
            super_pw = m.group(1)
    return xmlrpc_port, super_pw
