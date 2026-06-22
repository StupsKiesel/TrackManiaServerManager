"""Views for the player_manager addon."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class PlayerManagerView(BaseView):
    template_name = "player_manager/main.xml"
    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            players=[],
            page=1,
            total_pages=1,
            search="",
            status="",
            status_color="aaa",
            confirm_open=False,
            confirm_title="Confirm",
            confirm_message="",
            confirm_variant="primary",
            confirm_ok="OK",
            confirm_icon="check",
            level_options=[
                {"value": "player", "label": "Player"},
                {"value": "operator", "label": "Operator"},
                {"value": "admin", "label": "Admin"},
                {"value": "master", "label": "Master"},
            ],
            tab="online",
            all_players=[],
            all_page=1,
            all_total_pages=1,
            all_search="",
        )
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.build_context(login)
