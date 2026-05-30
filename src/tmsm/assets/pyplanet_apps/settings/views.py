"""Views for the PyPlanet app-settings master tool."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class SettingsView(BaseView):
    template_name = "tmsm_settings/settings.xml"
    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            apps=[], selected_app="", selected_app_name="",
            settings=[], categories=[],
            app_page=0, app_total_pages=1, apps_count=0,
            set_page=0, set_total_pages=1, set_count=0,
            search="", dirty_count=0,
            status="", status_color="aaa",
            is_master=False,
        )
        return ctx

    async def get_per_player_data(self, login):
        player = next(
            (p for p in self.app.instance.player_manager.online if p.login == login),
            None,
        )
        if player is None:
            return {}
        return await self.app.settings_context(player)
