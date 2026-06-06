"""Storage for local_rankings_widget.

Self-contained persistence so the widget does not depend on
pyplanet.apps.contrib.local_records.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from .models import LocalRankingRecord

logger = logging.getLogger(__name__)


class LocalRankingsStorage:
    def __init__(self, instance):
        self.instance = instance
        self._ready = False

    @property
    def _db(self):
        db = getattr(self.instance, "db", None)
        if db is None or not hasattr(db, "objects"):
            return None
        return db

    async def ensure_schema(self) -> None:
        db = self._db
        if db is None:
            return
        sql = (
            "CREATE TABLE IF NOT EXISTS `tmsm_local_rankings_record` ("
            "`id` INT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
            "`map_uid` VARCHAR(64) NOT NULL, "
            "`login` VARCHAR(64) NOT NULL, "
            "`nickname` VARCHAR(255) NULL, "
            "`score` INT NOT NULL DEFAULT 0, "
            "`created_at` DATETIME NULL, "
            "`updated_at` DATETIME NULL, "
            "UNIQUE KEY `uniq_map_login` (`map_uid`, `login`), "
            "KEY `idx_map_uid` (`map_uid`)"
            ") DEFAULT CHARSET=utf8mb4"
        )
        try:
            await self._exec_raw(sql)
            self._ready = True
        except Exception:
            logger.exception("local_rankings: schema bootstrap failed")

    async def _exec_raw(self, sql: str, params: tuple[Any, ...] | None = None):
        db = self._db
        if db is None:
            return []
        raw = LocalRankingRecord.raw(sql, *(params or ()))
        raw.database = db.objects.database
        return await db.objects.execute(raw)

    async def upsert(self, map_uid: str, login: str, nickname: str, score: int) -> None:
        if not map_uid or not login or score <= 0:
            return
        if not self._ready:
            await self.ensure_schema()
        now = _dt.datetime.utcnow()
        sql = (
            "INSERT INTO `tmsm_local_rankings_record` "
            "(`map_uid`, `login`, `nickname`, `score`, `created_at`, `updated_at`) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE "
            "`nickname` = VALUES(`nickname`), "
            "`score` = IF(`score` <= 0 OR VALUES(`score`) < `score`, VALUES(`score`), `score`), "
            "`updated_at` = VALUES(`updated_at`)"
        )
        try:
            await self._exec_raw(sql, (map_uid, login, nickname or None, int(score), now, now))
        except Exception:
            logger.exception("local_rankings: upsert failed map_uid=%s login=%s", map_uid, login)

    async def list_for_map(self, map_uid: str) -> list[dict[str, Any]]:
        if not map_uid:
            return []
        if not self._ready:
            await self.ensure_schema()
        sql = (
            "SELECT `login`, `nickname`, `score` "
            "FROM `tmsm_local_rankings_record` "
            "WHERE `map_uid` = %s AND `score` > 0 "
            "ORDER BY `score` ASC, `updated_at` ASC"
        )
        try:
            rows = await self._exec_raw(sql, (map_uid,))
        except Exception:
            logger.exception("local_rankings: list_for_map failed map_uid=%s", map_uid)
            return []

        out: list[dict[str, Any]] = []
        for row in rows:
            login = ""
            nickname = "Unknown"
            score = 0
            if isinstance(row, dict):
                login = str(row.get("login", "") or "")
                nickname = str(row.get("nickname", "Unknown") or "Unknown")
                try:
                    score = int(row.get("score", 0) or 0)
                except (TypeError, ValueError):
                    score = 0
            else:
                login = str(getattr(row, "login", "") or "")
                nickname = str(getattr(row, "nickname", "Unknown") or "Unknown")
                try:
                    score = int(getattr(row, "score", 0) or 0)
                except (TypeError, ValueError):
                    score = 0
            if not login or score <= 0:
                continue
            out.append({"login": login, "nickname": nickname, "score": score})
        return out
