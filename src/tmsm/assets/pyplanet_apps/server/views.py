"""Views for the server addon."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class ServerSettingsView(BaseView):
    template_name = "tmsm_server/settings.xml"

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(fields=[], status="", status_color="aaa", loading=True)
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.server_settings_context(login)


class ModeSettingsView(BaseView):
    template_name = "tmsm_server/mode.xml"

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(mode_name="", fields=[], status="",
                   status_color="aaa", loading=True)
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.mode_settings_context(login)
