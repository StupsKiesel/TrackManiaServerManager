"""TMX map-info bundled addon: fetch on map start, cache by UID, show widget."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import aiohttp
from peewee import DoesNotExist

from pyplanet.apps.config import AppConfig
from pyplanet.contrib.command import Command

from .models import TmxMapCache
from .tmx import fetch_by_uid
from .view import MapInfoWidget

logger = logging.getLogger(__name__)

CHAT_PREFIX = "$ff0[tmx]$z"


class TmxMapInfoApp(AppConfig):
    name = "pyplanet.apps.tmsm.tmx_map_info"
    app_dependencies = ["core.maniaplanet"]
    game_dependencies = ["trackmania", "trackmania_next"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.widget: MapInfoWidget | None = None
        self._hidden: set[str] = set()
        self._lock = asyncio.Lock()

    # ---- lifecycle -----------------------------------------------------

    async def on_start(self) -> None:
        # PyPlanet auto-discovers models from `models.py` during apps.discover()
        # and runs migrations from `db.initiate()` — no manual registration needed.
        await self._broadcast("$fffaddon starting...")

        # Register chat command FIRST so /mapinfo works even if other init fails.
        try:
            await self.instance.command_manager.register(
                Command(command="mapinfo", aliases=["mi"], target=self._cmd_mapinfo,
                        description="Show/hide/refresh the TMX map-info widget.")
                    .add_param("action", required=False, type=str,
                               help="show | hide | refresh (default: show)"),
            )
            logger.info("tmx_map_info: /mapinfo command registered")
            await self._broadcast("$0f0/mapinfo command registered")
        except Exception as e:
            logger.exception("tmx_map_info: command registration failed")
            await self._broadcast(f"$f00command registration FAILED: {e}")

        # Signals next — also independent of widget.
        try:
            self.context.signals.listen("maniaplanet:map_begin", self._on_map_begin)
            self.context.signals.listen("maniaplanet:player_connect", self._on_player_connect)
            await self._broadcast("$0f0signals subscribed")
        except Exception as e:
            logger.exception("tmx_map_info: signal subscription failed")
            await self._broadcast(f"$f00signal subscription FAILED: {e}")

        # Widget creation last; isolated so failures don't take down the command.
        try:
            self.widget = MapInfoWidget(self)
            self.widget.set_data({})
            await self.widget.display()
            logger.info("tmx_map_info: widget created and displayed")
            await self._broadcast("$0f0widget displayed")
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logger.exception("tmx_map_info: widget init/display failed")
            await self._broadcast(f"$f00widget init FAILED: {e}")
            for line in tb.splitlines()[-3:]:
                await self._broadcast(f"$f00{line}")
            self.widget = None

        try:
            await self._refresh_for_current(force=False)
        except Exception as e:
            logger.exception("tmx_map_info: initial refresh failed")
            await self._broadcast(f"$f00initial refresh FAILED: {e}")

    async def on_stop(self) -> None:
        if self.widget is not None:
            try:
                await self.widget.destroy()
            except Exception:
                logger.exception("tmx_map_info: widget destroy failed")
            self.widget = None

    # ---- signals -------------------------------------------------------

    async def _on_map_begin(self, *args, **kwargs):
        try:
            await self._refresh_for_current(force=False)
        except Exception:
            logger.exception("tmx_map_info: map_begin refresh failed")

    async def _on_player_connect(self, player, **kwargs):
        if self.widget is None or player.login in self._hidden:
            return
        try:
            await self.widget.display(player_logins=[player.login])
        except Exception:
            logger.exception("tmx_map_info: per-player display failed")

    # ---- chat command --------------------------------------------------

    async def _cmd_mapinfo(self, player, data, **kwargs):
        action = (getattr(data, "action", None) or "show").lower()
        if action == "hide":
            self._hidden.add(player.login)
            if self.widget is not None:
                await self.widget.hide(player_logins=[player.login])
            await self._say(player, "Map-info hidden. /mapinfo show to bring it back.")
        elif action == "refresh":
            self._hidden.discard(player.login)
            await self._refresh_for_current(force=True)
            await self._say(player, "Re-fetched TMX data.")
        else:
            self._hidden.discard(player.login)
            await self._refresh_for_current(force=False)
            if self.widget is not None:
                await self.widget.display(player_logins=[player.login])

    async def _say(self, player, msg: str) -> None:
        try:
            await self.instance.chat(f"{CHAT_PREFIX} $fff{msg}", player)
        except Exception:
            logger.exception("tmx_map_info: chat send failed")

    async def _broadcast(self, msg: str) -> None:
        """Send a message to all players (and log it)."""
        logger.info("tmx_map_info chat: %s", msg)
        try:
            await self.instance.chat(f"{CHAT_PREFIX} {msg}")
        except Exception:
            logger.exception("tmx_map_info: broadcast failed")

    # ---- core fetch/cache ---------------------------------------------

    async def _refresh_for_current(self, *, force: bool) -> None:
        if self.widget is None:
            await self._broadcast("$fa0refresh skipped: widget not initialized")
            return
        current = self.instance.map_manager.current_map
        uid = None
        if current is not None:
            uid = getattr(current, "uid", None) or getattr(current, "map_uid", None)
        logger.info("tmx_map_info: refresh uid=%r force=%s", uid, force)
        await self._broadcast(f"$fffrefresh uid={uid} force={force}")

        if not uid:
            self.widget.set_data({})
            await self.widget.display()
            return

        async with self._lock:
            data = await self._lookup(uid, force=force)

        if data.get("not_on_tmx"):
            await self._broadcast("$fa0map not found on TMX")
        elif data:
            await self._broadcast(f"$0f0showing: {data.get('name') or '?'} (tmx #{data.get('tmx_id')})")
        self.widget.set_data(data)
        await self.widget.display()

    async def _lookup(self, uid: str, *, force: bool) -> dict[str, Any]:
        cached = None
        if not force:
            try:
                cached = await TmxMapCache.get(TmxMapCache.map_uid == uid)
            except DoesNotExist:
                cached = None
            except Exception:
                logger.exception("tmx_map_info: cache read failed")
                cached = None
            if cached is not None:
                logger.info("tmx_map_info: cache hit for %s", uid)
                return _row_to_view(cached)

        try:
            entry = await fetch_by_uid(uid)
            logger.info("tmx_map_info: TMX response keys=%s", list(entry.keys()) if entry else None)
        except (aiohttp.ClientError, OSError, ValueError, asyncio.TimeoutError) as e:
            logger.warning("tmx_map_info: TMX lookup failed for %s: %s", uid, e)
            return _row_to_view(cached) if cached is not None else {}

        try:
            row = await _upsert(uid, entry)
        except Exception:
            logger.exception("tmx_map_info: cache write failed for %s", uid)
            return _entry_to_view(entry) if entry else {"not_on_tmx": True}

        return _row_to_view(row)


# ---- helpers ----------------------------------------------------------

def _ms_to_pretty(ms: int | None) -> str | None:
    if not ms or ms <= 0:
        return None
    total = ms // 1000
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"


def _primary_style(entry: dict) -> str | None:
    for key in ("StyleName", "Style"):
        v = entry.get(key)
        if v:
            return str(v)
    tags = entry.get("Tags")
    if isinstance(tags, list) and tags:
        first = tags[0]
        if isinstance(first, dict):
            return first.get("Name") or first.get("name")
        return str(first)
    if isinstance(tags, str) and tags:
        return tags.split(",")[0].strip() or None
    return None


def _tags_string(entry: dict) -> str | None:
    tags = entry.get("Tags")
    if isinstance(tags, list):
        names = []
        for t in tags:
            if isinstance(t, dict):
                n = t.get("Name") or t.get("name")
                if n:
                    names.append(str(n))
            elif t:
                names.append(str(t))
        return ", ".join(names) or None
    if isinstance(tags, str):
        return tags or None
    return None


def _fields_from_entry(entry: dict) -> dict:
    length_ms = entry.get("AuthorTime") or entry.get("authorTime")
    return dict(
        tmx_id=entry.get("TrackID") or entry.get("MapID") or entry.get("Id"),
        name=entry.get("Name") or entry.get("GbxMapName"),
        author=entry.get("Username") or entry.get("AuthorLogin"),
        difficulty=entry.get("DifficultyName") or entry.get("Difficulty"),
        length_name=entry.get("LengthName") or entry.get("Length"),
        length_ms=int(length_ms) if isinstance(length_ms, (int, float)) else None,
        style=_primary_style(entry),
        tags=_tags_string(entry),
        mood=entry.get("Mood"),
        mod_name=entry.get("ModName") or (entry.get("Mod") and "custom") or None,
        mod_url=entry.get("Mod") if isinstance(entry.get("Mod"), str) else None,
        not_on_tmx=0,
        raw_json=json.dumps(entry, ensure_ascii=False, default=str),
        fetched_at=datetime.utcnow(),
    )


async def _upsert(uid: str, entry: dict | None) -> TmxMapCache:
    """Insert or update the cache row for `uid`. None caches the negative result."""
    if entry is None:
        fields = {"not_on_tmx": 1, "fetched_at": datetime.utcnow()}
    else:
        fields = _fields_from_entry(entry)

    try:
        row = await TmxMapCache.get(TmxMapCache.map_uid == uid)
    except DoesNotExist:
        row = await TmxMapCache.create(map_uid=uid, **fields)
        logger.info("tmx_map_info: cached new row for %s (tmx_id=%s)", uid, fields.get("tmx_id"))
        return row

    for k, v in fields.items():
        setattr(row, k, v)
    await row.save()
    logger.info("tmx_map_info: updated cache row for %s", uid)
    return row


def _entry_to_view(entry: dict) -> dict[str, Any]:
    f = _fields_from_entry(entry)
    return {
        "tmx_id":      f["tmx_id"],
        "name":        f["name"],
        "author":      f["author"],
        "difficulty":  f["difficulty"],
        "length_name": f["length_name"] or _ms_to_pretty(f["length_ms"]) or "?",
        "style":       f["style"] or f["tags"] or "?",
        "mod_name":    f["mod_name"],
        "mod_url":     f["mod_url"],
        "raw":         entry,
    }


def _row_to_view(row: TmxMapCache) -> dict[str, Any]:
    if row.not_on_tmx:
        return {"not_on_tmx": True}
    raw = None
    if row.raw_json:
        try:
            raw = json.loads(row.raw_json)
        except (TypeError, ValueError):
            raw = None
    return {
        "tmx_id":      row.tmx_id,
        "name":        row.name,
        "author":      row.author,
        "difficulty":  row.difficulty,
        "length_name": row.length_name or _ms_to_pretty(row.length_ms) or "?",
        "style":       row.style or row.tags or "?",
        "mod_name":    row.mod_name,
        "mod_url":     row.mod_url,
        "raw":         raw,  # full TMX response for future widgets / details views
    }
