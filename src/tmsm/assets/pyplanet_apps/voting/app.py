"""Voting - placeholder addon.

Registers a tile in the tmsm hub. Clicking it opens a 'work in progress.
Soon TM' dialog. Replace `WipAppBase` with a real `AppConfig` subclass
when implementing the actual feature.
"""
from __future__ import annotations

from pyplanet.apps.tmsm.hub import Role, Status, WipAppBase


class App_Voting(WipAppBase):
    name = "pyplanet.apps.tmsm.voting"
    label = "voting"

    HUB_KEY = "voting"
    HUB_NAME = "Voting"
    HUB_ICON = "check-square"
    HUB_COLOR = "4d8"
    HUB_DESCRIPTION = "Run a vote among players."
    HUB_ROLE = Role.PLAYER
    HUB_STATUS = Status.WIP
    HUB_ORDER = 20
    HUB_COMMAND = "vote"
    HUB_VERSION = "0.1"