"""UFW firewall rules viewer / editor."""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Select, Static, Switch

from .sudo_helper import SudoModal, sudo_cached, sudo_run


# ── helpers ───────────────────────────────────────────────────────────────────

def _find_ufw() -> str | None:
    """Return the full path to ufw, or None if not installed."""
    return shutil.which("ufw", path="/usr/sbin:/usr/bin:/sbin:/bin")


def _ufw(*args: str, password: str | None = None) -> subprocess.CompletedProcess:
    binary = _find_ufw()
    if binary is None:
        raise FileNotFoundError("ufw not found")
    return sudo_run(binary, *args, password=password)


@dataclass
class UfwRule:
    number: int
    to: str
    action: str
    from_: str
    v6: bool


def _fetch_status(password: str | None = None) -> tuple[str, list[UfwRule]]:
    """Return (status_line, rules).  Requires sudo."""
    r = _ufw("status", "numbered", "verbose", password=password)
    raw = r.stdout + r.stderr
    status = "unknown"
    rules: list[UfwRule] = []

    for line in raw.splitlines():
        if line.startswith("Status:"):
            status = line.split(":", 1)[1].strip()
        # numbered rule  e.g.  [ 1] 22/tcp                     ALLOW IN    Anywhere
        m = re.match(r"\[\s*(\d+)\]\s+(.+?)\s+(ALLOW|DENY|REJECT|LIMIT)\s*(IN|OUT|FWD)?\s+(.*)", line)
        if m:
            num = int(m.group(1))
            to = m.group(2).strip()
            action = (m.group(3) + (" " + m.group(4) if m.group(4) else "")).strip()
            from_ = m.group(5).strip() or "Anywhere"
            v6 = "(v6)" in to or "(v6)" in from_
            rules.append(UfwRule(num, to.replace(" (v6)", ""), action, from_.replace(" (v6)", ""), v6))

    return status, rules


# ── add-rule modal ────────────────────────────────────────────────────────────

def _build_rule(action: str, direction: str, port: str, proto: str,
                from_ip: str, to_ip: str) -> list[str]:
    """Build the ufw argument list from form fields."""
    parts: list[str] = [action]
    if direction != "any":
        parts.append(direction)

    has_from = bool(from_ip) and from_ip.lower() != "anywhere"
    has_to   = bool(to_ip)   and to_ip.lower()   != "anywhere"
    has_port = bool(port)

    if has_from or has_to:
        parts += ["from", from_ip if has_from else "any"]
        parts += ["to",   to_ip   if has_to   else "any"]
        if has_port:
            parts += ["port", port]
        if proto != "any":
            parts += ["proto", proto]
    elif has_port:
        spec = f"{port}/{proto}" if proto != "any" else port
        parts.append(spec)

    return parts


class AddRuleModal(ModalScreen[list[str] | None]):
    """Structured form for adding a UFW rule."""

    DEFAULT_CSS = """
    AddRuleModal { align: center middle; }
    #add-box {
        width: 74;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    .add-row {
        height: auto;
        margin-bottom: 1;
    }
    .add-label {
        width: 12;
        padding-top: 1;
        text-align: right;
        padding-right: 1;
    }
    .add-field { width: 1fr; }
    #add-preview {
        margin-top: 1;
        padding: 0 1;
        color: $text-muted;
    }
    #add-buttons { margin-top: 1; align-horizontal: right; }
    Button { margin-left: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal
        with Container(id="add-box"):
            yield Label("[b]Add UFW Rule[/b]")

            with Horizontal(classes="add-row"):
                yield Label("Action", classes="add-label")
                yield Select(
                    [("Allow", "allow"), ("Deny", "deny"),
                     ("Reject", "reject"), ("Limit", "limit")],
                    value="allow", id="sel-action", classes="add-field",
                )

            with Horizontal(classes="add-row"):
                yield Label("Direction", classes="add-label")
                yield Select(
                    [("In (default)", "in"), ("Out", "out"), ("Both", "any")],
                    value="in", id="sel-direction", classes="add-field",
                )

            with Horizontal(classes="add-row"):
                yield Label("Port", classes="add-label")
                yield Input(
                    placeholder="e.g. 22  80:90  ssh  (leave blank for all)",
                    id="inp-port", classes="add-field",
                )

            with Horizontal(classes="add-row"):
                yield Label("Protocol", classes="add-label")
                yield Select(
                    [("Any", "any"), ("TCP", "tcp"), ("UDP", "udp")],
                    value="any", id="sel-proto", classes="add-field",
                )

            with Horizontal(classes="add-row"):
                yield Label("From IP", classes="add-label")
                yield Input(
                    placeholder="Anywhere  /  192.168.1.0/24  /  10.0.0.5",
                    id="inp-from", classes="add-field",
                )

            with Horizontal(classes="add-row"):
                yield Label("To IP", classes="add-label")
                yield Input(
                    placeholder="Anywhere  (leave blank = any)",
                    id="inp-to", classes="add-field",
                )

            yield Static("", id="add-preview")

            with Container(id="add-buttons"):
                yield Button("Add rule", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#inp-port", Input).focus()
        self._refresh_preview()

    # update preview whenever anything changes
    def on_select_changed(self, _: Select.Changed) -> None:
        self._refresh_preview()

    def on_input_changed(self, _: Input.Changed) -> None:
        self._refresh_preview()

    def _fields(self) -> tuple[str, str, str, str, str, str]:
        action    = str(self.query_one("#sel-action",    Select).value)
        direction = str(self.query_one("#sel-direction", Select).value)
        port      = self.query_one("#inp-port", Input).value.strip()
        proto     = str(self.query_one("#sel-proto",     Select).value)
        from_ip   = self.query_one("#inp-from",  Input).value.strip()
        to_ip     = self.query_one("#inp-to",    Input).value.strip()
        return action, direction, port, proto, from_ip, to_ip

    def _refresh_preview(self) -> None:
        parts = _build_rule(*self._fields())
        cmd = "sudo ufw " + " ".join(parts)
        self.query_one("#add-preview", Static).update(
            f"[dim]Preview:[/dim] [italic]{cmd}[/italic]"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._submit()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        parts = _build_rule(*self._fields())
        # must have at least one meaningful argument beyond the action
        if len(parts) < 2:
            self.query_one("#add-preview", Static).update(
                "[red]Specify at least a port, source IP, or direction.[/red]"
            )
            return
        self.dismiss(parts)


# ── toggle modal ──────────────────────────────────────────────────────────────

class UfwToggleModal(ModalScreen[tuple[bool, bool]]):
    """Dedicated enable/disable UFW confirmation dialog.
    Dismisses with (confirmed, add_ssh_rule).
    """

    DEFAULT_CSS = """
    UfwToggleModal { align: center middle; }

    #toggle-box {
        width: 56;
        height: auto;
        border: heavy $warning;
        background: $surface;
        padding: 0;
    }

    #toggle-header {
        height: auto;
        padding: 1 2;
        align: center middle;
    }
    #toggle-icon  { text-align: center; width: 1fr; }
    #toggle-title { text-align: center; width: 1fr; }

    #toggle-body { padding: 1 2; height: auto; }

    #toggle-state-row {
        height: auto;
        margin-bottom: 1;
        align: center middle;
    }
    .state-box {
        width: 1fr;
        height: 3;
        border: round $panel-lighten-2;
        align: center middle;
        text-align: center;
    }
    #state-arrow { width: 5; text-align: center; padding-top: 1; }

    #toggle-impact {
        margin-top: 1;
        padding: 0 1;
        color: $text-muted;
    }

    #ssh-row {
        height: auto;
        margin-top: 1;
        padding: 1 1;
        border: round $success 40%;
        align: center middle;
    }
    #ssh-label { width: 1fr; padding-left: 1; }

    #toggle-buttons {
        align-horizontal: center;
        height: auto;
        margin-top: 1;
        padding-bottom: 1;
    }
    #toggle-buttons Button { min-width: 16; margin: 0 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("y", "confirm", "Confirm"),
    ]

    def __init__(self, current_status: str) -> None:
        super().__init__()
        self._enabling = current_status != "active"

    def compose(self) -> ComposeResult:
        enabling = self._enabling
        icon        = "🛡️" if enabling else "⚠️"
        title       = "Enable UFW Firewall" if enabling else "Disable UFW Firewall"
        btn_variant = "success" if enabling else "warning"
        btn_label   = "Enable" if enabling else "Disable"
        from_label  = "[red]inactive[/red]" if enabling else "[green]active[/green]"
        to_label    = "[green]active[/green]" if enabling else "[red]inactive[/red]"

        if enabling:
            impact = (
                "[dim]Firewall will [green]block[/green] all traffic "
                "not matching a rule.[/dim]"
            )
        else:
            impact = (
                "[dim]All firewall rules will be [red]suspended[/red].\n"
                "The system will accept all incoming connections.[/dim]"
            )

        with Vertical(id="toggle-box"):
            with Vertical(id="toggle-header"):
                yield Static(icon,  id="toggle-icon")
                yield Static(f"[bold]{title}[/bold]", id="toggle-title")

            with Vertical(id="toggle-body"):
                with Horizontal(id="toggle-state-row"):
                    yield Static(f"Current\n{from_label}", classes="state-box")
                    yield Static("→", id="state-arrow")
                    yield Static(f"After\n{to_label}", classes="state-box")

                yield Static(impact, id="toggle-impact")

                if enabling:
                    with Horizontal(id="ssh-row"):
                        yield Switch(value=True, id="ssh-switch")
                        yield Static(
                            "  🔑 [bold]Allow SSH (port 22/tcp)[/bold] before enabling\n"
                            "  [dim]Recommended — prevents locking yourself out[/dim]",
                            id="ssh-label",
                        )

            with Horizontal(id="toggle-buttons"):
                yield Button(btn_label, id="ok", variant=btn_variant)
                yield Button("Cancel",  id="cancel", variant="default")

    def _ssh_checked(self) -> bool:
        try:
            return self.query_one("#ssh-switch", Switch).value
        except Exception:
            return False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self.dismiss((True, self._ssh_checked()))
        else:
            self.dismiss((False, False))

    def action_cancel(self) -> None:
        self.dismiss((False, False))

    def action_confirm(self) -> None:
        self.dismiss((True, self._ssh_checked()))


# ── main screen ───────────────────────────────────────────────────────────────

class UfwScreen(Screen):
    """View and manage UFW firewall rules."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q",      "app.pop_screen", "Back"),
        Binding("R",      "refresh",        "Refresh"),
        Binding("a",      "add_rule",       "Add rule"),
        Binding("d",      "delete_rule",    "Delete rule"),
        Binding("delete", "delete_rule",    "Delete rule", show=False),
        Binding("t",      "toggle_ufw",     "Enable/Disable UFW"),
    ]

    DEFAULT_CSS = """
    UfwScreen #ufw-wrap { padding: 0 1; }
    UfwScreen #ufw-status { padding: 1 1 0 1; }
    UfwScreen #ufw-help {
        padding: 0 1 1 1;
        color: $text-muted;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._status = "unknown"
        self._rules: list[UfwRule] = []
        self._password: str | None = None  # cached for session

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="ufw-status")
        with Container(id="ufw-wrap"):
            yield DataTable(id="ufw-table", cursor_type="row", zebra_stripes=True)
        yield Static(
            "a = add rule   d/Del = delete rule   t = toggle UFW   R = refresh   Esc = back",
            id="ufw-help",
        )
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one(DataTable)
        t.add_columns("#", "To / Port", "Action", "From", "IPv6")
        self.call_after_refresh(self._check_and_start)

    def _check_and_start(self) -> None:
        if _find_ufw() is None:
            self.query_one("#ufw-status", Static).update(
                " [bold red]ufw is not installed.[/bold red]\n"
                " Install it with:  [bold]sudo apt install ufw[/bold]"
            )
            return
        self._ensure_sudo()

    def _ensure_sudo(self) -> None:
        """Check sudo access; prompt for password in-TUI if needed."""
        # Prefer app-level cached password (shared across screens)
        cached_pw: str | None = getattr(self.app, "sudo_password", None)
        if cached_pw is not None:
            self._password = cached_pw
            self.refresh_rules()
            self.set_interval(5.0, self.refresh_rules)
            return
        if sudo_cached():
            self.refresh_rules()
            self.set_interval(5.0, self.refresh_rules)
            return
        # Need to prompt
        def _got(pw: str | None) -> None:
            if pw is None:
                self.app.pop_screen()
                return
            self._password = pw
            self.app.sudo_password = pw  # type: ignore[attr-defined]
            self.refresh_rules()
            self.set_interval(5.0, self.refresh_rules)

        self.app.push_screen(SudoModal(), _got)

    def refresh_rules(self) -> None:
        try:
            self._status, self._rules = _fetch_status(self._password)
        except FileNotFoundError:
            self.query_one("#ufw-status", Static).update(
                " [bold red]ufw is not installed.[/bold red]  "
                "Install with: [bold]sudo apt install ufw[/bold]"
            )
            return
        c = "green" if self._status == "active" else "red"
        self.query_one("#ufw-status", Static).update(
            f" UFW status: [{c}][b]{self._status}[/b][/{c}]"
        )
        t = self.query_one(DataTable)
        prev = t.cursor_row if t.row_count else 0
        t.clear()
        for r in self._rules:
            t.add_row(
                str(r.number),
                r.to,
                r.action,
                r.from_,
                "✓" if r.v6 else "",
            )
        if t.row_count:
            t.move_cursor(row=min(prev, t.row_count - 1))

    def _selected_rule(self) -> UfwRule | None:
        t = self.query_one(DataTable)
        if not t.row_count:
            return None
        return self._rules[t.cursor_row]

    def action_refresh(self) -> None:
        self.refresh_rules()

    def action_add_rule(self) -> None:
        def _apply(parts: list[str] | None) -> None:
            if not parts:
                return
            r = _ufw(*parts, password=self._password)
            if r.returncode == 0:
                self.notify("Rule added: ufw " + " ".join(parts))
            else:
                self.notify((r.stderr or r.stdout).strip(), severity="error", timeout=10)
            self.refresh_rules()

        self.app.push_screen(AddRuleModal(), _apply)

    def action_delete_rule(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            return
        from .confirm import ConfirmScreen

        def _do(confirmed: bool) -> None:
            if not confirmed:
                return
            r = _ufw("--force", "delete", str(rule.number), password=self._password)
            if r.returncode == 0:
                self.notify(f"Deleted rule #{rule.number}")
            else:
                self.notify((r.stderr or r.stdout).strip(), severity="error", timeout=10)
            self.refresh_rules()

        self.app.push_screen(
            ConfirmScreen(
                f"Delete rule #{rule.number}: {rule.action} {rule.to} from {rule.from_}?",
                title="Delete UFW rule",
                ok_label="Delete",
                destructive=True,
            ),
            _do,
        )

    def action_toggle_ufw(self) -> None:
        target = "disable" if self._status == "active" else "enable"

        def _do(result: tuple[bool, bool]) -> None:
            confirmed, add_ssh = result
            if not confirmed:
                return
            if add_ssh:
                r = _ufw("allow", "22/tcp", password=self._password)
                if r.returncode == 0:
                    self.notify("SSH rule added (22/tcp allowed)")
                else:
                    self.notify(
                        "Failed to add SSH rule: " + (r.stderr or r.stdout).strip(),
                        severity="error", timeout=10,
                    )
                    return  # abort enable if SSH rule failed
            r = _ufw("--force", target, password=self._password)
            if r.returncode == 0:
                self.notify(f"UFW {target}d")
            else:
                self.notify((r.stderr or r.stdout).strip(), severity="error", timeout=10)
            self.refresh_rules()

        self.app.push_screen(UfwToggleModal(self._status), _do)
