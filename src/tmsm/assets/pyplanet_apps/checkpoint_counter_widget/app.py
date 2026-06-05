"""Checkpoint counter widget.

Shows the player's current checkpoint index for the ongoing run as
`current/total` using TrackMania waypoint callbacks.
"""
from __future__ import annotations

import asyncio

from pyplanet.apps.tmsm.widget_engine import AnimDir, DriveMode
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase


class CheckpointCounterWidget(WidgetAppBase):
    name = "pyplanet.apps.tmsm.checkpoint_counter_widget"
    label = "checkpoint_counter_widget"

    WIDGET_KEY = "checkpoint_counter"
    WIDGET_NAME = "Checkpoint Counter"
    WIDGET_DESCRIPTION = "Current checkpoint in run (current/total)."
    WIDGET_ICON = "flag-checkered"
    WIDGET_TEMPLATE = "checkpoint_counter_widget/checkpoint_counter.xml"

    WIDGET_DEFAULT_X = 120.0
    WIDGET_DEFAULT_Y = 86.0
    WIDGET_DEFAULT_W = 22.0
    WIDGET_DEFAULT_H = 8.0

    # Event-driven refresh keeps frame script stable and avoids animation
    # flicker from frequent periodic full re-renders.
    WIDGET_REFRESH_SECONDS = 0.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_DRIVE_MODE = DriveMode.FIXED
    WIDGET_ANIM_DIR = AnimDir.RIGHT
    WIDGET_ANIM_DURATION_MS = 250
    WIDGET_ANIM_IN_DELAY_MS = 0
    WIDGET_ANIM_OUT_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "ffaa55ff"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cp_by_login: dict[str, int] = {}
        self._queued_refresh: asyncio.Task | None = None

    async def on_start(self) -> None:
        await super().on_start()
        try:
            self.context.signals.listen("trackmania:waypoint", self._on_waypoint)
            self.context.signals.listen("trackmania:give_up", self._on_giveup)
            self.context.signals.listen("maniaplanet:map_start", self._on_map_start)
            self.context.signals.listen("maniaplanet:player_disconnect", self._on_disconnect)
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
                await asyncio.sleep(0.10)
                if self.view is not None:
                    await self.view.refresh()
            except Exception:
                pass
            finally:
                self._queued_refresh = None

        self._queued_refresh = asyncio.create_task(_flush())

    async def _on_map_start(self, **kwargs) -> None:
        if not self._cp_by_login:
            return
        self._cp_by_login.clear()
        self._queue_refresh()

    async def _on_disconnect(self, player=None, login=None, **kwargs) -> None:
        key = str(login or getattr(player, "login", "") or "")
        if not key:
            return
        if key in self._cp_by_login:
            self._cp_by_login.pop(key, None)

    async def _on_giveup(self, player=None, **kwargs) -> None:
        login = str(getattr(player, "login", "") or "")
        if not login:
            return
        if self._cp_by_login.pop(login, None) is not None:
            self._queue_refresh()

    async def _on_waypoint(self, player=None, raw=None, **kwargs) -> None:
        if player is None or not isinstance(raw, dict):
            return
        login = str(getattr(player, "login", "") or "")
        if not login:
            return

        cp_zero = raw.get("checkpointinlap", raw.get("checkpointinrace", -1))
        try:
            current = int(cp_zero) + 1
        except (TypeError, ValueError):
            return
        if current < 0:
            return

        total = int(getattr(getattr(self.instance.map_manager, "current_map", None), "num_checkpoints", 0) or 0)
        if bool(raw.get("isendrace", False)) and total > 0:
            current = total
        elif total > 0:
            current = max(0, min(total, current))

        if self._cp_by_login.get(login) == current:
            return
        self._cp_by_login[login] = current
        self._queue_refresh()

    async def get_widget_data(self, login: str) -> dict:
        total = int(getattr(getattr(self.instance.map_manager, "current_map", None), "num_checkpoints", 0) or 0)
        current = int(self._cp_by_login.get(login, 0) or 0)
        if total > 0:
            current = max(0, min(total, current))
            progress = int((current / max(1, total)) * 100)
            ratio = f"{current}/{total}"
        else:
            progress = 0
            ratio = "0/0"
        return {
            "cp_current": current,
            "cp_total": total,
            "cp_ratio": ratio,
            "cp_progress": progress,
        }
