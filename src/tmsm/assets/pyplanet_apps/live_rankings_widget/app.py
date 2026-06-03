"""Live rankings widget.

Shows current standings using TrackMania callbacks and highlights the
viewer's own rank if available.
"""
from __future__ import annotations

import asyncio
from typing import Any

from pyplanet.apps.tmsm.widgets.widget_base import WidgetAppBase
from pyplanet.utils import times


class LiveRankingsWidget(WidgetAppBase):
    name = "pyplanet.apps.tmsm.live_rankings_widget"
    label = "live_rankings_widget"

    WIDGET_KEY = "live_rankings"
    WIDGET_NAME = "Live Rankings"
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
    WIDGET_HIDE_WHILE_DRIVING = False
    WIDGET_ANIM_DIR = "right"
    WIDGET_ANIM_DURATION_MS = 250
    WIDGET_ANIM_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "ff6688ff"

    ROW_LIMIT = 5

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_rankings: list[dict[str, Any]] = []
        self._queued_refresh: asyncio.Task | None = None

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

    @staticmethod
    def _is_mode_rounds(mode: str) -> bool:
        mode = (mode or "").lower()
        return any(
            token in mode
            for token in (
                "rounds",
                "teams",
                "cup",
                "laps",
                "tm_rounds_online",
                "tm_teams_online",
                "tm_cup_online",
                "tm_laps_online",
            )
        )

    @staticmethod
    def _is_mode_ta(mode: str) -> bool:
        mode = (mode or "").lower()
        return "timeattack" in mode or "tm_timeattack_online" in mode

    async def on_start(self) -> None:
        await super().on_start()
        try:
            self.context.signals.listen("trackmania:scores", self._on_scores)
            self.context.signals.listen("trackmania:waypoint", self._on_waypoint)
            self.context.signals.listen("trackmania:give_up", self._on_giveup)
            self.context.signals.listen("maniaplanet:map_start", self._on_reset)
        except Exception:
            pass

    async def _on_reset(self, **kwargs) -> None:
        if not self.current_rankings:
            return
        self.current_rankings = []
        self._queue_refresh()

    async def _on_scores(self, section=None, players=None, **kwargs) -> None:
        if section == "PreEndRound":
            return
        players = players or []
        try:
            current_script = (await self.instance.mode_manager.get_current_script()).lower()
        except Exception:
            current_script = ""

        rankings: list[dict[str, Any]] = []
        if self._is_mode_ta(current_script):
            for player in players:
                best_race = player.get("best_race_time")
                if best_race is None or int(best_race) == -1:
                    continue
                p = player.get("player")
                login = str(getattr(p, "login", "") or "")
                nickname = str(getattr(p, "nickname", login) or login)
                rankings.append(
                    {
                        "login": login,
                        "nickname": nickname,
                        "score": int(best_race),
                        "finish": True,
                        "giveup": False,
                    }
                )
            rankings.sort(key=lambda x: x.get("score", 0))
        elif self._is_mode_rounds(current_script):
            for player in players:
                mappoints = player.get("map_points")
                if mappoints is None or int(mappoints) == -1:
                    continue
                p = player.get("player")
                login = str(getattr(p, "login", "") or "")
                nickname = str(getattr(p, "nickname", login) or login)
                rankings.append(
                    {
                        "login": login,
                        "nickname": nickname,
                        "score": int(mappoints),
                        "points_added": 0,
                        "finish": True,
                        "giveup": False,
                    }
                )
            rankings.sort(key=lambda x: x.get("score", 0), reverse=True)
        if rankings != self.current_rankings:
            self.current_rankings = rankings
            self._queue_refresh()

    async def _on_giveup(self, player=None, **kwargs) -> None:
        login = str(getattr(player, "login", "") or "")
        if not login:
            return
        for row in self.current_rankings:
            if row.get("login") == login:
                if bool(row.get("giveup", False)):
                    return
                row["giveup"] = True
                self._queue_refresh()
                break

    async def _on_waypoint(self, player=None, race_time=None, raw=None, **kwargs) -> None:
        # Mirrors contrib live_rankings behavior: only used to build in-race
        # ordering for TA-like flows.
        try:
            current_script = (await self.instance.mode_manager.get_current_script()).lower()
        except Exception:
            current_script = ""
        if self._is_mode_rounds(current_script):
            return
        if player is None or not isinstance(raw, dict):
            return

        login = str(getattr(player, "login", "") or "")
        nickname = str(getattr(player, "nickname", login) or login)
        current = next((x for x in self.current_rankings if x.get("login") == login), None)
        item = {
            "login": login,
            "nickname": nickname,
            "score": int(raw.get("racetime", race_time or 0) or 0),
            "cps": int(raw.get("checkpointinrace", -1) or -1) + 1,
            "finish": bool(raw.get("isendrace", False)),
            "giveup": False,
        }
        if current is None:
            self.current_rankings.append(item)
        else:
            if current == item:
                return
            current.update(item)
        self.current_rankings.sort(key=lambda x: (-int(x.get("cps", 0) or 0), int(x.get("score", 0) or 0)))
        self._queue_refresh()

    @staticmethod
    def _format_score(item: dict[str, Any], points_mode: bool) -> str:
        if bool(item.get("giveup", False)):
            return "DNF"

        raw_score = item.get("score", 0)
        try:
            score = int(raw_score or 0)
        except (TypeError, ValueError):
            return str(raw_score or "-")

        if points_mode:
            points_added = item.get("points_added")
            if points_added is None:
                return str(score)
            try:
                added = int(points_added)
            except (TypeError, ValueError):
                added = 0
            if added > 0:
                return f"{score} (+{added})"
            return str(score)

        if bool(item.get("finish", False)):
            return times.format_time(score)

        # During a run the contrib app updates score with current split/race
        # time. Keeping the same formatter makes the list stable and compact.
        return times.format_time(score)

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        rankings = list(self.current_rankings)
        dict_rows = [row for row in rankings if isinstance(row, dict)]
        points_mode = any("points_added" in row for row in dict_rows)

        rows: list[dict[str, Any]] = []
        my_row = None

        for index, row_src in enumerate(dict_rows, start=1):
            row_login = str(row_src.get("login") or "")
            row = {
                "rank": index,
                "nickname": str(row_src.get("nickname") or row_login or "Unknown"),
                "score": self._format_score(row_src, points_mode),
                "is_me": bool(row_login and row_login == login),
            }
            if index <= self.ROW_LIMIT:
                rows.append(row)
            if row["is_me"]:
                my_row = row

        if my_row is not None and int(my_row["rank"]) <= self.ROW_LIMIT:
            my_row = None

        mode_label = "LIVE PTS" if points_mode else "LIVE TIME"
        note = ""
        if not rows:
            note = "No live data yet"

        return {
            "rows": rows,
            "my_row": my_row,
            "mode_label": mode_label,
            "note": note,
        }
