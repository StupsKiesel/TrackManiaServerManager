"""Karma - placeholder addon.

Registers a tile in the tmsm hub. Clicking it opens a 'work in progress.
Soon TM' dialog. Replace `WipAppBase` with a real `AppConfig` subclass
when implementing the actual feature.
"""
from __future__ import annotations

from pyplanet.apps.tmsm.hub import Role, Status, WipAppBase


class App_Karma(WipAppBase):
    name = "pyplanet.apps.tmsm.karma"
    label = "karma"

    HUB_KEY = "karma"
    HUB_NAME = "Karma"
    HUB_ICON = "heart"
    HUB_COLOR = "e44"
    HUB_DESCRIPTION = "Vote on the map you're playing."
    HUB_ROLE = Role.PLAYER
    HUB_STATUS = Status.WIP
    HUB_ORDER = 30
    HUB_COMMAND = "karma"
    HUB_VERSION = "0.1"