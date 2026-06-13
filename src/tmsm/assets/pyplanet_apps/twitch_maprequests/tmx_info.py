"""Fetch a single TMX map's metadata by track id.

Lives here (not in tmx_browser) so the twitch_maprequests app is
self-contained — it doesn't have to import another addon. Uses the v2
public API; no key required.
"""
from __future__ import annotations

import aiohttp
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Difficulty enum used by TMX v2. Mirrors tmx_browser/tmx.py so we stay
# consistent in chat messages.
DIFFICULTIES: dict[int, str] = {
    0: "Beginner", 1: "Intermediate", 2: "Advanced",
    3: "Expert",   4: "Lunatic",      5: "Impossible",
}
DIFFICULTY_BY_NAME: dict[str, int] = {v.lower(): k for k, v in DIFFICULTIES.items()}


_USER_AGENT = "tmsm-twitch-maprequests/1.0"


async def fetch_info(track_id: int, timeout: float = 8.0) -> Optional[dict[str, Any]]:
    """Return a normalised dict for the TMX map, or ``None`` if missing.

    Only the fields we need for safety-rail checks and chat replies:
    ``track_id, name, uploader, length_ms, difficulty_id,
    difficulty_name, downloadable``.
    """
    url = f"https://trackmania.exchange/api/maps/{int(track_id)}"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        ) as session:
            async with session.get(url) as resp:
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, OSError) as e:
        logger.warning("tmx_info: fetch #%s failed: %s", track_id, e)
        raise

    if not isinstance(data, dict):
        return None

    diff_raw = data.get("Difficulty")
    diff_id = diff_raw if isinstance(diff_raw, int) else -1
    uploader = data.get("Uploader")
    if isinstance(uploader, dict):
        uploader_name = str(uploader.get("Name") or "")
    else:
        uploader_name = str(data.get("Username") or "")
    length_ms = data.get("Length")
    return {
        "track_id":        int(data.get("MapId") or track_id),
        "uid":             str(data.get("MapUid") or ""),
        "name":            str(data.get("Name") or f"#{track_id}"),
        "uploader":        uploader_name,
        "length_ms":       int(length_ms) if isinstance(length_ms, int) else 0,
        "difficulty_id":   diff_id if diff_id >= 0 else None,
        "difficulty_name": DIFFICULTIES.get(diff_id, ""),
        "downloadable":    bool(data.get("IsPublic", True)),
    }
