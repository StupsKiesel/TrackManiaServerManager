"""Async client for the unified ManiaExchange v2 API (``/api/maps``).

Three sites, identical REST shape:

* TM2020 (Trackmania, ``tmnext``) → ``https://trackmania.exchange``
* Maniaplanet TM2 (``tm``)        → ``https://tm.mania.exchange``
* Maniaplanet SM  (``sm``)        → ``https://sm.mania.exchange``

Docs: https://api2.mania.exchange/Method/Index/53

Pagination is cursor-based: pass ``after=<MapId of last result>`` to fetch the
following page. ``page`` numbers exist only on the deprecated mapsearch2
endpoint and have been retired here.

We expose two operations:

* ``search(query, after, limit)`` → ``{"results": [...], "more": bool,
                                      "last_id": int | None}``
* ``download(track_id)``          → raw ``.Map.Gbx`` / ``.Challenge.Gbx`` bytes

The result dicts are normalized to a small shared shape::

    {
        "track_id":  int,                 # TMX id
        "uid":       str,                 # map UID
        "name":      str,                 # display name (with TMX $-codes preserved)
        "author":    str,                 # mapper username
        "length":    str,                 # e.g. "1:23" or "Long"
        "difficulty":str,                 # e.g. "Beginner" / "Lunatic"
        "awards":    int,                 # award/heart count
        "style":     str,                 # primary style, may be empty
        "uploaded":  str,                 # ISO date (best-effort)
        "filename":  str,                 # original .Map.Gbx filename (best-effort)
    }
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

USER_AGENT = "tmsm/tmx_browser (+https://github.com/StupsKiesel/TrackManiaServerManager)"
TIMEOUT_S = 20

# game key -> (base url, "maps" path component used by the GBX download URL)
_SITES: dict[str, tuple[str, str]] = {
    "tmnext": ("https://trackmania.exchange", "maps"),
    "tm":     ("https://tm.mania.exchange", "tracks"),
    "sm":     ("https://sm.mania.exchange", "tracks"),
}

# Map Search Orders (MX v2 /api/maps order1). Discovered empirically against
# trackmania.exchange — there is no public enum for the TMX site.
ORDER_RECENT  = 0    # UploadedAt desc
ORDER_AWARDED = 12   # AwardCount desc

# Field list requested from the v2 API. Keep this small but cover both the
# list and the detail sub-view.
_FIELDS = (
    "MapId,MapUid,Name,GbxMapName,Uploader.Name,Authors,Environment,Vehicle,"
    "Mood,MapType,TitlePack,Length,Laps,AwardCount,CommentCount,DownloadCount,"
    "ReplayCount,Difficulty,Routes,Tags,HasThumbnail,IsPublic,IsListed,"
    "AuthorComments,UploadedAt,UpdatedAt,Medals.Author"
)

_DIFFICULTIES: dict[int, str] = {
    0: "Beginner", 1: "Intermediate", 2: "Advanced",
    3: "Expert", 4: "Lunatic", 5: "Impossible",
}
_ROUTES: dict[int, str] = {
    0: "Single", 1: "Multi", 2: "Symmetric",
}
_MOODS: dict[int, str] = {
    0: "Day", 1: "Sunrise", 2: "Sunset", 3: "Night",
}
# Environment enum varies per game; the v2 docs only enumerate a few names per
# site. We render the int when the name is unknown.
_ENVIRONMENTS: dict[str, dict[int, str]] = {
    "tmnext": {0: "Custom", 1: "Stadium", 2: "Red Island",
               3: "Green Coast", 4: "Blue Bay", 5: "White Shore"},
    "tm":     {0: "Custom", 1: "Canyon", 2: "Stadium", 3: "Valley",
               4: "Lagoon", 5: "Desert", 6: "Snow", 7: "Rally",
               8: "Coast", 9: "Bay", 10: "Island"},
    "sm":     {0: "Custom", 1: "Storm"},
}
_VEHICLES: dict[str, dict[int, str]] = {
    "tmnext": {0: "Custom", 1: "CarSport"},
    "tm":     {0: "Custom"},
    "sm":     {0: "Custom"},
}


def site_for(game: str) -> tuple[str, str]:
    """Return ``(base_url, kind)`` for the given pyplanet ``game`` value.

    Falls back to TM2020 for unknown values so the UI never blows up.
    """
    return _SITES.get(game, _SITES["tmnext"])


def _fmt_length(ms: int) -> str:
    if ms <= 0:
        return ""
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


# Best-known TMX (TM2020) tag id -> short label. Used to render the comma-
# separated `Tags` string from mapsearch2 as readable chips. Unknown ids fall
# back to "#<id>".
TMNEXT_TAGS: dict[int, str] = {
    1: "Race",        2: "FullSpeed",  3: "LOL",          4: "Tech",
    5: "SpeedTech",   6: "RPG",        7: "Trial",        8: "Grass",
    9: "Stunt",      10: "Maze",      11: "Offroad",     12: "Laps",
    13: "Fragile",   14: "King",      15: "Platform",    16: "Slow Motion",
    17: "Bumper",    18: "MiniRPG",   19: "Obstacle",    20: "Transitional",
    21: "Backwards", 22: "EngineOff", 23: "NoSteer",     24: "NoBrakes",
    25: "Cruise",    26: "NoGear",    27: "NoGrip",      28: "Scenery",
    29: "Kacky",     30: "Endurance", 31: "Mini",        32: "Remake",
    33: "Mixed",     34: "Nascar",    35: "SpeedDrift",  36: "Minigame",
    37: "Obstacle",  38: "Transitional", 39: "FreeWheel", 40: "Signature",
    41: "Royal",     42: "Water",     43: "Plastic",     44: "Arena",
    45: "Freestyle", 46: "Educational", 47: "Sausage",   48: "Bobsleigh",
    49: "Ice",       50: "MultiLap",  51: "Fall",        52: "MiniRPG",
    53: "Hard",      54: "Dirt",      55: "Tag",         56: "Reactor",
    57: "Pathfinding", 58: "Flagrush",59: "PressForward",60: "MagnetCar",
    61: "Bobsleigh", 62: "Wood",      63: "Underwater",  64: "Turtle",
    65: "FlowDrift", 66: "Sub-Saharan", 67: "MainStream", 68: "Bonus",
}


def _tag_names(raw: Any) -> list[str]:
    """Render the v2 ``Tags`` array (or the legacy comma-string) as names."""
    out: list[str] = []
    if isinstance(raw, list):
        for t in raw:
            if isinstance(t, dict):
                name = str(t.get("Name") or "").strip()
                if name:
                    out.append(name)
                    continue
                tid = t.get("TagId")
                if isinstance(tid, int):
                    out.append(TMNEXT_TAGS.get(tid, f"#{tid}"))
        return out
    # Legacy: comma-separated tag ids.
    for chunk in (str(raw or "")).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            tid = int(chunk)
        except ValueError:
            out.append(chunk)
            continue
        out.append(TMNEXT_TAGS.get(tid, f"#{tid}"))
    return out


def thumbnail_url(game: str, track_id: int) -> str:
    """Public thumbnail URL (raw JPG) for the given map on the matching MX site.

    The v2 API exposes ``/api/maps/thumbnail/{id}`` on every site.
    """
    base, _ = site_for(game)
    return f"{base}/api/maps/thumbnail/{int(track_id)}"


def _norm(item: dict[str, Any], game: str = "tmnext") -> dict[str, Any]:
    """Coerce a raw v2 ``/api/maps`` item into our shared shape.

    Includes detail-view fields so the sub-window doesn't need a 2nd HTTP call.
    Also tolerates the legacy ``mapsearch2`` payload, for any old call site.
    """
    get = item.get

    tid = int(get("MapId") or get("TrackID") or get("MapID") or 0)

    # Uploader.Name in v2 is nested.
    uploader = get("Uploader")
    if isinstance(uploader, dict):
        author = str(uploader.get("Name") or "")
    else:
        author = str(get("Username") or get("AuthorLogin") or "")

    # Length is int milliseconds in v2; mapsearch2 used "LengthName" ("1 min").
    length_raw = get("Length")
    if isinstance(length_raw, int):
        length = _fmt_length(length_raw)
    else:
        length = str(get("LengthName") or length_raw or "")

    # Difficulty is enum int (v2) or already a name (legacy).
    diff_raw = get("Difficulty")
    if isinstance(diff_raw, int):
        difficulty = _DIFFICULTIES.get(diff_raw, str(diff_raw))
    else:
        difficulty = str(get("DifficultyName") or diff_raw or "")

    env_raw = get("Environment")
    if isinstance(env_raw, int):
        environment = _ENVIRONMENTS.get(game, {}).get(env_raw, str(env_raw))
    else:
        environment = str(get("EnvironmentName") or env_raw or "")

    veh_raw = get("Vehicle")
    if isinstance(veh_raw, int):
        vehicle = _VEHICLES.get(game, {}).get(veh_raw, str(veh_raw))
    else:
        vehicle = str(get("VehicleName") or veh_raw or "")

    mood_raw = get("Mood")
    if isinstance(mood_raw, int):
        mood = _MOODS.get(mood_raw, str(mood_raw))
    else:
        mood = str(get("MoodFull") or mood_raw or "")

    routes_raw = get("Routes")
    if isinstance(routes_raw, int):
        route = _ROUTES.get(routes_raw, str(routes_raw))
    else:
        route = str(get("RouteName") or routes_raw or "")

    return {
        # core (used by the list view)
        "track_id": tid,
        "uid":      str(get("MapUid") or get("MapUID") or get("TrackUID") or ""),
        "name":     str(get("Name") or "(unnamed)"),
        "author":   author,
        "length":   length,
        "difficulty": difficulty,
        "awards":   int(get("AwardCount") or 0),
        "style":    "",  # primary tag — fold in from tags[0] if needed
        "uploaded": str(get("UploadedAt") or get("UpdatedAt") or ""),
        "filename": str(get("GbxMapName") or get("Filename") or ""),
        # extra (used by the details sub-window)
        "map_type":      str(get("MapType") or get("TypeName") or ""),
        "title_pack":    str(get("TitlePack") or get("Titlepack") or ""),
        "environment":   environment,
        "vehicle":       vehicle,
        "mood":          mood,
        "route":         route,
        "tags":          _tag_names(get("Tags")),
        "comment_count": int(get("CommentCount") or 0),
        "replay_count":  int(get("ReplayCount") or 0),
        "track_value":   int(get("TrackValue") or 0),
        "display_cost":  int(get("DisplayCost") or 0),
        "laps":          int(get("Laps") or 0),
        "has_thumbnail": bool(get("HasThumbnail")),
        "downloadable":  bool(get("IsPublic", get("Downloadable", True))),
        "author_time":   int((get("Medals") or {}).get("Author") or get("AuthorTime") or 0),
        "comments":      str(get("AuthorComments") or get("Comments") or ""),
    }


# Collection ("in*") flag keys accepted by the v2 API. Single-select on UI.
COLLECTIONS: dict[str, str] = {
    "beta":          "inbeta",
    "featured":      "infeatured",
    "supporter":     "insupporter",
    "collaborative": "incollaborative",
    "totd":          "intotd",
}


async def search(
    game: str,
    query: str = "",
    after: int | None = None,
    limit: int = 12,
    order: int | None = None,
    random: bool = False,
    *,
    author: str = "",
    environment: int | None = None,
    vehicle: int | None = None,
    maptype: str = "",
    mood: int | None = None,
    difficulty: int | None = None,
    routes: int | None = None,
    tags: list[int] | None = None,
    length_min_ms: int | None = None,
    length_max_ms: int | None = None,
    order2: int | None = None,
    collection: str | None = None,
) -> dict[str, Any]:
    """Search/list maps via the v2 ``/api/maps`` endpoint.

    All filter kwargs are optional; only set parameters are forwarded to the
    API. ``collection`` is the short key (see ``COLLECTIONS``) that maps to
    one of the ``in*`` boolean flags.

    Returns ``{"results": [...], "more": bool, "last_id": int | None}``.
    """
    base, _ = site_for(game)
    count = 1 if random else max(1, min(100, int(limit)))
    params: dict[str, str] = {
        "fields": _FIELDS,
        "count":  str(count),
    }
    q = (query or "").strip()
    if q:
        params["name"] = q
    if order is not None:
        params["order1"] = str(int(order))
    if order2 is not None:
        params["order2"] = str(int(order2))
    if random:
        params["random"] = "1"
    elif after is not None and int(after) > 0:
        params["after"] = str(int(after))

    if author.strip():
        params["author"] = author.strip()
    if environment is not None:
        params["environment"] = str(int(environment))
    if vehicle is not None:
        params["vehicle"] = str(int(vehicle))
    if maptype.strip():
        params["maptype"] = maptype.strip()
    if mood is not None:
        params["mood"] = str(int(mood))
    if difficulty is not None:
        params["difficulty"] = str(int(difficulty))
    if routes is not None:
        params["routes"] = str(int(routes))
    if tags:
        params["tag"] = ",".join(str(int(t)) for t in tags)
    if length_min_ms is not None and length_min_ms > 0:
        params["lengthop"] = "1"  # >=
        params["length"] = str(int(length_min_ms))
    if length_max_ms is not None and length_max_ms > 0:
        # If both are set, only one length comparator is supported by the API;
        # prefer the upper bound (more selective). Min is then applied client-
        # side by the caller if needed.
        params["lengthop"] = "2"  # <=
        params["length"] = str(int(length_max_ms))
    if collection:
        flag = COLLECTIONS.get(collection)
        if flag:
            params[flag] = "1"

    url = f"{base}/api/maps"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)

    items: list[dict[str, Any]] = []
    more = False
    if isinstance(data, dict):
        raw = data.get("Results") or []
        more = bool(data.get("More"))
        items = [_norm(it, game=game) for it in raw if isinstance(it, dict)]

    # Client-side trim when both min and max were requested (API only honors
    # one side; we picked max above so filter the lower bound here).
    if (length_min_ms is not None and length_min_ms > 0
            and length_max_ms is not None and length_max_ms > 0):
        def _ms(row: dict[str, Any]) -> int:
            ln = row.get("length") or ""
            if isinstance(ln, str) and ":" in ln:
                m, s = ln.split(":", 1)
                try:
                    return (int(m) * 60 + int(s)) * 1000
                except ValueError:
                    return 0
            return 0
        items = [it for it in items if _ms(it) >= int(length_min_ms)]

    last_id = items[-1]["track_id"] if items else None
    return {"results": items, "more": more, "last_id": last_id}


async def tags(game: str) -> list[dict[str, Any]]:
    """Fetch the global TMX tag dictionary.

    Returns a list of ``{"id": int, "name": str, "color": str}``. Empty list
    on failure (network/HTTP).
    """
    base, _ = site_for(game)
    url = f"{base}/api/tags/gettags"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, OSError) as e:
        logger.warning("tmx.tags: fetch failed: %s", e)
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for t in data:
        if not isinstance(t, dict):
            continue
        try:
            tid = int(t.get("ID") or 0)
        except (TypeError, ValueError):
            continue
        if tid <= 0:
            continue
        out.append({
            "id":    tid,
            "name":  str(t.get("Name") or f"#{tid}"),
            "color": str(t.get("Color") or ""),
        })
    return out


async def download(game: str, track_id: int) -> Optional[bytes]:
    """Download the raw ``.Map.Gbx`` / ``.Challenge.Gbx`` bytes for ``track_id``.

    Uses the documented v2 endpoint ``/mapgbx/{id}``. Returns ``None`` on 404
    (map removed). All other HTTP errors propagate.
    """
    base, _ = site_for(game)
    url = f"{base}/mapgbx/{int(track_id)}"
    headers = {"User-Agent": USER_AGENT}
    timeout = aiohttp.ClientTimeout(total=60)

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            return await resp.read()


# ---- TMX description (BBCode-ish) parsing & flow layout -----------------

# Strip simple inline formatting tags we don't render.
_STRIP_TAGS_RE = re.compile(r"\[/?(?:b|i|u|s|center|left|right|quote|code|hr)\]",
                            re.IGNORECASE)
# Tags we parse into link segments. Order matters (longest first).
_URL_RE  = re.compile(r"\[url=([^\]]+)\](.*?)\[/url\]", re.IGNORECASE | re.DOTALL)
_URL_BARE_RE = re.compile(r"\[url\](.*?)\[/url\]", re.IGNORECASE | re.DOTALL)
_USER_RE = re.compile(r"\[user\](\d+)\[/user\]", re.IGNORECASE)
_MAP_RE  = re.compile(r"\[map\](\d+)\[/map\]",  re.IGNORECASE)


def _bbcode_segments(text: str) -> list[dict[str, str]]:
    """Tokenize a TMX description into a flat list of segments.

    Each segment is ``{"kind": "text", "text": str}`` or
    ``{"kind": "link", "text": str, "url": str}``. Plain newlines are kept
    verbatim inside text segments; the flow layout splits on them.
    """
    if not text:
        return []
    s = _STRIP_TAGS_RE.sub("", text)

    # Build a list of (start, end, segment) matches we want to consume.
    matches: list[tuple[int, int, dict[str, str]]] = []

    for m in _URL_RE.finditer(s):
        label = (m.group(2) or "").strip() or m.group(1).strip()
        matches.append((m.start(), m.end(),
                        {"kind": "link", "text": label, "url": m.group(1).strip()}))
    for m in _URL_BARE_RE.finditer(s):
        url = m.group(1).strip()
        matches.append((m.start(), m.end(),
                        {"kind": "link", "text": url, "url": url}))
    for m in _USER_RE.finditer(s):
        uid = m.group(1)
        matches.append((m.start(), m.end(), {
            "kind": "link",
            "text": f"User #{uid}",
            "url":  f"https://trackmania.exchange/usershow/{uid}",
        }))
    for m in _MAP_RE.finditer(s):
        mid = m.group(1)
        matches.append((m.start(), m.end(), {
            "kind": "link",
            "text": f"Map #{mid}",
            "url":  f"https://trackmania.exchange/maps/{mid}",
        }))

    matches.sort(key=lambda t: (t[0], -t[1]))
    # Remove overlapping matches (keep earliest, longest).
    pruned: list[tuple[int, int, dict[str, str]]] = []
    end = -1
    for start, stop, seg in matches:
        if start < end:
            continue
        pruned.append((start, stop, seg))
        end = stop

    out: list[dict[str, str]] = []
    cursor = 0
    for start, stop, seg in pruned:
        if start > cursor:
            out.append({"kind": "text", "text": s[cursor:start]})
        # Drop empty link labels entirely (avoids the orphan "" link before
        # a [user] tag, like in the demo description).
        if seg["kind"] == "link" and not seg.get("text"):
            cursor = stop
            continue
        out.append(seg)
        cursor = stop
    if cursor < len(s):
        out.append({"kind": "text", "text": s[cursor:]})
    return out


# Rough glyph width in manialink units for the 'sm' label size (font 0.9).
_CHAR_W_SM = 1.45
_LINE_H    = 4.5


def _seg_width(text: str) -> float:
    return max(0.0, len(text)) * _CHAR_W_SM


def flow_description(text: str, max_width: float = 228.0,
                     max_lines: int = 8) -> list[list[dict[str, Any]]]:
    """Lay out a TMX description into positioned lines.

    Returns a list of lines. Each line is a list of placed segments::

        {"kind": "text"|"link", "text": str, "url": str|None,
         "x": float, "w": float}

    The caller renders each segment at ``(line_x + x, base_y - i * line_h)``.
    Plain text is word-wrapped; links are kept atomic and pushed to the next
    line if they don't fit on the current one.
    """
    segments = _bbcode_segments(text or "")
    lines: list[list[dict[str, Any]]] = [[]]
    x = 0.0

    def push(seg: dict[str, Any]) -> None:
        nonlocal x
        w = _seg_width(seg["text"])
        seg = dict(seg)
        seg["x"] = x
        seg["w"] = w
        lines[-1].append(seg)
        x += w

    def newline() -> bool:
        nonlocal x
        if len(lines) >= max_lines:
            return False
        lines.append([])
        x = 0.0
        return True

    for seg in segments:
        if seg["kind"] == "text":
            # Preserve explicit newlines, then word-wrap each paragraph.
            parts = seg["text"].split("\n")
            for pi, part in enumerate(parts):
                if pi > 0 and not newline():
                    return lines
                # word wrap
                words = re.split(r"(\s+)", part)
                buf = ""
                for w in words:
                    if not w:
                        continue
                    cand = buf + w
                    if _seg_width(cand) + x <= max_width or not buf:
                        buf = cand
                        continue
                    # flush current buffer, then newline
                    push({"kind": "text", "text": buf.rstrip(), "url": None})
                    if not newline():
                        return lines
                    buf = w.lstrip()
                if buf:
                    push({"kind": "text", "text": buf, "url": None})
        else:
            w = _seg_width(seg["text"])
            if x > 0 and x + w > max_width:
                if not newline():
                    return lines
            # Truncate links that are wider than the whole line.
            if w > max_width:
                max_chars = max(4, int(max_width / _CHAR_W_SM) - 1)
                seg = dict(seg)
                seg["text"] = seg["text"][:max_chars] + "…"
            push(seg)

    # Trim trailing empty lines.
    while lines and not lines[-1]:
        lines.pop()
    return lines
