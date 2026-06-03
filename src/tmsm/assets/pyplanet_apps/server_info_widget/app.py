"""Server info widget.

Contrib-like compact server status panel: players/spectators and mode.
"""
from __future__ import annotations

import asyncio

from pyplanet.apps.tmsm.widgets.widget_base import WidgetAppBase


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
    WIDGET_HIDE_WHILE_DRIVING = False
    WIDGET_ANIM_DIR = "left"
    WIDGET_ANIM_DURATION_MS = 250
    WIDGET_ANIM_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "55aaffff"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._queued_refresh: asyncio.Task | None = None

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
        if mode_name.startswith("TrackMania\\"):
            mode_name = mode_name.split("\\")[-1]

        return {
            "players_text": f"{players}/{max_players if max_players > 0 else '?'}",
            "specs_text": f"{specs}/{max_specs if max_specs > 0 else '?'}",
            "mode_text": mode_name,
        }
