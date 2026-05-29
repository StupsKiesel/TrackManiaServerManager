from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Iterable

import psutil

from .. import paths
from ..config import Config
from ..supervisor import ProcInfo, Status
from .base import Instance, Kind


def _port_in_use(host: str, port: int) -> bool:
    target = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    try:
        with socket.create_connection((target, port), timeout=0.3):
            return True
    except OSError:
        return False


class MariaDBInstance(Instance):
    """The portable MariaDB tmsm manages. There is exactly one."""

    kind = Kind.SERVICE
    name = "mariadb"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = paths.MARIADB_DIR

    def cwd(self) -> Path:
        return self.root

    def log_file(self) -> Path:
        return self.root / "mariadb.log"

    def argv(self) -> list[str]:
        mysqld = paths.MARIADB_DIST / "bin" / "mysqld"
        return [
            str(mysqld),
            f"--defaults-file={self.root / 'my.cnf'}",
            f"--basedir={paths.MARIADB_DIST}",
            f"--datadir={paths.MARIADB_DATA}",
            f"--socket={self.root / 'mysql.sock'}",
            f"--port={self.cfg.mariadb.port}",
            f"--bind-address={self.cfg.mariadb.host}",
            f"--lc-messages-dir={paths.MARIADB_DIST / 'share'}",
            f"--plugin-dir={paths.MARIADB_DIST / 'lib' / 'plugin'}",
            f"--log-error={self.root / 'mariadb.err'}",
            f"--pid-file={self._pid_file()}",
        ]

    def is_installed(self) -> bool:
        return (paths.MARIADB_DIST / "bin" / "mysqld").is_file()

    def _pid_file(self) -> Path:
        return self.root / "mysqld.pid"

    def _read_pid(self) -> int | None:
        try:
            txt = self._pid_file().read_text().strip()
            return int(txt) if txt else None
        except (OSError, ValueError):
            return None

    def _pid_alive(self, pid: int) -> bool:
        # Verify the PID actually belongs to mysqld -- after a host reboot
        # the recorded PID is often reused by an unrelated process, and a
        # bare `os.kill(pid, 0)` would falsely report MariaDB as running.
        try:
            p = psutil.Process(pid)
            name = (p.name() or "").lower()
            if "mysqld" in name or "mariadb" in name:
                return True
            try:
                exe = (p.exe() or "").lower()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                exe = ""
            if "mysqld" in exe or "mariadb" in exe:
                return True
            return False
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def status(self) -> ProcInfo:
        # MariaDB is supervised via its own pidfile, not screen — the
        # `linux-systemd` binary forks/detaches in a way that tears the
        # screen window down even though mysqld keeps running.
        pid = self._read_pid()
        if pid is None or not self._pid_alive(pid):
            return ProcInfo(Status.STOPPED)
        try:
            p = psutil.Process(pid)
            with p.oneshot():
                cpu = p.cpu_percent(interval=None)
                mem = p.memory_info().rss / (1024 * 1024)
            return ProcInfo(Status.RUNNING, pid=pid, cpu=cpu, mem_mb=mem)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return ProcInfo(Status.RUNNING, pid=pid)

    def start(self) -> int:
        if not self.is_installed():
            raise RuntimeError(
                "MariaDB is not installed yet. Use the wizard to install it first."
            )
        if self.is_running:
            raise RuntimeError("mariadb already running")
        # Refuse to start if something is already on our TCP port, otherwise
        # mysqld dies immediately and the user is left puzzled.
        port = self.cfg.mariadb.port
        if _port_in_use(self.cfg.mariadb.host, port):
            raise RuntimeError(
                f"Port {port} on {self.cfg.mariadb.host} is already in use.\n"
                f"  To use a different port, edit ~/.tmsm/config.toml:\n"
                f"    [mariadb]\n"
                f"    port = 3307\n"
                f"  Or to find and stop the conflicting process:\n"
                f"    sudo ss -tlnp | grep ':{port}'\n"
                f"    sudo systemctl disable --now mariadb mysql 2>/dev/null"
            )
        # Keep my.cnf in sync with config (e.g. after a port change).
        from ..installers.mariadb import write_my_cnf
        write_my_cnf()
        # Clean stale pidfile so a fresh one indicates this run.
        try:
            self._pid_file().unlink()
        except FileNotFoundError:
            pass
        log = self.log_file()
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("")
        # Detach from this process group so mysqld survives tmsm exit and so
        # any double-fork inside mysqld doesn't drag us along.
        with open(log, "ab", buffering=0) as fh:
            subprocess.Popen(
                self.argv(),
                cwd=str(self.cwd()),
                stdin=subprocess.DEVNULL,
                stdout=fh,
                stderr=fh,
                start_new_session=True,
                close_fds=True,
            )
        # Wait for either pidfile + port, or a hard failure.
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            pid = self._read_pid()
            if pid and self._pid_alive(pid) and _port_in_use(self.cfg.mariadb.host, port):
                return pid
            time.sleep(0.2)
        # Failed to come up.
        try:
            tail = "\n".join((self.root / "mariadb.err").read_text(errors="replace").splitlines()[-30:])
        except OSError:
            tail = ""
        raise RuntimeError(
            "MariaDB did not start within 20s.\n"
            + (f"Last error log:\n{tail}" if tail else "(error log is empty)")
        )

    def stop(self) -> bool:
        pid = self._read_pid()
        if pid is None or not self._pid_alive(pid):
            # Tidy any stale pidfile.
            try:
                self._pid_file().unlink()
            except FileNotFoundError:
                pass
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if not self._pid_alive(pid):
                break
            time.sleep(0.3)
        if self._pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            self._pid_file().unlink()
        except FileNotFoundError:
            pass
        return True

    def restart(self) -> int:
        self.stop()
        return self.start()

    def detail_rows(self) -> Iterable[tuple[str, str]]:
        st = self.status()
        installed = (paths.MARIADB_DIST / "bin" / "mysqld").exists()
        yield ("Type", "MariaDB (portable)")
        yield ("Installed", "yes" if installed else "no — run wizard")
        yield ("Host", self.cfg.mariadb.host)
        yield ("Port", str(self.cfg.mariadb.port))
        yield ("Data dir", str(paths.MARIADB_DATA))
        yield ("Status", st.status.value)
        if st.pid:
            yield ("PID", str(st.pid))
        if st.mem_mb is not None:
            yield ("Memory", f"{st.mem_mb:.1f} MB")

    def editable_files(self) -> list[tuple[str, "Path"]]:
        my_cnf = self.root / "my.cnf"
        return [("MariaDB config (my.cnf)", my_cnf)] if my_cnf.is_file() else []
