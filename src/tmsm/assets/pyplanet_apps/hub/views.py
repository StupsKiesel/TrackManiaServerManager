"""Hub views: the launcher widget and the main hub window."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView
from pyplanet.apps.tmsm.widgets.widget_base import WidgetView


class HubLauncherView(WidgetView):
    """The hub launcher button — rendered through the tmsm_widgets frame so
    its position is configurable per-player and globally via the widgets
    editor."""

    template_name = "tmsm_hub/launcher.xml"

    def __init__(self, app):
        # WidgetView expects (app, widget_app). The hub app plays both roles.
        super().__init__(app, app)


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
