"""Instance model — everything in the main list is an Instance."""
from __future__ import annotations

from .base import Instance, Kind
from .server import GameServerInstance, GameType
from .pool import PyPlanetPoolInstance
from .service import MariaDBInstance
from .registry import discover_all

__all__ = [
    "Instance",
    "Kind",
    "GameServerInstance",
    "GameType",
    "PyPlanetPoolInstance",
    "MariaDBInstance",
    "discover_all",
]
