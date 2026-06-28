"""Views for the tmsm_gamemodes app."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.audience import Audience
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


class VotePanelWidgetView(BaseView):
    """Player-facing vote panel rendered through ``widget_engine``.

    Unlike :class:`VotePanelView` (a fixed bottom-centre overlay), this view
    is registered with the widget engine so the master admin can move/resize
    it like any other widget. The engine resolves its position/size and the
    app injects that, together with the live vote snapshot, via
    ``vote_widget_context_for``. It is concealed client-side (frame script
    honours ``widget_force_hidden``) whenever no vote is active.
    """

    template_name = "tmsm_gamemodes/vote_widget.xml"
    audience: Audience = Audience.everyone()
    breadcrumbs = []

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(self.app.vote_widget_context_for(None))
        return ctx

    async def get_per_player_data(self, login):
        return self.app.vote_widget_context_for(login)


class RmcResultsView(BaseView):
    """End-of-run results panel: medal contributions per player.

    Shown to everyone online when an RMC run finishes. Players close it
    via the Close button; until then it stays sticky so late joiners and
    spectators can see who carried the run.
    """

    template_name = "tmsm_gamemodes/rmc_results.xml"

    # No breadcrumbs: this is an overlay, not a hub-app screen.
    breadcrumbs = []

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            results=None,
            can_close=True,
        )
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.rmc_results_context(login)
