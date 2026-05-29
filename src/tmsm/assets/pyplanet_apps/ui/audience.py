"""Audience — declarative visibility rule for a view.

Used as a class attribute on `BaseView` subclasses:

    class AdminPanel(BaseView):
        audience = Audience.master_admins()

    class Scoreboard(BaseView):
        audience = Audience.everyone()

    class RedTeamHUD(BaseView):
        audience = Audience.matching(lambda p: p.flow.team_id == 0)

PyPlanet level constants (highest number = highest privilege):
    LEVEL_PLAYER   = 0
    LEVEL_OPERATOR = 1
    LEVEL_ADMIN    = 2
    LEVEL_MASTER   = 3
"""
from __future__ import annotations

from typing import Callable

from pyplanet.apps.core.maniaplanet.models import Player


class Audience:
    """A visibility rule expressed as a predicate over `Player`.

    Use the named factory methods (preferred) or `matching()` for custom rules.
    `everyone()` is special-cased to a true global display (no per-player filter).
    """

    __slots__ = ("_predicate", "is_global")

    def __init__(self, predicate: Callable[[object], bool], *, is_global: bool = False):
        self._predicate = predicate
        self.is_global = is_global

    def matches(self, player) -> bool:
        try:
            return bool(self._predicate(player))
        except Exception:
            return False

    # ---- factories -----------------------------------------------------

    @classmethod
    def everyone(cls) -> "Audience":
        return cls(lambda p: True, is_global=True)

    @classmethod
    def operators(cls) -> "Audience":
        return cls.minimum_level(Player.LEVEL_OPERATOR)

    @classmethod
    def admins(cls) -> "Audience":
        return cls.minimum_level(Player.LEVEL_ADMIN)

    @classmethod
    def master_admins(cls) -> "Audience":
        return cls.minimum_level(Player.LEVEL_MASTER)

    @classmethod
    def minimum_level(cls, level: int) -> "Audience":
        return cls(lambda p, _lvl=level: getattr(p, "level", 0) >= _lvl)

    @classmethod
    def matching(cls, predicate: Callable[[object], bool]) -> "Audience":
        return cls(predicate)
