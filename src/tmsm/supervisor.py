"""Process supervision via GNU `screen`.

Every instance runs inside a detached named screen session `tmsm-<name>`.
Source of truth for "is it running" is `screen -ls`. PID files are not used.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence

import psutil

from . import paths


SCREEN_PREFIX = "tmsm-"


def _screen_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Env with SCREENDIR forced to a persistent, 0700 dir under ~/.tmsm.

    The default /run/screen/S-<user> gets wiped on WSL boot and frequently
    ends up with the wrong perms, which makes `screen` exit 1 with no log.
    Routing every screen invocation through ~/.tmsm/screen makes this
    self-healing across reboots.
    """
    env = dict(base) if base is not None else os.environ.copy()
    paths.SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        paths.SCREEN_DIR.chmod(0o700)
    except OSError:
        pass
    env["SCREENDIR"] = str(paths.SCREEN_DIR)
    return env


class Status(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    CRASHED = "crashed"   # reserved for future use


@dataclass
class ProcInfo:
    status: Status
    pid: int | None = None      # PID of the inner server, not the screen wrapper
    cpu: float | None = None
    mem_mb: float | None = None


def session_name(name: str) -> str:
    return f"{SCREEN_PREFIX}{name}"


def _list_sessions() -> dict[str, int]:
    """Return {session_name: screen_pid} parsed from `screen -ls`."""
    try:
        out = subprocess.run(
            ["screen", "-ls"], capture_output=True, text=True, check=False,
            env=_screen_env(),
        ).stdout
    except FileNotFoundError:
        return {}
    sessions: dict[str, int] = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\.(\S+)\s+", line)
        if m:
            sessions[m.group(2)] = int(m.group(1))
    return sessions


def _inner_pid(screen_pid: int) -> int | None:
    """Find the spawned child of the screen wrapper (the actual server)."""
    try:
        proc = psutil.Process(screen_pid)
        kids = proc.children(recursive=False)
        return kids[0].pid if kids else None
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def status(name: str) -> ProcInfo:
    sessions = _list_sessions()
    sess = session_name(name)
    if sess not in sessions:
        return ProcInfo(Status.STOPPED)
    screen_pid = sessions[sess]
    inner = _inner_pid(screen_pid)
    target_pid = inner or screen_pid
    try:
        p = psutil.Process(target_pid)
        with p.oneshot():
            cpu = p.cpu_percent(interval=None)
            mem = p.memory_info().rss / (1024 * 1024)
        return ProcInfo(Status.RUNNING, pid=target_pid, cpu=cpu, mem_mb=mem)
    except psutil.NoSuchProcess:
        # Stale screen socket from before reboot/crash.
        return ProcInfo(Status.STOPPED)
    except psutil.AccessDenied:
        return ProcInfo(Status.RUNNING, pid=target_pid)


def prune_stale_sessions() -> None:
    """Remove dead screen session sockets (e.g. after a host reboot).

    `screen -ls` keeps listing sockets in $SCREENDIR even after the
    corresponding processes died, which makes `status()` report stale
    sessions as RUNNING (their PID is often reused by an unrelated
    process after reboot). `screen -wipe` deletes those sockets.
    """
    try:
        subprocess.run(
            ["screen", "-wipe"], capture_output=True, text=True, check=False,
            env=_screen_env(),
        )
    except FileNotFoundError:
        pass


def start(
    name: str,
    argv: Sequence[str],
    cwd: Path,
    log_file: Path,
    env: dict[str, str] | None = None,
) -> int:
    """Spawn the command inside a detached screen session. Returns inner PID (or screen PID)."""
    sess = session_name(name)
    if sess in _list_sessions():
        raise RuntimeError(f"{name} already running")

    if not argv:
        raise RuntimeError(f"{name} has no command to run")

    # Pre-flight: make sure the program actually exists, otherwise screen will
    # spawn, the inner command exits instantly, and the session disappears
    # before we can see it — producing the unhelpful "session not found".
    exe = argv[0]
    exe_path = Path(exe)
    if exe_path.is_absolute() or os.sep in exe or "/" in exe:
        if not exe_path.is_file():
            raise RuntimeError(
                f"{name}: executable not found: {exe}\n"
                f"The instance is probably not installed yet."
            )
        if not os.access(exe_path, os.X_OK):
            raise RuntimeError(f"{name}: not executable: {exe}")

    log_file.parent.mkdir(parents=True, exist_ok=True)
    # Truncate so the post-start tail only shows this run's output
    log_file.write_text("")

    cmd = [
        "screen",
        "-dmS", sess,
        "-L", "-Logfile", str(log_file),
        *argv,
    ]
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    full_env = _screen_env(full_env)
    try:
        subprocess.run(cmd, cwd=str(cwd), env=full_env, check=True)
    except FileNotFoundError as e:
        raise RuntimeError("`screen` is not installed. Run: sudo apt install screen") from e
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"screen failed to start {name} (exit {e.returncode})") from e

    # Give the inner process a moment to settle (or to fail) before we check.
    sp: int | None = None
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        sp = _list_sessions().get(sess)
        if sp is not None:
            break
        time.sleep(0.1)

    if sp is None:
        tail = _tail_log(log_file, 20)
        msg = (
            f"{name} exited immediately — the screen session is gone.\n"
            f"Command: {' '.join(argv)}"
        )
        if tail:
            msg += f"\nLast log lines:\n{tail}"
        else:
            msg += "\n(log file is empty)"
        raise RuntimeError(msg)
    return _inner_pid(sp) or sp


def _tail_log(path: Path, lines: int) -> str:
    try:
        data = path.read_text(errors="replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-lines:])


def stop(name: str, grace: float = 10.0) -> bool:
    sess = session_name(name)
    sessions = _list_sessions()
    if sess not in sessions:
        return False
    subprocess.run(["screen", "-S", sess, "-X", "quit"], check=False, env=_screen_env())
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if sess not in _list_sessions():
            return True
        time.sleep(0.2)
    # Force kill if screen didn't quit
    sessions = _list_sessions()
    if sess in sessions:
        try:
            os.kill(sessions[sess], signal.SIGKILL)
        except ProcessLookupError:
            pass
    return True


def restart(name: str, argv: Sequence[str], cwd: Path, log_file: Path,
            env: dict[str, str] | None = None) -> int:
    stop(name)
    return start(name, argv, cwd, log_file, env)


def attach_command(name: str) -> list[str]:
    """Command to attach to the screen session interactively. Detach with Ctrl-A d."""
    return ["env", f"SCREENDIR={paths.SCREEN_DIR}", "screen", "-r", session_name(name)]


@dataclass
class ScreenSession:
    session: str       # full screen session name (e.g. "tmsm-myserver")
    screen_pid: int    # PID of the screen wrapper
    inner_pid: int | None
    managed: bool      # True if name starts with SCREEN_PREFIX
    inst_name: str | None  # logical name (session without prefix) if managed


def list_all_sessions() -> list[ScreenSession]:
    """All running screen sessions on the system, not just tmsm-managed ones."""
    out: list[ScreenSession] = []
    for sess, spid in _list_sessions().items():
        managed = sess.startswith(SCREEN_PREFIX)
        out.append(ScreenSession(
            session=sess,
            screen_pid=spid,
            inner_pid=_inner_pid(spid),
            managed=managed,
            inst_name=sess[len(SCREEN_PREFIX):] if managed else None,
        ))
    out.sort(key=lambda s: (not s.managed, s.session))
    return out


def attach_command_raw(session: str) -> list[str]:
    """Attach command for an arbitrary screen session name."""
    return ["env", f"SCREENDIR={paths.SCREEN_DIR}", "screen", "-r", session]


def kill_session(session: str, grace: float = 5.0) -> bool:
    """Kill an arbitrary screen session by full name. Returns True if it was running."""
    sessions = _list_sessions()
    if session not in sessions:
        return False
    subprocess.run(["screen", "-S", session, "-X", "quit"], check=False, env=_screen_env())
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if session not in _list_sessions():
            return True
        time.sleep(0.2)
    still = _list_sessions()
    if session in still:
        try:
            os.kill(still[session], signal.SIGKILL)
        except ProcessLookupError:
            pass
    return True
