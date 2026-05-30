"""Views for the system addon."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class StatusView(BaseView):
    template_name = "tmsm_system/status.xml"
    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            ts="", active_tab="host", tabs=self.app.STATUS_TABS,
            hostname="?", os_name="?", kernel="", uptime_s="?",
            cpu_count=0, cpu_model="?",
            load1=0.0, load5=0.0, load15=0.0, load1_pct=0,
            mem_total="?", mem_used="?", mem_avail="?", mem_pct=0,
            swap_total="?", swap_used="?", swap_pct=0,
            disk_total="?", disk_used="?", disk_free="?", disk_pct=0,
        )
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.status_context(login)


class LogsView(BaseView):
    template_name = "tmsm_system/logs.xml"
    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            files=[], active="", filter="", lines=[], log_path="",
            page=1, total_pages=1, line_count=0, selected="",
            status="", status_color="aaa", confirm_delete="",
        )
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
