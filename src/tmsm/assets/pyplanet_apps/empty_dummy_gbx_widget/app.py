"""Empty dummy GBX widget.

A GBX manialink replacement that renders nothing. Its only purpose is
to act as a configurable holder for ``hide_ui_modules`` so the operator
can permanently hide any title-pack UI module via the standard widget
engine "MODULES" editor — no custom code required.

The widget owns a unique manialink id (``tmsm_empty_dummy_gbx_widget``)
that does not collide with any title-pack manialink, sends an empty
body, and disables chrome / animation / hotkey. The engine still fires
``Common.UIModules.SetProperties`` for the configured module ids on
register, on phase change, and on player connect, which is the only
side effect that matters here.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from pyplanet.apps.tmsm.widget_engine.registry import (
    GbxReplacement,
    WidgetKind,
)
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase

logger = logging.getLogger(__name__)


_MANIALINK_ID = "tmsm_empty_dummy_gbx_widget"


class EmptyDummyGbxWidgetApp(WidgetAppBase):
    name = "pyplanet.apps.tmsm.empty_dummy_gbx_widget"
    label = "empty_dummy_gbx_widget"

    WIDGET_KEY = "empty_dummy_gbx_widget"
    WIDGET_NAME = "Empty Dummy GBX"
    WIDGET_DESCRIPTION = (
        "Invisible GBX replacement used to hide configured title-pack UI "
        "modules. Use the MODULES editor to pick which ids to hide."
    )
    WIDGET_ICON = "eye-slash"

    # GBX replacement only — never render the regular persistent frame.
    WIDGET_KIND = WidgetKind.POPUP

    def build_entry(self):
        entry = super().build_entry()
        return replace(
            entry,
            gbx_replace=GbxReplacement(
                manialink_id=_MANIALINK_ID,
                # No defaults: operator picks the ids via the MODULES
                # editor (server-wide override persists in we_setting).
                hide_ui_modules=(),
                hotkey=None,
                # No chrome / animation: there is nothing to draw.
                chrome=False,
                # No connect delay: empty body cannot fight any existing
                # manialink, so push as soon as the player is known.
                connect_delay_s=0.0,
            ),
        )

    async def build_replacement_xml(self, login: str) -> str:  # noqa: ARG002
        return ""
