"""View for the master-only Logging Engine panel."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.audience import Audience
from pyplanet.apps.tmsm.ui.views import BaseView


class LoggingEngineView(BaseView):
    template_name = "logging_engine/panel.xml"
    audience = Audience.master_admins()
    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(await self.app.panel_context(None))
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.panel_context(login)
