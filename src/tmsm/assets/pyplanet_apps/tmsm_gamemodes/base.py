"""Base abstractions for gamemodes.

A mode is a plug-in class with a small lifecycle. The orchestrator owns
every shared service (vote engine, map picker, jukebox, notifier) and
exposes them to the mode via a ``GameModeContext`` proxy so modes never
reach into the AppConfig directly.
"""
from __future__ import annotations

import logging
from abc import ABC
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:                      # pragma: no cover - typing only
    from .app import TmsmGamemodesApp
    from .picker import MapPicker
    from .votes import VoteEngine

logger = logging.getLogger(__name__)


# A schema entry shapes a single operator-tunable config field. Used by
# the operator UI to render an editor row. Type is one of:
#   "int" "bool" "choice" "str"
class ConfigField(dict):
    @classmethod
    def make(cls, key: str, label: str, type: str,
             default: Any = None, *, help: str = "",
             min: int | None = None, max: int | None = None,
             choices: list[tuple[Any, str]] | None = None) -> "ConfigField":
        return cls(key=key, label=label, type=type, default=default,
                   help=help, min=min, max=max, choices=choices or [])


class GameModeContext:
    """Service proxy handed to every mode. Modes use *only* this surface."""

    def __init__(self, app: "TmsmGamemodesApp", mode_key: str) -> None:
        self._app = app
        self._mode_key = mode_key

    # ---- services ------------------------------------------------------

    @property
    def picker(self) -> "MapPicker":
        return self._app.picker

    @property
    def votes(self) -> "VoteEngine":
        return self._app.votes

    @property
    def instance(self):
        return self._app.instance

    @property
    def game(self) -> str:
        try:
            return str(self._app.instance.game.game or "tmnext")
        except Exception:
            return "tmnext"

    # ---- mode-private persistent state --------------------------------

    def load_state(self) -> dict[str, Any]:
        return dict(self._app._state.get("mode_states", {}).get(self._mode_key) or {})

    def save_state(self, data: dict[str, Any]) -> None:
        self._app._state.setdefault("mode_states", {})[self._mode_key] = dict(data)
        self._app._save_state()

    # ---- ops UI hooks --------------------------------------------------

    def set_status(self, lines: list[str]) -> None:
        """Set the status text the operator panel renders for this mode."""
        self._app._mode_status_lines = list(lines)
        self._app._schedule_refresh()

    def chat(self, message: str, login: str | None = None) -> None:
        """Server chat (best-effort)."""
        async def _go():
            try:
                if login is None:
                    await self._app.instance.chat(message)
                else:
                    await self._app.instance.chat(message, login)
            except Exception:
                logger.exception("gamemodes: chat send failed")
        import asyncio
        asyncio.ensure_future(_go())

    async def notify(self, message: str, severity: str = "info",
                     login: str | None = None, duration_ms: int = 4000) -> None:
        await self._app._notify(message, severity, login, duration_ms)


class GameMode(ABC):
    """Subclass to add a new game mode.

    Override ``on_*`` lifecycle methods as needed; defaults are no-ops.
    All work goes through ``self.ctx`` (a ``GameModeContext``).
    """

    # ---- identity ------------------------------------------------------
    key: str = ""                 # unique registry key (lowercase, snake)
    name: str = ""                # short display name
    description: str = ""         # one-line subtitle for the operator UI
    icon: str = "cog"
    color: str = "15f"            # 3-digit hex accent
    category: str = "rotation"    # free-form group label

    # ---- lifecycle -----------------------------------------------------

    def __init__(self, ctx: GameModeContext) -> None:
        self.ctx = ctx

    def default_config(self) -> dict[str, Any]:
        """Static defaults; will be merged with persisted operator values."""
        return {}

    def config_schema(self) -> list[ConfigField]:
        """Drive the operator settings UI; empty = no settings row."""
        return []

    async def on_enable(self, config: dict[str, Any]) -> None:
        """Called once when the operator activates the mode."""

    async def on_disable(self) -> None:
        """Called when deactivated; clean up any pending votes/state."""

    async def on_map_begin(self, map_obj) -> None:
        """Map just started playing."""

    async def on_map_end(self, map_obj) -> None:
        """Map ended (before podium)."""

    async def on_podium_start(self) -> None:
        """Podium just appeared - typical hook for picking the next map."""

    # ---- status reporting (operator panel + HUD) -----------------------

    def status_lines(self) -> list[str]:
        """Short status lines for the operator panel."""
        return []


# Mode registry. Modes register themselves at import time.
REGISTRY: dict[str, type[GameMode]] = {}


def register(cls: type[GameMode]) -> type[GameMode]:
    """Decorator: register a GameMode subclass under its ``key``."""
    if not cls.key:
        raise ValueError(f"{cls.__name__}: GameMode.key must be set")
    REGISTRY[cls.key] = cls
    return cls


# Re-export for typing convenience.
VoteFinishCallback = Callable[[dict], Awaitable[None]]
