"""Clock2 widget — sandbox clock wired to widget_engine (refactor target)."""
from __future__ import annotations

from pyplanet.apps.tmsm.widget_engine import AnimDir, DriveMode, Phase
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase


class Clock2Widget(WidgetAppBase):
    name = "pyplanet.apps.tmsm.clock2_widget"
    label = "clock2_widget"

    app_dependencies = ["core.maniaplanet", "widget_engine"]

    WIDGET_KEY = "clock2"
    WIDGET_NAME = "Clock (engine v2)"
    WIDGET_DESCRIPTION = "Local server time — rendered through widget_engine."
    WIDGET_ICON = "clock"
    WIDGET_TEMPLATE = "clock2_widget/clock.xml"

    WIDGET_DEFAULT_X = 0.0
    WIDGET_DEFAULT_Y = 0.0
    WIDGET_DEFAULT_W = 25.0
    WIDGET_DEFAULT_H = 8.0

    WIDGET_REFRESH_SECONDS = 0.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_DRIVE_MODE = DriveMode.HIDE_WHILE_DRIVING
    WIDGET_ANIM_DIR = AnimDir.RIGHT
    WIDGET_ANIM_DURATION_MS = 250
    WIDGET_ANIM_IN_DELAY_MS = 0
    WIDGET_ANIM_OUT_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "1155ffff"

    # Slice 3 test: clock only shows during the actual race.
    WIDGET_VISIBLE_PHASES = None

    async def get_widget_data(self, login):
        return {}
