"""WSL-side diagnostics: detect missing Windows host port-forwarding / firewall
rules / UDP relay needed for players on the LAN (or the host itself) to reach
the dedicated server running inside WSL2.

All Windows queries go through `netsh.exe`, `powershell.exe`, `cmd.exe` and
`wslpath` — WSL2 interop must be enabled (it is by default).

Fixes are applied via `Start-Process -Verb RunAs` so the user gets exactly
one UAC prompt per fix.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


# ── environment detection ─────────────────────────────────────────────────────

def is_wsl() -> bool:
    """Best-effort WSL detection."""
    for path in ("/proc/sys/kernel/osrelease", "/proc/version"):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                txt = f.read().lower()
            if "microsoft" in txt or "wsl" in txt:
                return True
        except OSError:
            continue
    return False


def wsl_ip() -> str | None:
    """First IPv4 address from `hostname -I`."""
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True,
                             check=False, timeout=2).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    for tok in out.split():
        if "." in tok and tok.count(".") == 3:
            return tok
    return None


@dataclass
class WindowsHostInfo:
    available: bool
    version: str = ""          # e.g. "10.0.22631.3672"
    build: int = 0
    is_win11: bool = False
    mirrored_mode: bool | None = None   # None = unknown


def windows_host_info() -> WindowsHostInfo:
    if not shutil.which("cmd.exe"):
        return WindowsHostInfo(False)
    try:
        out = subprocess.run(["cmd.exe", "/c", "ver"], capture_output=True,
                             text=True, check=False, timeout=3).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return WindowsHostInfo(False)
    m = re.search(r"Version\s+(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?", out)
    if not m:
        return WindowsHostInfo(True)
    major, minor, build = int(m.group(1)), int(m.group(2)), int(m.group(3))
    rev = int(m.group(4)) if m.group(4) else 0
    version = f"{major}.{minor}.{build}.{rev}"
    # Windows 11 = build >= 22000
    is_win11 = build >= 22000
    return WindowsHostInfo(True, version=version, build=build, is_win11=is_win11,
                           mirrored_mode=_detect_mirrored_mode())


def _detect_mirrored_mode() -> bool | None:
    """WSL2 'mirrored' networking (Win11 only) makes portproxy unnecessary —
    the WSL VM shares the host's network namespace. Detect by comparing the
    WSL IP to the Windows host's IPv4 list."""
    ip = wsl_ip()
    if not ip:
        return None
    if not shutil.which("powershell.exe"):
        return None
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "(Get-NetIPAddress -AddressFamily IPv4 | Select-Object -ExpandProperty IPAddress) -join ','"],
            capture_output=True, text=True, check=False, timeout=4,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    host_ips = {x.strip() for x in out.replace("\r", "").split(",") if x.strip()}
    return ip in host_ips


# ── Windows host state queries ────────────────────────────────────────────────

@dataclass
class PortproxyEntry:
    listen_addr: str
    listen_port: int
    connect_addr: str
    connect_port: int


def list_portproxy() -> list[PortproxyEntry]:
    try:
        out = subprocess.run(
            ["netsh.exe", "interface", "portproxy", "show", "v4tov4"],
            capture_output=True, text=True, check=False, timeout=4,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    entries: list[PortproxyEntry] = []
    for line in out.splitlines():
        # Lines look like: "0.0.0.0         2350        172.21.4.5      2350"
        parts = line.split()
        if len(parts) != 4:
            continue
        try:
            la, lp, ca, cp = parts[0], int(parts[1]), parts[2], int(parts[3])
        except ValueError:
            continue
        if not (la.count(".") == 3 and ca.count(".") == 3):
            continue
        entries.append(PortproxyEntry(la, lp, ca, cp))
    return entries


def firewall_rule_exists(name: str) -> bool:
    try:
        r = subprocess.run(
            ["netsh.exe", "advfirewall", "firewall", "show", "rule", f"name={name}"],
            capture_output=True, text=True, check=False, timeout=4,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    # netsh returns "No rules match the specified criteria." when missing
    blob = (r.stdout + r.stderr).lower()
    return r.returncode == 0 and "no rules match" not in blob


# ── path helpers ──────────────────────────────────────────────────────────────

def wslpath_to_win(linux_path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["wslpath", "-w", str(linux_path)],
            capture_output=True, text=True, check=False, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


# ── fixes ─────────────────────────────────────────────────────────────────────

def _run_elevated(ps_script: str) -> tuple[bool, str]:
    """Run a PowerShell script body under UAC. Returns (success, message)."""
    if not shutil.which("powershell.exe"):
        return False, "powershell.exe not found (WSL interop disabled?)"
    # -Wait so we know when the elevated child exits; suppress its own window
    # only for command execution, but the UAC prompt itself is always visible.
    launcher = (
        "$ErrorActionPreference='Stop';"
        "$enc=[Convert]::ToBase64String("
        "[Text.Encoding]::Unicode.GetBytes($script));"
        "Start-Process powershell.exe -Verb RunAs -Wait "
        "-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass',"
        "'-EncodedCommand',$enc"
    )
    full = f"$script=@'\n{ps_script}\n'@;\n{launcher}"
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", full],
            capture_output=True, text=True, check=False, timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, f"Failed to launch elevated PowerShell: {e}"
    if r.returncode != 0:
        return False, f"Elevation failed or was declined.\n{r.stderr.strip()}"
    return True, "Elevated commands executed (check the UAC-spawned window for details)."


def apply_portproxy_and_firewall(port: int, target_ip: str) -> tuple[bool, str]:
    """Refresh TCP portproxy + add TCP/UDP firewall rules. Single UAC prompt."""
    tcp_rule = f"TM Dedicated TCP {port}"
    udp_rule = f"TM Dedicated UDP {port}"
    script = "\n".join([
        f"netsh interface portproxy delete v4tov4 listenport={port} listenaddress=0.0.0.0 2>$null | Out-Null",
        f"netsh interface portproxy add    v4tov4 listenport={port} listenaddress=0.0.0.0 connectport={port} connectaddress={target_ip} | Out-Null",
        f"netsh advfirewall firewall delete rule name=\"{tcp_rule}\" 2>$null | Out-Null",
        f"netsh advfirewall firewall delete rule name=\"{udp_rule}\" 2>$null | Out-Null",
        f"netsh advfirewall firewall add rule name=\"{tcp_rule}\" dir=in action=allow protocol=TCP localport={port} | Out-Null",
        f"netsh advfirewall firewall add rule name=\"{udp_rule}\" dir=in action=allow protocol=UDP localport={port} | Out-Null",
        "Write-Host '[tmsm] Done.'",
        "Start-Sleep -Seconds 2",
    ])
    return _run_elevated(script)


def remove_portproxy_and_firewall(port: int) -> tuple[bool, str]:
    tcp_rule = f"TM Dedicated TCP {port}"
    udp_rule = f"TM Dedicated UDP {port}"
    script = "\n".join([
        f"netsh interface portproxy delete v4tov4 listenport={port} listenaddress=0.0.0.0 2>$null | Out-Null",
        f"netsh advfirewall firewall delete rule name=\"{tcp_rule}\" 2>$null | Out-Null",
        f"netsh advfirewall firewall delete rule name=\"{udp_rule}\" 2>$null | Out-Null",
        "Write-Host '[tmsm] Removed.'",
        "Start-Sleep -Seconds 2",
    ])
    return _run_elevated(script)


# ── UDP relay launcher (real .ps1 + thin .bat wrapper) ────────────────────────

# A unique marker that ends up verbatim on the relay's command line, so we can
# detect "is the relay for port N running?" reliably without false-matching
# other powershell processes that happen to mention 'UdpClient' or the port.
_RELAY_TAG = "TMSM_UDP_RELAY"

_WSL_TM_PS1 = r"""# Auto-generated by tmsm. Keep this window open while players connect.
# Marker for tmsm process detection: """ + _RELAY_TAG + r"""
param([int]$Port = 2350)

$ErrorActionPreference = 'Stop'

# Resolve WSL IP fresh on each launch (it changes on every WSL reboot).
$wsl = (wsl hostname -I) -split '\s+' | Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' } | Select-Object -First 1
if (-not $wsl) {
    Write-Host '[ERROR] Could not get WSL IP via `wsl hostname -I`.' -ForegroundColor Red
    Read-Host 'Press Enter to close'
    exit 1
}

Write-Host '============================================================' -ForegroundColor Cyan
Write-Host (' tmsm UDP relay  ({0})' -f '""" + _RELAY_TAG + r"""') -ForegroundColor Cyan
Write-Host (' WSL IP : {0}' -f $wsl)
Write-Host (' Port   : {0}  (UDP)' -f $Port)
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ' Leave this window open. Press Ctrl-C to stop.'
Write-Host '------------------------------------------------------------'

try {
    $front = New-Object System.Net.Sockets.UdpClient $Port
} catch {
    Write-Host ('[ERROR] Cannot bind UDP 0.0.0.0:{0} - is another relay already running?' -f $Port) -ForegroundColor Red
    Write-Host $_.Exception.Message
    Read-Host 'Press Enter to close'
    exit 1
}
$wslEp = [System.Net.IPEndPoint]::new([System.Net.IPAddress]::Parse($wsl), $Port)
$any   = [System.Net.IPEndPoint]::new([System.Net.IPAddress]::Any, 0)

# Per-client demux. TrackMania's dedicated server uses one UDP flow per player,
# so a single shared back-end socket would let the dedicated's replies all land
# at the same NAT 5-tuple — and only the most recent client would ever see them.
# We keep one back-end UdpClient per remote (ip:port) and reap idle entries.
$clients     = New-Object System.Collections.Hashtable
$idleTimeout = [TimeSpan]::FromMinutes(2)
$nextReap    = (Get-Date).AddSeconds(15)

Write-Host ('[relay] running: 0.0.0.0:{0} <-> {1}:{0}  (per-client demux)' -f $Port, $wsl) -ForegroundColor Green

while ($true) {
    $idle = $true

    # Front -> backend(s). Each unique remote endpoint gets its own
    # back-end UdpClient so the dedicated sees N distinct source ports.
    if ($front.Available -gt 0) {
        $ep = $any
        $d = $front.Receive([ref]$ep)
        $key = '{0}:{1}' -f $ep.Address, $ep.Port
        $c = $clients[$key]
        if (-not $c) {
            try {
                $back = New-Object System.Net.Sockets.UdpClient
            } catch {
                Write-Host ('[relay] failed to allocate back-end for {0}: {1}' -f $key, $_.Exception.Message) -ForegroundColor Yellow
                continue
            }
            $c = @{ Back = $back; Ep = $ep; Last = Get-Date }
            $clients[$key] = $c
            Write-Host ('[relay] +client {0}  (active={1})' -f $key, $clients.Count) -ForegroundColor DarkGray
        }
        try {
            [void]$c.Back.Send($d, $d.Length, $wslEp)
        } catch {
            Write-Host ('[relay] send to dedicated failed for {0}: {1}' -f $key, $_.Exception.Message) -ForegroundColor Yellow
        }
        $c.Last = Get-Date
        $idle = $false
    }

    # Backend(s) -> front. Poll every per-client socket.
    foreach ($key in @($clients.Keys)) {
        $c = $clients[$key]
        try {
            while ($c.Back.Available -gt 0) {
                $ep = $any
                $d = $c.Back.Receive([ref]$ep)
                [void]$front.Send($d, $d.Length, $c.Ep)
                $c.Last = Get-Date
                $idle = $false
            }
        } catch {
            Write-Host ('[relay] backend read failed for {0}: {1}' -f $key, $_.Exception.Message) -ForegroundColor Yellow
        }
    }

    # Reap idle clients so we don't leak sockets when players disconnect.
    if ((Get-Date) -ge $nextReap) {
        $now = Get-Date
        foreach ($key in @($clients.Keys)) {
            if (($now - $clients[$key].Last) -gt $idleTimeout) {
                try { $clients[$key].Back.Close() } catch {}
                [void]$clients.Remove($key)
                Write-Host ('[relay] -client {0} (idle)  (active={1})' -f $key, $clients.Count) -ForegroundColor DarkGray
            }
        }
        $nextReap = (Get-Date).AddSeconds(15)
    }

    if ($idle) { Start-Sleep -Milliseconds 1 }
}
"""

_WSL_TM_BAT = r"""@echo off
REM Auto-generated by tmsm. Thin wrapper that launches the PS1 relay.
REM Usage: tmsm-wsl-relay-<port>.bat [PORT]
setlocal
set "PORT=%~1"
if "%PORT%"=="" set "PORT=2350"
set "PS1=%~dp0tmsm-wsl-relay-%PORT%.ps1"
if not exist "%PS1%" set "PS1=%~dpn0.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Port %PORT%
echo Relay stopped.
pause
"""


def _windows_temp_dir() -> Path | None:
    """Resolve %TEMP% on the Windows host and translate to /mnt path."""
    if not shutil.which("cmd.exe"):
        return None
    try:
        out = subprocess.run(
            ["cmd.exe", "/c", "echo %TEMP%"],
            capture_output=True, text=True, check=False, timeout=3,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if not out or out.startswith("%"):
        return None
    try:
        r = subprocess.run(["wslpath", "-u", out], capture_output=True,
                           text=True, check=False, timeout=3)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    p = Path(r.stdout.strip())
    return p if p.is_dir() else None


def is_udp_relay_running(port: int) -> bool:
    """Detect the relay reliably via the unique TMSM_UDP_RELAY tag baked into
    the .ps1 script's path/cmdline."""
    if not shutil.which("powershell.exe"):
        return False
    ps_query = (
        "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
        "Where-Object { "
        f"$_.CommandLine -match 'tmsm-wsl-relay-{port}\\.ps1' "
        "} | Select-Object -First 1 -ExpandProperty ProcessId"
    )
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_query],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return bool(r.stdout.strip().isdigit())


def launch_udp_relay(port: int) -> tuple[bool, str]:
    """Write the relay .ps1 + .bat wrapper to %TEMP% on the host and launch
    them in a new console window. The relay needs to keep running while
    players play, so it lives in its own visible window — closing the
    window stops it."""
    tmp = _windows_temp_dir()
    if tmp is None:
        return False, "Could not resolve Windows %TEMP%."
    ps1_path = tmp / f"tmsm-wsl-relay-{port}.ps1"
    bat_path = tmp / f"tmsm-wsl-relay-{port}.bat"
    try:
        ps1_path.write_text(_WSL_TM_PS1, encoding="utf-8")
        bat_path.write_text(_WSL_TM_BAT, encoding="ascii")
    except OSError as e:
        return False, f"Failed to write relay script: {e}"
    win_bat = wslpath_to_win(bat_path)
    if win_bat is None:
        return False, f"wslpath failed for {bat_path}"
    if not shutil.which("cmd.exe"):
        return False, "cmd.exe not found (WSL interop disabled?)"
    # `start "" cmd.exe /k ...` opens a new console window. The relay itself
    # is not admin (firewall+portproxy already handle inbound; the PS UDP
    # loop just binds & forwards).
    title = f"tmsm UDP relay {port}"
    try:
        subprocess.Popen(
            ["cmd.exe", "/c", "start", title, "cmd.exe", "/k", win_bat, str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        return False, f"Failed to launch relay: {e}"
    return True, (f"Launched UDP relay window for port {port}.\n"
                  f"Keep that window open while players connect.\n"
                  f"Script: {ps1_path}")
