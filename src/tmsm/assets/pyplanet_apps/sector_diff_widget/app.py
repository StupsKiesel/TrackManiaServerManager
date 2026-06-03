"""Sector diff widget.

Shows last sector split and delta versus the player's best run on this map.
"""
from __future__ import annotations

import asyncio
from typing import Any

from pyplanet.apps.tmsm.widgets.widget_base import WidgetAppBase
from pyplanet.utils import times


class SectorDiffWidget(WidgetAppBase):
    name = "pyplanet.apps.tmsm.sector_diff_widget"
    label = "sector_diff_widget"

    WIDGET_KEY = "sector_diff"
    WIDGET_NAME = "Sector Diff"
    WIDGET_DESCRIPTION = "Last sector time and delta versus your best run."
    WIDGET_ICON = "tachometer-alt"
    WIDGET_TEMPLATE = "sector_diff_widget/sector_diff.xml"

    WIDGET_DEFAULT_X = 0.0
    WIDGET_DEFAULT_Y = -78.0
    WIDGET_DEFAULT_W = 40.0
    WIDGET_DEFAULT_H = 12.0

    WIDGET_REFRESH_SECONDS = 0.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_HIDE_WHILE_DRIVING = False
    WIDGET_ANIM_DIR = "down"
    WIDGET_ANIM_DURATION_MS = 250
    WIDGET_ANIM_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "66bbffff"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._run_by_login: dict[str, list[int]] = {}
        self._best_by_login: dict[str, list[int]] = {}
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
    def _fmt_delta(delta: int | None) -> str:
        if delta is None:
            return "--"
        if delta == 0:
            return "±0.000"
        sign = "+" if delta > 0 else "-"
        return f"{sign}{abs(delta) / 1000.0:.3f}"

    async def _on_reset(self, **kwargs) -> None:
        if not self._run_by_login and not self._best_by_login:
            return
        self._run_by_login.clear()
        self._best_by_login.clear()
        self._queue_refresh()

    async def _on_giveup(self, player=None, **kwargs) -> None:
        login = str(getattr(player, "login", "") or "")
        if not login:
            return
        if self._run_by_login.pop(login, None) is not None:
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
        old = self._run_by_login.get(login)
        if old == cps:
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
        if int(run[-1]) > 0 and int(run[-1]) < int(best[-1]):
            self._best_by_login[login] = list(run)
            self._queue_refresh()

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        run = list(self._run_by_login.get(login) or [])
        best = list(self._best_by_login.get(login) or [])

        cp = len(run)
        total = int(getattr(getattr(self.instance.map_manager, "current_map", None), "num_checkpoints", 0) or 0)
        cp_text = f"CP {cp}/{max(total, cp, 1)}" if cp > 0 else "CP --"

        if cp <= 0:
            return {
                "cp_text": cp_text,
                "sector_text": "--:--.---",
                "delta_text": "--",
                "delta_color": "aaa",
            }

        split_now = int(run[cp - 1])
        split_prev = int(run[cp - 2]) if cp >= 2 else 0
        sector_ms = max(0, split_now - split_prev)

        delta = None
        if cp <= len(best):
            best_now = int(best[cp - 1])
            best_prev = int(best[cp - 2]) if cp >= 2 else 0
            delta = (split_now - split_prev) - (best_now - best_prev)

        if delta is None:
            color = "aaa"
        elif delta <= 0:
            color = "3f8"
        else:
            color = "f66"

        return {
            "cp_text": cp_text,
            "sector_text": times.format_time(sector_ms),
            "delta_text": self._fmt_delta(delta),
            "delta_color": color,
        }
