"""Kicks - placeholder addon.

Registers a tile in the tmsm hub. Clicking it opens a 'work in progress.
Soon TM' dialog. Replace `WipAppBase` with a real `AppConfig` subclass
when implementing the actual feature.
"""
from __future__ import annotations

from pyplanet.apps.tmsm.hub import Role, Status, WipAppBase


class App_Kicks(WipAppBase):
    name = "pyplanet.apps.tmsm.kicks"
    label = "kicks"

    HUB_KEY = "kicks"
    HUB_NAME = "Kicks"
    HUB_ICON = "user-times"
    HUB_COLOR = "e87"
    HUB_DESCRIPTION = "Kick / blacklist players."
    HUB_ROLE = Role.ADMIN
    HUB_STATUS = Status.WIP
    HUB_ORDER = 40
    HUB_COMMAND = "kicks"
    HUB_VERSION = "0.1"