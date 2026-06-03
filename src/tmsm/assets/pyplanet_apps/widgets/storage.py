"""DB-backed persistence for widget configuration, with in-memory caches.

Two tables (see :mod:`.models`):

* ``tmsm_widget_config_global``    — admin-set defaults (position +
  behaviour) for every player.
* ``tmsm_widget_config_personal``  — per-player position overrides
  (win over the global row's position; behaviour is server-wide).

PyPlanet runs peewee on top of ``peewee_async``, so every query goes
through the manager at ``instance.db.objects`` (``execute``/``get``/
``create``/``update``). The hot read path (``resolve()``/
``resolve_behavior()``) is synchronous — it only touches the in-memory
caches that ``load()`` populates at app start. Mutations are async
(cache + DB upsert/delete).

On first start (global table empty), :meth:`seed_defaults` populates
the global table from ``defaults.json`` shipped alongside this module.
"""
from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any

from .models import WidgetConfigGlobal, WidgetConfigPersonal, WidgetGroupConfig, WidgetThemeOverride

logger = logging.getLogger(__name__)


_POS_FIELDS = ("x", "y", "w", "h")
_BEH_FIELDS = (
    "hide_while_driving",
    "drive_mode",
    "state_modes",
    "group_key",
    "group_member_enabled",
    "group_priority",
    "group_order",
    "anim_dir",
    "anim_duration_ms",
    "anim_delay_ms",
    "allow_personal",
    "strip_prefer_top",
    "widget_disabled",
)
# Subset of behaviour fields that may be overridden per-player.
_PERS_BEH_FIELDS = ("anim_dir", "anim_duration_ms", "anim_delay_ms")
_UI_CAL_KEY = "__ui_calibration__"
_STATE_FIELDS = (
    ("all", "state_all"),
    ("loading_map", "state_loading_map"),
    ("warmup", "state_warmup"),
    ("pre_race", "state_pre_race"),
    ("in_race", "state_in_race"),
    ("in_podium", "state_in_podium"),
    ("post_race", "state_post_race"),
)


def _clean_pos(pos: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for k in _POS_FIELDS:
        if k in pos:
            try:
                out[k] = float(pos[k])
            except (TypeError, ValueError):
                pass
    return out


class WidgetStorage:
    """In-memory cache + async write-through DB persistence."""

    def __init__(self, instance) -> None:
        self.instance = instance
        # widget_key -> {x,y,w,h}
        self._global: dict[str, dict[str, float]] = {}
        # login -> widget_key -> {x,y,w,h}
        self._players: dict[str, dict[str, dict[str, float]]] = {}
        # widget_key -> {hide_while_driving, anim_dir, anim_duration_ms, anim_delay_ms}
        # Only includes keys that have a non-null value in the DB row.
        self._behavior: dict[str, dict[str, Any]] = {}
        # login -> widget_key -> {anim_dir?, anim_duration_ms?, anim_delay_ms?}
        # Only includes keys with at least one non-null personal anim col.
        self._player_beh: dict[str, dict[str, dict[str, Any]]] = {}
        # group_key -> persisted group-level config
        self._groups: dict[str, dict[str, Any]] = {}
        # theme_key -> {token: value} master-admin overrides
        self._theme_overrides: dict[str, dict[str, str]] = {}
        # Global base-frame settings (master-admin). In-memory for now;
        # persistence can be wired later without changing call sites.
        self.strip_color_override: str = ""
        self.strip_thickness: float = 1.0
        self.bg_color_override: str = ""
        # Per-widget strip_prefer_top overrides (master-admin). In-memory.
        # Missing key = use the widget's class default WIDGET_STRIP_PREFER_TOP.
        self.strip_prefer_top: dict[str, bool] = {}
        self._loaded = False

    # ---- io ------------------------------------------------------------

    async def load(self) -> None:
        """Populate the in-memory caches from the DB."""
        self._global.clear()
        self._players.clear()
        self._behavior.clear()
        self._player_beh.clear()
        self._groups.clear()
        self._theme_overrides.clear()
        self.strip_prefer_top.clear()
        await self._ensure_schema()
        try:
            gcols = await self._existing_columns("tmsm_widget_config_global")
            gsel = self._select_existing_fields(
                WidgetConfigGlobal,
                gcols,
                (
                    "widget_key",
                    "x", "y", "w", "h",
                    "hide_while_driving",
                    "drive_mode",
                    "state_all",
                    "state_loading_map",
                    "state_warmup",
                    "state_pre_race",
                    "state_in_race",
                    "state_in_podium",
                    "state_post_race",
                    "group_key",
                    "group_member_enabled",
                    "group_priority",
                    "group_order",
                    "anim_dir",
                    "anim_duration_ms",
                    "anim_delay_ms",
                    "allow_personal",
                    "strip_prefer_top",
                    "widget_disabled",
                ),
            )
            rows = await WidgetConfigGlobal.objects.execute(
                WidgetConfigGlobal.select(*gsel)
            )
            for row in rows:
                self._global[row.widget_key] = {
                    "x": float(row.x), "y": float(row.y),
                    "w": float(row.w), "h": float(row.h),
                }
                beh: dict[str, Any] = {}
                if row.hide_while_driving is not None:
                    beh["hide_while_driving"] = bool(row.hide_while_driving)
                if getattr(row, "drive_mode", None) is not None:
                    beh["drive_mode"] = str(row.drive_mode)
                modes: list[str] = []
                for mode, col in _STATE_FIELDS:
                    val = getattr(row, col, None)
                    if val is True:
                        modes.append(mode)
                if modes:
                    if "all" in modes:
                        beh["state_modes"] = ["all"]
                    else:
                        beh["state_modes"] = modes
                if getattr(row, "group_key", None) is not None:
                    beh["group_key"] = str(row.group_key)
                if getattr(row, "group_member_enabled", None) is not None:
                    beh["group_member_enabled"] = bool(row.group_member_enabled)
                if getattr(row, "group_priority", None) is not None:
                    beh["group_priority"] = int(row.group_priority)
                if getattr(row, "group_order", None) is not None:
                    beh["group_order"] = int(row.group_order)
                if row.anim_dir is not None:
                    beh["anim_dir"] = str(row.anim_dir)
                if row.anim_duration_ms is not None:
                    beh["anim_duration_ms"] = int(row.anim_duration_ms)
                if row.anim_delay_ms is not None:
                    beh["anim_delay_ms"] = int(row.anim_delay_ms)
                if row.allow_personal is not None:
                    beh["allow_personal"] = bool(row.allow_personal)
                spt = getattr(row, "strip_prefer_top", None)
                if spt is not None:
                    val = bool(spt)
                    beh["strip_prefer_top"] = val
                    self.strip_prefer_top[row.widget_key] = val
                wd = getattr(row, "widget_disabled", None)
                if wd is not None:
                    beh["widget_disabled"] = bool(wd)
                if beh:
                    self._behavior[row.widget_key] = beh
        except Exception:
            logger.exception("widgets: failed to load global config")
        try:
            pcols = await self._existing_columns("tmsm_widget_config_personal")
            psel = self._select_existing_fields(
                WidgetConfigPersonal,
                pcols,
                (
                    "widget_key",
                    "login",
                    "x", "y", "w", "h",
                    "anim_dir",
                    "anim_duration_ms",
                    "anim_delay_ms",
                ),
            )
            rows = await WidgetConfigPersonal.objects.execute(
                WidgetConfigPersonal.select(*psel)
            )
            for row in rows:
                self._players.setdefault(row.login, {})[row.widget_key] = {
                    "x": float(row.x), "y": float(row.y),
                    "w": float(row.w), "h": float(row.h),
                }
                pbeh: dict[str, Any] = {}
                if row.anim_dir is not None:
                    pbeh["anim_dir"] = str(row.anim_dir)
                if row.anim_duration_ms is not None:
                    pbeh["anim_duration_ms"] = int(row.anim_duration_ms)
                if row.anim_delay_ms is not None:
                    pbeh["anim_delay_ms"] = int(row.anim_delay_ms)
                if pbeh:
                    self._player_beh.setdefault(row.login, {})[row.widget_key] = pbeh
        except Exception:
            logger.exception("widgets: failed to load personal config")
        try:
            rows = await WidgetGroupConfig.objects.execute(
                WidgetGroupConfig.select()
            )
            for row in rows:
                key = str(row.group_key or "").strip()
                if not key:
                    continue
                self._groups[key] = {
                    "key": key,
                    "label": str(row.label or key),
                    "description": str(row.description or ""),
                    "order": int(row.order or 0),
                    "anchor_x": float(getattr(row, "anchor_x", 0.0) or 0.0),
                    "anchor_y": float(getattr(row, "anchor_y", 0.0) or 0.0),
                    "anchor_w": float(getattr(row, "anchor_w", 18.0) or 18.0),
                    "anchor_h": float(getattr(row, "anchor_h", 8.0) or 8.0),
                    "mode": str(getattr(row, "mode", None) or "priority_active"),
                    "max_visible": int(getattr(row, "max_visible", 1) or 1),
                    "runtime_prev_enabled": bool(getattr(row, "runtime_prev_enabled", True) if getattr(row, "runtime_prev_enabled", None) is not None else True),
                    "runtime_next_enabled": bool(getattr(row, "runtime_next_enabled", True) if getattr(row, "runtime_next_enabled", None) is not None else True),
                    "runtime_auto_enabled": bool(getattr(row, "runtime_auto_enabled", True) if getattr(row, "runtime_auto_enabled", None) is not None else True),
                    "runtime_pin_enabled": bool(getattr(row, "runtime_pin_enabled", True) if getattr(row, "runtime_pin_enabled", None) is not None else True),
                    "fixed_widget_key": str(getattr(row, "fixed_widget_key", None) or ""),
                }
        except Exception:
            logger.exception("widgets: failed to load groups config")
        try:
            rows = await WidgetThemeOverride.objects.execute(
                WidgetThemeOverride.select()
            )
            for row in rows:
                tk = str(row.theme_key or "").strip()
                tok = str(row.token or "").strip()
                if not tk or not tok:
                    continue
                self._theme_overrides.setdefault(tk, {})[tok] = str(row.value or "")
        except Exception:
            logger.exception("widgets: failed to load theme overrides")
        # Hydrate global frame settings from theme overrides under '__frame__'.
        frame = self._theme_overrides.get("__frame__", {})
        self.strip_color_override = str(frame.get("strip_color", "") or "")
        self.bg_color_override = str(frame.get("bg_color", "") or "")
        try:
            self.strip_thickness = float(frame.get("strip_thickness", "1.0") or 1.0)
        except (TypeError, ValueError):
            self.strip_thickness = 1.0
        self._loaded = True
        logger.info(
            "widgets: storage loaded — %d global, %d players, %d behaviour, %d groups",
            len(self._global), len(self._players), len(self._behavior), len(self._groups),
        )

    async def _existing_columns(self, table: str) -> set[str]:
        """Best-effort table column discovery.

        If discovery fails (permissions / engine differences), return an empty
        set so callers can fall back to a safe minimal field list.
        """
        db = getattr(self.instance, "db", None)
        if db is None or not hasattr(db, "objects"):
            return set()
        try:
            # Raw queries must go through the peewee_async manager; the
            # PyPlanet ``Database`` wrapper has no ``execute`` of its own.
            # Model._meta.database is a peewee Proxy at import time, which
            # peewee_async refuses to swap. Pin the manager's real db on
            # the query so it executes against the live connection.
            raw = WidgetConfigGlobal.raw("SHOW COLUMNS FROM `{}`".format(table))
            raw.database = db.objects.database
            rows = await db.objects.execute(raw)
        except Exception:
            logger.warning(
                "widgets: failed to inspect columns for %s", table, exc_info=True,
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

    @staticmethod
    def _select_existing_fields(model_cls, existing: set[str], names: tuple[str, ...]):
        """Return peewee model fields filtered by discovered DB columns.

        Always keep the minimal identity/position fields if present in the
        model so old pools still load even when schema inspection returns empty.
        """
        fields = []
        # If discovery failed (existing == empty), NEVER select the full model;
        # keep to a minimal schema-safe subset to avoid unknown-column crashes.
        minimal_when_unknown = {"widget_key", "login", "x", "y", "w", "h"}
        for name in names:
            if existing:
                if name not in existing:
                    continue
            else:
                if name not in minimal_when_unknown:
                    continue
            try:
                fields.append(getattr(model_cls, name))
            except AttributeError:
                continue
        if not fields:
            for name in ("widget_key", "login", "x", "y", "w", "h"):
                try:
                    fields.append(getattr(model_cls, name))
                except AttributeError:
                    continue
        return fields

    async def _ensure_schema(self) -> None:
        """Add any columns that newer code expects but pre-existing
        installs may be missing. Each ALTER is best-effort: duplicate
        column errors are swallowed so this is idempotent."""
        adds = (
            ("tmsm_widget_config_global", "strip_prefer_top", "TINYINT(1) NULL"),
            ("tmsm_widget_config_global", "widget_disabled", "TINYINT(1) NULL"),
        )
        db = getattr(self.instance, "db", None)
        if db is None or not hasattr(db, "objects"):
            return
        # Discover existing columns once; skip ALTERs that would duplicate.
        existing = await self._existing_columns("tmsm_widget_config_global")
        for table, col, ddl in adds:
            if existing and col in existing:
                continue
            sql = "ALTER TABLE `{0}` ADD COLUMN `{1}` {2}".format(table, col, ddl)
            try:
                raw = WidgetConfigGlobal.raw(sql)
                raw.database = db.objects.database
                try:
                    await db.objects.execute(raw)
                except StopAsyncIteration:
                    pass
                except Exception as exc:
                    msg = str(exc).lower()
                    if "duplicate column" in msg or "exists" in msg:
                        continue
                    raise
            except Exception as exc:
                logger.warning("widgets: ensure_schema %s.%s failed: %s", table, col, exc)

    async def seed_defaults(self, path: Path) -> None:
        """Seed the global table from ``path`` if (and only if) it's empty."""
        try:
            rows = await WidgetConfigGlobal.objects.execute(
                WidgetConfigGlobal.select(WidgetConfigGlobal.widget_key).limit(1)
            )
            if list(rows):
                return
        except Exception:
            logger.exception("widgets: seed precheck failed; skipping")
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.info("widgets: no defaults.json at %s; skipping seed", path)
            return
        except (OSError, ValueError):
            logger.exception("widgets: defaults.json unreadable; skipping seed")
            return
        positions = (raw.get("positions") or {}) if isinstance(raw, dict) else {}
        behavior = (raw.get("behavior") or {}) if isinstance(raw, dict) else {}
        if not isinstance(positions, dict):
            return
        if not isinstance(behavior, dict):
            behavior = {}
        now = datetime.datetime.utcnow()
        inserted = 0
        all_keys = set(positions.keys()) | set(behavior.keys())
        for key in all_keys:
            pos = positions.get(key) if isinstance(positions.get(key), dict) else None
            beh = behavior.get(key) if isinstance(behavior.get(key), dict) else None
            kwargs: dict[str, Any] = {"widget_key": key, "updated_at": now}
            if pos is not None:
                clean = _clean_pos(pos)
                if not all(k in clean for k in _POS_FIELDS):
                    logger.warning(
                        "widgets: defaults.json entry '%s' missing x/y/w/h; skipped",
                        key,
                    )
                    continue
                kwargs["x"] = clean["x"]
                kwargs["y"] = clean["y"]
                kwargs["w"] = clean["w"]
                kwargs["h"] = clean["h"]
            else:
                # Behaviour-only row (e.g. widget_disabled flag with no
                # explicit position override). Use zeroed position so the
                # NOT NULL columns are satisfied; resolve() falls back to
                # code defaults when no position is cached.
                kwargs["x"] = 0.0
                kwargs["y"] = 0.0
                kwargs["w"] = 0.0
                kwargs["h"] = 0.0
            if beh is not None:
                modes = beh.get("state_modes")
                if isinstance(modes, list):
                    mode_set = {str(m) for m in modes}
                    if "all" in mode_set:
                        mode_set = {"all"}
                    for mode, col in _STATE_FIELDS:
                        kwargs[col] = mode in mode_set
                for fld in (
                    "hide_while_driving",
                    "drive_mode",
                    "group_key",
                    "group_member_enabled",
                    "group_priority",
                    "group_order",
                    "anim_dir",
                    "anim_duration_ms",
                    "anim_delay_ms",
                    "allow_personal",
                    "strip_prefer_top",
                    "widget_disabled",
                ):
                    if fld in beh:
                        kwargs[fld] = beh[fld]
            try:
                await WidgetConfigGlobal.objects.create(
                    WidgetConfigGlobal, **kwargs,
                )
                inserted += 1
            except Exception:
                logger.exception("widgets: seed insert failed for '%s'", key)
        if inserted:
            logger.info("widgets: seeded %d global default(s) from %s",
                        inserted, path)

    async def write_defaults(self, path: Path) -> int:
        """Write a JSON snapshot of current global widget config to ``path``.

        Returns the number of position rows written.
        """
        positions: dict[str, dict[str, float]] = {}
        for key in sorted(self._global.keys()):
            pos = self._global.get(key) or {}
            positions[key] = {
                "x": float(pos.get("x", 0.0) or 0.0),
                "y": float(pos.get("y", 0.0) or 0.0),
                "w": float(pos.get("w", 0.0) or 0.0),
                "h": float(pos.get("h", 0.0) or 0.0),
            }

        behavior: dict[str, dict[str, Any]] = {}
        for key in sorted(self._behavior.keys()):
            src = self._behavior.get(key) or {}
            row: dict[str, Any] = {}
            for fld in _BEH_FIELDS:
                if fld not in src:
                    continue
                val = src.get(fld)
                if fld == "state_modes":
                    row[fld] = [str(m) for m in (val or [])]
                else:
                    row[fld] = val
            if row:
                behavior[key] = row

        active = self.active_theme(default="")
        themes: dict[str, Any] = {}
        if active:
            themes["active"] = active

        frame = self._theme_overrides.get("__frame__", {})
        frame_overrides: dict[str, str] = {
            "strip_color": str(frame.get("strip_color", "") or ""),
            "strip_thickness": str(frame.get("strip_thickness", "") or ""),
            "bg_color": str(frame.get("bg_color", "") or ""),
        }

        payload: dict[str, Any] = {
            "_comment": (
                "Snapshot of current widget DB config, written by the widgets "
                "editor. On first boot (empty table), seed_defaults consumes "
                "positions only. Other sections are preserved for backup/future use."
            ),
            "positions": positions,
            "behavior": behavior,
            "themes": themes,
            "frame": frame_overrides,
        }
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        return len(positions)

    # ---- accessors (sync — caches only) --------------------------------

    def global_pos(self, key: str) -> dict[str, float]:
        return dict(self._global.get(key, {}))

    def player_pos(self, key: str, login: str) -> dict[str, float]:
        return dict(self._players.get(login, {}).get(key, {}))

    def resolve(self, key: str, login: str, defaults: dict[str, float]) -> dict[str, float]:
        """Merge code defaults < global override < per-player override."""
        out = dict(defaults)
        out.update(self.global_pos(key))
        out.update(self.player_pos(key, login))
        return out

    def resolve_behavior(self, key: str, defaults: dict[str, Any],
                         login: str | None = None) -> dict[str, Any]:
        out = dict(defaults)
        out.update(self._behavior.get(key, {}))
        if login and bool(out.get("allow_personal", True)):
            out.update(self._player_beh.get(login, {}).get(key, {}))
        return out

    def player_behavior(self, key: str, login: str) -> dict[str, Any]:
        return dict(self._player_beh.get(login, {}).get(key, {}))

    def list_groups(self) -> list[dict[str, Any]]:
        out = [dict(v) for v in self._groups.values()]
        out.sort(key=lambda g: (int(g.get("order", 0) or 0), str(g.get("key", ""))))
        return out

    def group_by_key(self, key: str) -> dict[str, Any] | None:
        g = self._groups.get(str(key or "").strip())
        if g is None:
            return None
        return dict(g)

    def get_ui_offset(self, login: str) -> dict[str, float]:
        row = self._players.get(login, {}).get(_UI_CAL_KEY, {})
        return {
            "x": float(row.get("x", 0.0) or 0.0),
            "y": float(row.get("y", 0.0) or 0.0),
            "stretch": float(row.get("w", 0.0) or 0.0),
        }

    async def set_ui_offset(self, login: str, x: float, y: float) -> None:
        cur = self._players.get(login, {}).get(_UI_CAL_KEY, {})
        pos = {
            "x": float(x),
            "y": float(y),
            "w": float(cur.get("w", 0.0) or 0.0),
            "h": 0.0,
        }
        self._players.setdefault(login, {})[_UI_CAL_KEY] = dict(pos)
        now = datetime.datetime.utcnow()
        try:
            try:
                row = await WidgetConfigPersonal.objects.get(
                    WidgetConfigPersonal,
                    (WidgetConfigPersonal.widget_key == _UI_CAL_KEY)
                    & (WidgetConfigPersonal.login == login),
                )
                row.x = pos["x"]
                row.y = pos["y"]
                row.w = pos["w"]
                row.h = pos["h"]
                row.updated_at = now
                await WidgetConfigPersonal.objects.update(row)
            except WidgetConfigPersonal.DoesNotExist:
                await WidgetConfigPersonal.objects.create(
                    WidgetConfigPersonal,
                    widget_key=_UI_CAL_KEY,
                    login=login,
                    x=pos["x"],
                    y=pos["y"],
                    w=pos["w"],
                    h=pos["h"],
                    updated_at=now,
                )
        except Exception:
            logger.exception("widgets: failed to persist ui calibration for '%s'", login)

    async def set_ui_stretch(self, login: str, stretch: float) -> None:
        cur = self._players.get(login, {}).get(_UI_CAL_KEY, {})
        pos = {
            "x": float(cur.get("x", 0.0) or 0.0),
            "y": float(cur.get("y", 0.0) or 0.0),
            "w": float(stretch),
            "h": 0.0,
        }
        self._players.setdefault(login, {})[_UI_CAL_KEY] = dict(pos)
        now = datetime.datetime.utcnow()
        try:
            try:
                row = await WidgetConfigPersonal.objects.get(
                    WidgetConfigPersonal,
                    (WidgetConfigPersonal.widget_key == _UI_CAL_KEY)
                    & (WidgetConfigPersonal.login == login),
                )
                row.x = pos["x"]
                row.y = pos["y"]
                row.w = pos["w"]
                row.h = pos["h"]
                row.updated_at = now
                await WidgetConfigPersonal.objects.update(row)
            except WidgetConfigPersonal.DoesNotExist:
                await WidgetConfigPersonal.objects.create(
                    WidgetConfigPersonal,
                    widget_key=_UI_CAL_KEY,
                    login=login,
                    x=pos["x"],
                    y=pos["y"],
                    w=pos["w"],
                    h=pos["h"],
                    updated_at=now,
                )
        except Exception:
            logger.exception("widgets: failed to persist ui stretch for '%s'", login)

    async def clear_ui_offset(self, login: str) -> None:
        if login in self._players:
            self._players[login].pop(_UI_CAL_KEY, None)
            if not self._players[login]:
                self._players.pop(login)
        try:
            await WidgetConfigPersonal.objects.execute(
                WidgetConfigPersonal.delete().where(
                    (WidgetConfigPersonal.widget_key == _UI_CAL_KEY)
                    & (WidgetConfigPersonal.login == login)
                )
            )
        except Exception:
            logger.exception("widgets: failed to clear ui calibration for '%s'", login)

    # ---- mutations (async write-through) -------------------------------

    async def _get_or_create_global(self, key: str, now: datetime.datetime):
        """Fetch the global row for ``key`` or create a stub with seeded
        position from the cache (so behaviour-only writes don't require
        a pre-existing position). Returns the (possibly new) row."""
        try:
            return await WidgetConfigGlobal.objects.get(
                WidgetConfigGlobal,
                WidgetConfigGlobal.widget_key == key,
            )
        except WidgetConfigGlobal.DoesNotExist:
            cached = self._global.get(key, {})
            return await WidgetConfigGlobal.objects.create(
                WidgetConfigGlobal,
                widget_key=key,
                x=float(cached.get("x", 0.0)),
                y=float(cached.get("y", 0.0)),
                w=float(cached.get("w", 0.0)),
                h=float(cached.get("h", 0.0)),
                updated_at=now,
            )

    async def set_global(self, key: str, pos: dict[str, float]) -> None:
        clean = _clean_pos(pos)
        if not clean:
            return
        cur = self._global.setdefault(key, {})
        cur.update(clean)
        if not all(k in cur for k in _POS_FIELDS):
            logger.debug("widgets: skipping global write for '%s' — partial", key)
            return
        now = datetime.datetime.utcnow()
        try:
            row = await self._get_or_create_global(key, now)
            row.x, row.y, row.w, row.h = cur["x"], cur["y"], cur["w"], cur["h"]
            row.updated_at = now
            await WidgetConfigGlobal.objects.update(row)
        except Exception:
            logger.exception("widgets: failed to persist global pos for '%s'", key)

    async def set_player(self, key: str, login: str, pos: dict[str, float]) -> None:
        clean = _clean_pos(pos)
        if not clean:
            return
        player_map = self._players.setdefault(login, {})
        cur = player_map.setdefault(key, {})
        # Personal rows always store all 4 floats — seed any missing
        # field from the resolved fallback so the row is complete.
        if not all(k in cur for k in _POS_FIELDS):
            base = dict(self._global.get(key, {}))
            for k in _POS_FIELDS:
                cur.setdefault(k, base.get(k, 0.0))
        cur.update(clean)
        now = datetime.datetime.utcnow()
        try:
            try:
                row = await WidgetConfigPersonal.objects.get(
                    WidgetConfigPersonal,
                    (WidgetConfigPersonal.widget_key == key)
                    & (WidgetConfigPersonal.login == login),
                )
                row.x, row.y, row.w, row.h = cur["x"], cur["y"], cur["w"], cur["h"]
                row.updated_at = now
                await WidgetConfigPersonal.objects.update(row)
            except WidgetConfigPersonal.DoesNotExist:
                pbeh = self._player_beh.get(login, {}).get(key, {})
                await WidgetConfigPersonal.objects.create(
                    WidgetConfigPersonal,
                    widget_key=key, login=login,
                    x=cur["x"], y=cur["y"], w=cur["w"], h=cur["h"],
                    anim_dir=pbeh.get("anim_dir"),
                    anim_duration_ms=pbeh.get("anim_duration_ms"),
                    anim_delay_ms=pbeh.get("anim_delay_ms"),
                    updated_at=now,
                )
        except Exception:
            logger.exception("widgets: failed to persist player pos for '%s'/%s",
                             key, login)

    async def _get_or_create_personal(self, key: str, login: str,
                                       now: datetime.datetime):
        """Fetch the personal row or create a stub seeded from the resolved
        global position so personal-behaviour writes don't require an
        existing position override."""
        try:
            return await WidgetConfigPersonal.objects.get(
                WidgetConfigPersonal,
                (WidgetConfigPersonal.widget_key == key)
                & (WidgetConfigPersonal.login == login),
            )
        except WidgetConfigPersonal.DoesNotExist:
            base = dict(self._global.get(key, {}))
            return await WidgetConfigPersonal.objects.create(
                WidgetConfigPersonal,
                widget_key=key, login=login,
                x=float(base.get("x", 0.0)),
                y=float(base.get("y", 0.0)),
                w=float(base.get("w", 0.0)),
                h=float(base.get("h", 0.0)),
                updated_at=now,
            )

    async def set_player_behavior(self, key: str, login: str,
                                   patch: dict[str, Any]) -> None:
        if not patch:
            return
        cur = self._player_beh.setdefault(login, {}).setdefault(key, {})
        for k, v in patch.items():
            if k in _PERS_BEH_FIELDS:
                cur[k] = v
        now = datetime.datetime.utcnow()
        try:
            row = await self._get_or_create_personal(key, login, now)
            if "anim_dir" in cur:
                row.anim_dir = str(cur["anim_dir"])
            if "anim_duration_ms" in cur:
                row.anim_duration_ms = int(cur["anim_duration_ms"])
            if "anim_delay_ms" in cur:
                row.anim_delay_ms = int(cur["anim_delay_ms"])
            row.updated_at = now
            await WidgetConfigPersonal.objects.update(row)
        except Exception:
            logger.exception(
                "widgets: failed to persist player behavior for '%s'/%s",
                key, login,
            )

    async def clear_player_behavior(self, key: str, login: str) -> None:
        """Null the per-player animation columns. Position stays put."""
        if login in self._player_beh:
            self._player_beh[login].pop(key, None)
            if not self._player_beh[login]:
                self._player_beh.pop(login)
        try:
            try:
                row = await WidgetConfigPersonal.objects.get(
                    WidgetConfigPersonal,
                    (WidgetConfigPersonal.widget_key == key)
                    & (WidgetConfigPersonal.login == login),
                )
            except WidgetConfigPersonal.DoesNotExist:
                return
            row.anim_dir = None
            row.anim_duration_ms = None
            row.anim_delay_ms = None
            row.updated_at = datetime.datetime.utcnow()
            await WidgetConfigPersonal.objects.update(row)
        except Exception:
            logger.exception(
                "widgets: failed to clear player behavior for '%s'/%s",
                key, login,
            )

    async def clear_global(self, key: str) -> None:
        """Clear the global position back to code defaults. Behaviour
        columns are preserved (a NULL position is not representable
        because x/y/w/h are NOT NULL), so we delete the entire row only
        if no behaviour values are set; otherwise we keep the row and
        reset position fields to 0 — but practically callers use this
        only as ``reset to code default``. Keep behaviour intact."""
        beh = self._behavior.get(key)
        self._global.pop(key, None)
        if not beh:
            try:
                await WidgetConfigGlobal.objects.execute(
                    WidgetConfigGlobal.delete().where(
                        WidgetConfigGlobal.widget_key == key
                    )
                )
            except Exception:
                logger.exception("widgets: failed to delete global row for '%s'", key)
            return
        # Behaviour present — strip position columns by deleting and
        # re-creating with zeros + preserved behaviour.
        now = datetime.datetime.utcnow()
        try:
            await WidgetConfigGlobal.objects.execute(
                WidgetConfigGlobal.delete().where(
                    WidgetConfigGlobal.widget_key == key
                )
            )
            await WidgetConfigGlobal.objects.create(
                WidgetConfigGlobal,
                widget_key=key,
                x=0.0, y=0.0, w=0.0, h=0.0,
                hide_while_driving=beh.get("hide_while_driving"),
                drive_mode=beh.get("drive_mode"),
                state_all=beh.get("state_modes") == ["all"],
                state_loading_map=("loading_map" in (beh.get("state_modes") or [])),
                state_warmup=("warmup" in (beh.get("state_modes") or [])),
                state_pre_race=("pre_race" in (beh.get("state_modes") or [])),
                state_in_race=("in_race" in (beh.get("state_modes") or [])),
                state_in_podium=("in_podium" in (beh.get("state_modes") or [])),
                state_post_race=("post_race" in (beh.get("state_modes") or [])),
                group_key=beh.get("group_key"),
                group_member_enabled=beh.get("group_member_enabled"),
                group_priority=beh.get("group_priority"),
                group_order=beh.get("group_order"),
                anim_dir=beh.get("anim_dir"),
                anim_duration_ms=beh.get("anim_duration_ms"),
                anim_delay_ms=beh.get("anim_delay_ms"),
                allow_personal=beh.get("allow_personal"),
                widget_disabled=beh.get("widget_disabled"),
                updated_at=now,
            )
        except Exception:
            logger.exception("widgets: failed to reset global row for '%s'", key)

    async def clear_player(self, key: str, login: str) -> None:
        if login in self._players:
            self._players[login].pop(key, None)
            if not self._players[login]:
                self._players.pop(login)
        if login in self._player_beh:
            self._player_beh[login].pop(key, None)
            if not self._player_beh[login]:
                self._player_beh.pop(login)
        try:
            await WidgetConfigPersonal.objects.execute(
                WidgetConfigPersonal.delete().where(
                    (WidgetConfigPersonal.widget_key == key)
                    & (WidgetConfigPersonal.login == login)
                )
            )
        except Exception:
            logger.exception(
                "widgets: failed to delete player pos for '%s'/%s", key, login,
            )

    # ---- group settings (stored in dedicated groups table) ------------

    async def set_group(self, group_key: str, patch: dict[str, Any]) -> None:
        key = str(group_key or "").strip()
        if not key:
            return
        cur = self._groups.setdefault(key, {
            "key": key,
            "label": key,
            "description": "",
            "order": 0,
            "anchor_x": 0.0,
            "anchor_y": 0.0,
            "anchor_w": 18.0,
            "anchor_h": 8.0,
            "mode": "priority_active",
            "max_visible": 1,
            "runtime_prev_enabled": True,
            "runtime_next_enabled": True,
            "runtime_auto_enabled": True,
            "runtime_pin_enabled": True,
            "fixed_widget_key": "",
        })
        if "label" in patch:
            cur["label"] = str(patch.get("label") or key)
        if "description" in patch:
            cur["description"] = str(patch.get("description") or "")
        if "order" in patch:
            try:
                cur["order"] = int(patch.get("order") or 0)
            except (TypeError, ValueError):
                cur["order"] = 0
        for fld in ("anchor_x", "anchor_y", "anchor_w", "anchor_h"):
            if fld in patch:
                try:
                    cur[fld] = float(patch.get(fld) or 0.0)
                except (TypeError, ValueError):
                    pass
        if "mode" in patch:
            cur["mode"] = str(patch.get("mode") or "priority_active")
        if "max_visible" in patch:
            try:
                cur["max_visible"] = max(1, int(patch.get("max_visible") or 1))
            except (TypeError, ValueError):
                cur["max_visible"] = 1
        for fld in (
            "runtime_prev_enabled",
            "runtime_next_enabled",
            "runtime_auto_enabled",
            "runtime_pin_enabled",
        ):
            if fld in patch:
                cur[fld] = bool(patch.get(fld))
        if "fixed_widget_key" in patch:
            cur["fixed_widget_key"] = str(patch.get("fixed_widget_key") or "")
        now = datetime.datetime.utcnow()
        try:
            try:
                row = await WidgetGroupConfig.objects.get(
                    WidgetGroupConfig,
                    WidgetGroupConfig.group_key == key,
                )
                row.label = str(cur.get("label") or key)
                row.description = str(cur.get("description") or "")
                row.order = int(cur.get("order") or 0)
                row.anchor_x = float(cur.get("anchor_x") or 0.0)
                row.anchor_y = float(cur.get("anchor_y") or 0.0)
                row.anchor_w = float(cur.get("anchor_w") or 18.0)
                row.anchor_h = float(cur.get("anchor_h") or 8.0)
                row.mode = str(cur.get("mode") or "priority_active")
                row.max_visible = int(cur.get("max_visible") or 1)
                row.runtime_prev_enabled = bool(cur.get("runtime_prev_enabled", True))
                row.runtime_next_enabled = bool(cur.get("runtime_next_enabled", True))
                row.runtime_auto_enabled = bool(cur.get("runtime_auto_enabled", True))
                row.runtime_pin_enabled = bool(cur.get("runtime_pin_enabled", True))
                row.fixed_widget_key = str(cur.get("fixed_widget_key") or "")
                row.updated_at = now
                await WidgetGroupConfig.objects.update(row)
            except WidgetGroupConfig.DoesNotExist:
                await WidgetGroupConfig.objects.create(
                    WidgetGroupConfig,
                    group_key=key,
                    label=str(cur.get("label") or key),
                    description=str(cur.get("description") or ""),
                    order=int(cur.get("order") or 0),
                    anchor_x=float(cur.get("anchor_x") or 0.0),
                    anchor_y=float(cur.get("anchor_y") or 0.0),
                    anchor_w=float(cur.get("anchor_w") or 18.0),
                    anchor_h=float(cur.get("anchor_h") or 8.0),
                    mode=str(cur.get("mode") or "priority_active"),
                    max_visible=int(cur.get("max_visible") or 1),
                    runtime_prev_enabled=bool(cur.get("runtime_prev_enabled", True)),
                    runtime_next_enabled=bool(cur.get("runtime_next_enabled", True)),
                    runtime_auto_enabled=bool(cur.get("runtime_auto_enabled", True)),
                    runtime_pin_enabled=bool(cur.get("runtime_pin_enabled", True)),
                    fixed_widget_key=str(cur.get("fixed_widget_key") or ""),
                    updated_at=now,
                )
        except Exception:
            logger.exception("widgets: failed to persist group '%s'", key)

    async def delete_group(self, group_key: str) -> None:
        key = str(group_key or "").strip()
        if not key:
            return
        self._groups.pop(key, None)
        try:
            await WidgetGroupConfig.objects.execute(
                WidgetGroupConfig.delete().where(
                    WidgetGroupConfig.group_key == key
                )
            )
        except Exception:
            logger.exception("widgets: failed to delete group '%s'", key)

    # ---- behavior settings (stored on the global row) -------------------

    async def set_behavior(self, key: str, patch: dict[str, Any]) -> None:
        if not patch:
            return
        if "show_while_driving" in patch and "hide_while_driving" not in patch:
            patch = dict(patch)
            patch["hide_while_driving"] = not bool(patch.pop("show_while_driving"))
        cur = self._behavior.setdefault(key, {})
        for k, v in patch.items():
            if k in _BEH_FIELDS:
                cur[k] = v
        if "strip_prefer_top" in patch:
            v = patch["strip_prefer_top"]
            if v is None:
                self.strip_prefer_top.pop(key, None)
                cur.pop("strip_prefer_top", None)
            else:
                self.strip_prefer_top[key] = bool(v)
        now = datetime.datetime.utcnow()
        try:
            row = await self._get_or_create_global(key, now)
            if "hide_while_driving" in cur:
                row.hide_while_driving = bool(cur["hide_while_driving"])
            if "drive_mode" in cur:
                row.drive_mode = str(cur["drive_mode"])
            if "state_modes" in cur:
                modes = [str(m) for m in (cur.get("state_modes") or [])]
                if "all" in modes:
                    modes = ["all"]
                for mode, col in _STATE_FIELDS:
                    setattr(row, col, mode in modes)
            if "group_key" in cur:
                row.group_key = str(cur["group_key"])
            if "group_member_enabled" in cur:
                row.group_member_enabled = bool(cur["group_member_enabled"])
            if "group_priority" in cur:
                row.group_priority = int(cur["group_priority"])
            if "group_order" in cur:
                row.group_order = int(cur["group_order"])
            if "anim_dir" in cur:
                row.anim_dir = str(cur["anim_dir"])
            if "anim_duration_ms" in cur:
                row.anim_duration_ms = int(cur["anim_duration_ms"])
            if "anim_delay_ms" in cur:
                row.anim_delay_ms = int(cur["anim_delay_ms"])
            if "allow_personal" in cur:
                row.allow_personal = bool(cur["allow_personal"])
            if "strip_prefer_top" in cur:
                v = cur["strip_prefer_top"]
                row.strip_prefer_top = None if v is None else bool(v)
            if "widget_disabled" in cur:
                v = cur["widget_disabled"]
                row.widget_disabled = None if v is None else bool(v)
            row.updated_at = now
            await WidgetConfigGlobal.objects.update(row)
        except Exception:
            logger.exception("widgets: failed to persist behavior for '%s'", key)

    async def clear_behavior(self, key: str) -> None:
        """Reset behaviour columns to NULL on the global row. The row
        itself (and its position) is preserved."""
        self._behavior.pop(key, None)
        try:
            try:
                row = await WidgetConfigGlobal.objects.get(
                    WidgetConfigGlobal,
                    WidgetConfigGlobal.widget_key == key,
                )
            except WidgetConfigGlobal.DoesNotExist:
                return
            row.hide_while_driving = None
            row.drive_mode = None
            row.state_all = None
            row.state_loading_map = None
            row.state_warmup = None
            row.state_pre_race = None
            row.state_in_race = None
            row.state_in_podium = None
            row.state_post_race = None
            row.group_key = None
            row.group_member_enabled = None
            row.group_priority = None
            row.group_order = None
            row.anim_dir = None
            row.anim_duration_ms = None
            row.anim_delay_ms = None
            row.allow_personal = None
            row.strip_prefer_top = None
            row.widget_disabled = None
            row.updated_at = datetime.datetime.utcnow()
            await WidgetConfigGlobal.objects.update(row)
        except Exception:
            logger.exception("widgets: failed to clear behavior for '%s'", key)

    # ---- theme overrides ------------------------------------------------

    _ACTIVE_META = ("__meta__", "active")

    def active_theme(self, default: str = "dark") -> str:
        return self._theme_overrides.get(self._ACTIVE_META[0], {}).get(
            self._ACTIVE_META[1], default
        )

    async def set_active_theme(self, theme_key: str) -> None:
        await self.set_theme_override(self._ACTIVE_META[0], self._ACTIVE_META[1], theme_key)

    def theme_overrides(self, theme_key: str) -> dict[str, str]:
        """Return a copy of the override map for ``theme_key`` (may be empty)."""
        return dict(self._theme_overrides.get(theme_key, {}))

    async def set_theme_override(self, theme_key: str, token: str, value: str) -> None:
        tk = str(theme_key or "").strip()
        tok = str(token or "").strip()
        val = str(value or "").strip()
        if not tk or not tok:
            return
        self._theme_overrides.setdefault(tk, {})[tok] = val
        now = datetime.datetime.utcnow()
        try:
            try:
                row = await WidgetThemeOverride.objects.get(
                    WidgetThemeOverride,
                    (WidgetThemeOverride.theme_key == tk)
                    & (WidgetThemeOverride.token == tok),
                )
                row.value = val
                row.updated_at = now
                await WidgetThemeOverride.objects.update(row)
            except WidgetThemeOverride.DoesNotExist:
                await WidgetThemeOverride.objects.create(
                    WidgetThemeOverride,
                    theme_key=tk, token=tok, value=val, updated_at=now,
                )
        except Exception:
            logger.exception("widgets: failed to persist theme override %s/%s", tk, tok)

    async def clear_theme_override(self, theme_key: str, token: str) -> None:
        tk = str(theme_key or "").strip()
        tok = str(token or "").strip()
        if not tk or not tok:
            return
        cache = self._theme_overrides.get(tk)
        if cache is not None:
            cache.pop(tok, None)
            if not cache:
                self._theme_overrides.pop(tk, None)
        try:
            await WidgetThemeOverride.objects.execute(
                WidgetThemeOverride.delete().where(
                    (WidgetThemeOverride.theme_key == tk)
                    & (WidgetThemeOverride.token == tok)
                )
            )
        except Exception:
            logger.exception("widgets: failed to clear theme override %s/%s", tk, tok)

    async def clear_theme_overrides(self, theme_key: str) -> None:
        tk = str(theme_key or "").strip()
        if not tk:
            return
        self._theme_overrides.pop(tk, None)
        try:
            await WidgetThemeOverride.objects.execute(
                WidgetThemeOverride.delete().where(
                    WidgetThemeOverride.theme_key == tk
                )
            )
        except Exception:
            logger.exception("widgets: failed to clear theme overrides for %s", tk)


def default_defaults_path() -> Path:
    """Path of the bundled ``defaults.json`` shipped with the app."""
    return Path(__file__).resolve().parent / "defaults.json"
