"""Views for the restart addon."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class RestartView(BaseView):
    template_name = "tmsm_restart/restart.xml"
    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            schedules=[],
            status="",
            status_color="aaa",
            watch_active=False,
            watch_dir="",
            watch_dir_exists=True,
        )
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.view_context(login)


class RestartScheduleFormView(BaseView):
    """Secondary window for creating a new restart schedule."""

    template_name = "tmsm_restart/schedule_form.xml"
    breadcrumbs = [
        {"key": "hub", "label": "Hub"},
        {"key": "restart", "label": "Restart"},
        {"key": "form", "label": "New schedule"},
    ]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            draft_time="04:00",
            draft_target="pyplanet",
            draft_freq="weekly",
            draft_days=127,
            draft_dom=1,
            draft_notifs=[{"min": 15, "text": "Pyplanet restart"}],
            day_labels=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            freq_options=["weekly", "monthly"],
            target_options=["pyplanet", "dedicated"],
            status="",
            status_color="aaa",
        )
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.form_context(login)
