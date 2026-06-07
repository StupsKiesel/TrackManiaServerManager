"""Views for the tmsm_gamemodes app."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class OperatorView(BaseView):
    """Operator main window: list modes, activate/stop, edit config, see status."""

    template_name = "tmsm_gamemodes/main.xml"
    breadcrumbs = [
        {"key": "hub", "label": "Hub"},
        {"key": "gamemodes_ops", "label": "Game modes"},
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
            is_admin=False,
            runtime_controls_enabled=True,
            operator_mode_allowed=False,
            operator_field_policy=[],
            widget_profile_rows=[],
            wprof_draft={
                "widget_key": "",
                "x": "",
                "y": "",
                "w": "",
                "h": "",
                "disabled": False,
            },
            wprof_window_open=False,
            wprof_view="list",
            wprof_picker_rows=[],
            wprof_editor=None,
            known_widget_keys=[],
        )
        return ctx

    async def refresh(self) -> None:
        if not self._visible or not self._visible_logins:
            return
        await self.display(player_logins=list(self._visible_logins))

    async def get_per_player_data(self, login):
        return await self.app.operator_context(login)


class AdminView(BaseView):
    """Admin manager window: full mode configuration + operator policy."""

    template_name = "tmsm_gamemodes/main.xml"
    breadcrumbs = [
        {"key": "hub", "label": "Hub"},
        {"key": "gamemodes_admin", "label": "Game modes admin"},
    ]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            modes=[],
            active_key=None,
            active_name="",
            active_status_lines=[],
            active_config=[],
            selected_key=None,
            editing_key=None,
            vote_snapshot=None,
            is_admin=True,
            runtime_controls_enabled=False,
            operator_mode_allowed=False,
            operator_field_policy=[],
            widget_profile_rows=[],
            wprof_draft={
                "widget_key": "",
                "x": "",
                "y": "",
                "w": "",
                "h": "",
                "disabled": False,
            },
            wprof_window_open=False,
            wprof_view="list",
            wprof_picker_rows=[],
            wprof_editor=None,
            known_widget_keys=[],
        )
        return ctx

    async def refresh(self) -> None:
        if not self._visible or not self._visible_logins:
            return
        await self.display(player_logins=list(self._visible_logins))

    async def get_per_player_data(self, login):
        return await self.app.admin_context(login)


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
