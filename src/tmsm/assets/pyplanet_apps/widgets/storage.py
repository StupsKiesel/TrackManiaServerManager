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

from .models import WidgetConfigGlobal, WidgetConfigPersonal

logger = logging.getLogger(__name__)


_POS_FIELDS = ("x", "y", "w", "h")
_BEH_FIELDS = (
    "hide_while_driving",
    "anim_dir",
    "anim_duration_ms",
    "anim_delay_ms",
    "allow_personal",
)
# Subset of behaviour fields that may be overridden per-player.
_PERS_BEH_FIELDS = ("anim_dir", "anim_duration_ms", "anim_delay_ms")


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
        self._loaded = False

    # ---- io ------------------------------------------------------------

    async def load(self) -> None:
        """Populate the in-memory caches from the DB."""
        self._global.clear()
        self._players.clear()
        self._behavior.clear()
        self._player_beh.clear()
        try:
            rows = await WidgetConfigGlobal.objects.execute(
                WidgetConfigGlobal.select()
            )
            for row in rows:
                self._global[row.widget_key] = {
                    "x": float(row.x), "y": float(row.y),
                    "w": float(row.w), "h": float(row.h),
                }
                beh: dict[str, Any] = {}
                if row.hide_while_driving is not None:
                    beh["hide_while_driving"] = bool(row.hide_while_driving)
                if row.anim_dir is not None:
                    beh["anim_dir"] = str(row.anim_dir)
                if row.anim_duration_ms is not None:
                    beh["anim_duration_ms"] = int(row.anim_duration_ms)
                if row.anim_delay_ms is not None:
                    beh["anim_delay_ms"] = int(row.anim_delay_ms)
                if row.allow_personal is not None:
                    beh["allow_personal"] = bool(row.allow_personal)
                if beh:
                    self._behavior[row.widget_key] = beh
        except Exception:
            logger.exception("widgets: failed to load global config")
        try:
            rows = await WidgetConfigPersonal.objects.execute(
                WidgetConfigPersonal.select()
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
        self._loaded = True
        logger.info(
            "widgets: storage loaded — %d global, %d players, %d behaviour",
            len(self._global), len(self._players), len(self._behavior),
        )

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
        if not isinstance(positions, dict):
            return
        now = datetime.datetime.utcnow()
        inserted = 0
        for key, pos in positions.items():
            if not isinstance(pos, dict):
                continue
            clean = _clean_pos(pos)
            if not all(k in clean for k in _POS_FIELDS):
                logger.warning(
                    "widgets: defaults.json entry '%s' missing x/y/w/h; skipped",
                    key,
                )
                continue
            try:
                await WidgetConfigGlobal.objects.create(
                    WidgetConfigGlobal,
                    widget_key=key,
                    x=clean["x"], y=clean["y"],
                    w=clean["w"], h=clean["h"],
                    updated_at=now,
                )
                inserted += 1
            except Exception:
                logger.exception("widgets: seed insert failed for '%s'", key)
        if inserted:
            logger.info("widgets: seeded %d global default(s) from %s",
                        inserted, path)

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
                anim_dir=beh.get("anim_dir"),
                anim_duration_ms=beh.get("anim_duration_ms"),
                anim_delay_ms=beh.get("anim_delay_ms"),
                allow_personal=beh.get("allow_personal"),
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

    # ---- behavior settings (stored on the global row) -------------------

    async def set_behavior(self, key: str, patch: dict[str, Any]) -> None:
        if not patch:
            return
        cur = self._behavior.setdefault(key, {})
        for k, v in patch.items():
            if k in _BEH_FIELDS:
                cur[k] = v
        now = datetime.datetime.utcnow()
        try:
            row = await self._get_or_create_global(key, now)
            if "hide_while_driving" in cur:
                row.hide_while_driving = bool(cur["hide_while_driving"])
            if "anim_dir" in cur:
                row.anim_dir = str(cur["anim_dir"])
            if "anim_duration_ms" in cur:
                row.anim_duration_ms = int(cur["anim_duration_ms"])
            if "anim_delay_ms" in cur:
                row.anim_delay_ms = int(cur["anim_delay_ms"])
            if "allow_personal" in cur:
                row.allow_personal = bool(cur["allow_personal"])
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
            row.anim_dir = None
            row.anim_duration_ms = None
            row.anim_delay_ms = None
            row.allow_personal = None
            row.updated_at = datetime.datetime.utcnow()
            await WidgetConfigGlobal.objects.update(row)
        except Exception:
            logger.exception("widgets: failed to clear behavior for '%s'", key)


def default_defaults_path() -> Path:
    """Path of the bundled ``defaults.json`` shipped with the app."""
    return Path(__file__).resolve().parent / "defaults.json"
