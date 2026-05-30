"""Players - placeholder addon.

Registers a tile in the tmsm hub. Clicking it opens a 'work in progress.
Soon TM' dialog. Replace `WipAppBase` with a real `AppConfig` subclass
when implementing the actual feature.
"""
from __future__ import annotations

from pyplanet.apps.tmsm.hub import Role, Status, WipAppBase


class App_Players(WipAppBase):
    name = "pyplanet.apps.tmsm.players"
    label = "players"

    HUB_KEY = "players"
    HUB_NAME = "Players"
    HUB_ICON = "users"
    HUB_COLOR = "6cf"
    HUB_DESCRIPTION = "Who is online right now."
    HUB_ROLE = Role.PLAYER
    HUB_STATUS = Status.WIP
    HUB_ORDER = 40
    HUB_COMMAND = "players"
    HUB_VERSION = "0.1"