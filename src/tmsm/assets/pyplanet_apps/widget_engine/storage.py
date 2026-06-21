"""DB-backed storage for the widget engine.

Slice 2: `we_widget` (one row per widget — global base).
Slice 4: `we_phase_override` (per-phase overlay; non-NULL columns win).

All raw SQL routes through `db.objects.execute(Model.raw(sql))` with
`raw.database = db.objects.database` to bypass peewee_async's Proxy
swap check (see notes/repo/widget-frame-working-snapshot.md).
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Optional

from .models import WePhaseOverride, WeRemoved, WeSetting, WeWidget
from .registry import AnimDir, DriveMode, Phase, WidgetEntry

logger = logging.getLogger(__name__)


# Every column on we_widget except the PK and updated_at. Used for both
# table bootstrap (CREATE TABLE) and missing-column detection (ALTER TABLE).
_COLUMN_DDL: tuple[tuple[str, str], ...] = (
    ("x",                 "DOUBLE NOT NULL DEFAULT 0"),
    ("y",                 "DOUBLE NOT NULL DEFAULT 0"),
    ("w",                 "DOUBLE NOT NULL DEFAULT 40"),
    ("h",                 "DOUBLE NOT NULL DEFAULT 10"),
    ("drive_mode",        "VARCHAR(32) NULL"),
    ("anim_dir",          "VARCHAR(16) NULL"),
    ("anim_duration_ms",  "INTEGER NULL"),
    ("anim_in_delay_ms",  "INTEGER NULL"),
    ("anim_out_delay_ms", "INTEGER NULL"),
    ("disabled",          "TINYINT(1) NULL"),
    ("updated_at",        "DATETIME NULL"),
)

# we_phase_override columns. PK is (widget_key, phase); every other
# column is nullable because absent = inherit from we_widget.
_PHASE_COLUMN_DDL: tuple[tuple[str, str], ...] = (
    ("x",                 "DOUBLE NULL"),
    ("y",                 "DOUBLE NULL"),
    ("w",                 "DOUBLE NULL"),
    ("h",                 "DOUBLE NULL"),
    ("drive_mode",        "VARCHAR(32) NULL"),
    ("anim_dir",          "VARCHAR(16) NULL"),
    ("anim_duration_ms",  "INTEGER NULL"),
    ("anim_in_delay_ms",  "INTEGER NULL"),
    ("anim_out_delay_ms", "INTEGER NULL"),
    ("disabled",          "TINYINT(1) NULL"),
    ("updated_at",        "DATETIME NULL"),
)

# Columns the phase overlay can carry (excludes PK/timestamp).
_PHASE_OVERLAY_COLUMNS: frozenset = frozenset({
    "x", "y", "w", "h",
    "drive_mode", "anim_dir",
    "anim_duration_ms", "anim_in_delay_ms", "anim_out_delay_ms",
    "disabled",
})


class WidgetStorage:
    def __init__(self, instance):
        self.instance = instance
        self._rows: dict[str, dict[str, Any]] = {}
        # (widget_key, phase_value) -> row dict; NULL columns stay None
        self._phase_rows: dict[tuple[str, str], dict[str, Any]] = {}
        # widget_keys the user has uninstalled; suppresses auto-install on
        # subsequent addon re-registrations.
        self._tombstones: set[str] = set()
        # Engine-wide settings (key -> str value). Persisted in we_setting.
        self._settings: dict[str, str] = {}
        self._loaded = False

    # ── lifecycle ─────────────────────────────────────────────────────

    async def load(self) -> None:
        await self._ensure_schema()
        db = self._db
        if db is None:
            return
        try:
            rows = await WeWidget.objects.execute(WeWidget.select())
        except Exception:
            logger.exception("widget_engine.storage: load failed")
            return
        self._rows = {r.widget_key: self._row_to_dict(r) for r in rows}
        try:
            prows = await WePhaseOverride.objects.execute(WePhaseOverride.select())
        except Exception:
            logger.exception("widget_engine.storage: phase override load failed")
            prows = []
        self._phase_rows = {
            (p.widget_key, p.phase): self._phase_row_to_dict(p) for p in prows
        }
        try:
            trows = await WeRemoved.objects.execute(WeRemoved.select())
        except Exception:
            logger.exception("widget_engine.storage: tombstone load failed")
            trows = []
        self._tombstones = {t.widget_key for t in trows}
        try:
            srows = await WeSetting.objects.execute(WeSetting.select())
        except Exception:
            logger.exception("widget_engine.storage: setting load failed")
            srows = []
        self._settings = {s.key: s.value for s in srows if s.value is not None}
        self._loaded = True
        logger.info(
            "widget_engine.storage: loaded %d row(s), %d phase override(s), %d tombstone(s)",
            len(self._rows), len(self._phase_rows), len(self._tombstones),
        )

    async def ensure_row(self, entry: WidgetEntry) -> None:
        """Insert a default row for `entry` if none exists yet."""
        if entry.key in self._rows:
            return
        db = self._db
        if db is None:
            return
        row = {
            "widget_key": entry.key,
            "x": entry.default_x,
            "y": entry.default_y,
            "w": entry.default_w,
            "h": entry.default_h,
            "drive_mode": entry.drive_mode.value,
            "anim_dir": entry.animation.direction.value,
            "anim_duration_ms": entry.animation.duration_ms,
            "anim_in_delay_ms": entry.animation.in_delay_ms,
            "anim_out_delay_ms": entry.animation.out_delay_ms,
            "disabled": False,
            "updated_at": _dt.datetime.utcnow(),
        }
        try:
            await WeWidget.objects.execute(WeWidget.insert(**row))
        except Exception:
            logger.exception(
                "widget_engine.storage: ensure_row '%s' insert failed", entry.key,
            )
            return
        self._rows[entry.key] = row
        logger.info("widget_engine.storage: seeded row for '%s'", entry.key)

    # ── read ──────────────────────────────────────────────────────────

    def get(self, key: str) -> Optional[dict[str, Any]]:
        return self._rows.get(key)

    def all(self) -> dict[str, dict[str, Any]]:
        return dict(self._rows)

    def phase_get(self, key: str, phase: Phase) -> Optional[dict[str, Any]]:
        return self._phase_rows.get((key, phase.value))

    def phase_all(self) -> dict[tuple[str, str], dict[str, Any]]:
        return dict(self._phase_rows)

    # ── write ─────────────────────────────────────────────────────────

    async def set_position(self, key: str, x: float, y: float, w: float, h: float) -> None:
        await self._update(key, {"x": float(x), "y": float(y), "w": float(w), "h": float(h)})

    async def set_disabled(self, key: str, value: bool) -> None:
        await self._update(key, {"disabled": bool(value)})

    async def set_drive_mode(self, key: str, value: DriveMode) -> None:
        await self._update(key, {"drive_mode": value.value})

    async def set_animation(
        self, key: str, *,
        direction: Optional[AnimDir] = None,
        duration_ms: Optional[int] = None,
        in_delay_ms: Optional[int] = None,
        out_delay_ms: Optional[int] = None,
    ) -> None:
        patch: dict[str, Any] = {}
        if direction is not None:    patch["anim_dir"] = direction.value
        if duration_ms is not None:  patch["anim_duration_ms"] = int(duration_ms)
        if in_delay_ms is not None:  patch["anim_in_delay_ms"] = int(in_delay_ms)
        if out_delay_ms is not None: patch["anim_out_delay_ms"] = int(out_delay_ms)
        if patch:
            await self._update(key, patch)

    async def _update(self, key: str, patch: dict[str, Any]) -> None:
        db = self._db
        if db is None:
            return
        if key not in self._rows:
            # Cache miss: the row may still exist in the DB (cache loaded
            # before the widget seeded its row, or a stale row from a prior
            # session). Hydrate before deciding it is truly absent so the
            # write is not silently dropped.
            await self._fetch_row(key)
        patch = dict(patch)
        patch["updated_at"] = _dt.datetime.utcnow()
        try:
            await WeWidget.objects.execute(
                WeWidget.update(**patch).where(WeWidget.widget_key == key)
            )
        except Exception:
            logger.exception("widget_engine.storage: update '%s' failed", key)
            return
        if key in self._rows:
            self._rows[key].update(patch)
        else:
            logger.warning(
                "widget_engine.storage: update '%s' applied with no base row "
                "in cache or DB; call ensure_row first", key,
            )

    # ── phase overrides ───────────────────────────────────────────────

    async def phase_set(
        self, key: str, phase: Phase, patch: dict[str, Any],
    ) -> None:
        """Upsert a phase override. `patch` may contain any subset of the
        overlay columns; non-overlay keys are silently dropped. Passing
        `None` for a column clears that override only (without deleting
        the whole row)."""
        db = self._db
        if db is None or key not in self._rows:
            return
        clean = {k: v for k, v in patch.items() if k in _PHASE_OVERLAY_COLUMNS}
        if not clean:
            return
        clean["updated_at"] = _dt.datetime.utcnow()
        cache_key = (key, phase.value)
        existing = self._phase_rows.get(cache_key)
        try:
            if existing is None:
                row = {"widget_key": key, "phase": phase.value, **clean}
                await WePhaseOverride.objects.execute(
                    WePhaseOverride.insert(**row)
                )
                # Pad missing overlay columns with None for the cache so
                # callers can treat all rows uniformly.
                for col in _PHASE_OVERLAY_COLUMNS:
                    row.setdefault(col, None)
                self._phase_rows[cache_key] = row
            else:
                await WePhaseOverride.objects.execute(
                    WePhaseOverride.update(**clean).where(
                        (WePhaseOverride.widget_key == key)
                        & (WePhaseOverride.phase == phase.value)
                    )
                )
                existing.update(clean)
        except Exception:
            logger.exception(
                "widget_engine.storage: phase_set '%s'/%s failed", key, phase.value,
            )

    async def phase_clear(self, key: str, phase: Phase) -> None:
        """Delete the phase override row entirely."""
        db = self._db
        if db is None:
            return
        cache_key = (key, phase.value)
        try:
            await WePhaseOverride.objects.execute(
                WePhaseOverride.delete().where(
                    (WePhaseOverride.widget_key == key)
                    & (WePhaseOverride.phase == phase.value)
                )
            )
        except Exception:
            logger.exception(
                "widget_engine.storage: phase_clear '%s'/%s failed",
                key, phase.value,
            )
            return
        self._phase_rows.pop(cache_key, None)

    async def delete_widget(self, key: str) -> None:
        """Delete the base row + every phase override for `key` and drop
        cache entries. The widget's runtime registration is untouched —
        the caller is responsible for re-seeding defaults via
        `ensure_row(entry)` if it wants the widget to keep rendering."""
        db = self._db
        if db is None:
            return
        try:
            await WePhaseOverride.objects.execute(
                WePhaseOverride.delete().where(WePhaseOverride.widget_key == key)
            )
        except Exception:
            logger.exception(
                "widget_engine.storage: delete_widget '%s' phase wipe failed", key,
            )
        try:
            await WeWidget.objects.execute(
                WeWidget.delete().where(WeWidget.widget_key == key)
            )
        except Exception:
            logger.exception(
                "widget_engine.storage: delete_widget '%s' base wipe failed", key,
            )
            return
        self._rows.pop(key, None)
        for cache_key in [ck for ck in self._phase_rows if ck[0] == key]:
            self._phase_rows.pop(cache_key, None)

    # ── tombstones (install/uninstall) ────────────────────────────────

    def is_removed(self, key: str) -> bool:
        return key in self._tombstones

    async def add_tombstone(self, key: str) -> None:
        if self._db is None:
            return
        try:
            await WeRemoved.objects.execute(
                WeRemoved.insert(widget_key=key, removed_at=_dt.datetime.utcnow())
                .on_conflict_replace()
            )
        except Exception:
            logger.exception("widget_engine.storage: add_tombstone '%s' failed", key)
            return
        self._tombstones.add(key)

    async def clear_tombstone(self, key: str) -> None:
        if self._db is None:
            return
        try:
            await WeRemoved.objects.execute(
                WeRemoved.delete().where(WeRemoved.widget_key == key)
            )
        except Exception:
            logger.exception("widget_engine.storage: clear_tombstone '%s' failed", key)
            return
        self._tombstones.discard(key)

    # ── settings (engine-wide key/value) ────────────────────────────

    def setting_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self._settings.get(key, default)

    def settings_all(self) -> dict[str, str]:
        return dict(self._settings)

    async def setting_set(self, key: str, value: str) -> None:
        db = self._db
        if db is None:
            return
        now = _dt.datetime.utcnow()
        try:
            raw = WeWidget.raw(
                "REPLACE INTO `we_setting` (`key`, `value`, `updated_at`) "
                "VALUES (%s, %s, %s)",
                key, value, now,
            )
            raw.database = db.objects.database
            await db.objects.execute(raw)
        except Exception:
            logger.exception(
                "widget_engine.storage: setting_set '%s' failed", key,
            )
            return
        self._settings[key] = value

    async def setting_delete(self, key: str) -> None:
        db = self._db
        if db is None:
            return
        try:
            await WeSetting.objects.execute(
                WeSetting.delete().where(WeSetting.key == key)
            )
        except Exception:
            logger.exception(
                "widget_engine.storage: setting_delete '%s' failed", key,
            )
            return
        self._settings.pop(key, None)

    # ── schema / introspection ────────────────────────────────────────

    @property
    def _db(self):
        db = getattr(self.instance, "db", None)
        if db is None or not hasattr(db, "objects"):
            return None
        return db

    async def _ensure_schema(self) -> None:
        """Create `we_widget` if missing; add any columns that aren't there.

        Raw SQL must go through `db.objects.execute(Model.raw(...))` with
        `raw.database` pinned to the live MySQLDatabase — peewee_async's
        manager refuses queries whose database is the Proxy that peewee
        sets at class-definition time.
        """
        db = self._db
        if db is None:
            return
        existing = await self._existing_columns("we_widget")
        if not existing:
            cols_sql = ", ".join(f"`{n}` {ddl}" for n, ddl in _COLUMN_DDL)
            create_sql = (
                "CREATE TABLE IF NOT EXISTS `we_widget` ("
                "`widget_key` VARCHAR(64) NOT NULL PRIMARY KEY, "
                f"{cols_sql}"
                ") DEFAULT CHARSET=utf8mb4"
            )
            await self._exec_raw(create_sql)
            logger.info("widget_engine.storage: created table we_widget")
        else:
            for name, ddl in _COLUMN_DDL:
                if name in existing:
                    continue
                await self._exec_raw(
                    f"ALTER TABLE `we_widget` ADD COLUMN `{name}` {ddl}"
                )
                logger.info("widget_engine.storage: added column we_widget.%s", name)

        # Phase override table.
        existing_p = await self._existing_columns("we_phase_override")
        if not existing_p:
            cols_sql = ", ".join(f"`{n}` {ddl}" for n, ddl in _PHASE_COLUMN_DDL)
            create_sql = (
                "CREATE TABLE IF NOT EXISTS `we_phase_override` ("
                "`widget_key` VARCHAR(64) NOT NULL, "
                "`phase` VARCHAR(16) NOT NULL, "
                f"{cols_sql}, "
                "PRIMARY KEY (`widget_key`, `phase`)"
                ") DEFAULT CHARSET=utf8mb4"
            )
            await self._exec_raw(create_sql)
            logger.info("widget_engine.storage: created table we_phase_override")
        else:
            for name, ddl in _PHASE_COLUMN_DDL:
                if name in existing_p:
                    continue
                await self._exec_raw(
                    f"ALTER TABLE `we_phase_override` ADD COLUMN `{name}` {ddl}"
                )
                logger.info(
                    "widget_engine.storage: added column we_phase_override.%s", name,
                )

        # Tombstone table (uninstall marker). Single PK column + timestamp.
        existing_t = await self._existing_columns("we_removed")
        if not existing_t:
            await self._exec_raw(
                "CREATE TABLE IF NOT EXISTS `we_removed` ("
                "`widget_key` VARCHAR(64) NOT NULL PRIMARY KEY, "
                "`removed_at` DATETIME NULL"
                ") DEFAULT CHARSET=utf8mb4"
            )
            logger.info("widget_engine.storage: created table we_removed")

        # Setting table (engine-wide key/value).
        existing_s = await self._existing_columns("we_setting")
        if not existing_s:
            await self._exec_raw(
                "CREATE TABLE IF NOT EXISTS `we_setting` ("
                "`key` VARCHAR(64) NOT NULL PRIMARY KEY, "
                "`value` VARCHAR(255) NULL, "
                "`updated_at` DATETIME NULL"
                ") DEFAULT CHARSET=utf8mb4"
            )
            logger.info("widget_engine.storage: created table we_setting")

    async def _existing_columns(self, table: str) -> set[str]:
        db = self._db
        if db is None:
            return set()
        try:
            raw = WeWidget.raw(f"SHOW COLUMNS FROM `{table}`")
            raw.database = db.objects.database
            rows = await db.objects.execute(raw)
        except Exception:
            logger.warning(
                "widget_engine.storage: failed to inspect columns for %s", table,
                exc_info=True,
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
        raw = WeWidget.raw(sql)
        raw.database = db.objects.database
        await db.objects.execute(raw)

    @staticmethod
    def _row_to_dict(row: WeWidget) -> dict[str, Any]:
        return {
            "widget_key": row.widget_key,
            "x": float(row.x),
            "y": float(row.y),
            "w": float(row.w),
            "h": float(row.h),
            "drive_mode": row.drive_mode,
            "anim_dir": row.anim_dir,
            "anim_duration_ms": row.anim_duration_ms,
            "anim_in_delay_ms": row.anim_in_delay_ms,
            "anim_out_delay_ms": row.anim_out_delay_ms,
            "disabled": bool(row.disabled) if row.disabled is not None else False,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def _phase_row_to_dict(row: WePhaseOverride) -> dict[str, Any]:
        return {
            "widget_key": row.widget_key,
            "phase": row.phase,
            "x": float(row.x) if row.x is not None else None,
            "y": float(row.y) if row.y is not None else None,
            "w": float(row.w) if row.w is not None else None,
            "h": float(row.h) if row.h is not None else None,
            "drive_mode": row.drive_mode,
            "anim_dir": row.anim_dir,
            "anim_duration_ms": row.anim_duration_ms,
            "anim_in_delay_ms": row.anim_in_delay_ms,
            "anim_out_delay_ms": row.anim_out_delay_ms,
            "disabled": bool(row.disabled) if row.disabled is not None else None,
            "updated_at": row.updated_at,
        }
