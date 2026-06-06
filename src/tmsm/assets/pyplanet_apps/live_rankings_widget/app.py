"""Live rankings widget.

Shows current standings using TrackMania callbacks and highlights the
viewer's own rank if available.
"""
from __future__ import annotations

import asyncio
from typing import Any

from pyplanet.apps.tmsm.widget_engine import AnimDir, DriveMode
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase
from pyplanet.utils import times


class LiveRankingsWidget(WidgetAppBase):
    name = "pyplanet.apps.tmsm.live_rankings_widget"
    label = "live_rankings_widget"

    WIDGET_KEY = "live_rankings"
    WIDGET_NAME = "Live Records"
    WIDGET_DESCRIPTION = "Current round standings from live rankings."
    WIDGET_ICON = "chart-line"
    WIDGET_TEMPLATE = "live_rankings_widget/live_rankings.xml"

    WIDGET_DEFAULT_X = 130.0
    WIDGET_DEFAULT_Y = 50.0
    WIDGET_DEFAULT_W = 62.0
    WIDGET_DEFAULT_H = 22.0

    # Keep refresh event-driven. Periodic full re-renders reset the frame
    # script and make hide/show animation look like repeated fade flicker.
    WIDGET_REFRESH_SECONDS = 0.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_DRIVE_MODE = DriveMode.FIXED
    WIDGET_ANIM_DIR = AnimDir.RIGHT
    WIDGET_ANIM_DURATION_MS = 250
    WIDGET_ANIM_IN_DELAY_MS = 0
    WIDGET_ANIM_OUT_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "ff6688ff"

    ROW_LIMIT = 5
    _ROW_PITCH = 3.2
    _HEADER_RESERVED = 4.6

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_rankings: list[dict[str, Any]] = []
        self._queued_refresh: asyncio.Task | None = None
        self._map_uid: str = ""

    def _current_map_uid(self) -> str:
        current_map = getattr(self.instance.map_manager, "current_map", None)
        if current_map is None:
            return ""
        return str(getattr(current_map, "uid", "") or "")

    def _sync_map_uid(self) -> bool:
        """Return True when the map changed and ranking cache was cleared."""
        uid = self._current_map_uid()
        if uid == self._map_uid:
            return False
        self._map_uid = uid
        if self.current_rankings:
            self.current_rankings = []
        # Always refresh after a map swap so the widget redraws even when
        # no live scores exist yet on the new map.
        self._queue_refresh()
        return True

    def _is_multi_lap_map(self) -> bool:
        current_map = getattr(self.instance.map_manager, "current_map", None)
        if current_map is None:
            return False
        try:
            return int(getattr(current_map, "num_laps", 0) or 0) > 1
        except (TypeError, ValueError):
            return False

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
                # Coalesce bursts of waypoint/score callbacks.
                await asyncio.sleep(0.12)
                if self.view is not None:
                    await self.view.refresh()
            except Exception:
                pass
            finally:
                self._queued_refresh = None

        self._queued_refresh = asyncio.create_task(_flush())

    def _upsert_best(self, login: str, nickname: str, score: int) -> bool:
        """Keep only session PB per player for the current map."""
        if not login or score <= 0:
            return False
        current = next((x for x in self.current_rankings if x.get("login") == login), None)
        changed = False
        if current is None:
            self.current_rankings.append(
                {
                    "login": login,
                    "nickname": nickname,
                    "score": int(score),
                    "finish": True,
                }
            )
            changed = True
        else:
            prev = int(current.get("score", 0) or 0)
            if prev <= 0 or score < prev:
                current["score"] = int(score)
                changed = True
            if nickname and str(current.get("nickname") or "") != nickname:
                current["nickname"] = nickname
                changed = True
        if changed:
            self.current_rankings.sort(key=lambda x: int(x.get("score", 0) or 0))
        return changed

    async def on_start(self) -> None:
        await super().on_start()
        self._map_uid = self._current_map_uid()
        try:
            self.context.signals.listen("trackmania:scores", self._on_scores)
            self.context.signals.listen("trackmania:finish", self._on_finish)
            self.context.signals.listen("trackmania:waypoint", self._on_waypoint)
            self.context.signals.listen("trackmania:give_up", self._on_giveup)
            self.context.signals.listen("maniaplanet:map_begin", self._on_reset)
            self.context.signals.listen("maniaplanet:map_start", self._on_reset)
        except Exception:
            pass

    async def _on_reset(self, **kwargs) -> None:
        self._map_uid = self._current_map_uid()
        if self.current_rankings:
            self.current_rankings = []
        self._queue_refresh()

    async def _on_scores(self, section=None, players=None, **kwargs) -> None:
        self._sync_map_uid()
        if section == "PreEndRound":
            return
        # On multi-lap maps this feed can represent best lap times; keep
        # live records based on end-race callbacks only.
        if self._is_multi_lap_map():
            return
        changed = False
        for item in list(players or []):
            if not isinstance(item, dict):
                continue
            raw_best = item.get("best_race_time")
            try:
                best = int(raw_best)
            except (TypeError, ValueError):
                continue
            if best <= 0:
                continue
            p = item.get("player")
            login = str(getattr(p, "login", "") or "")
            nickname = str(getattr(p, "nickname", login) or login)
            changed = self._upsert_best(login, nickname, best) or changed
        if changed:
            self._queue_refresh()

    async def _on_giveup(self, player=None, **kwargs) -> None:
        self._sync_map_uid()
        # Session PB should remain visible even if player gives up later.
        return

    async def _on_finish(self, player=None, lap_time=None, race_time=None, is_end_race=None, **kwargs) -> None:
        self._sync_map_uid()
        if is_end_race is False:
            return
        login = str(getattr(player, "login", "") or "")
        nickname = str(getattr(player, "nickname", login) or login)
        try:
            score = int(race_time or lap_time or 0)
        except (TypeError, ValueError):
            score = 0
        if self._upsert_best(login, nickname, score):
            self._queue_refresh()

    async def _on_waypoint(self, player=None, race_time=None, raw=None, **kwargs) -> None:
        self._sync_map_uid()
        # Waypoint callbacks are ignored to avoid checkpoint times in the list.
        if player is None or not isinstance(raw, dict):
            return
        if not bool(raw.get("isendrace", False)):
            return

        login = str(getattr(player, "login", "") or "")
        nickname = str(getattr(player, "nickname", login) or login)
        try:
            score = int(raw.get("racetime", race_time or 0) or 0)
        except (TypeError, ValueError):
            score = 0
        if self._upsert_best(login, nickname, score):
            self._queue_refresh()

    @staticmethod
    def _format_score(item: dict[str, Any], points_mode: bool) -> str:
        raw_score = item.get("score", 0)
        try:
            score = int(raw_score or 0)
        except (TypeError, ValueError):
            return str(raw_score or "-")
        if score <= 0:
            return "-"
        return times.format_time(score)

    def _visible_row_capacity(self, login: str) -> int:
        """Compute visible row count from resolved widget height."""
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
        return max(1, min(self.ROW_LIMIT * 4, fit))

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        self._sync_map_uid()
        visible_rows = self._visible_row_capacity(login)
        rankings = list(self.current_rankings)
        dict_rows = [row for row in rankings if isinstance(row, dict)]

        rows: list[dict[str, Any]] = []
        my_row = None

        for index, row_src in enumerate(dict_rows, start=1):
            row_login = str(row_src.get("login") or "")
            row = {
                "rank": index,
                "nickname": str(row_src.get("nickname") or row_login or "Unknown"),
                "score": self._format_score(row_src, False),
                "is_me": bool(row_login and row_login == login),
            }
            if index <= visible_rows:
                rows.append(row)
            if row["is_me"]:
                my_row = row

        if my_row is not None and int(my_row["rank"]) <= visible_rows:
            my_row = None

        mode_label = "Live Records"
        note = ""
        if not rows:
            note = "No live data yet"

        return {
            "rows": rows,
            "my_row": my_row,
            "mode_label": mode_label,
            "note": note,
        }
