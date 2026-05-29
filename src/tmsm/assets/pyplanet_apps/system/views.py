"""Views for the system addon."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class StatusView(BaseView):
    template_name = "tmsm_system/status.xml"

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(self.app.collect_status())
        return ctx


class LogsView(BaseView):
    template_name = "tmsm_system/logs.xml"

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(files=self.app.list_log_files(), active="", filter="", lines=[],
                   log_path="")
        return ctx

    async def get_per_player_data(self, login):
        return self.app.logs_context(login)


class AppsView(BaseView):
    template_name = "tmsm_system/apps.xml"

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(self.app.apps_context(None))
        return ctx

    async def get_per_player_data(self, login):
        return self.app.apps_context(login)
