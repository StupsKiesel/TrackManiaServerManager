"""DB-backed persistence for widget positions, with in-memory caches.

Two tables (see :mod:`.models`):

* ``tmsm_widget_position_global``  — admin-set defaults for all players
* ``tmsm_widget_position_player``  — per-player overrides (win over global)

PyPlanet runs peewee on top of ``peewee_async``, so every query must go
through the manager at ``instance.db.objects`` (``execute``/``get``/
``create``/``update``). The hot read path (``resolve_position()``) is
synchronous — it only touches the in-memory caches that ``load()``
populates at app start. Mutations are async (cache + DB upsert/delete).

On first start (global table empty), :meth:`seed_defaults` populates the
global table from ``defaults.json`` shipped alongside this module.
"""
from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any

from .models import WidgetPositionGlobal, WidgetPositionPlayer

logger = logging.getLogger(__name__)


# Keys we persist; anything else in a pos dict is dropped silently.
_FIELDS = ("x", "y", "w", "h")


def _clean(pos: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for k in _FIELDS:
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
        self._loaded = False

    # ---- io ------------------------------------------------------------

    async def load(self) -> None:
        """Populate the in-memory caches from the DB."""
        self._global.clear()
        self._players.clear()
        try:
            rows = await WidgetPositionGlobal.objects.execute(
                WidgetPositionGlobal.select()
            )
            for row in rows:
                self._global[row.widget_key] = {
                    "x": float(row.x), "y": float(row.y),
                    "w": float(row.w), "h": float(row.h),
                }
        except Exception:
            logger.exception("widgets: failed to load global positions")
        try:
            rows = await WidgetPositionPlayer.objects.execute(
                WidgetPositionPlayer.select()
            )
            for row in rows:
                self._players.setdefault(row.login, {})[row.widget_key] = {
                    "x": float(row.x), "y": float(row.y),
                    "w": float(row.w), "h": float(row.h),
                }
        except Exception:
            logger.exception("widgets: failed to load per-player positions")
        self._loaded = True
        logger.info(
            "widgets: storage loaded — %d global, %d players",
            len(self._global), len(self._players),
        )

    async def seed_defaults(self, path: Path) -> None:
        """Seed the global table from ``path`` if (and only if) it's empty."""
        try:
            rows = await WidgetPositionGlobal.objects.execute(
                WidgetPositionGlobal.select(WidgetPositionGlobal.widget_key).limit(1)
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
            clean = _clean(pos)
            if not all(k in clean for k in _FIELDS):
                logger.warning(
                    "widgets: defaults.json entry '%s' missing x/y/w/h; skipped",
                    key,
                )
                continue
            try:
                await WidgetPositionGlobal.objects.create(
                    WidgetPositionGlobal,
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

    # ---- mutations (async write-through) -------------------------------

    async def set_global(self, key: str, pos: dict[str, float]) -> None:
        clean = _clean(pos)
        if not clean:
            return
        cur = self._global.setdefault(key, {})
        cur.update(clean)
        if not all(k in cur for k in _FIELDS):
            logger.debug("widgets: skipping global write for '%s' — partial", key)
            return
        now = datetime.datetime.utcnow()
        try:
            try:
                row = await WidgetPositionGlobal.objects.get(
                    WidgetPositionGlobal,
                    WidgetPositionGlobal.widget_key == key,
                )
                row.x, row.y, row.w, row.h = cur["x"], cur["y"], cur["w"], cur["h"]
                row.updated_at = now
                await WidgetPositionGlobal.objects.update(row)
            except WidgetPositionGlobal.DoesNotExist:
                await WidgetPositionGlobal.objects.create(
                    WidgetPositionGlobal,
                    widget_key=key,
                    x=cur["x"], y=cur["y"], w=cur["w"], h=cur["h"],
                    updated_at=now,
                )
        except Exception:
            logger.exception("widgets: failed to persist global pos for '%s'", key)

    async def set_player(self, key: str, login: str, pos: dict[str, float]) -> None:
        clean = _clean(pos)
        if not clean:
            return
        player_map = self._players.setdefault(login, {})
        cur = player_map.setdefault(key, {})
        # Personal rows always store all 4 floats — seed any missing
        # field from the resolved fallback so the row is complete.
        if not all(k in cur for k in _FIELDS):
            base = dict(self._global.get(key, {}))
            for k in _FIELDS:
                cur.setdefault(k, base.get(k, 0.0))
        cur.update(clean)
        now = datetime.datetime.utcnow()
        try:
            try:
                row = await WidgetPositionPlayer.objects.get(
                    WidgetPositionPlayer,
                    (WidgetPositionPlayer.widget_key == key)
                    & (WidgetPositionPlayer.login == login),
                )
                row.x, row.y, row.w, row.h = cur["x"], cur["y"], cur["w"], cur["h"]
                row.updated_at = now
                await WidgetPositionPlayer.objects.update(row)
            except WidgetPositionPlayer.DoesNotExist:
                await WidgetPositionPlayer.objects.create(
                    WidgetPositionPlayer,
                    widget_key=key, login=login,
                    x=cur["x"], y=cur["y"], w=cur["w"], h=cur["h"],
                    updated_at=now,
                )
        except Exception:
            logger.exception("widgets: failed to persist player pos for '%s'/%s",
                             key, login)

    async def clear_global(self, key: str) -> None:
        self._global.pop(key, None)
        try:
            await WidgetPositionGlobal.objects.execute(
                WidgetPositionGlobal.delete().where(
                    WidgetPositionGlobal.widget_key == key
                )
            )
        except Exception:
            logger.exception("widgets: failed to delete global pos for '%s'", key)

    async def clear_player(self, key: str, login: str) -> None:
        if login in self._players:
            self._players[login].pop(key, None)
            if not self._players[login]:
                self._players.pop(login)
        try:
            await WidgetPositionPlayer.objects.execute(
                WidgetPositionPlayer.delete().where(
                    (WidgetPositionPlayer.widget_key == key)
                    & (WidgetPositionPlayer.login == login)
                )
            )
        except Exception:
            logger.exception(
                "widgets: failed to delete player pos for '%s'/%s", key, login,
            )


def default_defaults_path() -> Path:
    """Path of the bundled ``defaults.json`` shipped with the app."""
    return Path(__file__).resolve().parent / "defaults.json"
