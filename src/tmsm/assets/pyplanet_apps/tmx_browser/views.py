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
            query="",
            page=1,
            results=[],
            more=False,
            busy=False,
            loaded=False,
            selected_idx=-1,
            selected_id=None,
            filter_count=0,
            juke_after=True,
            save_match=False,
            status="",
            status_color="aaa",
            columns=[],
            col_scroll=0,
            cols_total=0,
            cols_visible=0,
            col_can_prev=False,
            col_can_next=False,
            is_admin=False,
            policy_active=False,
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


class TmxFiltersView(BaseView):
    """Sub-window with the granular search filters."""
    template_name = "tmsm_tmx_browser/tmx_filters.xml"
    breadcrumbs = [
        {"key": "hub", "label": "Hub"},
        {"key": "tmx", "label": "TMX"},
        {"key": "filters", "label": "Filters"},
    ]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            site_label="Trackmania Exchange",
            draft={},
            draft_length_min_text="",
            draft_length_max_text="",
            open_combo=None,
            tags_all=[],
            tag_chips=[],
            env_opts=[],
            vehicle_opts=[],
            maptype_opts=[],
            mood_opts=[],
            diff_opts=[],
            route_opts=[],
            collection_opts=[],
            order_opts=[],
            status="",
            status_color="aaa",
            policy_locked={},
            policy_hidden=[],
            policy_min_text="",
            policy_max_text="",
            policy_req_tag_ids=[],
            policy_blocked_tag_ids=[],
        )
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.filters_context(login)


class TmxPolicyView(BaseView):
    """Admin-only sub-window: edit the server-wide TMX search policy."""
    template_name = "tmsm_tmx_browser/tmx_policy.xml"
    breadcrumbs = [
        {"key": "hub", "label": "Hub"},
        {"key": "tmx", "label": "TMX"},
        {"key": "policy", "label": "Policy"},
    ]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            site_label="Trackmania Exchange",
            draft={},
            draft_length_min_text="",
            draft_length_max_text="",
            locked={},
            hidden=[],
            open_combo=None,
            tags_all=[],
            tags_req_chips=[],
            tags_blk_chips=[],
            tag_mode="req",
            env_opts=[],
            vehicle_opts=[],
            maptype_opts=[],
            mood_opts=[],
            diff_opts=[],
            route_opts=[],
            collection_opts=[],
            status="",
            status_color="aaa",
            tab="filters",
        )
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.policy_context(login)
