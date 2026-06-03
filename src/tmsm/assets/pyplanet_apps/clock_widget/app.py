"""Clock widget — server time HUD, hides while in the menu."""
from __future__ import annotations

from datetime import datetime

from pyplanet.apps.tmsm.widgets.widget_base import WidgetAppBase


class ClockWidget(WidgetAppBase):
    name = "pyplanet.apps.tmsm.clock_widget"
    label = "clock_widget"

    WIDGET_KEY = "clock"
    WIDGET_NAME = "Clock"
    WIDGET_DESCRIPTION = "Local server time."
    WIDGET_ICON = "clock"
    WIDGET_TEMPLATE = "clock_widget/clock.xml"

    WIDGET_DEFAULT_X = 0.0
    WIDGET_DEFAULT_Y = 0.0
    WIDGET_DEFAULT_W = 25.0
    WIDGET_DEFAULT_H = 8.0

    WIDGET_REFRESH_SECONDS = 0.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_HIDE_WHILE_DRIVING = True
    WIDGET_ANIM_DIR = "right"
    WIDGET_ANIM_DURATION_MS = 250
    WIDGET_ANIM_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "1155ffff"

    async def get_widget_data(self, login):
        return {}
