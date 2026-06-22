"""logging_engine — central per-app log level register + master UI.

On start this app discovers every loaded ``pyplanet.apps.tmsm.*`` app and
registers a ``Log level`` setting on each of them (see :mod:`.loglevel`), so
developers can raise/lower verbosity per app — e.g. set only ``widget_engine``
to ``DEBUG`` — without flooding the console by touching the root logger.

Control surfaces:
  * ``//loglevel`` admin chat command (list / show / set / set-all).
  * A master-admin-only panel (hub tile or ``//logging``) with a per-app
    level button grid.
"""
from __future__ import annotations

import asyncio
import logging

from pyplanet.apps.config import AppConfig
from pyplanet.contrib.command import Command

from .loglevel import (
    LOG_LEVELS,
    apply_level,
    register_log_level_setting,
    registry,
)

logger = logging.getLogger(__name__)

_TMSM_PREFIX = "pyplanet.apps.tmsm."

# Per-level display data for the UI grid.
_ABBR = {
    "DEFAULT": "DEF", "DEBUG": "DBG", "INFO": "INF",
    "WARNING": "WRN", "ERROR": "ERR", "CRITICAL": "CRT",
}
_COLORS = {
    "DEFAULT": "aaa", "DEBUG": "5cf", "INFO": "3d6",
    "WARNING": "fc4", "ERROR": "f80", "CRITICAL": "f44",
}

try:  # master-only hub tile is optional — works without tmsm_hub installed.
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

try:
    from pyplanet.apps.tmsm.ui import perms as _perms
except Exception:
    _perms = None

try:
    from .views import LoggingEngineView
    _HAS_VIEW = True
except Exception:
    LoggingEngineView = None  # type: ignore[assignment]
    _HAS_VIEW = False


def _short(label: str) -> str:
    """Drop the ``tmsm_`` label prefix for display (Py3.8-safe)."""
    return label[5:] if label.startswith("tmsm_") else label


class LoggingApp(AppConfig):
    name = "pyplanet.apps.tmsm.logging_engine"
    label = "logging_engine"
    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_hub"]
    game_dependencies = ["trackmania", "trackmania_next"]

    HUB_KEY = "logging"
    HUB_NAME = "Logging Engine"
    HUB_ICON = "bug"
    HUB_DESCRIPTION = "Per-app log verbosity (master only)."
    HUB_ORDER = 90

    PAGE_SIZE = 15

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.view = None
        self._page: dict[str, int] = {}            # login -> page
        self._status: dict[str, tuple[str, str]] = {}  # login -> (msg, color)

    # ---- lifecycle -----------------------------------------------------

    async def on_start(self) -> None:
        if _HAS_VIEW:
            try:
                self.view = LoggingEngineView(self)
                self.view.handle_catch_all = self._catch_all
            except Exception:
                logger.exception("logging_engine: view init failed")
                self.view = None

        try:
            await self.instance.command_manager.register(
                Command(
                    command="loglevel", target=self._cmd_loglevel, admin=True,
                    description="Per-app log level: //loglevel [app|all] [LEVEL]",
                ).add_param("args", nargs="*", type=str, required=False),
            )
        except Exception:
            logger.exception("logging_engine: /loglevel command registration failed")
        try:
            await self.instance.command_manager.register(
                Command(
                    command="logging", target=self._cmd_open, admin=True,
                    description="Open the Logging Engine panel (master only).",
                ),
            )
        except Exception:
            logger.exception("logging_engine: /logging command registration failed")

        # Register a master-only hub tile when the hub is available.
        if _HAS_HUB and self.view is not None:
            try:
                sig = self.context.signals.get_signal("tmsm_hub:register")
                entry = HubAppEntry(
                    key=self.HUB_KEY, name=self.HUB_NAME, icon=self.HUB_ICON,
                    role=Role.MASTER, order=self.HUB_ORDER,
                    description=self.HUB_DESCRIPTION, open=self._open,
                )
                await sig.send_robust({"entry": entry}, raw=True)
            except KeyError:
                logger.info("logging_engine: tmsm_hub:register not ready yet")
            except Exception:
                logger.exception("logging_engine: hub tile registration failed")

        # Apps load in dependency order, so most tmsm apps start *after* us.
        # Defer discovery until registrations settle, then sweep twice to
        # catch late starters. register_log_level_setting() is idempotent.
        try:
            asyncio.ensure_future(self._discover_and_register())
        except Exception:
            logger.exception("logging_engine: schedule discovery failed")

    async def on_stop(self) -> None:
        if self.view is not None:
            try:
                await self.view.destroy()
            except Exception:
                pass

    async def _discover_and_register(self) -> None:
        for delay in (3.0, 6.0):
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            await self._register_all()
        logger.info(
            "logging_engine: ready — %d app(s) have a Log level setting",
            len(registry()),
        )

    async def _register_all(self) -> None:
        try:
            apps = list(self.instance.apps.apps.items())
        except Exception:
            return
        for label, app in apps:
            name = getattr(app, "name", "") or ""
            if not name.startswith(_TMSM_PREFIX):
                continue
            try:
                await register_log_level_setting(app)
            except Exception:
                logger.exception("logging_engine: register for %s failed", label)

    # ---- permission gate ----------------------------------------------

    def _is_master(self, player) -> bool:
        if _perms is not None:
            try:
                return _perms.is_master(player)
            except Exception:
                pass
        return int(getattr(player, "level", 0)) >= 3

    # ---- registry helpers ---------------------------------------------

    def _resolve(self, target: str):
        """Resolve a user-supplied app token to a registry label, or None."""
        reg = registry()
        t = (target or "").strip().lower()
        for label in reg:
            low = label.lower()
            if low == t or low == "tmsm_" + t or _short(low) == t:
                return label
        return None

    async def _set_level(self, label: str, level: str) -> bool:
        reg = registry()
        entry = reg.get(label)
        if entry is None:
            return False
        log_name, setting = entry
        try:
            await setting.set_value(level)
        except Exception:
            logger.exception("logging_engine: set_value failed for %s", label)
            return False
        # Apply explicitly too, so the change lands even if the setting's
        # change_target hook is not invoked on this PyPlanet version.
        apply_level(log_name, level)
        return True

    # ---- UI ------------------------------------------------------------

    async def panel_context(self, login):
        reg = registry()
        items = sorted(reg.items())
        total = len(items)
        total_pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        page = self._page.get(login, 1) if login else 1
        page = max(1, min(page, total_pages))
        start = (page - 1) * self.PAGE_SIZE
        rows = []
        for label, (log_name, setting) in items[start:start + self.PAGE_SIZE]:
            try:
                val = str(await setting.get_value() or "DEFAULT").upper()
            except Exception:
                val = "DEFAULT"
            if val not in LOG_LEVELS:
                val = "DEFAULT"
            rows.append({
                "label": label, "short": _short(label),
                "logger": log_name, "level": val,
            })
        status = self._status.get(login) if login else None
        return {
            "rows": rows,
            "levels": list(LOG_LEVELS),
            "abbr": _ABBR,
            "colors": _COLORS,
            "page": page,
            "total_pages": total_pages,
            "total_count": total,
            "status_text": status[0] if status else "",
            "status_color": status[1] if status else "aaa",
        }

    async def _open(self, player) -> None:
        if self.view is None:
            return
        if not self._is_master(player):
            await self.instance.chat(
                "$z$s$f00>> Logging Engine is master-admins only.", player)
            return
        try:
            await self.view.display(player_logins=[player.login])
        except Exception:
            logger.exception("logging_engine: open display failed")

    async def _catch_all(self, player, action, values, **kwargs):
        if not self._is_master(player):
            return

        if action.startswith("set__"):
            body = action[len("set__"):]
            label, _sep, level = body.rpartition("__")
            level = level.strip().upper()
            if label and level in LOG_LEVELS:
                ok = await self._set_level(label, level)
                self._status[player.login] = (
                    (f"{_short(label)} -> {level}", _COLORS.get(level, "0f0"))
                    if ok else ("could not set level", "f44"))
            await self._open(player)
            return

        if action == "all_debug":
            await self._apply_all("DEBUG")
            self._status[player.login] = ("all apps -> DEBUG", "5cf")
            await self._open(player)
            return

        if action == "all_default":
            await self._apply_all("DEFAULT")
            self._status[player.login] = ("all apps -> DEFAULT", "aaa")
            await self._open(player)
            return

        if action == "refresh":
            self._status.pop(player.login, None)
            await self._open(player)
            return

        if action.startswith("pg__"):
            await self._handle_page(player, action[len("pg__"):])
            await self._open(player)
            return

    async def _apply_all(self, level: str) -> None:
        for label in list(registry().keys()):
            await self._set_level(label, level)

    async def _handle_page(self, player, tail: str) -> None:
        total = len(registry())
        total_pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        cur = self._page.get(player.login, 1)
        if tail == "first":
            cur = 1
        elif tail == "prev":
            cur -= 1
        elif tail == "next":
            cur += 1
        elif tail == "last":
            cur = total_pages
        elif tail.startswith("page__"):
            try:
                cur = int(tail[len("page__"):])
            except ValueError:
                pass
        self._page[player.login] = max(1, min(cur, total_pages))

    # ---- chat commands -------------------------------------------------

    async def _cmd_open(self, player, data, **kwargs) -> None:
        await self._open(player)

    async def _cmd_loglevel(self, player, data, **kwargs) -> None:
        args = list(getattr(data, "args", None) or [])
        reg = registry()

        if not args:
            if not reg:
                await self.instance.chat(
                    "$z$s$f80>> No tmsm apps registered yet.", player)
                return
            lines = ["$z$s$f80>> Log levels:"]
            for label in sorted(reg):
                _log_name, setting = reg[label]
                try:
                    val = await setting.get_value()
                except Exception:
                    val = "?"
                lines.append(f"$fff{_short(label)}$g$z: $ff0{val}")
            await self.instance.chat("\n".join(lines), player)
            return

        token = args[0].strip().lower()
        level = args[1].strip().upper() if len(args) > 1 else None

        # //loglevel all <LEVEL>
        if token == "all":
            if level is None or level not in LOG_LEVELS:
                await self.instance.chat(
                    "$z$s$f00>> Usage: //loglevel all <" + "|".join(LOG_LEVELS) + ">",
                    player)
                return
            await self._apply_all(level)
            await self.instance.chat(
                f"$z$s$f80>> All apps log level -> $ff0{level}", player)
            return

        label = self._resolve(token)
        if label is None:
            await self.instance.chat(
                f"$z$s$f00>> Unknown app '{args[0]}'. Use //loglevel to list.",
                player)
            return
        _log_name, setting = reg[label]

        if level is None:
            try:
                val = await setting.get_value()
            except Exception:
                val = "?"
            await self.instance.chat(
                f"$z$s$f80>> {_short(label)}: $ff0{val}", player)
            return

        if level not in LOG_LEVELS:
            await self.instance.chat(
                "$z$s$f00>> Invalid level. Choose: " + ", ".join(LOG_LEVELS),
                player)
            return
        if await self._set_level(label, level):
            await self.instance.chat(
                f"$z$s$f80>> {_short(label)} log level -> $ff0{level}", player)
        else:
            await self.instance.chat("$z$s$f00>> Failed to set level.", player)
