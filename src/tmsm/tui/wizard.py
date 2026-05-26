"""New-instance wizard."""
from __future__ import annotations

import re
import tomllib

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
            self._launch(f"Install {kind} server '{name}'", runner)
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

    def _launch(self, title: str, runner) -> None:
        if self._launched:
            return
        self._launched = True
        screen = InstallScreen(title, runner)
        # Dismiss the wizard first so we don't dismiss a non-top screen,
        # then push the install screen on the next frame.
        self.dismiss(None)
        self.app.call_after_refresh(self.app.push_screen, screen)


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
