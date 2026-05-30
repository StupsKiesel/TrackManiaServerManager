"""Views for the server addon."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class ServerSettingsView(BaseView):
    template_name = "tmsm_server/settings.xml"
    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(fields=[], status="", status_color="aaa", loading=True)
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.server_settings_context(login)


class GameSettingsView(BaseView):
    template_name = "tmsm_server/game.xml"
    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            tabs=[], active_tab="mode", is_master=False, is_admin=False,
            loaded_profile="", mode_name="", categories=[], dirty_count=0,
            status="", status_color="aaa",
            switcher_open=False, picker_page=0, scripts=[], active_script="",
            match_page=0, profiles=[], save_as="", confirm_delete="",
            page_size=12,
        )
        return ctx

    async def get_per_player_data(self, login):
        player = next(
            (p for p in self.app.instance.player_manager.online if p.login == login),
            None,
        )
        if player is None:
            return {}
        return await self.app.game_context(player)
