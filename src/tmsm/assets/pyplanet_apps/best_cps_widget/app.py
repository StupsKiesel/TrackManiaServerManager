"""Best CPs widget.

Tracks the best checkpoint split reached for each CP in the current round.
"""
from __future__ import annotations

import asyncio
from typing import Any

from pyplanet.apps.tmsm.widget_engine import AnimDir, DriveMode
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase
from pyplanet.utils import times


class BestCpsWidget(WidgetAppBase):
    name = "pyplanet.apps.tmsm.best_cps_widget"
    label = "best_cps_widget"

    WIDGET_KEY = "best_cps"
    WIDGET_NAME = "Best CPs"
    WIDGET_DESCRIPTION = "Best checkpoint split per CP in this round."
    WIDGET_ICON = "stopwatch"
    WIDGET_TEMPLATE = "best_cps_widget/best_cps.xml"

    WIDGET_DEFAULT_X = -120.0
    WIDGET_DEFAULT_Y = 72.0
    WIDGET_DEFAULT_W = 56.0
    WIDGET_DEFAULT_H = 22.0

    WIDGET_REFRESH_SECONDS = 0.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_DRIVE_MODE = DriveMode.FIXED
    WIDGET_ANIM_DIR = AnimDir.LEFT
    WIDGET_ANIM_DURATION_MS = 250
    WIDGET_ANIM_IN_DELAY_MS = 0
    WIDGET_ANIM_OUT_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "44cc88ff"

    # Row geometry mirrors template: first row at y=-3.6 and each next
    # row at -3.2. Reserve header strip before row list.
    ROW_LIMIT = 5
    _ROW_PITCH = 3.2
    _HEADER_RESERVED = 4.6

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._best_by_cp: dict[int, dict[str, Any]] = {}
        self._player_cp: dict[str, int] = {}
        self._queued_refresh: asyncio.Task | None = None

    async def on_start(self) -> None:
        await super().on_start()
        try:
            self.context.signals.listen("trackmania:waypoint", self._on_waypoint)
            self.context.signals.listen("maniaplanet:map_start", self._on_reset)
            self.context.signals.listen("trackmania:give_up", self._on_giveup)
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

    async def _on_reset(self, **kwargs) -> None:
        if not self._best_by_cp and not self._player_cp:
            return
        self._best_by_cp.clear()
        self._player_cp.clear()
        self._queue_refresh()

    async def _on_giveup(self, player=None, **kwargs) -> None:
        login = str(getattr(player, "login", "") or "")
        if not login:
            return
        if self._player_cp.pop(login, None) is not None:
            self._queue_refresh()

    async def _on_waypoint(self, player=None, raw=None, race_time=None, **kwargs) -> None:
        if player is None or not isinstance(raw, dict):
            return
        login = str(getattr(player, "login", "") or "")
        nickname = str(getattr(player, "nickname", login) or login)
        if not login:
            return

        cp_raw = raw.get("checkpointinlap", raw.get("checkpointinrace", -1))
        try:
            cp_idx = int(cp_raw) + 1
        except (TypeError, ValueError):
            return
        if cp_idx <= 0:
            return

        split_raw = raw.get("racetime", race_time)
        try:
            split_ms = int(split_raw or 0)
        except (TypeError, ValueError):
            return
        if split_ms <= 0:
            return

        self._player_cp[login] = cp_idx
        current = self._best_by_cp.get(cp_idx)
        if current is None or split_ms < int(current.get("time", 0) or 0):
            self._best_by_cp[cp_idx] = {
                "cp": cp_idx,
                "time": split_ms,
                "nickname": nickname,
                "login": login,
            }
            self._queue_refresh()

    def _visible_row_capacity(self, login: str) -> int:
        """Compute how many rows fit in current resolved widget height."""
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
        # Keep at least one line visible and never exceed historical hard cap.
        return max(1, min(self.ROW_LIMIT * 4, fit))

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        total = int(getattr(getattr(self.instance.map_manager, "current_map", None), "num_checkpoints", 0) or 0)
        current_cp = int(self._player_cp.get(login, 0) or 0)
        visible_rows = self._visible_row_capacity(login)
        rows = []
        max_cp = total if total > 0 else max(visible_rows, max(self._best_by_cp.keys() or [0]))
        max_cp = max(1, max_cp)

        if max_cp <= visible_rows:
            first_cp = 1
            last_cp = max_cp
        else:
            # Sliding window: when the local player progresses beyond the
            # viewport, drop the oldest CP row and append the next CP row.
            end_cp = current_cp if current_cp > 0 else visible_rows
            end_cp = max(visible_rows, min(end_cp, max_cp))
            first_cp = end_cp - visible_rows + 1
            last_cp = end_cp

        for cp in range(first_cp, last_cp + 1):
            rec = self._best_by_cp.get(cp)
            rows.append({
                "cp": cp,
                "time": times.format_time(int(rec.get("time", 0))) if rec else "--:--.---",
                "nickname": str(rec.get("nickname", "-")) if rec else "-",
            })

        title_right = f"CP {current_cp}/{max_cp}" if max_cp > 0 else "CP 0/0"
        if current_cp <= 0:
            title_right = "CP --"

        return {
            "rows": rows,
            "title_left": "BEST CPs",
            "title_right": title_right,
        }
