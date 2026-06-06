"""Server info widget.

Contrib-like compact server status panel: players/spectators and mode.
"""
from __future__ import annotations

import asyncio
import time

from pyplanet.apps.tmsm.widget_engine import AnimDir, DriveMode
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase


class ServerInfoWidget(WidgetAppBase):
    name = "pyplanet.apps.tmsm.server_info_widget"
    label = "server_info_widget"

    WIDGET_KEY = "server_info"
    WIDGET_NAME = "Server Info"
    WIDGET_DESCRIPTION = "Server population and active mode/script."
    WIDGET_ICON = "server"
    WIDGET_TEMPLATE = "server_info_widget/server_info.xml"

    WIDGET_DEFAULT_X = -160.0
    WIDGET_DEFAULT_Y = 90.0
    WIDGET_DEFAULT_W = 58.0
    WIDGET_DEFAULT_H = 12.0

    WIDGET_REFRESH_SECONDS = 0.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_DRIVE_MODE = DriveMode.FIXED
    WIDGET_ANIM_DIR = AnimDir.LEFT
    WIDGET_ANIM_DURATION_MS = 250
    WIDGET_ANIM_IN_DELAY_MS = 0
    WIDGET_ANIM_OUT_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "55aaffff"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._queued_refresh: asyncio.Task | None = None
        self._server_name_cache: str = "-"
        self._server_name_last_fetch: float = 0.0
        self._server_password_protected: bool = False

    async def on_start(self) -> None:
        await super().on_start()
        try:
            self.context.signals.listen("maniaplanet:player_connect", self._on_refresh_signal)
            self.context.signals.listen("maniaplanet:player_disconnect", self._on_refresh_signal)
            self.context.signals.listen("maniaplanet:player_info_changed", self._on_refresh_signal)
            self.context.signals.listen("maniaplanet:map_start", self._on_refresh_signal)
            self.context.signals.listen("maniaplanet:loading_map_start", self._on_refresh_signal)
        except Exception:
            pass

    async def on_stop(self) -> None:
        if self._queued_refresh is not None:
            self._queued_refresh.cancel()
            self._queued_refresh = None
        await super().on_stop()

    def _queue_refresh(self) -> None:
        if self.view is None:
            return
        if self._queued_refresh is not None and not self._queued_refresh.done():
            return

        async def _flush() -> None:
            try:
                await asyncio.sleep(0.12)
                if self.view is not None:
                    await self.view.refresh()
            except Exception:
                pass
            finally:
                self._queued_refresh = None

        self._queued_refresh = asyncio.create_task(_flush())

    async def _on_refresh_signal(self, **kwargs) -> None:
        self._queue_refresh()

    async def _server_cfg_name(self) -> str:
        """Resolve dedicated_cfg `<server_options><name>` via GBX.

        `GetServerOptions` exposes the live options loaded from
        `dedicated_cfg.txt`; cache briefly to avoid per-player spam.
        """
        now = time.monotonic()
        if self._server_name_cache and (now - self._server_name_last_fetch) < 5.0:
            return self._server_name_cache
        try:
            opts = await asyncio.wait_for(self.instance.gbx("GetServerOptions"), timeout=0.6)
        except Exception:
            self._server_name_last_fetch = now
            return self._server_name_cache or "-"
        self._server_name_last_fetch = now
        if not isinstance(opts, dict):
            return self._server_name_cache or "-"
        name = str(opts.get("Name") or "").strip()
        if name:
            self._server_name_cache = name
        # Password-protected when either player or spectator password is set.
        pw_player = str(opts.get("Password") or "").strip()
        pw_spec = str(opts.get("PasswordForSpectator") or "").strip()
        self._server_password_protected = bool(pw_player or pw_spec)
        return self._server_name_cache or "-"

    @staticmethod
    def _short_mode_name(raw: str) -> str:
        """Compress mode script identifiers to a clean short label.

        Examples:
        - Trackmania/TM_TimeAttack_online -> TimeAttack
        - TrackMania\\Modes\\TM_Rounds_Online.Script.txt -> Rounds
        """
        s = str(raw or "").strip()
        if not s:
            return "-"

        # Keep only basename after path separators.
        s = s.replace("\\", "/")
        if "/" in s:
            s = s.split("/")[-1]

        # Remove common wrappers/prefix/suffix noise.
        for pre in ("TM_", "tm_"):
            if s.startswith(pre):
                s = s[len(pre):]
        for suf in ("_online", "_Online", ".Script.txt", ".Script", ".txt"):
            if s.endswith(suf):
                s = s[: -len(suf)]

        # Normalize separators and trim.
        s = s.replace("_", " ").replace("-", " ").strip()
        if not s:
            return "-"

        # Convert "Time Attack" -> "TimeAttack" while preserving
        # already-camel variants.
        parts = [p for p in s.split() if p]
        if len(parts) > 1:
            s = "".join(p[:1].upper() + p[1:] for p in parts)
        return s

    async def get_widget_data(self, login):
        pm = self.instance.player_manager
        players = int(getattr(pm, "count_players", 0) or 0)
        max_players = int(getattr(pm, "max_players", 0) or 0)
        specs = int(getattr(pm, "count_spectators", 0) or 0)
        max_specs = int(getattr(pm, "max_spectators", 0) or 0)

        mode_name = "-"
        try:
            mode_name = str(await self.instance.mode_manager.get_current_script() or "-")
        except Exception:
            pass

        server_name = await self._server_cfg_name()

        return {
            "server_name": server_name,
            "players_text": f"{players}/{max_players if max_players > 0 else '?'}",
            "specs_text": f"{specs}/{max_specs if max_specs > 0 else '?'}",
            "mode_text": self._short_mode_name(mode_name),
            "lock_icon": "&#xf023;" if self._server_password_protected else "&#xf09c;",
            "lock_color": "f55" if self._server_password_protected else "6d6",
        }
