"""Views for the tmsm tournaments app."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class TournamentView(BaseView):
    template_name = "tournaments/tournaments.xml"
    breadcrumbs = [
        {"key": "hub", "label": "Hub"},
        {"key": "tournaments", "label": "Tournaments"},
    ]

    async def refresh(self) -> None:
        if not self._visible or not self._visible_logins:
            return
        await self.display(player_logins=list(self._visible_logins))

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            screen="list",
            is_admin=False,
            tournaments=[],
            total=0,
            page=1,
            total_pages=1,
            status="",
            status_color="aaa",
        )
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.view_context(login)
