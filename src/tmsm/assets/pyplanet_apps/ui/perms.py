"""tmsm UI permission helper — single source of truth for "what level am I".

Every tmsm app that gates UI or behavior on PyPlanet permission level reads
through here instead of using ``player.level`` directly. The impersonate app
calls :func:`set_override` to temporarily lower a master's effective level for
UI/permission testing; clearing the override (or the player disconnecting)
restores the real level.

The override is **session-scoped**: it is stored in process memory only and is
cleared on PyPlanet restart and on the player's disconnect. There is no DB
persistence on purpose — leaving an admin stuck in fake-player mode across
restarts would be a footgun.

Public API
==========

::

    from pyplanet.apps.tmsm.ui.perms import (
        effective_level, is_admin, is_master, is_operator, level_label,
        set_override, clear_override, get_override, get_real_level,
        reset_all, subscribe_changed,
    )

``effective_level(player_or_login)`` returns the level a tmsm view should treat
the player as having.  Without an override this equals the real PyPlanet level.

``subscribe_changed(callback)`` registers a coroutine
``async def cb(login: str, new_level: int, real_level: int) -> None`` that is
called whenever an override is applied, cleared, or wiped. Apps use this to
re-render their views for the affected login.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from pyplanet.apps.core.maniaplanet.models import Player

logger = logging.getLogger(__name__)

# login -> override level (0..3). Absent = no override.
_overrides: dict[str, int] = {}

# Coroutines registered via subscribe_changed().
_listeners: list[Callable[[str, int, int], Awaitable[None]]] = []

LEVEL_PLAYER = 0
LEVEL_OPERATOR = 1
LEVEL_ADMIN = 2
LEVEL_MASTER = 3

_LEVEL_LABELS = {0: "player", 1: "operator", 2: "admin", 3: "master"}


def _login_of(player_or_login) -> str:
    if isinstance(player_or_login, str):
        return player_or_login
    return getattr(player_or_login, "login", "") or ""


def _real_level_of(player_or_login) -> int:
    if isinstance(player_or_login, str):
        # Best-effort sync lookup; falls back to 0 if we can't find them.
        try:
            from pyplanet.core.instance import Controller
            inst = Controller.instance  # type: ignore[attr-defined]
            for p in inst.player_manager.online:
                if p.login == player_or_login:
                    return int(getattr(p, "level", 0))
        except Exception:
            pass
        return 0
    return int(getattr(player_or_login, "level", 0))


# ---- public ----------------------------------------------------------------


def effective_level(player_or_login) -> int:
    """Return the level the UI should treat ``player_or_login`` as having.

    Applies the override when set, else returns the real PyPlanet level.
    The override can also *raise* a level (rarely useful) — both directions
    work, capped at LEVEL_MASTER and floored at LEVEL_PLAYER.
    """
    login = _login_of(player_or_login)
    if login and login in _overrides:
        return max(LEVEL_PLAYER, min(LEVEL_MASTER, int(_overrides[login])))
    return _real_level_of(player_or_login)


def get_real_level(player_or_login) -> int:
    """Return the real PyPlanet level, ignoring any override."""
    return _real_level_of(player_or_login)


def get_override(login: str) -> Optional[int]:
    if not login:
        return None
    return _overrides.get(login)


def is_player(player_or_login) -> bool:
    return effective_level(player_or_login) == LEVEL_PLAYER


def is_operator(player_or_login) -> bool:
    return effective_level(player_or_login) >= LEVEL_OPERATOR


def is_admin(player_or_login) -> bool:
    return effective_level(player_or_login) >= LEVEL_ADMIN


def is_master(player_or_login) -> bool:
    return effective_level(player_or_login) >= LEVEL_MASTER


def level_label(player_or_login) -> str:
    return _LEVEL_LABELS[effective_level(player_or_login)]


# ---- mutators --------------------------------------------------------------


async def set_override(login: str, level: Optional[int]) -> None:
    """Set or clear the override for one login. ``level=None`` clears it.

    Notifies every subscriber via :func:`subscribe_changed`.
    """
    if not login:
        return
    real = _real_level_of(login)
    if level is None:
        if login not in _overrides:
            return
        _overrides.pop(login, None)
        await _emit_changed(login, real, real)
        return
    lvl = max(LEVEL_PLAYER, min(LEVEL_MASTER, int(level)))
    prev = _overrides.get(login)
    if prev == lvl:
        return
    _overrides[login] = lvl
    await _emit_changed(login, lvl, real)


async def clear_override(login: str) -> None:
    await set_override(login, None)


async def reset_all() -> None:
    """Drop every override. Used on PyPlanet restart / shutdown."""
    if not _overrides:
        return
    affected = list(_overrides.keys())
    _overrides.clear()
    for login in affected:
        real = _real_level_of(login)
        await _emit_changed(login, real, real)


# ---- pub/sub ---------------------------------------------------------------


def subscribe_changed(callback: Callable[[str, int, int], Awaitable[None]]) -> None:
    """Register ``async def cb(login, new_level, real_level)``.

    Idempotent: re-registering the same callback is a no-op.
    """
    if callback not in _listeners:
        _listeners.append(callback)


def unsubscribe_changed(callback: Callable[[str, int, int], Awaitable[None]]) -> None:
    try:
        _listeners.remove(callback)
    except ValueError:
        pass


async def _emit_changed(login: str, new_level: int, real_level: int) -> None:
    for cb in list(_listeners):
        try:
            await cb(login, new_level, real_level)
        except Exception:
            logger.exception("perms: subscriber %r raised", cb)
