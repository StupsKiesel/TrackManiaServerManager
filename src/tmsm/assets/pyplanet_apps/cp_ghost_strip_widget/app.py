"""Checkpoint ghost mini-strip widget.

Per-player compact strip:
- G: reached CP and equal/faster than personal best split
- R: reached CP and slower than personal best split
- .: not reached yet
- ?: reached but no ghost split available
"""
from __future__ import annotations

import asyncio
from typing import Any

from pyplanet.apps.tmsm.widget_engine import AnimDir, DriveMode
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase


class CpGhostStripWidget(WidgetAppBase):
    name = "pyplanet.apps.tmsm.cp_ghost_strip_widget"
    label = "cp_ghost_strip_widget"

    WIDGET_KEY = "cp_ghost_strip"
    WIDGET_NAME = "CP Ghost Strip"
    WIDGET_DESCRIPTION = "Compact CP-vs-ghost strip with delta at current checkpoint."
    WIDGET_ICON = "history"
    WIDGET_TEMPLATE = "cp_ghost_strip_widget/cp_ghost_strip.xml"

    WIDGET_DEFAULT_X = 130.0
    WIDGET_DEFAULT_Y = 33.0
    WIDGET_DEFAULT_W = 62.0
    WIDGET_DEFAULT_H = 9.0

    WIDGET_REFRESH_SECONDS = 0.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_DRIVE_MODE = DriveMode.FIXED
    WIDGET_ANIM_DIR = AnimDir.RIGHT
    WIDGET_ANIM_DURATION_MS = 250
    WIDGET_ANIM_IN_DELAY_MS = 0
    WIDGET_ANIM_OUT_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "66aaddff"

    MAX_CELLS = 14

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._best_by_login: dict[str, list[int]] = {}
        self._run_by_login: dict[str, list[int]] = {}
        self._queued_refresh: asyncio.Task | None = None

    async def on_start(self) -> None:
        await super().on_start()
        try:
            self.context.signals.listen("trackmania:waypoint", self._on_waypoint)
            self.context.signals.listen("trackmania:finish", self._on_finish)
            self.context.signals.listen("trackmania:give_up", self._on_giveup)
            self.context.signals.listen("maniaplanet:map_start", self._on_reset)
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

    @staticmethod
    def _ints(values: Any) -> list[int]:
        out: list[int] = []
        for value in list(values or []):
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                out.append(parsed)
        return out

    @staticmethod
    def _fmt_delta(delta_ms: int | None) -> str:
        if delta_ms is None:
            return "--"
        sign = "+" if delta_ms > 0 else "-"
        if delta_ms == 0:
            sign = "±"
        value = abs(int(delta_ms))
        return f"{sign}{value / 1000.0:.3f}"

    async def _on_reset(self, **kwargs) -> None:
        if not self._best_by_login and not self._run_by_login:
            return
        self._best_by_login = {}
        self._run_by_login = {}
        self._queue_refresh()

    async def _on_giveup(self, player=None, **kwargs) -> None:
        login = str(getattr(player, "login", "") or "")
        if not login:
            return
        if login in self._run_by_login:
            self._run_by_login.pop(login, None)
            self._queue_refresh()

    async def _on_waypoint(self, player=None, raw=None, **kwargs) -> None:
        if player is None or not isinstance(raw, dict):
            return
        login = str(getattr(player, "login", "") or "")
        if not login:
            return
        cps = self._ints(raw.get("curracecheckpoints"))
        if not cps:
            return
        previous = self._run_by_login.get(login)
        if previous == cps:
            return
        self._run_by_login[login] = cps
        self._queue_refresh()

    async def _on_finish(self, player=None, race_cps=None, cps=None, is_end_race=None, **kwargs) -> None:
        if not bool(is_end_race):
            return
        login = str(getattr(player, "login", "") or "")
        if not login:
            return
        run = self._ints(race_cps or cps)
        if not run:
            return
        self._run_by_login[login] = run

        best = self._best_by_login.get(login)
        if best is None:
            self._best_by_login[login] = list(run)
            self._queue_refresh()
            return
        best_final = int(best[-1]) if best else 0
        run_final = int(run[-1]) if run else 0
        if best_final <= 0 or (run_final > 0 and run_final < best_final):
            self._best_by_login[login] = list(run)
            self._queue_refresh()

    def _window(self, total: int, current_idx: int) -> tuple[int, int]:
        cells = max(1, min(self.MAX_CELLS, total if total > 0 else self.MAX_CELLS))
        if total <= cells:
            return 1, cells
        start = max(1, current_idx - (cells // 2))
        max_start = total - cells + 1
        if start > max_start:
            start = max_start
        end = start + cells - 1
        return start, end

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        run = list(self._run_by_login.get(login) or [])
        best = list(self._best_by_login.get(login) or [])

        total_map_cp = int(getattr(self.instance.map_manager.current_map, "num_checkpoints", 0) or 0)
        total = max(total_map_cp, len(run), len(best), 1)
        current_idx = len(run)
        if current_idx <= 0:
            current_idx = 1

        start, end = self._window(total, current_idx)

        symbols: list[str] = []
        marker_pos = 0
        for cp_idx in range(start, end + 1):
            if cp_idx <= len(run):
                if cp_idx <= len(best):
                    delta = int(run[cp_idx - 1]) - int(best[cp_idx - 1])
                    sym = "G" if delta <= 0 else "R"
                else:
                    sym = "?"
            else:
                sym = "."
            symbols.append(sym)
            if cp_idx == min(current_idx, total):
                marker_pos = cp_idx - start

        bar = "".join(symbols)
        marker = " " * max(0, marker_pos) + "^"

        cp_title = f"CP {min(len(run), total)}/{total}"
        delta_ms: int | None = None
        cp_i = len(run)
        if cp_i > 0 and cp_i <= len(best):
            delta_ms = int(run[cp_i - 1]) - int(best[cp_i - 1])
        delta_title = f"Δ {self._fmt_delta(delta_ms)}"

        if not run:
            delta_title = "Δ --"

        left_hint = "<" if start > 1 else " "
        right_hint = ">" if end < total else " "

        return {
            "title_left": cp_title,
            "title_right": delta_title,
            "strip_bar": f"{left_hint}{bar}{right_hint}",
            "strip_marker": f" {marker}",
            "legend": "G ahead  R behind",
        }
