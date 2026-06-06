"""Views for the bug_reports app."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.audience import Audience
from pyplanet.apps.tmsm.ui.views import BaseView


class ReportFormView(BaseView):
    """Player-facing report submission window."""
    template_name = "tmsm_bug_reports/report_form.xml"
    breadcrumbs = [{"key": "hub", "label": "Hub"}]
    audience = Audience.everyone()

    async def get_per_player_data(self, login):
        return await self.app.build_form_context(login)


class ReportListView(BaseView):
    """Master-admin triage window."""
    template_name = "tmsm_bug_reports/report_list.xml"
    breadcrumbs = [{"key": "hub", "label": "Hub"}]
    audience = Audience.master_admins()

    async def get_per_player_data(self, login):
        return await self.app.build_list_context(login)


class SettingsView(BaseView):
    """Master-admin delivery & retention configuration sub-window."""
    template_name = "tmsm_bug_reports/settings.xml"
    breadcrumbs = [
        {"key": "hub", "label": "Hub"},
        {"key": "reports", "label": "Bug reports"},
    ]
    audience = Audience.master_admins()

    async def get_per_player_data(self, login):
        return await self.app.build_settings_context(login)

