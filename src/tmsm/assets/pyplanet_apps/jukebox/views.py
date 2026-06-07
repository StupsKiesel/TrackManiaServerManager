"""Views for the tmsm jukebox app."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class JukeboxView(BaseView):
    template_name = "jukebox/jukebox.xml"
    breadcrumbs = [
        {"key": "hub", "label": "Hub"},
        {"key": "jukebox", "label": "Jukebox"},
    ]

    async def refresh(self) -> None:
        # Keep this window strictly per-player: never fall back to show(),
        # which can target the full audience when BaseView visibility state
        # is temporarily out of sync.
        if not self._visible or not self._visible_logins:
            return
        await self.display(player_logins=list(self._visible_logins))

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            entries=[],
            current_map="",
            current_uid="",
            total=0,
            page=1,
            total_pages=1,
            is_admin=False,
            allow_juking=True,
            has_maplist=False,
            status="",
            status_color="aaa",
        )
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.view_context(login)
