"""Showcase widget: ~10 distinct widget *shapes* / silhouettes.

Goal is to compare visual designs (rectangle, rounded, pill, arrow,
ribbon, hex, tabbed, L-shape, frame-only, diagonal-stripe) — not colors.
Every card uses the same neutral palette and the same sample content so
the silhouette is the only variable.

Once you pick the design(s) you like, we extract them into reusable
panel macros for real widgets to use.
"""
from __future__ import annotations

from pyplanet.apps.tmsm.widgets.widget_base import WidgetAppBase


class WidgetShapesShowcase(WidgetAppBase):
    name = "pyplanet.apps.tmsm.widget_shapes_showcase"
    label = "widget_shapes_showcase"

    WIDGET_KEY = "shapes_showcase"
    WIDGET_NAME = "Shapes showcase"
    WIDGET_DESCRIPTION = "Compare 10 distinct widget silhouettes side-by-side."
    WIDGET_ICON = "shapes"
    WIDGET_TEMPLATE = "widget_shapes_showcase/showcase.xml"

    WIDGET_DEFAULT_X = -80.0
    WIDGET_DEFAULT_Y = 40.0
    WIDGET_DEFAULT_W = 160.0
    WIDGET_DEFAULT_H = 80.0

    WIDGET_REFRESH_SECONDS = 0.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_HIDE_WHILE_DRIVING = True
    WIDGET_ANIM_DIR = "right"
    WIDGET_ANIM_DURATION_MS = 250

    async def get_widget_data(self, login):
        return {}
