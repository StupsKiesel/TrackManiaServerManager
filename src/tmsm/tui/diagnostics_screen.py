"""Diagnostics screen — auto-detects common server problems and offers one-click fixes."""
from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import psutil

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Label, Static

from .. import paths, supervisor
from .. import wsl_host
from ..config import Config
from ..instances import Instance, Kind, discover_all
from ..instances.pool import PyPlanetPoolInstance
from ..instances.server import GameServerInstance


# ── result model ──────────────────────────────────────────────────────────────

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"

STATUS_DOT = {
    STATUS_OK:   "[green]●[/green]",
    STATUS_WARN: "[yellow]●[/yellow]",
    STATUS_FAIL: "[red]●[/red]",
    STATUS_SKIP: "[grey50]○[/grey50]",
}
STATUS_RANK = {STATUS_FAIL: 0, STATUS_WARN: 1, STATUS_OK: 2, STATUS_SKIP: 3}


@dataclass
class CheckResult:
    id: str
    title: str
    status: str
    summary: str                                # one-line shown in table
    detail: str = ""                            # multi-line, shown in details pane
    fix_label: str = ""                         # button text; empty = no fix
    fix_confirm_title: str = "Apply fix?"
    fix_confirm_body: str = ""                  # full text shown in confirm modal
    fix: Callable[[], tuple[bool, str]] | None = None   # returns (success, message)


# ── helpers ───────────────────────────────────────────────────────────────────

def _port_owner(port: int, proto: str) -> int | None:
    """Return PID listening on (port, proto) or None. proto is 'tcp' or 'udp'."""
    try:
        conns = psutil.net_connections(kind=proto)
    except (psutil.AccessDenied, PermissionError):
        return None
    for c in conns:
        if not c.laddr or c.laddr.port != port:
            continue
        if proto == "tcp" and c.status != psutil.CONN_LISTEN:
            continue
        if c.pid:
            return c.pid
    return None


def _proc_info_line(pid: int) -> str:
    try:
        p = psutil.Process(pid)
        with p.oneshot():
            name = p.name()
            cmd = " ".join(p.cmdline())[:120]
            try:
                cwd = p.cwd()
            except (psutil.AccessDenied, FileNotFoundError):
                cwd = "?"
            started = datetime.fromtimestamp(p.create_time()).strftime("%Y-%m-%d %H:%M")
            mem = p.memory_info().rss / (1024 * 1024)
            user = p.username()
        return (f"  PID {pid}  ({name}, user {user})\n"
                f"    started {started}   mem {mem:.0f} MB\n"
                f"    cwd     {cwd}\n"
                f"    cmdline {cmd}")
    except psutil.NoSuchProcess:
        return f"  PID {pid}  (already gone)"


def _kill_pids(pids: list[int], grace: float = 5.0) -> tuple[int, list[str]]:
    """SIGTERM then SIGKILL. Returns (killed_count, error_lines)."""
    errors: list[str] = []
    alive: list[int] = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            alive.append(pid)
        except ProcessLookupError:
            pass
        except PermissionError as e:
            errors.append(f"PID {pid}: {e}")
    deadline = time.monotonic() + grace
    while alive and time.monotonic() < deadline:
        alive = [p for p in alive if psutil.pid_exists(p)]
        if not alive:
            break
        time.sleep(0.2)
    for pid in alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as e:
            errors.append(f"PID {pid}: {e}")
    killed = sum(1 for p in pids if not psutil.pid_exists(p))
    return killed, errors


def _read_pool_port(pool: PyPlanetPoolInstance) -> int | None:
    base = pool.root / "settings" / "base.py"
    try:
        text = base.read_text(errors="replace")
    except OSError:
        return None
    m = re.search(r'["\']PORT["\']\s*:\s*(\d+)', text)
    return int(m.group(1)) if m else None


# ── checks ────────────────────────────────────────────────────────────────────

def check_stale_screen_sockets() -> CheckResult:
    try:
        out = subprocess.run(
            ["screen", "-ls"], capture_output=True, text=True, check=False,
            env={**os.environ, "SCREENDIR": str(paths.SCREEN_DIR)},
        ).stdout
    except FileNotFoundError:
        return CheckResult("stale_screen", "Stale screen sockets", STATUS_SKIP,
                           "screen not installed")
    dead = [ln.strip() for ln in out.splitlines() if "Dead" in ln or "(Removed)" in ln]
    if not dead:
        return CheckResult("stale_screen", "Stale screen sockets", STATUS_OK,
                           "none found")

    def fix() -> tuple[bool, str]:
        subprocess.run(
            ["screen", "-wipe"], capture_output=True, text=True, check=False,
            env={**os.environ, "SCREENDIR": str(paths.SCREEN_DIR)},
        )
        return True, f"Wiped {len(dead)} stale socket(s)."

    detail = "Dead screen sockets keep instances showing as RUNNING after a reboot.\n\n" + "\n".join(dead)
    return CheckResult(
        "stale_screen", "Stale screen sockets", STATUS_WARN,
        f"{len(dead)} dead", detail=detail,
        fix_label="Wipe stale sockets",
        fix_confirm_title="Run `screen -wipe`?",
        fix_confirm_body="This removes the listed dead screen sockets.\nNo running processes are affected.\n\n" + "\n".join(dead),
        fix=fix,
    )


def _managed_inner_pids() -> dict[str, int]:
    """{instance_name: inner_pid} for currently-running tmsm-managed sessions."""
    out: dict[str, int] = {}
    for s in supervisor.list_all_sessions():
        if s.managed and s.inst_name and s.inner_pid:
            out[s.inst_name] = s.inner_pid
    return out


def check_orphan_trackmania(managed: dict[str, int]) -> CheckResult:
    """TrackmaniaServer processes that aren't the inner PID of any tmsm-managed session."""
    managed_pids = set(managed.values())
    orphans: list[int] = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            nm = p.info["name"] or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "TrackmaniaServer" not in nm:
            continue
        pid = p.info["pid"]
        if pid in managed_pids:
            continue
        # Also skip if it's a child of a managed screen wrapper that just hasn't been
        # picked up as inner yet (rare race, but harmless to skip).
        try:
            parent = p.parent()
            if parent and parent.pid in managed_pids:
                continue
        except psutil.NoSuchProcess:
            continue
        orphans.append(pid)

    if not orphans:
        return CheckResult("orphan_tm", "Orphan TrackmaniaServer processes", STATUS_OK,
                           "none — all running gameservers are tmsm-managed")

    info_text = "\n\n".join(_proc_info_line(pid) for pid in orphans)
    body = ("These TrackmaniaServer processes are NOT managed by tmsm.\n"
            "They may be left over from manual launches, crashes, or earlier\n"
            "tmsm versions. While they run, the *real* tmsm gameserver may be\n"
            "unable to bind its game/XMLRPC ports — and PyPlanet may attach\n"
            "to the wrong one.\n\n"
            "Review each PID below before killing:\n\n" + info_text)

    def fix() -> tuple[bool, str]:
        killed, errors = _kill_pids(orphans)
        msg = f"Terminated {killed}/{len(orphans)} orphan process(es)."
        if errors:
            msg += "\nErrors:\n" + "\n".join(errors)
        return killed == len(orphans) and not errors, msg

    return CheckResult(
        "orphan_tm", "Orphan TrackmaniaServer processes", STATUS_FAIL,
        f"{len(orphans)} found", detail=body,
        fix_label=f"Kill {len(orphans)} orphan process(es)",
        fix_confirm_title="Kill orphan TrackmaniaServer processes?",
        fix_confirm_body=body,
        fix=fix,
    )


def check_port_alignment(cfg: Config, instances: list[Instance],
                         managed: dict[str, int]) -> list[CheckResult]:
    """For each tmsm server: verify the configured game/xmlrpc ports are
    actually owned by the tmsm-managed PID (not a ghost)."""
    results: list[CheckResult] = []
    for inst in instances:
        if not isinstance(inst, GameServerInstance):
            continue
        expected_pid = managed.get(inst.name)
        if expected_pid is None:
            # Server not running — nothing to verify.
            continue

        gp = inst.meta.game_port
        xp = inst.meta.xmlrpc_port
        owner_udp = _port_owner(gp, "udp")
        owner_tcp = _port_owner(xp, "tcp")

        mismatch: list[str] = []
        if owner_udp not in (None, expected_pid):
            mismatch.append(f"UDP {gp} (game port) is owned by PID {owner_udp}, not {expected_pid}.")
        if owner_tcp not in (None, expected_pid):
            mismatch.append(f"TCP {xp} (XML-RPC) is owned by PID {owner_tcp}, not {expected_pid}.")

        if not mismatch:
            results.append(CheckResult(
                f"port_{inst.name}", f"Ports for server '{inst.name}'", STATUS_OK,
                f"UDP {gp} + TCP {xp} owned by PID {expected_pid}",
            ))
            continue

        owners_info: list[str] = []
        for pid in {p for p in (owner_udp, owner_tcp) if p and p != expected_pid}:
            owners_info.append(_proc_info_line(pid))

        body = (f"The tmsm-managed gameserver '{inst.name}' is PID {expected_pid},\n"
                f"but its configured ports are being held by other processes:\n\n"
                + "\n".join(mismatch)
                + "\n\nForeign port owners:\n\n"
                + ("\n\n".join(owners_info) if owners_info else "(no detail)"))

        results.append(CheckResult(
            f"port_{inst.name}", f"Ports for server '{inst.name}'", STATUS_FAIL,
            "owned by foreign PID(s)", detail=body,
            # No automatic fix here — use the orphan-killer above for safety.
        ))
    return results


def check_pool_alignment(instances: list[Instance]) -> list[CheckResult]:
    """For each pool: verify settings/base.py PORT matches the linked server's xmlrpc_port."""
    by_name: dict[str, GameServerInstance] = {
        i.name: i for i in instances if isinstance(i, GameServerInstance)
    }
    results: list[CheckResult] = []
    for inst in instances:
        if not isinstance(inst, PyPlanetPoolInstance):
            continue
        target = inst.meta.target_server
        if not target:
            results.append(CheckResult(
                f"pool_{inst.name}", f"Pool '{inst.name}' link", STATUS_WARN,
                "no target server linked",
            ))
            continue
        srv = by_name.get(target)
        if srv is None:
            results.append(CheckResult(
                f"pool_{inst.name}", f"Pool '{inst.name}' link", STATUS_FAIL,
                f"target server '{target}' not found",
            ))
            continue
        pool_port = _read_pool_port(inst)
        if pool_port is None:
            results.append(CheckResult(
                f"pool_{inst.name}", f"Pool '{inst.name}' link", STATUS_WARN,
                "could not parse PORT from settings/base.py",
            ))
            continue
        if pool_port != srv.meta.xmlrpc_port:
            body = (f"Pool '{inst.name}' settings/base.py points at XML-RPC port {pool_port},\n"
                    f"but its linked server '{target}' is configured for {srv.meta.xmlrpc_port}.\n\n"
                    f"This is the classic 'PyPlanet talks to a ghost server' setup —\n"
                    f"either the server's port was changed and the pool was not regenerated,\n"
                    f"or the link is wrong.")

            def fix(pool=inst, srv=srv) -> tuple[bool, str]:
                base = pool.root / "settings" / "base.py"
                text = base.read_text()
                new = re.sub(
                    r'(["\']PORT["\']\s*:\s*)\d+',
                    rf'\g<1>{srv.meta.xmlrpc_port}',
                    text, count=1,
                )
                base.write_text(new)
                return True, f"Updated pool '{pool.name}' PORT → {srv.meta.xmlrpc_port}. Restart the pool to apply."

            results.append(CheckResult(
                f"pool_{inst.name}", f"Pool '{inst.name}' link", STATUS_FAIL,
                f"PORT {pool_port} ≠ server {srv.meta.xmlrpc_port}", detail=body,
                fix_label=f"Realign pool PORT → {srv.meta.xmlrpc_port}",
                fix_confirm_title="Rewrite pool settings/base.py?",
                fix_confirm_body=body + "\n\nThis rewrites only the PORT entry. Restart the pool afterwards.",
                fix=fix,
            ))
        else:
            results.append(CheckResult(
                f"pool_{inst.name}", f"Pool '{inst.name}' link", STATUS_OK,
                f"PORT {pool_port} matches server '{target}'",
            ))
    return results


def check_mariadb(cfg: Config) -> CheckResult:
    host, port = cfg.mariadb.host, cfg.mariadb.port
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return CheckResult("mariadb", "MariaDB reachable", STATUS_OK,
                               f"{host}:{port} accepting connections")
    except OSError as e:
        return CheckResult(
            "mariadb", "MariaDB reachable", STATUS_FAIL,
            f"cannot connect to {host}:{port}",
            detail=f"socket error: {e}\n\nStart the mariadb service from the main screen.",
        )


def check_wsl_host_forwarding(instances: list[Instance],
                              managed: dict[str, int]) -> list[CheckResult]:
    """If running inside WSL2, verify that the Windows host has the TCP
    portproxy, firewall rules, and UDP relay needed for each running
    gameserver. Each missing piece becomes its own fixable check."""
    results: list[CheckResult] = []
    if not wsl_host.is_wsl():
        return results

    host = wsl_host.windows_host_info()
    ip = wsl_host.wsl_ip()
    if not host.available:
        results.append(CheckResult(
            "wsl_host", "WSL ↔ Windows interop", STATUS_WARN,
            "Windows interop unreachable (cmd.exe not on PATH)",
            detail="tmsm is running under WSL but cannot call back to the Windows host.\n"
                   "Enable WSL interop in /etc/wsl.conf: [interop] enabled=true appendWindowsPath=true",
        ))
        return results

    # Win11 with mirrored networking doesn't need portproxy/relay.
    if host.is_win11 and host.mirrored_mode:
        results.append(CheckResult(
            "wsl_host", "WSL networking mode", STATUS_OK,
            f"Windows {host.version} — mirrored mode (no relay required)",
        ))
        return results

    summary_extra = f"Windows {host.version}"
    if host.is_win11 and host.mirrored_mode is False:
        summary_extra += " (NAT mode — relay required; enable mirrored mode in .wslconfig to skip this)"
    elif host.is_win11:
        summary_extra += " (Win11)"
    else:
        summary_extra += " (Win10 — relay required)"
    results.append(CheckResult(
        "wsl_env", "WSL environment", STATUS_OK,
        f"WSL IP {ip or '?'}  ·  {summary_extra}",
    ))

    if not ip:
        results.append(CheckResult(
            "wsl_ip", "WSL IP detection", STATUS_FAIL,
            "could not read `hostname -I`",
        ))
        return results

    proxies = wsl_host.list_portproxy()

    for inst in instances:
        if not isinstance(inst, GameServerInstance):
            continue
        if inst.name not in managed:
            continue  # only check running servers
        results.extend(_wsl_checks_for_server(inst, ip, proxies))

    return results


def _wsl_checks_for_server(inst: "GameServerInstance",
                           ip: str,
                           proxies: list,
                           *,
                           include_udp: bool = True,
                           include_udp_only: bool = False) -> list[CheckResult]:
    """Run the per-server WSL host checks (portproxy + firewall + UDP relay).

    Used both by the legacy `check_wsl_host_forwarding` aggregator and by
    `plan_checks`, where we split portproxy/firewall and UDP relay into
    separate steps so progress feedback is meaningful.
    """
    results: list[CheckResult] = []
    proxy_by_port = {e.listen_port: e for e in proxies}
    port = inst.meta.game_port

    if not include_udp_only:
        # 1) TCP portproxy entry
        entry = proxy_by_port.get(port)
        if entry is None:
            body = (f"No TCP portproxy entry on the Windows host for port {port}.\n\n"
                    f"Required:  0.0.0.0:{port}  →  {ip}:{port}\n\n"
                    f"This blocks any TCP traffic (e.g. PyPlanet from other machines\n"
                    f"or the game's master-server registration) from reaching the\n"
                    f"WSL2 VM. Applying the fix runs netsh under UAC and also adds\n"
                    f"matching firewall rules in one prompt.")

            def fix(p=port, t=ip) -> tuple[bool, str]:
                return wsl_host.apply_portproxy_and_firewall(p, t)

            results.append(CheckResult(
                f"wsl_pp_{inst.name}", f"WSL portproxy + firewall — '{inst.name}' (TCP {port})",
                STATUS_FAIL, "missing on Windows host", detail=body,
                fix_label="Apply portproxy + firewall (UAC)",
                fix_confirm_title=f"Configure Windows host for port {port}?",
                fix_confirm_body=body,
                fix=fix,
            ))
        elif entry.connect_addr != ip or entry.connect_port != port:
            body = (f"Portproxy for {port} points at the wrong target:\n"
                    f"  current:  {entry.listen_addr}:{entry.listen_port}  →  {entry.connect_addr}:{entry.connect_port}\n"
                    f"  expected: 0.0.0.0:{port}  →  {ip}:{port}\n\n"
                    f"This typically happens after the WSL VM's IP changed (every reboot).\n"
                    f"The fix re-creates the entry and refreshes the firewall rules.")

            def fix(p=port, t=ip) -> tuple[bool, str]:
                return wsl_host.apply_portproxy_and_firewall(p, t)

            results.append(CheckResult(
                f"wsl_pp_{inst.name}", f"WSL portproxy + firewall — '{inst.name}' (TCP {port})",
                STATUS_FAIL, f"target {entry.connect_addr}:{entry.connect_port} ≠ {ip}:{port}",
                detail=body,
                fix_label="Refresh portproxy (UAC)",
                fix_confirm_title=f"Refresh Windows portproxy for port {port}?",
                fix_confirm_body=body,
                fix=fix,
            ))
        else:
            # 2) Firewall rules (only check if portproxy is correct)
            tcp_rule = f"TM Dedicated TCP {port}"
            udp_rule = f"TM Dedicated UDP {port}"
            missing = []
            if not wsl_host.firewall_rule_exists(tcp_rule):
                missing.append(tcp_rule)
            if not wsl_host.firewall_rule_exists(udp_rule):
                missing.append(udp_rule)
            if missing:
                body = (f"The Windows firewall is missing the following inbound rules:\n  - "
                        + "\n  - ".join(missing)
                        + f"\n\nWithout these, players outside the host cannot reach port {port}.\n"
                          f"The fix adds both TCP+UDP rules + refreshes the portproxy in one UAC prompt.")

                def fix(p=port, t=ip) -> tuple[bool, str]:
                    return wsl_host.apply_portproxy_and_firewall(p, t)

                results.append(CheckResult(
                    f"wsl_fw_{inst.name}", f"Windows firewall — '{inst.name}' ({port})",
                    STATUS_FAIL, f"{len(missing)} rule(s) missing", detail=body,
                    fix_label="Add firewall rules (UAC)",
                    fix_confirm_title=f"Add Windows firewall rules for port {port}?",
                    fix_confirm_body=body,
                    fix=fix,
                ))
            else:
                results.append(CheckResult(
                    f"wsl_pp_{inst.name}", f"WSL portproxy + firewall — '{inst.name}' ({port})",
                    STATUS_OK, f"0.0.0.0:{port} → {ip}:{port}  ·  TCP+UDP rules present",
                ))

    if include_udp or include_udp_only:
        # 3) UDP relay process (always needed in NAT mode — netsh can't proxy UDP)
        if not wsl_host.is_udp_relay_running(port):
            body = (f"No UDP relay process found for port {port} on the Windows host.\n\n"
                    f"netsh's portproxy is TCP-only. Trackmania's game traffic is UDP,\n"
                    f"so without a relay process, players cannot connect even with the\n"
                    f"portproxy and firewall in place.\n\n"
                    f"The fix writes a relay script to %TEMP% and launches it in a new\n"
                    f"console window. Keep that window open while players play —\n"
                    f"closing it stops the relay.")

            def fix(p=port) -> tuple[bool, str]:
                return wsl_host.launch_udp_relay(p)

            results.append(CheckResult(
                f"wsl_udp_{inst.name}", f"UDP relay — '{inst.name}' ({port})",
                STATUS_FAIL, "not running on Windows host", detail=body,
                fix_label="Launch UDP relay window",
                fix_confirm_title=f"Launch UDP relay for port {port}?",
                fix_confirm_body=body,
                fix=fix,
            ))
        else:
            results.append(CheckResult(
                f"wsl_udp_{inst.name}", f"UDP relay — '{inst.name}' ({port})",
                STATUS_OK, "running on Windows host",
            ))

    return results


# ── extra checks: credentials / DB creds / disk / time / registration / addons ─

def _read_pool_db_settings(pool: PyPlanetPoolInstance) -> dict[str, str] | None:
    """Best-effort parse of DATABASES['default']['OPTIONS'] + NAME from settings/base.py.

    We use regex rather than execing the file because settings/base.py may
    import third-party modules that aren't available outside the pool venv.
    Returns dict with keys: name, host, port, user, password (any may be missing).
    """
    base = pool.root / "settings" / "base.py"
    try:
        text = base.read_text(errors="replace")
    except OSError:
        return None
    out: dict[str, str] = {}
    name_m = re.search(r'["\']NAME["\']\s*:\s*["\']([^"\']+)["\']', text)
    if name_m:
        out["name"] = name_m.group(1)
    for key in ("host", "port", "user", "password"):
        m = re.search(rf'["\']{key}["\']\s*:\s*["\']?([^"\',\s}}]+)["\']?', text)
        if m:
            out[key] = m.group(1)
    return out or None


def _read_pool_super_pw(pool: PyPlanetPoolInstance) -> str | None:
    base = pool.root / "settings" / "base.py"
    try:
        text = base.read_text(errors="replace")
    except OSError:
        return None
    m = re.search(r'["\']PASSWORD["\']\s*:\s*["\']([^"\']*)["\']', text)
    return m.group(1) if m else None


def _read_server_dedicated_cfg(srv: GameServerInstance) -> dict[str, str] | None:
    """Parse SuperAdmin password + masterserver login/password from dedicated_cfg.txt."""
    import xml.etree.ElementTree as ET
    cfg = srv.server_dir() / "UserData" / "Config" / "dedicated_cfg.txt"
    if not cfg.is_file():
        return None
    try:
        root = ET.parse(cfg).getroot()
    except (ET.ParseError, OSError):
        return None
    out: dict[str, str] = {}
    super_pw = root.find(".//authorization_levels/level[name='SuperAdmin']/password")
    if super_pw is not None and super_pw.text:
        out["super_admin_pw"] = super_pw.text.strip()
    ms_login = root.find("masterserver_account/login")
    if ms_login is not None and ms_login.text:
        out["ms_login"] = ms_login.text.strip()
    ms_pw = root.find("masterserver_account/password")
    if ms_pw is not None and ms_pw.text:
        out["ms_password"] = ms_pw.text.strip()
    return out


def check_pool_superadmin(instances: list[Instance]) -> list[CheckResult]:
    """Pool SuperAdmin password must match the linked server's SuperAdmin
    password — otherwise PyPlanet authenticates against the wrong server or
    fails XML-RPC auth silently."""
    by_name = {i.name: i for i in instances if isinstance(i, GameServerInstance)}
    results: list[CheckResult] = []
    for inst in instances:
        if not isinstance(inst, PyPlanetPoolInstance):
            continue
        target = inst.meta.target_server
        if not target:
            continue
        srv = by_name.get(target)
        if srv is None:
            continue
        pool_pw = _read_pool_super_pw(inst)
        srv_cfg = _read_server_dedicated_cfg(srv)
        if pool_pw is None or srv_cfg is None or "super_admin_pw" not in srv_cfg:
            results.append(CheckResult(
                f"superpw_{inst.name}", f"SuperAdmin password — '{inst.name}'",
                STATUS_WARN, "could not read one side",
                detail=("Could not parse the pool's settings/base.py PASSWORD\n"
                        "and/or the server's dedicated_cfg.txt SuperAdmin password."),
            ))
            continue
        srv_pw = srv_cfg["super_admin_pw"]
        if pool_pw != srv_pw:
            body = (f"Pool '{inst.name}' settings/base.py SuperAdmin password\n"
                    f"does NOT match the linked server '{srv.name}' dedicated_cfg.txt.\n\n"
                    f"  pool:   {'(empty)' if not pool_pw else '*' * len(pool_pw)}  ({len(pool_pw)} chars)\n"
                    f"  server: {'(empty)' if not srv_pw else '*' * len(srv_pw)}  ({len(srv_pw)} chars)\n\n"
                    f"PyPlanet will fail XML-RPC auth and exit shortly after start.\n"
                    f"Fix: copy the server's SuperAdmin password into the pool's base.py.")

            def fix(pool=inst, new_pw=srv_pw) -> tuple[bool, str]:
                base = pool.root / "settings" / "base.py"
                text = base.read_text()
                new = re.sub(
                    r'(["\']PASSWORD["\']\s*:\s*["\'])[^"\']*(["\'])',
                    rf'\g<1>{new_pw}\g<2>',
                    text, count=1,
                )
                base.write_text(new)
                return True, f"Updated pool '{pool.name}' SuperAdmin password. Restart the pool to apply."

            results.append(CheckResult(
                f"superpw_{inst.name}", f"SuperAdmin password — '{inst.name}'",
                STATUS_FAIL, "pool ≠ server", detail=body,
                fix_label="Copy server password → pool",
                fix_confirm_title="Rewrite pool SuperAdmin password?",
                fix_confirm_body=body,
                fix=fix,
            ))
        else:
            results.append(CheckResult(
                f"superpw_{inst.name}", f"SuperAdmin password — '{inst.name}'",
                STATUS_OK, "matches linked server",
            ))
    return results


def check_pool_db_creds(instances: list[Instance]) -> list[CheckResult]:
    """Each pool's DATABASES creds must actually connect — wrong host/user/pw
    is the classic 'pool starts then dies seconds later' failure mode.

    We probe via the PyPlanet venv's python (it already ships ``pymysql``
    as a transitive dep of ``aiomysql``) so tmsm itself doesn't need an
    extra runtime dependency."""
    results: list[CheckResult] = []
    pools = [i for i in instances if isinstance(i, PyPlanetPoolInstance)]
    if not pools:
        return results
    py = paths.PYPLANET_VENV / "bin" / "python"
    if not py.is_file():
        results.append(CheckResult(
            "pool_db", "Pool DB credentials", STATUS_SKIP,
            "PyPlanet venv not present",
        ))
        return results

    for pool in pools:
        s = _read_pool_db_settings(pool)
        if not s or not s.get("name"):
            results.append(CheckResult(
                f"pooldb_{pool.name}", f"Pool DB — '{pool.name}'", STATUS_WARN,
                "could not parse settings/base.py",
            ))
            continue
        host = s.get("host", "127.0.0.1")
        port = int(s.get("port", 3306))
        user = s.get("user", "")
        pw = s.get("password", "")
        db = s["name"]
        code = (
            "import sys\n"
            "try:\n"
            "    import pymysql\n"
            "except ImportError:\n"
            "    print('SKIP pymysql-missing'); sys.exit(0)\n"
            "try:\n"
            f"    c = pymysql.connect(host={host!r}, port={port}, user={user!r},\n"
            f"        password={pw!r}, database={db!r}, connect_timeout=3)\n"
            "    c.close()\n"
            "    print('OK')\n"
            "except Exception as e:\n"
            "    print('FAIL', type(e).__name__, str(e).splitlines()[0][:160])\n"
        )
        try:
            r = subprocess.run([str(py), "-c", code], capture_output=True,
                               text=True, timeout=6, check=False)
        except subprocess.TimeoutExpired:
            results.append(CheckResult(
                f"pooldb_{pool.name}", f"Pool DB — '{pool.name}'", STATUS_WARN,
                "DB probe timed out",
            ))
            continue
        out = (r.stdout or "").strip()
        if out.startswith("OK"):
            results.append(CheckResult(
                f"pooldb_{pool.name}", f"Pool DB — '{pool.name}'", STATUS_OK,
                f"{user}@{host}:{port}/{db}",
            ))
        elif out.startswith("SKIP"):
            results.append(CheckResult(
                f"pooldb_{pool.name}", f"Pool DB — '{pool.name}'", STATUS_SKIP,
                "pymysql not in PyPlanet venv (install aiomysql/pymysql)",
            ))
        else:
            err = out[5:] if out.startswith("FAIL ") else (out or "unknown error")
            body = (f"Pool '{pool.name}' cannot connect to its database:\n\n"
                    f"  host:     {host}:{port}\n"
                    f"  database: {db}\n"
                    f"  user:     {user}\n"
                    f"  error:    {err}\n\n"
                    f"PyPlanet will start, log the error, and exit.\n"
                    f"Edit settings/base.py to fix credentials,\n"
                    f"or re-create the database via MariaDB.")
            results.append(CheckResult(
                f"pooldb_{pool.name}", f"Pool DB — '{pool.name}'", STATUS_FAIL,
                err[:120], detail=body,
            ))
    return results


def check_disk_space() -> CheckResult:
    """Warn if TMSM_HOME has < 2 GB free, fail at < 500 MB."""
    import shutil
    try:
        usage = shutil.disk_usage(str(paths.HOME))
    except OSError as e:
        return CheckResult("disk", "Disk space (TMSM_HOME)", STATUS_WARN,
                           f"cannot stat: {e}")
    free_mb = usage.free / (1024 * 1024)
    free_gb = free_mb / 1024
    pct_used = (usage.used / usage.total * 100) if usage.total else 0
    summary = f"{free_gb:.1f} GB free  ·  {pct_used:.0f}% used  ({paths.HOME})"
    if free_mb < 500:
        return CheckResult(
            "disk", "Disk space (TMSM_HOME)", STATUS_FAIL, summary,
            detail=("Less than 500 MB free on the filesystem hosting TMSM_HOME.\n"
                    "MariaDB will refuse writes, screen logs will fail, and downloads\n"
                    "(server binaries, backups) will abort."),
        )
    if free_mb < 2048:
        return CheckResult(
            "disk", "Disk space (TMSM_HOME)", STATUS_WARN, summary,
            detail=("Less than 2 GB free. Consider rotating logs, pruning old\n"
                    "backups, or moving the SQLite/MariaDB datadirs."),
        )
    return CheckResult("disk", "Disk space (TMSM_HOME)", STATUS_OK, summary)


def check_time_sync() -> CheckResult:
    """Clock skew breaks Dedimania record submission and HTTPS to master-server."""
    try:
        r = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized,SystemClockSynchronized"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return CheckResult("time", "System clock sync", STATUS_SKIP,
                           "timedatectl not available")
    out = r.stdout
    synced = "yes" in out.lower()
    if synced:
        return CheckResult("time", "System clock sync", STATUS_OK,
                           "clock synchronized via NTP")
    return CheckResult(
        "time", "System clock sync", STATUS_WARN,
        "clock NOT synchronized",
        detail=("The system clock isn't NTP-synchronized. Symptoms:\n"
                "  • Dedimania rejects records with 'timestamp skew'\n"
                "  • HTTPS to the master-server may fail certificate validation\n\n"
                "Fix:\n"
                "  sudo timedatectl set-ntp true\n"
                "  sudo systemctl enable --now systemd-timesyncd"),
    )


def check_master_registration(instances: list[Instance],
                              managed: dict[str, int]) -> list[CheckResult]:
    """For each running TM2020 gameserver: check the public TM master list.

    If the server's account login isn't visible, it means the server didn't
    successfully register — usually wrong masterserver_account password or
    NAT/firewall blocking outbound to api.trackmania.com."""
    results: list[CheckResult] = []
    try:
        from urllib.request import urlopen, Request
        from urllib.error import URLError, HTTPError
        import json
    except ImportError:
        return results

    from .. import __version__
    # trackmania.io requires a descriptive User-Agent with contact info,
    # otherwise it returns 403 Forbidden. See https://openplanet.dev/tmio/api
    ua = (f"tmsm/{__version__} (TrackManiaServerManager; "
          f"https://github.com/TrackManiaServerManager/TrackManiaServerManager)")

    for inst in instances:
        if not isinstance(inst, GameServerInstance):
            continue
        if inst.name not in managed:
            continue  # only check running servers
        from ..instances.server import GameType
        if inst.meta.game is not GameType.TM2020:
            continue
        srv_cfg = _read_server_dedicated_cfg(inst)
        if not srv_cfg or not srv_cfg.get("ms_login"):
            results.append(CheckResult(
                f"masterreg_{inst.name}", f"Master-server registration — '{inst.name}'",
                STATUS_WARN, "no masterserver_account/login configured",
                detail=("dedicated_cfg.txt has no <masterserver_account><login>.\n"
                        "The server runs locally but isn't listed publicly."),
            ))
            continue
        login = srv_cfg["ms_login"]
        # Trackmania.io is a community-maintained mirror of the official server list.
        url = f"https://trackmania.io/api/server/{login}"
        req = Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
        try:
            with urlopen(req, timeout=4) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                body_bytes = resp.read()
            if "json" not in ctype:
                # API returned HTML (e.g. an error page or the endpoint moved).
                # We can't tell anything useful from this — skip rather than fail.
                results.append(CheckResult(
                    f"masterreg_{inst.name}", f"Master-server registration — '{inst.name}'",
                    STATUS_SKIP, "trackmania.io API returned non-JSON",
                    detail=("The trackmania.io server-lookup endpoint returned a non-JSON\n"
                            "response (probably an HTML page). The community API is\n"
                            "undocumented and unsupported — the route may have moved.\n"
                            "Skipping this check; server registration is unaffected."),
                ))
                continue
            try:
                data = json.loads(body_bytes.decode("utf-8", errors="replace"))
            except ValueError as e:
                results.append(CheckResult(
                    f"masterreg_{inst.name}", f"Master-server registration — '{inst.name}'",
                    STATUS_SKIP, f"trackmania.io response not valid JSON ({e})",
                ))
                continue
            if isinstance(data, dict) and data.get("login") == login:
                results.append(CheckResult(
                    f"masterreg_{inst.name}", f"Master-server registration — '{inst.name}'",
                    STATUS_OK, f"'{login}' visible on trackmania.io",
                ))
            else:
                results.append(CheckResult(
                    f"masterreg_{inst.name}", f"Master-server registration — '{inst.name}'",
                    STATUS_WARN, f"'{login}' not listed",
                    detail=("trackmania.io does not show the server. Common causes:\n"
                            "  • Wrong masterserver_account password in dedicated_cfg.txt\n"
                            "  • Outbound HTTPS blocked (corporate proxy / firewall)\n"
                            "  • Server was just started — registration can take 1–2 min\n"
                            "  • Account not validated on the Trackmania account portal"),
                ))
        except HTTPError as e:
            # 404 = login isn't registered (real failure). Others = API issue.
            if e.code == 404:
                results.append(CheckResult(
                    f"masterreg_{inst.name}", f"Master-server registration — '{inst.name}'",
                    STATUS_WARN, f"'{login}' not registered (HTTP 404)",
                    detail=("trackmania.io has no record of this server login.\n"
                            "Either the masterserver_account credentials are wrong,\n"
                            "the server hasn't finished its first registration yet,\n"
                            "or the account isn't a valid dedicated-server account."),
                ))
            else:
                results.append(CheckResult(
                    f"masterreg_{inst.name}", f"Master-server registration — '{inst.name}'",
                    STATUS_SKIP, f"trackmania.io HTTP {e.code} {e.reason}",
                    detail=(f"trackmania.io returned HTTP {e.code} {e.reason}.\n"
                            "This is an API/network issue, not necessarily a problem\n"
                            "with your server. Try again later."),
                ))
        except URLError as e:
            results.append(CheckResult(
                f"masterreg_{inst.name}", f"Master-server registration — '{inst.name}'",
                STATUS_SKIP, f"could not reach trackmania.io: {e.reason}",
            ))
        except Exception as e:
            results.append(CheckResult(
                f"masterreg_{inst.name}", f"Master-server registration — '{inst.name}'",
                STATUS_SKIP, f"probe failed: {e}",
            ))
    return results


def check_addon_importability() -> list[CheckResult]:
    """For each tmsm-installed addon: smoke-test that its module imports
    inside the PyPlanet venv. Catches broken symlinks, missing deps, and
    bad __init__.py rewrites."""
    results: list[CheckResult] = []
    try:
        from ..assets import list_installed
    except ImportError:
        return results
    py = paths.PYPLANET_VENV / "bin" / "python"
    if not py.is_file():
        results.append(CheckResult(
            "addon_imports", "Installed addon imports", STATUS_SKIP,
            "PyPlanet venv not present",
        ))
        return results
    installed = list_installed()
    if not installed:
        return results
    modules = [a.module_name for a in installed]
    # Probe all in one python invocation for speed; print one line per module.
    code = (
        "import importlib, sys\n"
        f"mods = {modules!r}\n"
        "for m in mods:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "        print('OK', m)\n"
        "    except Exception as e:\n"
        "        print('FAIL', m, type(e).__name__, str(e).splitlines()[0][:120])\n"
    )
    env = {**os.environ, "PYTHONPATH": str(paths.PYPLANET_SRC)}
    try:
        r = subprocess.run([str(py), "-c", code], capture_output=True, text=True,
                           timeout=15, env=env, check=False)
    except subprocess.TimeoutExpired:
        results.append(CheckResult(
            "addon_imports", "Installed addon imports", STATUS_WARN,
            "import probe timed out",
        ))
        return results
    fails: list[str] = []
    for line in r.stdout.splitlines():
        if line.startswith("FAIL"):
            fails.append(line[5:])
    if not fails:
        results.append(CheckResult(
            "addon_imports", "Installed addon imports", STATUS_OK,
            f"all {len(modules)} module(s) import cleanly",
        ))
    else:
        body = ("The following installed addons cannot be imported inside the\n"
                "PyPlanet venv. PyPlanet will refuse to start any pool that\n"
                "enables them.\n\n  - " + "\n  - ".join(fails) +
                "\n\nCommon causes: broken symlink in pyplanet/apps/{tmsm,contrib}/,\n"
                "missing pip dependency, or a syntax error in the addon's code.")
        results.append(CheckResult(
            "addon_imports", "Installed addon imports", STATUS_FAIL,
            f"{len(fails)}/{len(modules)} failed", detail=body,
        ))
    return results


def plan_checks(cfg: Config) -> list[tuple[str, Callable[[], list[CheckResult]]]]:
    """Return a list of (step_label, runner) pairs.

    Splitting the work up front lets the UI show per-step progress instead
    of freezing for the whole batch. WSL-host probes are split into one
    step per query, since each subprocess hop through Windows interop costs
    several hundred ms.
    """
    instances = discover_all(cfg)
    managed = _managed_inner_pids()
    steps: list[tuple[str, Callable[[], list[CheckResult]]]] = [
        ("Checking for stale screen sockets",
         lambda: [check_stale_screen_sockets()]),
        ("Scanning for orphan TrackmaniaServer processes",
         lambda: [check_orphan_trackmania(managed)]),
        ("Checking disk space",
         lambda: [check_disk_space()]),
        ("Checking system clock sync",
         lambda: [check_time_sync()]),
        ("Probing MariaDB",
         lambda: [check_mariadb(cfg)]),
        ("Verifying server port ownership",
         lambda: check_port_alignment(cfg, instances, managed)),
        ("Verifying pool ↔ server port alignment",
         lambda: check_pool_alignment(instances)),
        ("Verifying pool ↔ server SuperAdmin passwords",
         lambda: check_pool_superadmin(instances)),
        ("Probing pool database credentials",
         lambda: check_pool_db_creds(instances)),
        ("Smoke-testing installed addon imports",
         lambda: check_addon_importability()),
        ("Querying master-server registration (trackmania.io)",
         lambda: check_master_registration(instances, managed)),
    ]
    if not wsl_host.is_wsl():
        return steps

    # WSL: split host probes into multiple steps. We pre-fetch the per-step
    # state via a shared mutable dict so each step only does the one thing
    # its label promises.
    state: dict = {"instances": instances, "managed": managed}

    def _probe_host_env() -> list[CheckResult]:
        state["host"] = wsl_host.windows_host_info()
        state["ip"] = wsl_host.wsl_ip()
        host = state["host"]
        ip = state["ip"]
        if not host.available:
            state["abort"] = True
            return [CheckResult(
                "wsl_host", "WSL ↔ Windows interop", STATUS_WARN,
                "Windows interop unreachable (cmd.exe not on PATH)",
                detail="tmsm is running under WSL but cannot call back to the Windows host.\n"
                       "Enable WSL interop in /etc/wsl.conf: [interop] enabled=true appendWindowsPath=true",
            )]
        if host.is_win11 and host.mirrored_mode:
            state["abort"] = True
            return [CheckResult(
                "wsl_host", "WSL networking mode", STATUS_OK,
                f"Windows {host.version} — mirrored mode (no relay required)",
            )]
        summary_extra = f"Windows {host.version}"
        if host.is_win11 and host.mirrored_mode is False:
            summary_extra += " (NAT mode — relay required; enable mirrored mode in .wslconfig to skip this)"
        elif host.is_win11:
            summary_extra += " (Win11)"
        else:
            summary_extra += " (Win10 — relay required)"
        out: list[CheckResult] = [CheckResult(
            "wsl_env", "WSL environment", STATUS_OK,
            f"WSL IP {ip or '?'}  ·  {summary_extra}",
        )]
        if not ip:
            state["abort"] = True
            out.append(CheckResult(
                "wsl_ip", "WSL IP detection", STATUS_FAIL,
                "could not read `hostname -I`",
            ))
        return out

    def _probe_portproxy() -> list[CheckResult]:
        if state.get("abort"):
            return []
        state["proxies"] = wsl_host.list_portproxy()
        return []

    steps.append(("Querying WSL ↔ Windows host environment", _probe_host_env))
    steps.append(("Reading Windows portproxy table", _probe_portproxy))

    # One pair of (firewall, UDP relay) steps per running tmsm gameserver.
    running_servers = [
        i for i in instances
        if isinstance(i, GameServerInstance) and i.name in managed
    ]
    for srv in running_servers:
        def _check_one(srv=srv) -> list[CheckResult]:
            if state.get("abort"):
                return []
            return _wsl_checks_for_server(
                srv, state["ip"], state["proxies"], include_udp=False,
            )

        def _check_udp(srv=srv) -> list[CheckResult]:
            if state.get("abort"):
                return []
            return _wsl_checks_for_server(
                srv, state["ip"], state["proxies"], include_udp_only=True,
            )

        steps.append((
            f"Checking portproxy + firewall for '{srv.name}' (port {srv.meta.game_port})",
            _check_one,
        ))
        steps.append((
            f"Checking UDP relay for '{srv.name}' (port {srv.meta.game_port})",
            _check_udp,
        ))

    return steps


def run_all_checks(cfg: Config,
                   on_step: Callable[[int, int, str], None] | None = None) -> list[CheckResult]:
    """Execute every diagnostic check, optionally reporting progress.

    on_step is called as on_step(index_starting_at_1, total, label) BEFORE
    each step runs, so the UI can update without blocking.
    """
    steps = plan_checks(cfg)
    total = len(steps)
    results: list[CheckResult] = []
    for idx, (label, fn) in enumerate(steps, start=1):
        if on_step:
            on_step(idx, total, label)
        try:
            results.extend(fn())
        except Exception as e:
            results.append(CheckResult(
                f"step_{idx}_error", label, STATUS_FAIL,
                "check raised an exception",
                detail=f"{type(e).__name__}: {e}",
            ))
    results.sort(key=lambda r: (STATUS_RANK.get(r.status, 9), r.title))
    return results


# ── progress modal ───────────────────────────────────────────────────────────

class _ProgressModal(ModalScreen[None]):
    """Indeterminate / per-step progress indicator while a worker runs.

    Closed by the worker (`finish()`); no user-visible buttons.
    """

    DEFAULT_CSS = """
    _ProgressModal { align: center middle; }
    #prog-box {
        width: 70;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #prog-title { padding-bottom: 1; }
    #prog-step { color: $text-muted; padding-top: 1; }
    ProgressBar { padding-top: 1; }
    """

    def __init__(self, title: str, total: int | None = None) -> None:
        super().__init__()
        self.title_text = title
        self.total = total  # None = indeterminate

    def compose(self) -> ComposeResult:
        from textual.widgets import ProgressBar
        with Container(id="prog-box"):
            yield Label(f"[b]{self.title_text}[/b]", id="prog-title")
            if self.total is not None:
                yield ProgressBar(total=self.total, show_eta=False, id="prog-bar")
            else:
                # Indeterminate: total=None makes the bar pulse.
                yield ProgressBar(show_eta=False, show_percentage=False, id="prog-bar")
            yield Label("Starting…", id="prog-step")

    def set_step(self, idx: int, total: int, label: str) -> None:
        try:
            from textual.widgets import ProgressBar
            bar = self.query_one("#prog-bar", ProgressBar)
            if self.total is not None:
                bar.update(progress=idx)
            self.query_one("#prog-step", Label).update(f"[{idx}/{total}] {label}")
        except Exception:
            pass

    def set_label(self, label: str) -> None:
        try:
            self.query_one("#prog-step", Label).update(label)
        except Exception:
            pass

    def finish(self) -> None:
        try:
            self.dismiss(None)
        except Exception:
            pass


# ── confirm modal (richer than ConfirmScreen, for multi-line details) ─────────

class _ConfirmFixScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("y", "ok", "Yes"),
        Binding("n", "cancel", "No"),
    ]

    DEFAULT_CSS = """
    _ConfirmFixScreen { align: center middle; }
    #fix-box {
        width: 90;
        height: 30;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }
    #fix-body {
        height: 1fr;
        border: round $accent;
        padding: 0 1;
    }
    #fix-buttons { padding-top: 1; align-horizontal: right; }
    Button { margin: 0 1; }
    """

    def __init__(self, title: str, body: str, ok_label: str) -> None:
        super().__init__()
        self.title_text = title
        self.body_text = body
        self.ok_label = ok_label

    def compose(self) -> ComposeResult:
        with Container(id="fix-box"):
            yield Label(f"[b]{self.title_text}[/b]")
            with VerticalScroll(id="fix-body"):
                yield Static(self.body_text)
            with Horizontal(id="fix-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.ok_label, id="ok", variant="error")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "ok")

    def action_ok(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ── main screen ───────────────────────────────────────────────────────────────

class DiagnosticsScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("r", "rerun", "Re-run"),
        Binding("enter", "fix_selected", "Fix selected"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.results: list[CheckResult] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="table-wrap"):
            yield DataTable(id="checks", cursor_type="row", zebra_stripes=True)
        yield Vertical(Static("Running diagnostics…", id="details-body"), id="details")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("", "Check", "Detail", "Action")
        self.action_rerun()

    # --- data ---

    def action_rerun(self) -> None:
        """Push a progress modal and run all checks in a worker thread."""
        cfg = self.app.cfg  # type: ignore[attr-defined]
        total = len(plan_checks(cfg))
        modal = _ProgressModal("Running diagnostics…", total=total)
        self.app.push_screen(modal)

        def on_step(idx: int, total: int, label: str) -> None:
            self.app.call_from_thread(modal.set_step, idx, total, label)

        def worker() -> None:
            try:
                results = run_all_checks(cfg, on_step=on_step)
            except Exception as e:
                self.app.call_from_thread(modal.finish)
                self.app.call_from_thread(
                    self.notify, f"Diagnostics failed: {e}",
                    severity="error", timeout=8,
                )
                return
            self.app.call_from_thread(self._apply_results, results)
            self.app.call_from_thread(modal.finish)

        self.run_worker(worker, thread=True, exclusive=True, name="diagnostics")

    def _apply_results(self, results: list[CheckResult]) -> None:
        self.results = results
        table = self.query_one(DataTable)
        table.clear()
        for r in self.results:
            action = f"[b]{r.fix_label}[/b]" if r.fix else "—"
            table.add_row(STATUS_DOT.get(r.status, "?"), r.title, r.summary, action)
        if table.row_count:
            table.move_cursor(row=0)
        self._update_details()

    def _selected(self) -> CheckResult | None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        return self.results[table.cursor_row]

    def _update_details(self) -> None:
        body = self.query_one("#details-body", Static)
        r = self._selected()
        if r is None:
            body.update("No checks to display.")
            return
        lines = [f"[b]{r.title}[/b]   [dim]{r.status}[/dim]", "", r.summary]
        if r.detail:
            lines += ["", r.detail]
        if r.fix:
            lines += ["", f"[b][yellow]Press Enter to: {r.fix_label}[/yellow][/b]"]
        body.update("\n".join(lines))

    def on_data_table_row_highlighted(self, _e: DataTable.RowHighlighted) -> None:
        self._update_details()

    def on_data_table_row_selected(self, _e: DataTable.RowSelected) -> None:
        self.action_fix_selected()

    def action_fix_selected(self) -> None:
        r = self._selected()
        if r is None or r.fix is None:
            return

        def after(confirmed: bool | None) -> None:
            if not confirmed or r.fix is None:
                return
            # Indeterminate progress modal while the fix runs — many fixes
            # spawn UAC / netsh and may take a few seconds.
            modal = _ProgressModal(f"Applying: {r.fix_label}", total=None)
            self.app.push_screen(modal)

            def worker() -> None:
                try:
                    ok, msg = r.fix()  # type: ignore[misc]
                except Exception as e:
                    self.app.call_from_thread(modal.finish)
                    self.app.call_from_thread(
                        self.notify, f"Fix failed: {e}",
                        severity="error", timeout=8,
                    )
                    return
                self.app.call_from_thread(modal.finish)
                self.app.call_from_thread(
                    self.notify, msg,
                    severity="information" if ok else "warning", timeout=8,
                )
                self.app.call_from_thread(self.action_rerun)

            self.run_worker(worker, thread=True, exclusive=False, name="diagnostics-fix")

        self.app.push_screen(
            _ConfirmFixScreen(r.fix_confirm_title, r.fix_confirm_body, r.fix_label),
            after,
        )
