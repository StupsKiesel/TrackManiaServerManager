"""View for the toast stack — one per-player manialink, refreshed on every state change."""
from __future__ import annotations

from typing import Any

from pyplanet.apps.tmsm.ui.audience import Audience
from pyplanet.apps.tmsm.ui.views import BaseView


class StatusView(BaseView):
    template_name = "tmsm_status/status.xml"
    audience: Audience = Audience.everyone()

    def __init__(self, app):
        super().__init__(app)
        self.status_app = app

    async def get_per_player_data(self, login: str) -> dict[str, Any]:
        return self.status_app.context_for(login)

    async def get_context_data(self) -> dict[str, Any]:
        ctx = await super().get_context_data() or {}
        ctx.update(notifications=[])
        return ctx
