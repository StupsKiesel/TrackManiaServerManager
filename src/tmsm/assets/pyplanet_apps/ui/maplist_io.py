"""Shared match-settings (maplist) I/O for tmsm PyPlanet apps.

Single source of truth for *which* matchsettings file the dedicated server
boots with, and for persisting the live playlist into it.

The startup maplist is stored in the instance's ``instance.toml`` under the
``matchsettings`` key (relative to the dedicated's ``UserData/Maps/`` folder,
e.g. ``MatchSettings/example.txt``). The host TUI reads the same key when it
assembles the boot command line, so a change made here takes effect on the
next start/restart.

PyPlanet runs in a separate runtime (WSL) and cannot import the host ``tmsm``
package, so this module locates the instance root from the live server via the
``GameDataDirectory`` GBX call (``<root>/server/UserData`` → ``<root>``) and
reads/writes ``instance.toml`` with a tiny flat-key parser/updater.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_MATCHSETTINGS = "MatchSettings/example.txt"

_TOML_KV = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$")


async def instance_root(instance) -> Optional[Path]:
    """Resolve the tmsm instance root (the dir holding ``instance.toml``).

    ``GameDataDirectory`` returns ``<root>/server/UserData``; the instance
    root is therefore two levels up. Returns ``None`` if the path can't be
    determined or doesn't look like a tmsm instance.
    """
    try:
        raw = await instance.gbx("GameDataDirectory")
    except Exception:
        logger.exception("maplist_io: GameDataDirectory failed")
        return None
    if not raw:
        return None
    gd = Path(str(raw))
    root = gd.parent.parent
    if (root / "instance.toml").is_file():
        return root
    return None


def _read_matchsettings_key(toml_path: Path) -> Optional[str]:
    """Read the ``matchsettings`` scalar from a flat instance.toml."""
    try:
        text = toml_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("["):
            continue
        m = _TOML_KV.match(line)
        if not m or m.group(1) != "matchsettings":
            continue
        val = m.group(2).strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        return val.strip() or None
    return None


def _set_matchsettings_key(toml_path: Path, rel: str) -> bool:
    """Insert/replace the ``matchsettings`` scalar in a flat instance.toml.

    Operates line-by-line so we don't need a TOML writer in the PyPlanet
    runtime. Returns True on success.
    """
    try:
        text = toml_path.read_text(encoding="utf-8")
    except OSError:
        logger.exception("maplist_io: read instance.toml failed")
        return False
    new_line = f'matchsettings = "{rel}"'
    out: list[str] = []
    replaced = False
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0].strip()
        m = _TOML_KV.match(stripped)
        if m and m.group(1) == "matchsettings":
            out.append(new_line)
            replaced = True
        else:
            out.append(raw)
    if not replaced:
        out.append(new_line)
    payload = "\n".join(out)
    if text.endswith("\n"):
        payload += "\n"
    try:
        toml_path.write_text(payload, encoding="utf-8")
    except OSError:
        logger.exception("maplist_io: write instance.toml failed")
        return False
    return True


async def active_matchsettings_rel(instance) -> str:
    """Return the matchsettings path the server boots with (relative to
    ``UserData/Maps/``), read from ``instance.toml``. Falls back to
    :data:`DEFAULT_MATCHSETTINGS`."""
    root = await instance_root(instance)
    if root is not None:
        rel = _read_matchsettings_key(root / "instance.toml")
        if rel:
            return rel
    return DEFAULT_MATCHSETTINGS


async def set_active_matchsettings_rel(instance, rel: str) -> bool:
    """Persist ``rel`` as the startup matchsettings in ``instance.toml``."""
    rel = (rel or "").strip()
    if not rel:
        return False
    root = await instance_root(instance)
    if root is None:
        logger.warning("maplist_io: instance root not found; cannot persist matchsettings")
        return False
    return _set_matchsettings_key(root / "instance.toml", rel)


async def write_active_matchsettings(instance) -> str:
    """Save the live playlist into the active startup matchsettings file and
    refresh PyPlanet's map list. Returns the relative path written.

    This is the correct equivalent of ``//wml``: it writes to the exact file
    the dedicated server boots with (no spurious ``MatchSettings/`` prefix).
    """
    rel = await active_matchsettings_rel(instance)
    await instance.map_manager.save_matchsettings(rel)
    try:
        await instance.map_manager.update_list(full_update=True)
    except Exception:
        logger.exception("maplist_io: update_list after save failed")
    logger.info("maplist_io: wrote matchsettings to %s", rel)
    return rel
