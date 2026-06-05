"""Hub views: the launcher widget and the main hub window."""
from __future__ import annotations

from typing import Any

from pyplanet.apps.tmsm.ui.views import BaseView


class HubLauncherView(BaseView):
    """The hub launcher button — rendered through the widget_engine frame
    so its position (per-player and global override) is configurable from
    the widget_engine manager. Only created when widget_engine is loaded;
    otherwise the hub is reachable only via the `/hub` chat command.
    """

    template_name = "tmsm_hub/launcher.xml"

    def __init__(self, app):
        super().__init__(app)
        self.hub_app = app

    async def get_per_player_data(self, login: str) -> dict[str, Any]:
        return self.hub_app.frame_context(login)

    async def get_context_data(self) -> dict[str, Any]:
        ctx = await super().get_context_data() or {}
        # Fallback frame context so a global render (login="") still works.
        ctx.update(self.hub_app.frame_context(""))
        return ctx


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
            page=1,
            total_pages=1,
            page_size=18,
            server_name="server",
            viewer_login="",
        )
        return ctx
