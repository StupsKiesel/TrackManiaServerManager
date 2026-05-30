"""Views for the master Apps store."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class AppsStoreView(BaseView):
    template_name = "tmsm_apps_store/store.xml"
    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(self.app.apps_context(None))
        return ctx

    async def get_per_player_data(self, login):
        return self.app.apps_context(login)
