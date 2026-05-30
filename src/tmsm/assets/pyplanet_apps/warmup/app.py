"""Warmup - placeholder addon.

Registers a tile in the tmsm hub. Clicking it opens a 'work in progress.
Soon TM' dialog. Replace `WipAppBase` with a real `AppConfig` subclass
when implementing the actual feature.
"""
from __future__ import annotations

from pyplanet.apps.tmsm.hub import Role, Status, WipAppBase


class App_Warmup(WipAppBase):
    name = "pyplanet.apps.tmsm.warmup"
    label = "warmup"

    HUB_KEY = "warmup"
    HUB_NAME = "Warmup"
    HUB_ICON = "fire"
    HUB_COLOR = "f73"
    HUB_DESCRIPTION = "Manage warmup rounds."
    HUB_ROLE = Role.OPERATOR
    HUB_STATUS = Status.WIP
    HUB_ORDER = 30
    HUB_COMMAND = "warmup"
    HUB_VERSION = "0.1"