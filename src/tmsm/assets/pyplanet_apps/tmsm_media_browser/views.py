"""Views for the tmsm_media_browser addon."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class GridView(BaseView):
    """Main grid: tabs of categories, thumbnails, custom-URL tester."""

    template_name = "tmsm_media_browser/grid.xml"
    breadcrumbs = [
        {"key": "hub",   "label": "Hub"},
        {"key": "media", "label": "Media browser"},
    ]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            categories=[],     # [{key, label}]
            active_cat=None,
            items=[],          # [{key, label, url, note}]
            custom_url="",
        )
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.grid_context(login)


class DetailView(BaseView):
    """Single-image preview + copy-URL field."""

    template_name = "tmsm_media_browser/detail.xml"
    breadcrumbs = [
        {"key": "hub",   "label": "Hub"},
        {"key": "media", "label": "Media browser"},
        {"key": "item",  "label": "Preview"},
    ]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            label="",
            url="",
            note="",
        )
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.detail_context(login)
