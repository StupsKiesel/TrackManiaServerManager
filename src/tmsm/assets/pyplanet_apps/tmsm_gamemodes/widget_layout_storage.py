"""DB-backed storage for per-mode widget layout overrides.

Mirrors the bootstrap pattern from `widget_engine.storage`: schema is
created on first use via raw SQL through `db.objects.execute(Model.raw(...))`
with `raw.database` pinned to the live MySQLDatabase. The in-memory cache
is the source of truth for synchronous reads from operator UI rendering.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from .models import GmWidgetConfig

logger = logging.getLogger(__name__)


_COLUMN_DDL: tuple[tuple[str, str], ...] = (
    ("x",                 "DOUBLE NOT NULL DEFAULT 0"),
    ("y",                 "DOUBLE NOT NULL DEFAULT 0"),
    ("w",                 "DOUBLE NOT NULL DEFAULT 40"),
    ("h",                 "DOUBLE NOT NULL DEFAULT 10"),
    ("disabled",          "TINYINT(1) NULL"),
    ("drive_mode",        "VARCHAR(32) NULL"),
    ("anim_dir",          "VARCHAR(16) NULL"),
    ("anim_duration_ms",  "INTEGER NULL"),
    ("anim_in_delay_ms",  "INTEGER NULL"),
    ("anim_out_delay_ms", "INTEGER NULL"),
    ("updated_at",        "DATETIME NULL"),
)

_VALID_DRIVE = {"fixed", "hide_while_driving", "only_shown_while_driving"}
_VALID_ANIM = {"none", "left", "right", "up", "down"}


def _opt_int(val: Any) -> Any:
    if val is None or val == "":
        return None
    try:
        return int(round(float(val)))
    except (TypeError, ValueError):
        return None


def _clean_row(row: dict[str, Any]) -> dict[str, Any] | None:
    key = str(row.get("widget_key") or row.get("key") or "").strip()
    if not key:
        return None
    try:
        x = float(row.get("x", 0.0) or 0.0)
        y = float(row.get("y", 0.0) or 0.0)
        w = float(row.get("w", 40.0) or 40.0)
        h = float(row.get("h", 10.0) or 10.0)
    except (TypeError, ValueError):
        return None
    drive_mode = row.get("drive_mode")
    if drive_mode is not None:
        drive_mode = str(drive_mode).lower()
        if drive_mode not in _VALID_DRIVE:
            drive_mode = None
    anim_dir = row.get("anim_dir")
    if anim_dir is not None:
        anim_dir = str(anim_dir).lower()
        if anim_dir not in _VALID_ANIM:
            anim_dir = None
    return {
        "key": key,
        "widget_key": key,
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "disabled": bool(row.get("disabled", False)),
        "drive_mode": drive_mode,
        "anim_dir": anim_dir,
        "anim_duration_ms": _opt_int(row.get("anim_duration_ms")),
        "anim_in_delay_ms": _opt_int(row.get("anim_in_delay_ms")),
        "anim_out_delay_ms": _opt_int(row.get("anim_out_delay_ms")),
    }


class WidgetLayoutStorage:
    def __init__(self, instance):
        self.instance = instance
        self._by_mode: dict[str, dict[str, dict[str, Any]]] = {}
        self._loaded = False

    # ── lifecycle ─────────────────────────────────────────────────────

    async def load(self) -> None:
        await self._ensure_schema()
        db = self._db
        if db is None:
            return
        try:
            rows = await GmWidgetConfig.objects.execute(GmWidgetConfig.select())
        except Exception:
            logger.exception("tmsm_gamemodes.storage: widget layout load failed")
            return
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for r in rows:
            cleaned = _clean_row({
                "widget_key": r.widget_key,
                "x": r.x, "y": r.y, "w": r.w, "h": r.h,
                "disabled": r.disabled,
                "drive_mode": r.drive_mode,
                "anim_dir": r.anim_dir,
                "anim_duration_ms": r.anim_duration_ms,
                "anim_in_delay_ms": r.anim_in_delay_ms,
                "anim_out_delay_ms": r.anim_out_delay_ms,
            })
            if cleaned is None:
                continue
            out.setdefault(r.mode_key, {})[cleaned["widget_key"]] = cleaned
        self._by_mode = out
        self._loaded = True
        total = sum(len(v) for v in out.values())
        logger.info(
            "tmsm_gamemodes.storage: loaded %d widget override(s) across %d mode(s)",
            total, len(out),
        )

    # ── reads (sync from in-memory cache) ─────────────────────────────

    def get(self, mode_key: str) -> list[dict[str, Any]]:
        bucket = self._by_mode.get(mode_key) or {}
        return sorted(
            (dict(r) for r in bucket.values()),
            key=lambda r: str(r.get("widget_key") or ""),
        )

    def all(self) -> dict[str, list[dict[str, Any]]]:
        return {k: self.get(k) for k in self._by_mode.keys()}

    # ── writes ────────────────────────────────────────────────────────

    async def upsert(self, mode_key: str, row: dict[str, Any]) -> None:
        cleaned = _clean_row(row)
        if cleaned is None:
            return
        db = self._db
        if db is None:
            return
        now = _dt.datetime.utcnow()
        payload = {
            "mode_key": mode_key,
            "widget_key": cleaned["widget_key"],
            "x": cleaned["x"],
            "y": cleaned["y"],
            "w": cleaned["w"],
            "h": cleaned["h"],
            "disabled": bool(cleaned.get("disabled", False)),
            "drive_mode": cleaned.get("drive_mode"),
            "anim_dir": cleaned.get("anim_dir"),
            "anim_duration_ms": cleaned.get("anim_duration_ms"),
            "anim_in_delay_ms": cleaned.get("anim_in_delay_ms"),
            "anim_out_delay_ms": cleaned.get("anim_out_delay_ms"),
            "updated_at": now,
        }
        bucket = self._by_mode.setdefault(mode_key, {})
        existed = cleaned["widget_key"] in bucket
        try:
            if existed:
                patch = {k: v for k, v in payload.items()
                         if k not in ("mode_key", "widget_key")}
                await GmWidgetConfig.objects.execute(
                    GmWidgetConfig.update(**patch).where(
                        (GmWidgetConfig.mode_key == mode_key)
                        & (GmWidgetConfig.widget_key == cleaned["widget_key"])
                    )
                )
            else:
                await GmWidgetConfig.objects.execute(
                    GmWidgetConfig.insert(**payload)
                )
        except Exception:
            logger.exception(
                "tmsm_gamemodes.storage: upsert %s/%s failed",
                mode_key, cleaned["widget_key"],
            )
            return
        bucket[cleaned["widget_key"]] = cleaned

    async def delete(self, mode_key: str, widget_key: str) -> None:
        db = self._db
        if db is None:
            return
        try:
            await GmWidgetConfig.objects.execute(
                GmWidgetConfig.delete().where(
                    (GmWidgetConfig.mode_key == mode_key)
                    & (GmWidgetConfig.widget_key == widget_key)
                )
            )
        except Exception:
            logger.exception(
                "tmsm_gamemodes.storage: delete %s/%s failed",
                mode_key, widget_key,
            )
            return
        bucket = self._by_mode.get(mode_key)
        if bucket is not None:
            bucket.pop(widget_key, None)
            if not bucket:
                self._by_mode.pop(mode_key, None)

    async def clear(self, mode_key: str) -> None:
        db = self._db
        if db is None:
            return
        try:
            await GmWidgetConfig.objects.execute(
                GmWidgetConfig.delete().where(GmWidgetConfig.mode_key == mode_key)
            )
        except Exception:
            logger.exception(
                "tmsm_gamemodes.storage: clear %s failed", mode_key,
            )
            return
        self._by_mode.pop(mode_key, None)

    async def replace_mode(self, mode_key: str, rows: list[dict[str, Any]]) -> None:
        await self.clear(mode_key)
        for row in rows:
            await self.upsert(mode_key, row)

    # ── schema bootstrap ──────────────────────────────────────────────

    @property
    def _db(self):
        db = getattr(self.instance, "db", None)
        if db is None or not hasattr(db, "objects"):
            return None
        return db

    async def _ensure_schema(self) -> None:
        db = self._db
        if db is None:
            return
        existing = await self._existing_columns("gm_widget_config")
        if not existing:
            cols_sql = ", ".join(f"`{n}` {ddl}" for n, ddl in _COLUMN_DDL)
            create_sql = (
                "CREATE TABLE IF NOT EXISTS `gm_widget_config` ("
                "`mode_key` VARCHAR(64) NOT NULL, "
                "`widget_key` VARCHAR(64) NOT NULL, "
                f"{cols_sql}, "
                "PRIMARY KEY (`mode_key`, `widget_key`)"
                ") DEFAULT CHARSET=utf8mb4"
            )
            await self._exec_raw(create_sql)
            logger.info("tmsm_gamemodes.storage: created table gm_widget_config")
        else:
            for name, ddl in _COLUMN_DDL:
                if name in existing:
                    continue
                await self._exec_raw(
                    f"ALTER TABLE `gm_widget_config` ADD COLUMN `{name}` {ddl}"
                )
                logger.info(
                    "tmsm_gamemodes.storage: added column gm_widget_config.%s", name,
                )

    async def _existing_columns(self, table: str) -> set[str]:
        db = self._db
        if db is None:
            return set()
        try:
            raw = GmWidgetConfig.raw(f"SHOW COLUMNS FROM `{table}`")
            raw.database = db.objects.database
            rows = await db.objects.execute(raw)
        except Exception:
            logger.warning(
                "tmsm_gamemodes.storage: failed to inspect columns for %s",
                table, exc_info=True,
            )
            return set()
        out: set[str] = set()
        for row in rows:
            field = None
            if isinstance(row, dict):
                field = row.get("Field")
            else:
                field = getattr(row, "Field", None)
                if field is None:
                    try:
                        field = row[0]
                    except Exception:
                        field = None
            if field:
                out.add(str(field))
        return out

    async def _exec_raw(self, sql: str) -> None:
        db = self._db
        if db is None:
            return
        raw = GmWidgetConfig.raw(sql)
        raw.database = db.objects.database
        await db.objects.execute(raw)
