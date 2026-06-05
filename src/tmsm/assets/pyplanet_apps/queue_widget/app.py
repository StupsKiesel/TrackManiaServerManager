"""Queue status widget.

Contrib-like queue panel showing queue size and the viewer's position.
"""
from __future__ import annotations

import asyncio

from pyplanet.apps.tmsm.widget_engine import AnimDir, DriveMode
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase


class QueueWidget(WidgetAppBase):
    name = "pyplanet.apps.tmsm.queue_widget"
    label = "queue_widget"

    WIDGET_KEY = "queue_status"
    WIDGET_NAME = "Queue"
    WIDGET_DESCRIPTION = "Spectator queue status and your position."
    WIDGET_ICON = "list-ol"
    WIDGET_TEMPLATE = "queue_widget/queue.xml"

    WIDGET_DEFAULT_X = -122.0
    WIDGET_DEFAULT_Y = -66.0
    WIDGET_DEFAULT_W = 42.0
    WIDGET_DEFAULT_H = 11.0

    WIDGET_REFRESH_SECONDS = 0.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_DRIVE_MODE = DriveMode.FIXED
    WIDGET_ANIM_DIR = AnimDir.LEFT
    WIDGET_ANIM_DURATION_MS = 250
    WIDGET_ANIM_IN_DELAY_MS = 0
    WIDGET_ANIM_OUT_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "ffbb44ff"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._queued_refresh: asyncio.Task | None = None

    async def on_start(self) -> None:
        await super().on_start()
        try:
            self.context.signals.listen("maniaplanet:player_connect", self._on_refresh_signal)
            self.context.signals.listen("maniaplanet:player_disconnect", self._on_refresh_signal)
            self.context.signals.listen("maniaplanet:player_info_changed", self._on_refresh_signal)
            self.context.signals.listen("maniaplanet:player_enter_player_slot", self._on_refresh_signal)
            self.context.signals.listen("maniaplanet:player_enter_spectator_slot", self._on_refresh_signal)
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
        queue_app = getattr(self.instance.apps, "apps", {}).get("queue")
        if queue_app is None:
            return {
                "queue_total": 0,
                "queue_position": "--",
                "status_text": "Queue app not loaded",
            }

        total = 0
        try:
            total = int(await queue_app.list.count())
        except Exception:
            total = 0

        pos_text = "--"
        status_text = "Not in queue"
        try:
            player = await self.instance.player_manager.get_player(login=login)
            pos = await queue_app.list.get_position(player)
            if pos is not None:
                pos_text = str(int(pos))
                status_text = "Queued"
        except Exception:
            pass

        return {
            "queue_total": total,
            "queue_position": pos_text,
            "status_text": status_text,
        }
