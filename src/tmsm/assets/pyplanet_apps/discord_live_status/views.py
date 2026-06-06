"""Views for discord_live_status."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.audience import Audience
from pyplanet.apps.tmsm.ui.views import BaseView


class DiscordLiveStatusSettingsView(BaseView):
    template_name = "discord_live_status/settings.xml"
    breadcrumbs = [{"key": "hub", "label": "Hub"}]
    audience = Audience.admins()

    async def get_per_player_data(self, login):
        return await self.app.build_settings_context(login)
