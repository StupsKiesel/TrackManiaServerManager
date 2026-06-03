"""Views for the monitor calibration app."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class MonitorView(BaseView):
    template_name = "monitor/monitor.xml"
    breadcrumbs = [
        {"key": "hub", "label": "Hub"},
        {"key": "monitor", "label": "Monitor"},
    ]

    async def get_context_data(self):
        ctx = await super().get_context_data() or {}
        ctx.update(
            edgefit=0,
            stretchfit=0,
            note="",
            note2="",
        )
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.view_context(login)
