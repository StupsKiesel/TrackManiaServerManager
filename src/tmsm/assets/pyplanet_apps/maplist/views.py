"""Views for the maplist player app."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class MaplistView(BaseView):
    template_name = "tmsm_maplist/maplist.xml"
    breadcrumbs = [
        {"key": "hub", "label": "Hub"},
        {"key": "maplist", "label": "Maplist"},
    ]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            sections=[],
            section="all",
            query="",
            page=1,
            results=[],
            more=False,
            total=0,
            jukebox_count=0,
            current_uid="",
            status="",
            status_color="aaa",
        )
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.view_context(login)


class MaplistDetailView(BaseView):
    """Sub-window with full info for one map."""
    template_name = "tmsm_maplist/maplist_details.xml"
    breadcrumbs = [
        {"key": "hub", "label": "Hub"},
        {"key": "maplist", "label": "Maplist"},
        {"key": "details", "label": "Details"},
    ]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            map={},
            in_queue=False,
            queue_pos=0,
            queue_requester="",
            is_current=False,
            status="",
            status_color="aaa",
        )
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.detail_context(login)
