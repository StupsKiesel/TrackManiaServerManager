"""Views for player voting."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.audience import Audience
from pyplanet.apps.tmsm.ui.views import BaseView


class VotingView(BaseView):
    template_name = "voting/window.xml"
    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    async def refresh(self) -> None:
        # Keep this window strictly per-player: never fall back to BaseView.show(),
        # which would target the full audience when _visible_logins is empty.
        if not self._visible or not self._visible_logins:
            return
        await self.display(player_logins=list(self._visible_logins))

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(await self.app.view_context(None))
        return ctx

    async def get_per_player_data(self, login):
        return await self.app.view_context(login)


class VotingWidgetView(BaseView):
    template_name = "voting/widget.xml"
    audience: Audience = Audience.everyone()

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(self.app.widget_context_for(None))
        return ctx

    async def get_per_player_data(self, login):
        return self.app.widget_context_for(login)
