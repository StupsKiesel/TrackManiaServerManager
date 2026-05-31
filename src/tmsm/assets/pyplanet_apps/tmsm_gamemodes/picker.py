"""Validated TMX map picker for game modes.

Reuses the tmx_browser TMX HTTP client (``tmx_browser.tmx``) so we don't
maintain two copies of the API code. Picker fetches random maps matching
a filter dict, runs every validator against each candidate, and returns
the first row that survives. Installs into the dedicated by writing the
GBX file via the storage driver, then ``map_manager.add_map`` +
``set_next_map``.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Dict, Iterable

import aiohttp

# Reach across into tmx_browser for the API client. tmx_browser is a sibling
# bundled app so importing it directly is fine in our installer layout.
from pyplanet.apps.tmsm.tmx_browser.tmx import (
    download as tmx_download,
    search as tmx_search,
)

logger = logging.getLogger(__name__)


# A validator returns True to accept the row, False to reject it.
Validator = Callable[[Dict[str, Any]], bool]


# ---- canned validators -------------------------------------------------

def reject_difficulty(*names: str) -> Validator:
    """Reject rows whose difficulty matches any of the given names (case-i)."""
    bad = {n.strip().lower() for n in names if n}
    def _v(row: dict[str, Any]) -> bool:
        return (row.get("difficulty") or "").strip().lower() not in bad
    return _v


def reject_tags(*tag_names: str) -> Validator:
    """Reject rows that carry any of the given tag names (case-insensitive)."""
    bad = {n.strip().lower() for n in tag_names if n}
    def _v(row: dict[str, Any]) -> bool:
        tags = {str(t).strip().lower() for t in (row.get("tags") or [])}
        return tags.isdisjoint(bad)
    return _v


def require_thumbnail() -> Validator:
    def _v(row: dict[str, Any]) -> bool:
        return bool(row.get("has_thumbnail"))
    return _v


def downloadable() -> Validator:
    def _v(row: dict[str, Any]) -> bool:
        return bool(row.get("downloadable", True))
    return _v


def min_awards(n: int) -> Validator:
    def _v(row: dict[str, Any]) -> bool:
        return int(row.get("awards") or 0) >= int(n)
    return _v


# ---- filename helper (mirrors tmx_browser._safe_filename) --------------

def _safe_filename(name: str, track_id: int, ext: str) -> str:
    base = re.sub(r"\$[0-9a-fA-F]{3}", "", name or "")
    base = re.sub(r"[^A-Za-z0-9._ \-]+", "_", base).strip()
    base = (base or "map")[:60]
    return f"tmx/{base}_#{int(track_id)}{ext}"


class MapPicker:
    """Picker + installer. Bound to the orchestrator AppConfig."""

    def __init__(self, app) -> None:
        self.app = app

    def _game(self) -> str:
        try:
            return str(self.app.instance.game.game or "tmnext")
        except Exception:
            return "tmnext"

    async def pick_random(self,
                          *,
                          filters: dict[str, Any] | None = None,
                          validators: Iterable[Validator] = (),
                          excluded_tmx_ids: Iterable[int] = (),
                          max_attempts: int = 8) -> dict[str, Any] | None:
        """Pull random TMX maps matching ``filters`` and return the first
        candidate that passes every validator and isn't in the exclusion set.
        Returns ``None`` if no valid candidate was found after
        ``max_attempts`` API calls.
        """
        f = dict(filters or {})
        ex = {int(x) for x in (excluded_tmx_ids or [])}
        vlist = list(validators)
        # Each call returns one random row (count=1 via random=True).
        for _ in range(max(1, int(max_attempts))):
            try:
                data = await tmx_search(self._game(), random=True, **f)
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
                logger.warning("gamemodes: tmx random search failed", exc_info=True)
                continue
            rows = data.get("results") or []
            if not rows:
                continue
            row = rows[0]
            tid = int(row.get("track_id") or 0)
            if tid in ex:
                continue
            if any(not v(row) for v in vlist):
                continue
            return row
        return None

    async def install(self, row: dict[str, Any],
                      juke_next: bool = True) -> dict[str, Any] | None:
        """Download the GBX, write to the dedicated, add to playlist, and
        (optionally) set as the next map. Returns the PyPlanet ``Map`` on
        success, ``None`` on failure (errors are logged).
        """
        tid = int(row.get("track_id") or 0)
        if tid <= 0:
            return None
        try:
            blob = await tmx_download(self._game(), tid)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
            logger.exception("gamemodes: tmx download failed for #%s", tid)
            return None
        if not blob:
            logger.warning("gamemodes: tmx download empty (#%s) - skipping", tid)
            return None
        ext = ".Map.Gbx" if self._game() == "tmnext" else ".Challenge.Gbx"
        filename = _safe_filename(row.get("name", f"tmx_{tid}"), tid, ext)
        storage = self.app.instance.storage
        try:
            tmx_dir = f"{storage.MAP_FOLDER}/tmx"
            if not await storage.driver.exists(tmx_dir):
                await storage.driver.mkdir(tmx_dir)
            async with storage.open_map(filename, "wb+") as fw:
                await fw.write(blob)
        except Exception:
            logger.exception("gamemodes: write map file failed (#%s)", tid)
            return None
        try:
            await self.app.instance.map_manager.add_map(
                filename, insert=False, save_matchsettings=False,
            )
        except Exception:
            logger.exception("gamemodes: add_map failed (#%s)", tid)
            return None
        # `add_map` returns the raw gbx result (truthy/bool), not a Map
        # instance. Refresh the playlist cache, then locate the freshly
        # added map by filename so we can hand a real Map to set_next_map.
        uploaded = None
        try:
            await self.app.instance.map_manager.update_list(full_update=False)
            for m in self.app.instance.map_manager.maps:
                if getattr(m, "file", None) == filename:
                    uploaded = m
                    break
        except Exception:
            logger.exception("gamemodes: update_list failed (#%s)", tid)
        if juke_next and uploaded is not None:
            try:
                await self.app.instance.map_manager.set_next_map(uploaded)
            except Exception:
                logger.exception("gamemodes: set_next_map failed (#%s)", tid)
        return uploaded
