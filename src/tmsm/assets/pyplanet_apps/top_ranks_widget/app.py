"""Top Ranks podium widget (headline).

Leaderboard of which players hold the most local records across all maps.
Reads the local records table via stats_tracker. Height-aware row count.
"""
from __future__ import annotations

import logging
from typing import Any

from pyplanet.apps.tmsm.widget_engine import AnimDir, DriveMode
from pyplanet.apps.tmsm.widget_engine.registry import Phase
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase

logger = logging.getLogger(__name__)


class TopRanksWidget(WidgetAppBase):
    name = "pyplanet.apps.tmsm.top_ranks_widget"
    label = "top_ranks_widget"

    WIDGET_KEY = "top_ranks"
    WIDGET_NAME = "Top Ranks"
    WIDGET_DESCRIPTION = "Players holding the most local records."
    WIDGET_ICON = "star"
    WIDGET_TEMPLATE = "top_ranks_widget/top_ranks.xml"

    WIDGET_DEFAULT_X = 0.0
    WIDGET_DEFAULT_Y = 72.0
    WIDGET_DEFAULT_W = 70.0
    WIDGET_DEFAULT_H = 30.0

    WIDGET_REFRESH_SECONDS = 0.0
    WIDGET_DRIVE_MODE = DriveMode.FIXED
    WIDGET_ANIM_DIR = AnimDir.UP
    WIDGET_STRIP_COLOR = "ffd000ff"
    WIDGET_VISIBLE_PHASES = (Phase.IN_PODIUM,)

    _ROW_PITCH = 3.2
    _HEADER_RESERVED = 4.6
    _MAX_ROWS = 30
    TITLE = "Top Ranks"

    def _tracker(self):
        return self.instance.apps.apps.get("stats_tracker")

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
        return max(1, min(self._MAX_ROWS, int(usable // self._ROW_PITCH)))

    def _format_value(self, item: dict[str, Any]) -> str:
        return str(int(item.get("records", 0) or 0))

    async def _query(self, tracker, limit: int) -> list[dict[str, Any]]:
        return await tracker.query_top_ranks(limit)

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        rows_n = self._visible_row_capacity(login)
        tracker = self._tracker()
        if tracker is None:
            return {"title": self.TITLE, "rows": [], "note": "stats tracker not running"}
        try:
            data = await self._query(tracker, rows_n)
        except Exception:
            logger.exception("%s: query failed", self.WIDGET_KEY)
            data = []
        rows = [
            {
                "rank": index,
                "label": str(item.get("nickname") or item.get("name") or "Unknown"),
                "value": self._format_value(item),
            }
            for index, item in enumerate(data, start=1)
        ]
        return {
            "title": self.TITLE,
            "rows": rows,
            "note": "" if rows else "no local records yet",
        }
