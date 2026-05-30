"""Views for the tmx_browser addon."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class TmxBrowserView(BaseView):
    template_name = "tmsm_tmx_browser/tmx_browser.xml"
    breadcrumbs = [
        {"key": "hub", "label": "Hub"},
        {"key": "tmx", "label": "TMX"},
    ]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            site_label="Trackmania Exchange",
            sections=[],
            section="recent",
            is_search=False,
            query="",
            page=1,
            results=[],
            more=False,
            busy=False,
            loaded=False,
            juke_after=True,
            save_match=False,
            status="",
            status_color="aaa",
        )
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.view_context(login)


class TmxDetailView(BaseView):
    """Sub-window showing rich details for a single TMX map."""
    template_name = "tmsm_tmx_browser/tmx_details.xml"
    breadcrumbs = [
        {"key": "hub", "label": "Hub"},
        {"key": "tmx", "label": "TMX"},
        {"key": "details", "label": "Details"},
    ]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            site_label="Trackmania Exchange",
            game="tmnext",
            map={},
            thumb_url="",
            status="",
            status_color="aaa",
            busy=False,
        )
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.detail_context(login)
