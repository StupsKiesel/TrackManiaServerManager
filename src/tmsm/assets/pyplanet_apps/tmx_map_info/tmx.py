"""Tiny async client for the trackmania.exchange (TM2020) maps API."""
from __future__ import annotations

from typing import Any, Optional

import aiohttp

TMX_API_BASE = "https://trackmania.exchange/api/maps/get_map_info/uid"
USER_AGENT = "tmsm/tmx_map_info (+https://github.com/StupsKiesel/TrackManiaServerManager)"
TIMEOUT_S = 15


async def fetch_by_uid(uid: str) -> Optional[dict[str, Any]]:
    """Return the TMX map dict for `uid`, or None if the map isn't on TMX.

    Raises aiohttp errors on network/HTTP failure — caller decides whether to
    cache the absence or just retry next time.
    """
    url = f"{TMX_API_BASE}/{uid}"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            data = await resp.json(content_type=None)

    # Endpoint returns the map dict directly. Guard against odd shapes / "not found" payloads.
    if isinstance(data, dict):
        if not data or "TrackID" not in data and "MapID" not in data and "Id" not in data:
            return None
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None
