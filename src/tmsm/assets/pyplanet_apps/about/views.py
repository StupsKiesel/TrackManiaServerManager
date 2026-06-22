"""View for the About panel (all players)."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.audience import Audience
from pyplanet.apps.tmsm.ui.views import BaseView


class AboutView(BaseView):
    template_name = "about/panel.xml"
    audience = Audience.everyone()
    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(self.app.panel_context())
        return ctx
