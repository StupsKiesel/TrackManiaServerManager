"""Local rankings widget.

Shows top local records from the local_records tables and highlights the
viewer's own rank if available.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from pyplanet.apps.tmsm.widget_engine import AnimDir, DriveMode
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase
from pyplanet.utils import times


logger = logging.getLogger(__name__)


class LocalRankingsWidget(WidgetAppBase):
    name = "pyplanet.apps.tmsm.local_rankings_widget"
    label = "local_rankings_widget"

    WIDGET_KEY = "local_rankings"
    WIDGET_NAME = "Local Records"
    WIDGET_DESCRIPTION = "Top local records for the current map."
    WIDGET_ICON = "trophy"
    WIDGET_TEMPLATE = "local_rankings_widget/local_rankings.xml"

    WIDGET_DEFAULT_X = 130.0
    WIDGET_DEFAULT_Y = 74.0
    WIDGET_DEFAULT_W = 62.0
    WIDGET_DEFAULT_H = 22.0

    # Keep refresh event-driven. Periodic full re-renders reset the frame
    # script and can cause repeated hide/show animation flicker.
    WIDGET_REFRESH_SECONDS = 0.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_DRIVE_MODE = DriveMode.FIXED
    WIDGET_ANIM_DIR = AnimDir.RIGHT
    WIDGET_ANIM_DURATION_MS = 250
    WIDGET_ANIM_IN_DELAY_MS = 0
    WIDGET_ANIM_OUT_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "22ccaaff"

    ROW_LIMIT = 5
    _ROW_PITCH = 3.2
    _HEADER_RESERVED = 4.6

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_records: list[dict[str, Any]] = []
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
                # Coalesce bursts of scores/finish callbacks.
                await asyncio.sleep(0.12)
                if self.view is not None:
                    await self.view.refresh()
            except Exception:
                pass
            finally:
                self._queued_refresh = None

        self._queued_refresh = asyncio.create_task(_flush())

    async def on_start(self) -> None:
        await super().on_start()
        try:
            self.context.signals.listen("trackmania:scores", self._on_scores)
            self.context.signals.listen("trackmania:finish", self._on_finish)
            self.context.signals.listen("maniaplanet:map_begin", self._on_reset)
            self.context.signals.listen("maniaplanet:map_start", self._on_reset)
        except Exception:
            pass

    async def _on_reset(self, **kwargs) -> None:
        self.current_records = []
        self._queue_refresh()

    def _upsert_record(self, login: str, nickname: str, score: int) -> bool:
        if not login or score <= 0:
            return False
        changed = False
        current = next((x for x in self.current_records if x.get("login") == login), None)
        if current is None:
            self.current_records.append({"login": login, "nickname": nickname, "score": int(score)})
            changed = True
        else:
            prev = int(current.get("score", 0) or 0)
            if prev <= 0 or score < prev:
                current["score"] = int(score)
                changed = True
            if nickname:
                if str(current.get("nickname") or "") != nickname:
                    changed = True
                current["nickname"] = nickname
        self.current_records.sort(key=lambda x: int(x.get("score", 0) or 0))
        return changed

    async def _on_scores(self, section=None, players=None, **kwargs) -> None:
        if section == "PreEndRound":
            return
        changed = False
        for item in list(players or []):
            if not isinstance(item, dict):
                continue
            best_race = item.get("best_race_time")
            if best_race is None:
                continue
            try:
                best = int(best_race)
            except (TypeError, ValueError):
                continue
            if best <= 0:
                continue
            player = item.get("player")
            login = str(getattr(player, "login", "") or "")
            nickname = str(getattr(player, "nickname", login) or login)
            changed = self._upsert_record(login, nickname, best) or changed
        if changed:
            self._queue_refresh()

    async def _on_finish(self, player=None, lap_time=None, race_time=None, is_end_race=None, **kwargs) -> None:
        # Some modes/signals omit is_end_race; only reject explicit False.
        if is_end_race is False:
            return
        login = str(getattr(player, "login", "") or "")
        nickname = str(getattr(player, "nickname", login) or login)
        try:
            score = int(lap_time or race_time or 0)
        except (TypeError, ValueError):
            score = 0
        if self._upsert_record(login, nickname, score):
            self._queue_refresh()

    async def _load_current_records(self) -> list[Any]:
        current_map = getattr(self.instance.map_manager, "current_map", None)
        if current_map is None:
            return []
        try:
            from pyplanet.apps.contrib.local_records.models import LocalRecord
            from pyplanet.apps.core.maniaplanet.models import Player

            query = (
                LocalRecord.select(LocalRecord, Player)
                .join(Player)
                .where(LocalRecord.map_id == current_map.get_id())
                .order_by(LocalRecord.score.asc())
            )
            rows = await LocalRecord.objects.execute(query)
            return list(rows)
        except Exception:
            logger.exception("local_rankings: failed loading local_records rows")
            return []

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

    def _load_contrib_current_records(self) -> list[dict[str, Any]]:
        """Use contrib local_records in-memory cache when available.

        This is the most up-to-date source right after map restart/map_begin.
        """
        try:
            apps = getattr(self.instance.apps, "apps", {}) or {}
        except Exception:
            apps = {}

        app = None
        for key in (
            "local_records",
            "pyplanet.apps.contrib.local_records",
            "pyplanet.apps.contrib.local_records.app",
        ):
            app = apps.get(key)
            if app is not None:
                break

        if app is None:
            for key, candidate in apps.items():
                module = str(getattr(candidate.__class__, "__module__", "") or "")
                key_str = str(key or "")
                if "contrib.local_records" in module or key_str.endswith("local_records"):
                    app = candidate
                    break

        if app is None:
            return []
        out: list[dict[str, Any]] = []
        for rec in list(getattr(app, "current_records", []) or []):
            player = getattr(rec, "player", None)
            score = int(getattr(rec, "score", 0) or 0)
            if score <= 0:
                continue
            out.append(
                {
                    "login": str(getattr(player, "login", "") or ""),
                    "nickname": str(getattr(player, "nickname", "Unknown") or "Unknown"),
                    "score": score,
                }
            )
        return out

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        visible_rows = self._visible_row_capacity(login)
        db_records = await self._load_current_records()
        rows: list[dict[str, Any]] = []
        my_row = None

        feed: list[dict[str, Any]] = []
        contrib_records = self._load_contrib_current_records()
        if contrib_records:
            feed = contrib_records
        elif db_records:
            for rec in db_records:
                player = getattr(rec, "player", None)
                feed.append(
                    {
                        "login": str(getattr(player, "login", "") or ""),
                        "nickname": str(getattr(player, "nickname", "Unknown") or "Unknown"),
                        "score": int(getattr(rec, "score", 0) or 0),
                    }
                )
        else:
            feed = list(self.current_records)

        for index, rec in enumerate(feed, start=1):
            player_login = str(rec.get("login") or "")
            nickname = str(rec.get("nickname") or "Unknown")
            raw_score = int(rec.get("score", 0) or 0)
            row = {
                "rank": index,
                "nickname": nickname,
                "score": times.format_time(raw_score),
                "is_me": bool(player_login and player_login == login),
            }
            if index <= visible_rows:
                rows.append(row)
            if row["is_me"]:
                my_row = row

        if my_row is not None and int(my_row["rank"]) <= visible_rows:
            my_row = None

        note = ""
        if not rows:
            note = "No local records yet"

        return {
            "rows": rows,
            "my_row": my_row,
            "mode_label": "Local Records",
            "note": note,
        }
