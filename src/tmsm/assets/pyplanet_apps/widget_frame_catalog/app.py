"""Static preview catalog for the upcoming reusable widget frame.

Renders all combinations of:
  - rounded:   False, True                          (2)
  - accent:    edge, inset, tab                     (3)
  - direction: left, right, top, bottom             (4)
= 24 cards. Identical sample content per card.
"""
from __future__ import annotations

from pyplanet.apps.tmsm.widgets.widget_base import WidgetAppBase


class WidgetFrameCatalog(WidgetAppBase):
    name = "pyplanet.apps.tmsm.widget_frame_catalog"
    label = "widget_frame_catalog"

    WIDGET_KEY = "frame_catalog"
    WIDGET_NAME = "Frame catalog"
    WIDGET_DESCRIPTION = "Preview every rounded x accent x direction combo for the reusable widget frame."
    WIDGET_ICON = "grid"
    WIDGET_TEMPLATE = "widget_frame_catalog/catalog.xml"

    WIDGET_DEFAULT_X = -80.0
    WIDGET_DEFAULT_Y = 70.0
    WIDGET_DEFAULT_W = 160.0
    WIDGET_DEFAULT_H = 140.0

    WIDGET_REFRESH_SECONDS = 0.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_HIDE_WHILE_DRIVING = True
    WIDGET_ANIM_DIR = "right"
    WIDGET_ANIM_DURATION_MS = 250

    async def get_widget_data(self, login):
        return {}
