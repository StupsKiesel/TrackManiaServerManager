"""Round finishes widget.

Tracks finish callbacks on the current map and displays the fastest
finish times. Duplicates are intentionally allowed, so one player can
occupy multiple slots.
"""
from __future__ import annotations

import asyncio
from typing import Any

from pyplanet.apps.tmsm.widget_engine import AnimDir, DriveMode
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase
from pyplanet.utils import times


class RoundFinishesWidget(WidgetAppBase):
    name = "pyplanet.apps.tmsm.round_finishes_widget"
    label = "round_finishes_widget"

    WIDGET_KEY = "round_finishes"
    WIDGET_NAME = "Round Finishes"
    WIDGET_DESCRIPTION = "Fastest finish times on current map (duplicates allowed)."
    WIDGET_ICON = "trophy"
    WIDGET_TEMPLATE = "round_finishes_widget/widget.xml"

    WIDGET_DEFAULT_X = 130.0
    WIDGET_DEFAULT_Y = 50.0
    WIDGET_DEFAULT_W = 62.0
    WIDGET_DEFAULT_H = 22.0

    # Event-driven refresh keeps the widget smooth and avoids animation flicker.
    WIDGET_REFRESH_SECONDS = 0.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_DRIVE_MODE = DriveMode.FIXED
    WIDGET_ANIM_DIR = AnimDir.RIGHT
    WIDGET_ANIM_DURATION_MS = 250
    WIDGET_ANIM_IN_DELAY_MS = 0
    WIDGET_ANIM_OUT_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "d98c2fff"

    _ROW_PITCH = 3.2
    _HEADER_RESERVED = 4.6

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._queued_refresh: asyncio.Task | None = None
        self._seq: int = 0
        self._finishes: list[dict[str, Any]] = []

    async def on_start(self) -> None:
        await super().on_start()
        try:
            self.context.signals.listen("trackmania:finish", self._on_finish)
            self.context.signals.listen("maniaplanet:map_begin", self._on_reset)
            self.context.signals.listen("maniaplanet:map_start", self._on_reset)
        except Exception:
            pass

    async def on_stop(self) -> None:
        if self._queued_refresh is not None:
            self._queued_refresh.cancel()
            self._queued_refresh = None
        await super().on_stop()

    async def _on_reset(self, **kwargs) -> None:
        self._seq = 0
        self._finishes = []
        self._queue_refresh()

    async def _on_finish(self, player=None, lap_time=None, race_time=None, is_end_race=None, **kwargs) -> None:
        # Some modes omit is_end_race; only reject explicit False.
        if is_end_race is False:
            return

        try:
            score = int(lap_time or race_time or 0)
        except (TypeError, ValueError):
            score = 0
        if score <= 0:
            return

        login = str(getattr(player, "login", "") or "")
        nickname = str(getattr(player, "nickname", login) or login or "Unknown")

        self._seq += 1
        self._finishes.append({
            "seq": self._seq,
            "login": login,
            "nickname": nickname,
            "score": score,
        })
        # Fastest first, then first-to-set for tie stability.
        self._finishes.sort(key=lambda x: (int(x.get("score", 0) or 0), int(x.get("seq", 0) or 0)))
        self._queue_refresh()

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

    def _visible_row_capacity(self, login: str) -> int:
        h = float(self.WIDGET_DEFAULT_H)
        try:
            host = self.instance.apps.apps.get("widget_engine")
            if host is not None:
                resolved = host.engine.resolve(self.WIDGET_KEY, login)
                if resolved is not None:
                    h = float(getattr(resolved, "h", h) or h)
        except Exception:
            pass
        usable = max(0.0, h - self._HEADER_RESERVED)
        fit = int(usable // self._ROW_PITCH)
        return max(1, min(40, fit))

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        limit = self._visible_row_capacity(login)
        rows = []
        for idx, item in enumerate(self._finishes[:limit], start=1):
            rows.append({
                "rank": idx,
                "nickname": str(item.get("nickname") or "Unknown"),
                "score": times.format_time(int(item.get("score", 0) or 0)),
            })

        hidden = max(0, len(self._finishes) - len(rows))
        note = ""
        if not rows:
            note = "No finishes yet"
        elif hidden > 0:
            note = f"+{hidden} more"

        return {
            "rows": rows,
            "total_finishes": len(self._finishes),
            "note": note,
        }
