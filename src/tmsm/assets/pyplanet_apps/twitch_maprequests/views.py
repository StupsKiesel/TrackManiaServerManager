"""Config window for the Twitch Map Requests app."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui import Audience, BaseView


class TwitchMrView(BaseView):
    """Admin-only window: per-pool config + recent activity feed."""

    template_name = "twitch_maprequests/config.xml"

    # Admin-only window. We only auto-redisplay to admins; the actual
    # opener is targeted via `display(player_logins=[…])` in the hub click.
    audience = Audience.admins()

    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    async def get_per_player_data(self, login):
        """Return the per-player render context (current values + draft)."""
        return await self.app.view_context(login)
