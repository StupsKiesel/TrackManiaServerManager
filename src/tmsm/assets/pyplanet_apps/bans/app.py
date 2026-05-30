"""Bans - placeholder addon.

Registers a tile in the tmsm hub. Clicking it opens a 'work in progress.
Soon TM' dialog. Replace `WipAppBase` with a real `AppConfig` subclass
when implementing the actual feature.
"""
from __future__ import annotations

from pyplanet.apps.tmsm.hub import Role, Status, WipAppBase


class App_Bans(WipAppBase):
    name = "pyplanet.apps.tmsm.bans"
    label = "bans"

    HUB_KEY = "bans"
    HUB_NAME = "Bans"
    HUB_ICON = "ban"
    HUB_COLOR = "c44"
    HUB_DESCRIPTION = "Manage banned players."
    HUB_ROLE = Role.ADMIN
    HUB_STATUS = Status.WIP
    HUB_ORDER = 30
    HUB_COMMAND = "bans"
    HUB_VERSION = "0.1"