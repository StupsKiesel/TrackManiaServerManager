"""Best CPs 2 widget.

Same data logic as best_cps_widget (best split per CP in current round),
but rendered as horizontally flowing items that wrap to the next row.
"""
from __future__ import annotations

import asyncio
from typing import Any

from pyplanet.apps.tmsm.widget_engine import AnimDir, DriveMode
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase
from pyplanet.utils import times


class BestCps2Widget(WidgetAppBase):
    name = "pyplanet.apps.tmsm.best_cps2_widget"
    label = "best_cps2_widget"

    WIDGET_KEY = "best_cps2"
    WIDGET_NAME = "Best CPs 2"
    WIDGET_DESCRIPTION = "Best checkpoint split per CP in this round (wrapping layout)."
    WIDGET_ICON = "stopwatch"
    WIDGET_TEMPLATE = "best_cps2_widget/best_cps2.xml"

    # Top edge default placement.
    WIDGET_DEFAULT_X = 0.0
    WIDGET_DEFAULT_Y = 90.0
    WIDGET_DEFAULT_W = 120.0
    WIDGET_DEFAULT_H = 16.0

    WIDGET_REFRESH_SECONDS = 0.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_DRIVE_MODE = DriveMode.FIXED
    # Keep a concrete direction so widget_engine's disabled/active toggle
    # can move this widget out/in reliably.
    WIDGET_ANIM_DIR = AnimDir.LEFT
    WIDGET_ANIM_DURATION_MS = 250
    WIDGET_ANIM_IN_DELAY_MS = 0
    WIDGET_ANIM_OUT_DELAY_MS = 0

    # Fully transparent frame surface to avoid blocking the player's view.
    WIDGET_BG_COLOR = "0000"
    WIDGET_STRIP_ENABLED = False
    WIDGET_STRIP_COLOR = "44cc88ff"

    _ITEM_W = 38.0
    _ROW_H = 3.2
    _HEADER_RESERVED = 4.4

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
            cp_raw_i = int(cp_raw)
        except (TypeError, ValueError):
            return

        # Different callbacks/servers expose CP index as either 0-based or
        # 1-based. Finishes can also come through as an extra CP step.
        total_cps = int(getattr(getattr(self.instance.map_manager, "current_map", None), "num_checkpoints", 0) or 0)
        if total_cps > 0:
            if 0 <= cp_raw_i <= total_cps - 1:
                cp_idx = cp_raw_i + 1
            elif 1 <= cp_raw_i <= total_cps:
                cp_idx = cp_raw_i
            elif cp_raw_i == total_cps + 1:
                # Treat finish-as-extra as the last real checkpoint slot.
                cp_idx = total_cps
            else:
                return
        else:
            # Fallback when map metadata is unavailable.
            cp_idx = cp_raw_i + 1 if cp_raw_i >= 0 else cp_raw_i

        if cp_idx <= 0:
            return

        prev_cp = int(self._player_cp.get(login, 0) or 0)
        if cp_idx != prev_cp:
            self._player_cp[login] = cp_idx
            self._queue_refresh()

        split_raw = raw.get("racetime")
        if split_raw in (None, ""):
            split_raw = raw.get("checkpointtime", raw.get("cp_time", race_time))
        try:
            split_ms = int(split_raw or 0)
        except (TypeError, ValueError):
            return
        if split_ms <= 0:
            return

        current = self._best_by_cp.get(cp_idx)
        if current is None or split_ms < int(current.get("time", 0) or 0):
            self._best_by_cp[cp_idx] = {
                "cp": cp_idx,
                "time": split_ms,
                "nickname": nickname,
                "login": login,
            }
            self._queue_refresh()

    @staticmethod
    def _truncate(value: str, max_len: int) -> str:
        s = str(value or "").strip()
        if len(s) <= max_len:
            return s
        if max_len <= 1:
            return s[:max_len]
        return s[: max_len - 1] + "."

    def _visible_capacity(self, login: str) -> tuple[int, int, int]:
        """Return (cols, rows, total_capacity) for current resolved size."""
        w = float(self.WIDGET_DEFAULT_W)
        h = float(self.WIDGET_DEFAULT_H)
        try:
            host = self.instance.apps.apps.get("widget_engine")
            if host is not None:
                resolved = host.engine.resolve(self.WIDGET_KEY, login)
                if resolved is not None:
                    w = float(getattr(resolved, "w", w) or w)
                    h = float(getattr(resolved, "h", h) or h)
        except Exception:
            pass

        cols = max(1, int(max(1.0, w - 2.0) // self._ITEM_W))
        usable_h = max(0.0, h - self._HEADER_RESERVED)
        rows = max(1, int(usable_h // self._ROW_H))
        cap = max(1, cols * rows)
        return cols, rows, cap

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        total = int(getattr(getattr(self.instance.map_manager, "current_map", None), "num_checkpoints", 0) or 0)
        current_cp = int(self._player_cp.get(login, 0) or 0)
        cols, rows_count, visible = self._visible_capacity(login)

        entries: list[dict[str, Any]] = []
        seen_cps = sorted(int(cp) for cp in self._best_by_cp.keys() if int(cp) > 0)

        # Keep a sliding window so newly reached/high CPs are visible.
        if seen_cps:
            end_cp = current_cp if current_cp > 0 else seen_cps[-1]
            end_cp = max(end_cp, seen_cps[-1])
            start_cp = max(1, end_cp - visible + 1)
            window = [cp for cp in seen_cps if start_cp <= cp <= end_cp]
            # If the window has gaps and therefore too few entries, backfill
            # with earlier seen CPs so we still use available row capacity.
            if len(window) < visible:
                earlier = [cp for cp in seen_cps if cp < start_cp]
                need = visible - len(window)
                window = earlier[-need:] + window
        else:
            window = []

        for cp in window[:visible]:
            rec = self._best_by_cp.get(cp) or {}
            entries.append({
                "cp": cp,
                "time": times.format_time(int(rec.get("time", 0) or 0)),
                "nickname": self._truncate(str(rec.get("nickname", "-")), 14),
            })

        # Header counter should show checkpoints only (exclude finish).
        if total > 0:
            display_total = max(0, total - 1)
        else:
            display_total = max(seen_cps or [0])
        display_current = min(max(0, current_cp), display_total) if display_total > 0 else 0

        title_right = f"CP {display_current}/{display_total}" if display_total > 0 else "CP 0/0"
        if current_cp <= 0:
            title_right = "CP --"

        return {
            "entries": entries,
            "cols": cols,
            "rows_count": rows_count,
            "title_left": "BEST CPs",
            "title_right": title_right,
        }
