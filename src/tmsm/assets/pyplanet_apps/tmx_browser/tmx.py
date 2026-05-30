"""Async client for trackmania.exchange (TM2020) and mania-exchange (TM2 / SM).

Two distinct sites with very similar — but not identical — REST shapes:

* TM2020 (Trackmania, ``tmnext``) → ``https://trackmania.exchange``
* Maniaplanet TM2 (``tm``)        → ``https://tm.mania-exchange.com``
* Maniaplanet SM (``sm``)         → ``https://sm.mania-exchange.com``

We expose two operations:

* ``search(query, page, limit)``  → ``{"results": [Map, ...], "more": bool}``
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

# game key -> (base url, "maps" path component used by both search & download)
_SITES: dict[str, tuple[str, str]] = {
    "tmnext": ("https://trackmania.exchange", "maps"),
    "tm":     ("https://tm.mania-exchange.com", "tracks"),
    "sm":     ("https://sm.mania-exchange.com", "tracks"),
}


def site_for(game: str) -> tuple[str, str]:
    """Return ``(base_url, kind)`` for the given pyplanet ``game`` value.

    Falls back to TM2020 for unknown values so the UI never blows up.
    """
    return _SITES.get(game, _SITES["tmnext"])


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


def _tag_names(raw: str | None) -> list[str]:
    """Split TMX's comma-separated tag id list into readable names."""
    out: list[str] = []
    for chunk in (raw or "").split(","):
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
    """Public thumbnail URL (raw JPG) for the given map on the matching TMX site.

    TM2020 serves the raw image at ``/mapthumb/<id>`` (``/maps/thumbnail/<id>``
    is the HTML viewer page). Mania-exchange sites expose ``/tracks/thumbnail/<id>``
    directly.
    """
    base, kind = site_for(game)
    if kind == "maps":  # trackmania.exchange (TM2020)
        return f"{base}/mapthumb/{int(track_id)}"
    return f"{base}/tracks/thumbnail/{int(track_id)}"


def _norm(item: dict[str, Any]) -> dict[str, Any]:
    """Coerce a raw TMX/MX map dict into our shared shape (see module docstring).

    Includes detail-view fields so the sub-window doesn't need a 2nd HTTP call.
    """
    get = item.get
    tid = int(get("TrackID") or get("MapID") or get("Id") or 0)
    return {
        # core (used by the list view)
        "track_id": tid,
        "uid":      str(get("TrackUID") or get("MapUID") or get("MapUid") or ""),
        "name":     str(get("Name") or "(unnamed)"),
        "author":   str(get("Username") or get("AuthorLogin") or get("GbxAuthorLogin") or ""),
        "length":   str(get("LengthName") or get("Length") or ""),
        "difficulty": str(get("DifficultyName") or get("Difficulty") or ""),
        "awards":   int(get("AwardCount") or get("Awards") or 0),
        "style":    str(get("StyleName") or get("PrimaryType") or ""),
        "uploaded": str(get("UploadedAt") or get("UpdatedAt") or ""),
        "filename": str(get("GbxMapName") or get("Filename") or ""),
        # extra (used by the details sub-window)
        "map_type":      str(get("TypeName") or get("MapType") or ""),
        "title_pack":    str(get("TitlePack") or ""),
        "environment":   str(get("EnvironmentName") or ""),
        "vehicle":       str(get("VehicleName") or ""),
        "mood":          str(get("Mood") or ""),
        "route":         str(get("RouteName") or ""),
        "tags":          _tag_names(get("Tags")),
        "comment_count": int(get("CommentCount") or 0),
        "replay_count":  int(get("ReplayCount") or 0),
        "track_value":   int(get("TrackValue") or get("MapValue") or 0),
        "display_cost":  int(get("DisplayCost") or 0),
        "laps":          int(get("Laps") or 0),
        "has_thumbnail": bool(get("HasThumbnail")),
        "downloadable":  bool(get("Downloadable", True)),
        "author_time":   int(get("AuthorTime") or 0),
        "comments":      str(get("Comments") or ""),
    }


async def search(
    game: str,
    query: str = "",
    page: int = 1,
    limit: int = 12,
    order: int | None = None,
    random: bool = False,
) -> dict[str, Any]:
    """Search/list maps on TMX.

    ``query``  — partial map name (omitted when empty).
    ``order``  — TMX sort code. Stable values (mapsearch2):
                   2 = Uploaded (desc, "Recent")
                   4 = Awards   (desc, "Most awarded")
                 omit for site default.
    ``random`` — ask TMX for a random sample; pairs well with ``page=1``.
    """
    base, kind = site_for(game)
    # TMX endpoint differs by site:
    #   * trackmania.exchange  →  /mapsearch2/search
    #   * *.mania-exchange.com →  /tracksearch2/search
    path = "mapsearch2/search" if kind == "maps" else "tracksearch2/search"
    params: dict[str, str] = {
        "api":    "on",
        "format": "json",
        "limit":  str(max(1, min(50, int(limit)))),
        "page":   str(max(1, int(page))),
    }
    q = (query or "").strip()
    if q:
        params["trackname"] = q
    if order is not None:
        params["order"] = str(int(order))
    if random:
        params["random"] = "1"

    url = f"{base}/{path}"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)

    items: list[dict[str, Any]] = []
    more = False
    if isinstance(data, dict):
        raw = data.get("Results") or data.get("results") or []
        more = bool(data.get("More") or data.get("more"))
        items = [_norm(it) for it in raw if isinstance(it, dict)]
    elif isinstance(data, list):
        items = [_norm(it) for it in data if isinstance(it, dict)]
    return {"results": items, "more": more}


async def download(game: str, track_id: int) -> Optional[bytes]:
    """Download the raw ``.Map.Gbx`` / ``.Challenge.Gbx`` bytes for ``track_id``.

    Returns ``None`` on 404 (map removed). All other HTTP errors propagate.
    """
    base, kind = site_for(game)
    url = f"{base}/{kind}/download/{int(track_id)}"
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
