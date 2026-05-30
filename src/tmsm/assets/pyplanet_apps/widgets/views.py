"""Views for the widgets app — currently just the position editor."""
from __future__ import annotations

from typing import Any

from pyplanet.apps.tmsm.ui.audience import Audience
from pyplanet.apps.tmsm.ui.views import BaseView


class WidgetEditorView(BaseView):
    template_name = "tmsm_widgets/editor.xml"
    # Editor is opened on-demand per login (display targets the specific
    # player). Audience is permissive; scope restriction (player vs global)
    # is enforced server-side in WidgetsApp._act_scope.
    audience: Audience = Audience.everyone()
    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    def __init__(self, app):
        super().__init__(app)
        self.widgets_app = app
        # Override BaseView's default `_close` handler so the window's X
        # tears down edit-mode (not just hides the manialink).
        self.connect("_close", self._on_window_close)

    async def _on_window_close(self, player) -> None:
        await self.widgets_app._close_editor(player.login)

    async def _on_crumb_hub(self, player) -> None:
        # Same teardown as the X button before returning to the hub.
        await self.widgets_app._close_editor(player.login)
        await super()._on_crumb_hub(player)

    async def get_per_player_data(self, login: str) -> dict[str, Any]:
        return self.widgets_app.editor_context(login)

    async def get_context_data(self) -> dict[str, Any]:
        ctx = await super().get_context_data() or {}
        ctx.update(
            rows=[],
            selected_key="",
            selected_name="",
            scope="global",
            step=1.0,
            step_options=[0.5, 1.0, 2.0, 5.0],
        )
        return ctx
