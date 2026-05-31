"""Views for the tmsm_gamemodes app."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class OperatorView(BaseView):
    """Operator main window: list modes, activate/stop, edit config, see status."""

    template_name = "tmsm_gamemodes/main.xml"
    breadcrumbs = [
        {"key": "hub", "label": "Hub"},
        {"key": "gamemodes", "label": "Game modes"},
    ]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            modes=[],            # [{key,name,description,icon,color,category,active,is_admin}]
            active_key=None,
            active_name="",
            active_status_lines=[],
            active_config=[],    # rendered config rows for the editor sub-panel
            selected_key=None,
            editing_key=None,
            vote_snapshot=None,  # mirror of the live vote, if any
        )
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.operator_context(login)


class VotePanelView(BaseView):
    """Player-facing vote panel; shown while a vote is active."""

    template_name = "tmsm_gamemodes/vote.xml"

    # No breadcrumbs: this is an overlay, not a hub-app screen.
    breadcrumbs = []

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            vote=None,           # vote snapshot dict
            has_voted=False,
            picked_value=None,
        )
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.vote_panel_context(login)
