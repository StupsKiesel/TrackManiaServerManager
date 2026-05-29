"""Hub views: the pinned launcher button and the main hub window."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class HubLauncherView(BaseView):
    template_name = "tmsm_hub/launcher.xml"


class HubView(BaseView):
    template_name = "tmsm_hub/hub.xml"

    def __init__(self, app):
        super().__init__(app)
        # global render but per-player data via get_per_player_data
        self.hide_click = False

    async def get_per_player_data(self, login):
        return self.app.build_player_context(login)

    async def get_context_data(self):
        ctx = await super().get_context_data()
        # Fallback values so the template renders cleanly even before per-player
        # data is merged (Jinja resolves bare names against this dict first).
        ctx.update(
            active_tab="player",
            viewer_level=0,
            viewer_level_label="player",
            visible_tabs=[],
            entries_by_role={},
            total_entries=0,
        )
        return ctx
