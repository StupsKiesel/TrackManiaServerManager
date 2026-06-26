"""Persistence + query API for the stats_tracker app.

Self-contained raw-SQL access (mirrors the local_rankings_widget pattern).
stats_tracker is the *only* writer; the podium statistic widgets call the
``query_*`` helpers here (via the app) read-only.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from .models import StatsMap, StatsPlayer

logger = logging.getLogger(__name__)

# The local records table written by local_rankings_widget. Read-only here;
# it is the basis for the Top Ranks (most local records held) widget.
LOCAL_RECORDS_TABLE = "tmsm_local_rankings_record"


class StatsStorage:
    def __init__(self, instance):
        self.instance = instance
        self._ready = False

    @property
    def _db(self):
        db = getattr(self.instance, "db", None)
        if db is None or not hasattr(db, "objects"):
            return None
        return db

    # ---- schema --------------------------------------------------------

    async def ensure_schema(self) -> None:
        db = self._db
        if db is None:
            return
        player_sql = (
            "CREATE TABLE IF NOT EXISTS `tmsm_stats_player` ("
            "`id` INT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
            "`login` VARCHAR(64) NOT NULL, "
            "`nickname` VARCHAR(255) NULL, "
            "`visits` INT NOT NULL DEFAULT 0, "
            "`playtime_s` BIGINT NOT NULL DEFAULT 0, "
            "`spectate_time_s` BIGINT NOT NULL DEFAULT 0, "
            "`finishes` INT NOT NULL DEFAULT 0, "
            "`wins` INT NOT NULL DEFAULT 0, "
            "`comp_points` BIGINT NOT NULL DEFAULT 0, "
            "`first_seen` DATETIME NULL, "
            "`last_seen` DATETIME NULL, "
            "UNIQUE KEY `uniq_login` (`login`)"
            ") DEFAULT CHARSET=utf8mb4"
        )
        map_sql = (
            "CREATE TABLE IF NOT EXISTS `tmsm_stats_map` ("
            "`id` INT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
            "`uid` VARCHAR(64) NOT NULL, "
            "`name` VARCHAR(255) NULL, "
            "`author` VARCHAR(255) NULL, "
            "`plays` INT NOT NULL DEFAULT 0, "
            "`last_played_at` DATETIME NULL, "
            "UNIQUE KEY `uniq_uid` (`uid`)"
            ") DEFAULT CHARSET=utf8mb4"
        )
        try:
            await self._exec(StatsPlayer, player_sql)
            await self._exec(StatsMap, map_sql)
            self._ready = True
        except Exception:
            logger.exception("stats_tracker: schema bootstrap failed")

    async def _exec(self, model, sql: str, params: tuple[Any, ...] | None = None):
        db = self._db
        if db is None:
            return []
        raw = model.raw(sql, *(params or ()))
        raw.database = db.objects.database
        return await db.objects.execute(raw)

    async def _ready_check(self) -> bool:
        if not self._ready:
            await self.ensure_schema()
        return self._ready

    # ---- player writes -------------------------------------------------

    async def get_last_seen(self, login: str) -> _dt.datetime | None:
        if not await self._ready_check():
            return None
        try:
            rows = await self._exec(
                StatsPlayer,
                "SELECT `last_seen` FROM `tmsm_stats_player` WHERE `login` = %s",
                (login,),
            )
        except Exception:
            logger.exception("stats_tracker: get_last_seen failed login=%s", login)
            return None
        for row in rows:
            value = row.get("last_seen") if isinstance(row, dict) else getattr(row, "last_seen", None)
            return value
        return None

    async def bump_visit(self, login: str, nickname: str, now: _dt.datetime) -> None:
        """Increment visit counter and refresh first/last seen + nickname."""
        if not await self._ready_check():
            return
        sql = (
            "INSERT INTO `tmsm_stats_player` "
            "(`login`, `nickname`, `visits`, `playtime_s`, `spectate_time_s`, "
            "`finishes`, `wins`, `comp_points`, `first_seen`, `last_seen`) "
            "VALUES (%s, %s, 1, 0, 0, 0, 0, 0, %s, %s) "
            "ON DUPLICATE KEY UPDATE "
            "`visits` = `visits` + 1, "
            "`nickname` = VALUES(`nickname`), "
            "`first_seen` = COALESCE(`first_seen`, VALUES(`first_seen`)), "
            "`last_seen` = VALUES(`last_seen`)"
        )
        try:
            await self._exec(StatsPlayer, sql, (login, nickname or None, now, now))
        except Exception:
            logger.exception("stats_tracker: bump_visit failed login=%s", login)

    async def touch_seen(self, login: str, nickname: str, now: _dt.datetime) -> None:
        """Refresh last_seen/nickname without counting a new visit."""
        if not await self._ready_check():
            return
        sql = (
            "INSERT INTO `tmsm_stats_player` "
            "(`login`, `nickname`, `visits`, `playtime_s`, `spectate_time_s`, "
            "`finishes`, `wins`, `comp_points`, `first_seen`, `last_seen`) "
            "VALUES (%s, %s, 0, 0, 0, 0, 0, 0, %s, %s) "
            "ON DUPLICATE KEY UPDATE "
            "`nickname` = VALUES(`nickname`), "
            "`first_seen` = COALESCE(`first_seen`, VALUES(`first_seen`)), "
            "`last_seen` = VALUES(`last_seen`)"
        )
        try:
            await self._exec(StatsPlayer, sql, (login, nickname or None, now, now))
        except Exception:
            logger.exception("stats_tracker: touch_seen failed login=%s", login)

    async def add_playtime(self, login: str, active_s: int, spectate_s: int, now: _dt.datetime) -> None:
        if active_s <= 0 and spectate_s <= 0:
            return
        if not await self._ready_check():
            return
        sql = (
            "INSERT INTO `tmsm_stats_player` "
            "(`login`, `visits`, `playtime_s`, `spectate_time_s`, "
            "`finishes`, `wins`, `comp_points`, `last_seen`) "
            "VALUES (%s, 0, %s, %s, 0, 0, 0, %s) "
            "ON DUPLICATE KEY UPDATE "
            "`playtime_s` = `playtime_s` + VALUES(`playtime_s`), "
            "`spectate_time_s` = `spectate_time_s` + VALUES(`spectate_time_s`), "
            "`last_seen` = VALUES(`last_seen`)"
        )
        try:
            await self._exec(StatsPlayer, sql, (login, int(max(0, active_s)), int(max(0, spectate_s)), now))
        except Exception:
            logger.exception("stats_tracker: add_playtime failed login=%s", login)

    async def bump_finish(self, login: str, nickname: str, now: _dt.datetime) -> None:
        if not await self._ready_check():
            return
        sql = (
            "INSERT INTO `tmsm_stats_player` "
            "(`login`, `nickname`, `visits`, `playtime_s`, `spectate_time_s`, "
            "`finishes`, `wins`, `comp_points`, `last_seen`) "
            "VALUES (%s, %s, 0, 0, 0, 1, 0, 0, %s) "
            "ON DUPLICATE KEY UPDATE "
            "`finishes` = `finishes` + 1, "
            "`nickname` = VALUES(`nickname`), "
            "`last_seen` = VALUES(`last_seen`)"
        )
        try:
            await self._exec(StatsPlayer, sql, (login, nickname or None, now))
        except Exception:
            logger.exception("stats_tracker: bump_finish failed login=%s", login)

    async def award_result(self, login: str, nickname: str, opponents_beaten: int, is_winner: bool, now: _dt.datetime) -> None:
        if not await self._ready_check():
            return
        win_inc = 1 if is_winner else 0
        pts = int(max(0, opponents_beaten))
        sql = (
            "INSERT INTO `tmsm_stats_player` "
            "(`login`, `nickname`, `visits`, `playtime_s`, `spectate_time_s`, "
            "`finishes`, `wins`, `comp_points`, `last_seen`) "
            "VALUES (%s, %s, 0, 0, 0, 0, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE "
            "`wins` = `wins` + VALUES(`wins`), "
            "`comp_points` = `comp_points` + VALUES(`comp_points`), "
            "`nickname` = VALUES(`nickname`), "
            "`last_seen` = VALUES(`last_seen`)"
        )
        try:
            await self._exec(StatsPlayer, sql, (login, nickname or None, win_inc, pts, now))
        except Exception:
            logger.exception("stats_tracker: award_result failed login=%s", login)

    async def bump_map_play(self, uid: str, name: str, author: str, now: _dt.datetime) -> None:
        if not uid:
            return
        if not await self._ready_check():
            return
        sql = (
            "INSERT INTO `tmsm_stats_map` "
            "(`uid`, `name`, `author`, `plays`, `last_played_at`) "
            "VALUES (%s, %s, %s, 1, %s) "
            "ON DUPLICATE KEY UPDATE "
            "`plays` = `plays` + 1, "
            "`name` = VALUES(`name`), "
            "`author` = VALUES(`author`), "
            "`last_played_at` = VALUES(`last_played_at`)"
        )
        try:
            await self._exec(StatsMap, sql, (uid, name or None, author or None, now))
        except Exception:
            logger.exception("stats_tracker: bump_map_play failed uid=%s", uid)

    # ---- query helpers (read-only) -------------------------------------

    async def _top_players(self, column: str, limit: int) -> list[dict[str, Any]]:
        if not await self._ready_check():
            return []
        sql = (
            f"SELECT `login`, `nickname`, `{column}` AS `value` "
            "FROM `tmsm_stats_player` "
            f"WHERE `{column}` > 0 "
            f"ORDER BY `{column}` DESC, `last_seen` DESC "
            "LIMIT %s"
        )
        try:
            rows = await self._exec(StatsPlayer, sql, (int(limit),))
        except Exception:
            logger.exception("stats_tracker: top players failed column=%s", column)
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                login = str(row.get("login", "") or "")
                nickname = str(row.get("nickname") or login or "Unknown")
                value = int(row.get("value", 0) or 0)
            else:
                login = str(getattr(row, "login", "") or "")
                nickname = str(getattr(row, "nickname", "") or login or "Unknown")
                value = int(getattr(row, "value", 0) or 0)
            if not login:
                continue
            out.append({"login": login, "nickname": nickname, "value": value})
        return out

    async def top_visitors(self, limit: int) -> list[dict[str, Any]]:
        return await self._top_players("visits", limit)

    async def most_playtime(self, limit: int) -> list[dict[str, Any]]:
        return await self._top_players("playtime_s", limit)

    async def most_finishes(self, limit: int) -> list[dict[str, Any]]:
        return await self._top_players("finishes", limit)

    async def top_winners(self, limit: int) -> list[dict[str, Any]]:
        return await self._top_players("wins", limit)

    async def most_played_maps(self, limit: int) -> list[dict[str, Any]]:
        if not await self._ready_check():
            return []
        sql = (
            "SELECT `uid`, `name`, `author`, `plays` "
            "FROM `tmsm_stats_map` "
            "WHERE `plays` > 0 "
            "ORDER BY `plays` DESC, `last_played_at` DESC "
            "LIMIT %s"
        )
        try:
            rows = await self._exec(StatsMap, sql, (int(limit),))
        except Exception:
            logger.exception("stats_tracker: most_played_maps failed")
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                out.append({
                    "uid": str(row.get("uid", "") or ""),
                    "name": str(row.get("name") or "?"),
                    "author": str(row.get("author") or ""),
                    "plays": int(row.get("plays", 0) or 0),
                })
            else:
                out.append({
                    "uid": str(getattr(row, "uid", "") or ""),
                    "name": str(getattr(row, "name", "") or "?"),
                    "author": str(getattr(row, "author", "") or ""),
                    "plays": int(getattr(row, "plays", 0) or 0),
                })
        return out

    async def map_last_played(self, uids: list[str]) -> dict[str, _dt.datetime | None]:
        """Return {uid: last_played_at} for the given uids (missing => absent)."""
        if not uids or not await self._ready_check():
            return {}
        placeholders = ", ".join(["%s"] * len(uids))
        sql = (
            "SELECT `uid`, `last_played_at` FROM `tmsm_stats_map` "
            f"WHERE `uid` IN ({placeholders})"
        )
        try:
            rows = await self._exec(StatsMap, sql, tuple(uids))
        except Exception:
            logger.exception("stats_tracker: map_last_played failed")
            return {}
        out: dict[str, _dt.datetime | None] = {}
        for row in rows:
            if isinstance(row, dict):
                out[str(row.get("uid", "") or "")] = row.get("last_played_at")
            else:
                out[str(getattr(row, "uid", "") or "")] = getattr(row, "last_played_at", None)
        return out

    # ---- Top Ranks (most local records held) ---------------------------

    async def top_ranks(self, limit: int) -> list[dict[str, Any]]:
        """Players holding the most local records across all maps."""
        if not await self._ready_check():
            return []
        try:
            rows = await self._exec(
                StatsPlayer,
                "SELECT `login`, MAX(`nickname`) AS `nickname`, COUNT(*) AS `records` "
                f"FROM `{LOCAL_RECORDS_TABLE}` "
                "WHERE `score` > 0 AND `login` NOT LIKE '*%%' "
                "GROUP BY `login` "
                "ORDER BY `records` DESC "
                "LIMIT %s",
                (int(limit),),
            )
        except Exception:
            # local_rankings_widget may not be installed -> no records table.
            logger.exception("stats_tracker: top_ranks lookup failed")
            return []

        out: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                login = str(row.get("login", "") or "")
                nickname = str(row.get("nickname") or login or "Unknown")
                records = int(row.get("records", 0) or 0)
            else:
                login = str(getattr(row, "login", "") or "")
                nickname = str(getattr(row, "nickname", "") or login or "Unknown")
                records = int(getattr(row, "records", 0) or 0)
            if not login or records <= 0:
                continue
            out.append({"login": login, "nickname": nickname, "records": records})
        return out

