"""Persistence + queries for the tournaments app (raw SQL, self-contained)."""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from .models import (
    Tournament,
    TournamentMap,
    TournamentParticipant,
    TournamentResult,
)

logger = logging.getLogger(__name__)

# Columns callers may update on a tournament row.
_TOURNAMENT_UPDATABLE = {
    "name", "status", "match_mode", "match_mode_label",
    "lock_to_participants", "self_signup", "current_map_index", "winner_login",
    "auto_advance", "auto_start_threshold",
}


class TournamentStorage:
    def __init__(self, instance):
        self.instance = instance
        self._ready = False

    @property
    def _db(self):
        db = getattr(self.instance, "db", None)
        if db is None or not hasattr(db, "objects"):
            return None
        return db

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

    # ---- schema --------------------------------------------------------

    async def ensure_schema(self) -> None:
        db = self._db
        if db is None:
            return
        statements = [
            (Tournament,
             "CREATE TABLE IF NOT EXISTS `tmsm_tournament` ("
             "`id` INT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
             "`name` VARCHAR(128) NOT NULL, "
             "`status` VARCHAR(24) NOT NULL DEFAULT 'draft', "
             "`match_mode` VARCHAR(255) NULL, "
             "`match_mode_label` VARCHAR(64) NULL, "
             "`lock_to_participants` TINYINT(1) NOT NULL DEFAULT 1, "
             "`self_signup` TINYINT(1) NOT NULL DEFAULT 1, "
             "`current_map_index` INT NOT NULL DEFAULT 0, "
             "`auto_advance` TINYINT(1) NOT NULL DEFAULT 1, "
             "`auto_start_threshold` INT NOT NULL DEFAULT 0, "
             "`winner_login` VARCHAR(64) NULL, "
             "`created_at` DATETIME NULL"
             ") DEFAULT CHARSET=utf8mb4"),
            (TournamentParticipant,
             "CREATE TABLE IF NOT EXISTS `tmsm_tournament_participant` ("
             "`id` INT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
             "`tournament_id` INT NOT NULL, "
             "`login` VARCHAR(64) NOT NULL, "
             "`nickname` VARCHAR(255) NULL, "
             "`seed` INT NOT NULL DEFAULT 0, "
             "`points` INT NOT NULL DEFAULT 0, "
             "`joined_at` DATETIME NULL, "
             "UNIQUE KEY `uniq_tp` (`tournament_id`, `login`), "
             "KEY `idx_tp_tid` (`tournament_id`)"
             ") DEFAULT CHARSET=utf8mb4"),
            (TournamentMap,
             "CREATE TABLE IF NOT EXISTS `tmsm_tournament_map` ("
             "`id` INT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
             "`tournament_id` INT NOT NULL, "
             "`order_index` INT NOT NULL DEFAULT 0, "
             "`map_uid` VARCHAR(64) NOT NULL, "
             "`name` VARCHAR(255) NULL, "
             "`status` VARCHAR(24) NOT NULL DEFAULT 'pending', "
             "`played_at` DATETIME NULL, "
             "KEY `idx_tm_tid` (`tournament_id`)"
             ") DEFAULT CHARSET=utf8mb4"),
            (TournamentResult,
             "CREATE TABLE IF NOT EXISTS `tmsm_tournament_result` ("
             "`id` INT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
             "`tournament_id` INT NOT NULL, "
             "`map_id` INT NOT NULL, "
             "`login` VARCHAR(64) NOT NULL, "
             "`nickname` VARCHAR(255) NULL, "
             "`position` INT NOT NULL DEFAULT 0, "
             "`points` INT NOT NULL DEFAULT 0, "
             "`score` INT NOT NULL DEFAULT 0, "
             "`created_at` DATETIME NULL, "
             "UNIQUE KEY `uniq_tr` (`map_id`, `login`), "
             "KEY `idx_tr_tid` (`tournament_id`)"
             ") DEFAULT CHARSET=utf8mb4"),
        ]
        try:
            for model, sql in statements:
                await self._exec(model, sql)
            self._ready = True
        except Exception:
            logger.exception("tournaments: schema bootstrap failed")

    # ---- tournaments ---------------------------------------------------

    @staticmethod
    def _row(row) -> dict[str, Any]:
        if isinstance(row, dict):
            return dict(row)
        # peewee raw rows are model instances. __data__ holds loaded columns
        # (incl. query aliases like `mx`/`id`), but for `SELECT *` the auto
        # primary key may be missing from it -> also pull every model field
        # name via getattr so `id` is always present.
        data: dict[str, Any] = dict(getattr(row, "__data__", {}) or {})
        meta = getattr(type(row), "_meta", None)
        field_names = getattr(meta, "sorted_field_names", None) if meta else None
        for name in (field_names or []):
            if name not in data:
                try:
                    data[name] = getattr(row, name)
                except Exception:
                    pass
        return data

    async def create_tournament(self, name: str, now: _dt.datetime) -> int:
        if not await self._ready_check():
            return 0
        await self._exec(
            Tournament,
            "INSERT INTO `tmsm_tournament` "
            "(`name`, `status`, `lock_to_participants`, `self_signup`, "
            "`current_map_index`, `created_at`) "
            "VALUES (%s, 'draft', 1, 1, 0, %s)",
            (name, now),
        )
        rows = await self._exec(
            Tournament, "SELECT LAST_INSERT_ID() AS `id`")
        for row in rows:
            return int(self._row(row).get("id", 0) or 0)
        return 0

    async def list_tournaments(self) -> list[dict[str, Any]]:
        if not await self._ready_check():
            return []
        rows = await self._exec(
            Tournament,
            "SELECT * FROM `tmsm_tournament` ORDER BY `created_at` DESC, `id` DESC",
        )
        return [self._row(r) for r in rows]

    async def get_tournament(self, tid: int) -> dict[str, Any] | None:
        if not await self._ready_check():
            return None
        rows = await self._exec(
            Tournament, "SELECT * FROM `tmsm_tournament` WHERE `id` = %s", (int(tid),))
        for row in rows:
            return self._row(row)
        return None

    async def update_tournament(self, tid: int, **fields) -> None:
        cols = {k: v for k, v in fields.items() if k in _TOURNAMENT_UPDATABLE}
        if not cols or not await self._ready_check():
            return
        assignments = ", ".join(f"`{k}` = %s" for k in cols)
        params = tuple(cols.values()) + (int(tid),)
        await self._exec(
            Tournament,
            f"UPDATE `tmsm_tournament` SET {assignments} WHERE `id` = %s",
            params,
        )

    async def delete_tournament(self, tid: int) -> None:
        if not await self._ready_check():
            return
        tid = int(tid)
        await self._exec(TournamentResult,
                         "DELETE FROM `tmsm_tournament_result` WHERE `tournament_id` = %s", (tid,))
        await self._exec(TournamentMap,
                         "DELETE FROM `tmsm_tournament_map` WHERE `tournament_id` = %s", (tid,))
        await self._exec(TournamentParticipant,
                         "DELETE FROM `tmsm_tournament_participant` WHERE `tournament_id` = %s", (tid,))
        await self._exec(Tournament,
                         "DELETE FROM `tmsm_tournament` WHERE `id` = %s", (tid,))

    async def clone_tournament(self, tid: int, now: _dt.datetime) -> int:
        """Copy a tournament's settings + map pool into a fresh draft.

        Participants and results are NOT copied (the clone starts clean).
        """
        src = await self.get_tournament(tid)
        if not src:
            return 0
        name = str(src.get("name") or "Tournament")
        await self._exec(
            Tournament,
            "INSERT INTO `tmsm_tournament` "
            "(`name`, `status`, `match_mode`, `match_mode_label`, "
            "`lock_to_participants`, `self_signup`, `current_map_index`, "
            "`auto_advance`, `auto_start_threshold`, `created_at`) "
            "VALUES (%s, 'draft', %s, %s, %s, %s, 0, %s, %s, %s)",
            (f"{name} (copy)", src.get("match_mode"), src.get("match_mode_label"),
             1 if src.get("lock_to_participants", True) else 0,
             1 if src.get("self_signup", True) else 0,
             1 if src.get("auto_advance", True) else 0,
             int(src.get("auto_start_threshold", 0) or 0), now),
        )
        rows = await self._exec(Tournament, "SELECT LAST_INSERT_ID() AS `id`")
        new_id = 0
        for row in rows:
            new_id = int(self._row(row).get("id", 0) or 0)
        if not new_id:
            return 0
        for m in await self.list_maps(tid):
            await self._exec(
                TournamentMap,
                "INSERT INTO `tmsm_tournament_map` "
                "(`tournament_id`, `order_index`, `map_uid`, `name`, `status`) "
                "VALUES (%s, %s, %s, %s, 'pending')",
                (new_id, int(m.get("order_index", 0) or 0),
                 m.get("map_uid"), m.get("name")),
            )
        return new_id

    # ---- participants --------------------------------------------------

    async def add_participant(self, tid: int, login: str, nickname: str, now: _dt.datetime) -> bool:
        if not login or not await self._ready_check():
            return False
        try:
            await self._exec(
                TournamentParticipant,
                "INSERT INTO `tmsm_tournament_participant` "
                "(`tournament_id`, `login`, `nickname`, `seed`, `points`, `joined_at`) "
                "VALUES (%s, %s, %s, 0, 0, %s) "
                "ON DUPLICATE KEY UPDATE `nickname` = VALUES(`nickname`)",
                (int(tid), login, nickname or None, now),
            )
            return True
        except Exception:
            logger.exception("tournaments: add_participant failed tid=%s login=%s", tid, login)
            return False

    async def remove_participant(self, tid: int, login: str) -> None:
        if not await self._ready_check():
            return
        await self._exec(
            TournamentParticipant,
            "DELETE FROM `tmsm_tournament_participant` "
            "WHERE `tournament_id` = %s AND `login` = %s",
            (int(tid), login),
        )

    async def list_participants(self, tid: int) -> list[dict[str, Any]]:
        if not await self._ready_check():
            return []
        rows = await self._exec(
            TournamentParticipant,
            "SELECT * FROM `tmsm_tournament_participant` "
            "WHERE `tournament_id` = %s ORDER BY `points` DESC, `seed` ASC, `id` ASC",
            (int(tid),),
        )
        return [self._row(r) for r in rows]

    async def is_participant(self, tid: int, login: str) -> bool:
        if not await self._ready_check():
            return False
        rows = await self._exec(
            TournamentParticipant,
            "SELECT `id` FROM `tmsm_tournament_participant` "
            "WHERE `tournament_id` = %s AND `login` = %s",
            (int(tid), login),
        )
        return any(True for _ in rows)

    # ---- maps ----------------------------------------------------------

    async def add_map(self, tid: int, uid: str, name: str) -> None:
        if not uid or not await self._ready_check():
            return
        rows = await self._exec(
            TournamentMap,
            "SELECT COALESCE(MAX(`order_index`), -1) AS `mx` "
            "FROM `tmsm_tournament_map` WHERE `tournament_id` = %s",
            (int(tid),),
        )
        order_index = 0
        for row in rows:
            order_index = int(self._row(row).get("mx", -1) or -1) + 1
        await self._exec(
            TournamentMap,
            "INSERT INTO `tmsm_tournament_map` "
            "(`tournament_id`, `order_index`, `map_uid`, `name`, `status`) "
            "VALUES (%s, %s, %s, %s, 'pending')",
            (int(tid), order_index, uid, name or None),
        )

    async def remove_map(self, map_id: int) -> None:
        if not await self._ready_check():
            return
        await self._exec(TournamentResult,
                         "DELETE FROM `tmsm_tournament_result` WHERE `map_id` = %s", (int(map_id),))
        await self._exec(TournamentMap,
                         "DELETE FROM `tmsm_tournament_map` WHERE `id` = %s", (int(map_id),))

    async def move_map(self, tid: int, map_id: int, direction: int) -> None:
        """Swap a map with its neighbour in the pool (direction -1 up / +1 down)."""
        if not await self._ready_check():
            return
        maps = await self.list_maps(tid)
        idx = next((i for i, m in enumerate(maps) if int(m["id"]) == int(map_id)), None)
        if idx is None:
            return
        target = idx + (1 if direction > 0 else -1)
        if target < 0 or target >= len(maps):
            return
        a, b = maps[idx], maps[target]
        await self._exec(
            TournamentMap,
            "UPDATE `tmsm_tournament_map` SET `order_index` = %s WHERE `id` = %s",
            (int(b["order_index"]), int(a["id"])),
        )
        await self._exec(
            TournamentMap,
            "UPDATE `tmsm_tournament_map` SET `order_index` = %s WHERE `id` = %s",
            (int(a["order_index"]), int(b["id"])),
        )

    async def list_maps(self, tid: int) -> list[dict[str, Any]]:
        if not await self._ready_check():
            return []
        rows = await self._exec(
            TournamentMap,
            "SELECT * FROM `tmsm_tournament_map` "
            "WHERE `tournament_id` = %s ORDER BY `order_index` ASC, `id` ASC",
            (int(tid),),
        )
        return [self._row(r) for r in rows]

    async def get_map(self, map_id: int) -> dict[str, Any] | None:
        if not await self._ready_check():
            return None
        rows = await self._exec(
            TournamentMap, "SELECT * FROM `tmsm_tournament_map` WHERE `id` = %s", (int(map_id),))
        for row in rows:
            return self._row(row)
        return None

    async def set_map_status(self, map_id: int, status: str, played_at: _dt.datetime | None = None) -> None:
        if not await self._ready_check():
            return
        await self._exec(
            TournamentMap,
            "UPDATE `tmsm_tournament_map` SET `status` = %s, `played_at` = %s WHERE `id` = %s",
            (status, played_at, int(map_id)),
        )

    # ---- results + standings ------------------------------------------

    async def clear_results_for_map(self, map_id: int) -> None:
        if not await self._ready_check():
            return
        await self._exec(TournamentResult,
                         "DELETE FROM `tmsm_tournament_result` WHERE `map_id` = %s", (int(map_id),))

    async def add_result(self, tid: int, map_id: int, login: str, nickname: str,
                         position: int, points: int, score: int, now: _dt.datetime) -> None:
        if not login or not await self._ready_check():
            return
        await self._exec(
            TournamentResult,
            "INSERT INTO `tmsm_tournament_result` "
            "(`tournament_id`, `map_id`, `login`, `nickname`, `position`, `points`, `score`, `created_at`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE "
            "`nickname` = VALUES(`nickname`), `position` = VALUES(`position`), "
            "`points` = VALUES(`points`), `score` = VALUES(`score`)",
            (int(tid), int(map_id), login, nickname or None,
             int(position), int(points), int(score), now),
        )

    async def list_results(self, tid: int) -> list[dict[str, Any]]:
        if not await self._ready_check():
            return []
        rows = await self._exec(
            TournamentResult,
            "SELECT * FROM `tmsm_tournament_result` WHERE `tournament_id` = %s",
            (int(tid),),
        )
        return [self._row(r) for r in rows]

    async def standings(self, tid: int) -> list[dict[str, Any]]:
        """Aggregate results into a ranked leaderboard with tiebreakers."""
        participants = await self.list_participants(tid)
        results = await self.list_results(tid)

        agg: dict[str, dict[str, Any]] = {}
        for p in participants:
            login = str(p.get("login") or "")
            if not login:
                continue
            agg[login] = {
                "login": login,
                "nickname": str(p.get("nickname") or login),
                "points": 0,
                "placements": {},   # position -> count
                "best_position": 0,
            }
        for r in results:
            login = str(r.get("login") or "")
            if login not in agg:
                # A result from someone no longer registered: still show them.
                agg[login] = {
                    "login": login,
                    "nickname": str(r.get("nickname") or login),
                    "points": 0,
                    "placements": {},
                    "best_position": 0,
                }
            entry = agg[login]
            entry["points"] += int(r.get("points", 0) or 0)
            pos = int(r.get("position", 0) or 0)
            if pos > 0:
                entry["placements"][pos] = entry["placements"].get(pos, 0) + 1
                if entry["best_position"] == 0 or pos < entry["best_position"]:
                    entry["best_position"] = pos

        max_pos = 0
        for entry in agg.values():
            for pos in entry["placements"]:
                max_pos = max(max_pos, pos)

        def sort_key(entry: dict[str, Any]):
            # Higher points first; then more better placements (more 1sts,
            # then 2nds, ...); then best single finish.
            placement_vec = tuple(
                -entry["placements"].get(pos, 0) for pos in range(1, max_pos + 1)
            )
            best = entry["best_position"] or (max_pos + 1)
            return (-entry["points"], placement_vec, best)

        ordered = sorted(agg.values(), key=sort_key)
        out: list[dict[str, Any]] = []
        for rank, entry in enumerate(ordered, start=1):
            out.append({
                "rank": rank,
                "login": entry["login"],
                "nickname": entry["nickname"],
                "points": entry["points"],
                "best_position": entry["best_position"],
            })
        return out

    async def recompute_participant_points(self, tid: int) -> None:
        """Cache each participant's total points (for quick display)."""
        if not await self._ready_check():
            return
        standings = await self.standings(tid)
        for entry in standings:
            await self._exec(
                TournamentParticipant,
                "UPDATE `tmsm_tournament_participant` SET `points` = %s "
                "WHERE `tournament_id` = %s AND `login` = %s",
                (int(entry["points"]), int(tid), entry["login"]),
            )
