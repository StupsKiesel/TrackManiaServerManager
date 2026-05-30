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
from enum import Enum, IntEnum
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


class Status(str, Enum):
    """Lifecycle status — drives the auto-badge in the corner of the tile."""

    OK = "ok"          # nothing extra rendered
    NEW = "new"        # green 'NEW' pill
    BETA = "beta"      # amber 'BETA' pill
    WIP = "wip"        # red 'WIP' pill
    DISABLED = "disabled"  # tile rendered dimmed


# Default badge text + color (3-digit hex, no '#') per status. The hub
# renders this in the top-right corner of each tile when entry.badge is
# unset. Caller may always override by providing badge/badge_color.
STATUS_BADGE: dict[Status, tuple[str, str]] = {
    Status.OK: ("", ""),
    Status.NEW: ("NEW", "0c4"),
    Status.BETA: ("BETA", "f93"),
    Status.WIP: ("WIP", "f55"),
    Status.DISABLED: ("OFF", "888"),
}


OpenCallback = Callable[[object], Awaitable[None]]


@dataclass
class HubAppEntry:
    # ── identification ──────────────────────────────────────────────────
    key: str
    name: str
    # ── visuals ────────────────────────────────────────────────────────
    icon: str = "cog"
    # Optional bitmap URL rendered instead of the glyph (http(s):// or
    # file:// path the player's TM client can fetch). When set, the hub
    # tile uses the image and ignores `icon`.
    icon_image: str | None = None
    color: str = "15f"             # 3-digit hex (no '#') — accent strip / icon glow
    description: str = ""          # short subtitle shown under the name
    # ── classification ─────────────────────────────────────────────────
    role: Role = Role.PLAYER
    status: Status = Status.OK
    tags: list[str] = field(default_factory=list)
    order: int = 100
    # ── badge override ─────────────────────────────────────────────────
    # If `badge` is set explicitly it wins over the status auto-badge.
    badge: str | None = None
    badge_color: str | None = None
    # ── notifications (Discord-style red pill) ─────────────────────────
    # Per-player count map: {login: int}. Use 0/missing for no badge,
    # >0 to render a red dot with the number ("9+" for >9).
    # Set/updated via the `tmsm_hub:notify` signal — see hub app.
    notifications: dict[str, int] = field(default_factory=dict)
    # ── chat command ───────────────────────────────────────────────────
    # If set, the hub auto-registers `/<command>` which opens this tile
    # for the calling player (skipping the hub window). Permission level
    # is auto-derived from `role`.
    command: str | None = None
    # ── metadata (shown in tooltip / about) ────────────────────────────
    author: str = ""
    version: str = ""
    # ── runtime ────────────────────────────────────────────────────────
    open: Optional[OpenCallback] = field(default=None, repr=False)
    enabled: bool = True

    # convenience accessors ---------------------------------------------

    def effective_badge(self) -> tuple[str, str]:
        """Return (text, color3hex). Empty text means 'no badge'."""
        if self.badge is not None:
            return self.badge, (self.badge_color or "c33")
        if isinstance(self.status, Status):
            return STATUS_BADGE.get(self.status, ("", ""))
        return "", ""

    def notif_for(self, login: str) -> int:
        try:
            return int(self.notifications.get(login, 0))
        except (TypeError, ValueError):
            return 0
