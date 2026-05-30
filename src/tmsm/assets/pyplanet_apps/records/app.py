"""Records - placeholder addon.

Registers a tile in the tmsm hub. Clicking it opens a 'work in progress.
Soon TM' dialog. Replace `WipAppBase` with a real `AppConfig` subclass
when implementing the actual feature.
"""
from __future__ import annotations

from pyplanet.apps.tmsm.hub import Role, Status, WipAppBase


class App_Records(WipAppBase):
    name = "pyplanet.apps.tmsm.records"
    label = "records"

    HUB_KEY = "records"
    HUB_NAME = "Records"
    HUB_ICON = "trophy"
    HUB_COLOR = "fc3"
    HUB_DESCRIPTION = "Top times on the current map."
    HUB_ROLE = Role.PLAYER
    HUB_STATUS = Status.WIP
    HUB_ORDER = 20
    HUB_COMMAND = "records"
    HUB_VERSION = "0.1"