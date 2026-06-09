"""Views for chat history app."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class ChatHistoryView(BaseView):
    template_name = "tmsm_chat_history/history.xml"
    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            players=[],
            messages=[],
            selected_label="Global",
            selected_key="__global__",
            status="",
            status_color="8af",
        )
        return ctx

    async def get_per_player_data(self, login):
        return self.app.build_view_context(login)
