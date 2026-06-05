"""Random MX Points widget.

Shows live points leaderboard and player progress for the
`random_challenge_points` mode.
"""
from __future__ import annotations

import asyncio
from typing import Any

from pyplanet.apps.tmsm.widget_engine import AnimDir, DriveMode
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase


class RandomMxPointsWidget(WidgetAppBase):
    name = "pyplanet.apps.tmsm.random_mx_points_widget"
    label = "random_mx_points_widget"

    WIDGET_KEY = "random_mx_points"
    WIDGET_NAME = "Random MX Points"
    WIDGET_DESCRIPTION = "Ranking, player points, delta to AT, and mode time left."
    WIDGET_ICON = "trophy"
    WIDGET_TEMPLATE = "random_mx_points_widget/widget.xml"

    WIDGET_DEFAULT_X = -126.0
    WIDGET_DEFAULT_Y = 70.0
    WIDGET_DEFAULT_W = 58.0
    WIDGET_DEFAULT_H = 22.0

    # Needs a ticking refresh for the mode-end countdown.
    WIDGET_REFRESH_SECONDS = 1.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_DRIVE_MODE = DriveMode.FIXED
    # Periodic renders + no animation avoids pop-in artifacts.
    WIDGET_ANIM_DIR = AnimDir.NONE
    WIDGET_ANIM_DURATION_MS = 0
    WIDGET_ANIM_IN_DELAY_MS = 0
    WIDGET_ANIM_OUT_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "f0bb33ff"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._queued_refresh: asyncio.Task | None = None

    async def on_start(self) -> None:
        await super().on_start()
        try:
            self.context.signals.listen("trackmania:finish", self._on_refresh_signal)
            self.context.signals.listen("maniaplanet:map_begin", self._on_refresh_signal)
            self.context.signals.listen("maniaplanet:map_start", self._on_refresh_signal)
            self.context.signals.listen("maniaplanet:player_connect", self._on_refresh_signal)
            self.context.signals.listen("maniaplanet:player_disconnect", self._on_refresh_signal)
            self.context.signals.listen("maniaplanet:player_chat", self._on_refresh_signal)
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
                await asyncio.sleep(0.15)
                if self.view is not None:
                    await self.view.refresh()
            except Exception:
                pass
            finally:
                self._queued_refresh = None

        self._queued_refresh = asyncio.create_task(_flush())

    async def _on_refresh_signal(self, **kwargs) -> None:
        self._queue_refresh()

    def _fallback(self) -> dict[str, Any]:
        return {
            "is_active": False,
            "time_left_text": "--:--",
            "my_rank_text": "--",
            "my_points": 0,
            "at_delta_text": "--",
            "rows": [],
            "status_text": "Mode inactive",
        }

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        apps = getattr(self.instance.apps, "apps", {})
        gm = apps.get("tmsm_gamemodes")
        if gm is None:
            out = self._fallback()
            out["status_text"] = "Game modes app not loaded"
            return out

        active = getattr(gm, "_active", None)
        if active is None or str(getattr(active, "key", "") or "") != "random_challenge_points":
            out = self._fallback()
            out["status_text"] = "Random MX Points mode inactive"
            return out

        snap = None
        try:
            getter = getattr(active, "widget_snapshot", None)
            snap = getter(login) if callable(getter) else None
        except Exception:
            snap = None

        if not isinstance(snap, dict):
            out = self._fallback()
            out["status_text"] = "Snapshot unavailable"
            return out

        rows = []
        for row in list(snap.get("top_rows") or [])[:5]:
            rows.append({
                "rank": int(row.get("rank") or 0),
                "nickname": str(row.get("nickname") or "player"),
                "points": int(row.get("points") or 0),
            })

        my_rank = int(snap.get("my_rank") or 0)
        out = {
            "is_active": bool(snap.get("active", True)),
            "time_left_text": str(snap.get("time_left_text") or "--:--"),
            "my_rank_text": (str(my_rank) if my_rank > 0 else "--"),
            "my_points": int(snap.get("my_points") or 0),
            "at_delta_text": str(snap.get("at_delta_text") or "--"),
            "rows": rows,
            "status_text": "Live",
        }
        return out
