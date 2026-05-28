"""Download maps from (Trackmania|Mania|Shootmania) Exchange and add to MatchSettings."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

import httpx

from .instances.server import GameServerInstance, GameType

Log = Callable[[str], None]


# Map host per game. Endpoint shape: https://<host>/mapgbx/<id>
_EXCHANGE_HOSTS: dict[GameType, str] = {
    GameType.TM2020:      "trackmania.exchange",
    GameType.MANIAPLANET: "tm.mania.exchange",
    # Shootmania would be sm.mania.exchange — not currently a supported GameType.
}


def exchange_host(game: GameType) -> str:
    try:
        return _EXCHANGE_HOSTS[game]
    except KeyError as e:
        raise RuntimeError(f"No map exchange host known for game type '{game.value}'") from e


def parse_map_id(value: str) -> int:
    """Accept a bare ID ("12345") or any exchange URL containing /maps/<id>."""
    s = value.strip()
    if s.isdigit():
        return int(s)
    m = re.search(r"/maps?/(\d+)", s)
    if m:
        return int(m.group(1))
    raise ValueError(f"Could not parse a map ID from: {value!r}")


def _filename_from_headers(resp: httpx.Response, fallback: str) -> str:
    """Extract filename from Content-Disposition, sanitised; fall back if absent."""
    disp = resp.headers.get("content-disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disp, flags=re.IGNORECASE)
    name = m.group(1).strip() if m else fallback
    # Strip any path separators that might have snuck in.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    # Ensure .Map.Gbx suffix.
    if not name.lower().endswith(".map.gbx"):
        name += ".Map.Gbx"
    # Replace characters that are awkward on disk.
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)
    return name


def download_map_gbx(game: GameType, map_id: int, dest_dir: Path, log: Log) -> Path:
    host = exchange_host(game)
    url = f"https://{host}/mapgbx/{map_id}"
    log(f"GET {url}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as r:
        if r.status_code == 404:
            raise RuntimeError(f"Map {map_id} not found on {host}")
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "html" in ctype.lower():
            raise RuntimeError(
                f"Got HTML instead of a .Gbx from {host} — map {map_id} may "
                f"be unlisted or require authentication."
            )
        name = _filename_from_headers(r, fallback=f"{map_id}")
        out = dest_dir / name
        if out.exists():
            log(f"Already present: {out.name} (overwriting)")
        with out.open("wb") as f:
            for chunk in r.iter_bytes(chunk_size=64 * 1024):
                f.write(chunk)
    log(f"Saved -> {out}")
    return out


def add_map_to_matchsettings(matchsettings: Path, map_file_rel: str, log: Log) -> bool:
    """Append <map><file>...</file></map> to the playlist. Returns True if added.

    Idempotent: if an entry with the same <file> already exists, returns False.
    Uses text manipulation (not ElementTree.write) so the file's existing
    indentation and whitespace are preserved.
    """
    if not matchsettings.is_file():
        raise RuntimeError(f"MatchSettings file not found: {matchsettings}")

    text = matchsettings.read_text(encoding="utf-8")

    # Quick idempotency check via XML parse — robust against attribute order
    # and whitespace differences.
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise RuntimeError(f"Could not parse {matchsettings.name}: {e}") from e
    if root.tag != "playlist":
        raise RuntimeError(
            f"{matchsettings.name} root element is <{root.tag}>, expected <playlist>"
        )
    for existing in root.findall("map/file"):
        if (existing.text or "").strip() == map_file_rel:
            log(f"Map already in {matchsettings.name}: {map_file_rel}")
            return False

    # Infer the indent used for existing <map> entries (fall back to two spaces).
    m = re.search(r"(?m)^([ \t]+)<map\b", text)
    indent = m.group(1) if m else "  "
    new_entry = f"{indent}<map><file>{map_file_rel}</file></map>\n"

    # Insert immediately before the closing </playlist>, preserving any
    # indentation in front of it.
    close_re = re.compile(r"(?m)^([ \t]*)</playlist>\s*\Z")
    m_close = close_re.search(text)
    if m_close is None:
        # Fall back to the last occurrence anywhere in the file.
        idx = text.rfind("</playlist>")
        if idx < 0:
            raise RuntimeError(f"{matchsettings.name} has no </playlist> closing tag")
        new_text = text[:idx] + new_entry + text[idx:]
    else:
        close_indent = m_close.group(1)
        # Make sure there's exactly one newline before the new entry.
        head = text[: m_close.start()]
        if not head.endswith("\n"):
            head += "\n"
        new_text = head + new_entry + f"{close_indent}</playlist>\n"

    matchsettings.write_text(new_text, encoding="utf-8")
    log(f"Added to {matchsettings.name}: <map><file>{map_file_rel}</file></map>")
    return True


def add_map_from_exchange(
    inst: GameServerInstance,
    map_id_input: str,
    matchsettings: Path,
    log: Log,
) -> Path:
    """High-level: parse ID, download .Gbx, add to MatchSettings. Returns map path."""
    map_id = parse_map_id(map_id_input)
    log(f"Map ID: {map_id}  ({inst.meta.game.value})")

    maps_root = inst.server_dir() / "UserData" / "Maps"
    download_dir = maps_root / "Downloaded"
    gbx_path = download_map_gbx(inst.meta.game, map_id, download_dir, log)

    rel = gbx_path.relative_to(maps_root).as_posix()
    add_map_to_matchsettings(matchsettings, rel, log)
    return gbx_path
