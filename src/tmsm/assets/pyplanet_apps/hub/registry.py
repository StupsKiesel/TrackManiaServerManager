"""Hub app registry — the contract every hub-aware addon registers against.

Other addons announce themselves by sending the `tmsm:hub.register` signal
during or after their own `on_start`. The hub app collects entries and
re-renders its grid; entries are addressed by their `key` so re-registering
upserts.

    from pyplanet.apps.tmsm.hub import HubAppEntry, Role

    await self.context.signals.get_signal("tmsm:hub.register").send_robust({
        "entry": HubAppEntry(
            key="maplist",
            name="Maplist",
            icon="list",
            role=Role.PLAYER,
            description="Browse the server's map rotation",
            open=self.on_hub_open,
        ),
    })

`open(player)` is an async callback the hub invokes when the tile is
clicked. The hub will hide itself first; the callback should display the
app's own window. The app should provide a back-to-hub button by sending
the `tmsm:hub.show` signal, or by including a `ui.push_button` wired to
`hub_back`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Awaitable, Callable, Optional

from pyplanet.apps.core.maniaplanet.models import Player


class Role(IntEnum):
    """Mirrors `Player.LEVEL_*`. Use the highest tier a player needs to see the tile."""

    PLAYER = Player.LEVEL_PLAYER
    OPERATOR = Player.LEVEL_OPERATOR
    ADMIN = Player.LEVEL_ADMIN
    MASTER = Player.LEVEL_MASTER

    @property
    def label(self) -> str:
        return {0: "Player", 1: "Operator", 2: "Admin", 3: "Master"}[int(self)]


OpenCallback = Callable[[object], Awaitable[None]]


@dataclass
class HubAppEntry:
    key: str
    name: str
    icon: str = "cog"
    role: Role = Role.PLAYER
    description: str = ""
    badge: str | None = None
    order: int = 100
    open: Optional[OpenCallback] = field(default=None, repr=False)
    enabled: bool = True
