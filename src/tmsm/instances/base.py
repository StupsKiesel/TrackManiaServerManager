from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import Iterable

from .. import supervisor
from ..supervisor import ProcInfo, Status


class Kind(str, Enum):
    SERVER = "server"
    POOL = "pool"
    SERVICE = "service"
    BOT = "bot"


class Instance(ABC):
    """Common interface for everything shown in the main list."""

    kind: Kind
    name: str
    root: Path     # the instance's folder under ~/.tmsm/

    # --- subclass contract ---

    @abstractmethod
    def argv(self) -> list[str]: ...

    @abstractmethod
    def cwd(self) -> Path: ...

    @abstractmethod
    def log_file(self) -> Path: ...

    def env(self) -> dict[str, str]:
        return {}

    # --- detail rendering ---

    @abstractmethod
    def detail_rows(self) -> Iterable[tuple[str, str]]:
        """Key/value pairs shown in the details pane."""

    def editable_files(self) -> list[tuple[str, Path]]:
        """List of (label, path) tuples for config files exposed in the editor."""
        return []

    def extra_log_files(self) -> list[tuple[str, Path]]:
        """Additional log files (besides log_file()) shown in the log viewer picker."""
        return []

    # --- lifecycle ---

    def status(self) -> ProcInfo:
        return supervisor.status(self.name)

    def start(self) -> int:
        return supervisor.start(self.name, self.argv(), self.cwd(), self.log_file(), self.env())

    def stop(self) -> bool:
        return supervisor.stop(self.name)

    def restart(self) -> int:
        return supervisor.restart(self.name, self.argv(), self.cwd(), self.log_file(), self.env())

    # --- ui helpers ---

    @property
    def is_running(self) -> bool:
        return self.status().status is Status.RUNNING

    # --- table column helpers (override in subclasses as needed) ---

    def xmlrpc_port_str(self) -> str:
        """XMLRPC/RPC port shown in the main table."""
        return "—"

    def account_name(self) -> str:
        """Master-server login shown in the main table."""
        return "—"

    def cmd_summary(self) -> str:
        """Short display of the start command."""
        try:
            parts = self.argv()
            return Path(parts[0]).name + (" " + " ".join(parts[1:]) if len(parts) > 1 else "")
        except Exception:
            return "—"

    def screen_session(self) -> str:
        """Name of the GNU screen session used by the supervisor."""
        return supervisor.session_name(self.name)
