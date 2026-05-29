"""tmsm.ui app — minimal AppConfig so the template prefix `tmsm_ui` registers.

This app does no game-specific work; its only job is to expose the shared
Jinja templates under the `tmsm_ui` prefix so other addons can:

    {% import 'tmsm_ui/widgets.xml' as ui %}
"""
from __future__ import annotations

import logging

from pyplanet.apps.config import AppConfig

logger = logging.getLogger(__name__)


class UiApp(AppConfig):
    name = "pyplanet.apps.tmsm.ui"
    label = "tmsm_ui"
    app_dependencies = ["core.maniaplanet"]
    game_dependencies = ["trackmania", "trackmania_next"]

    async def on_start(self) -> None:
        logger.info("tmsm.ui framework loaded (templates available as 'tmsm_ui/*.xml')")
