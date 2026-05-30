"""ConsoleView — single window with tabs for Dedicated (GBX) and PyPlanet."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class ConsoleView(BaseView):
    template_name = "tmsm_consoles/console.xml"
    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            active_kind="gbx",
            tabs=[
                {"key": "gbx", "label": "Dedicated"},
                {"key": "pyp", "label": "PyPlanet"},
            ],
            prompt="$",
            hint="",
            lines=[],
            input_value="",
            last_status="",
            last_status_color="aaa",
            log_path="",
        )
        return ctx

    async def get_per_player_data(self, login):
        return self.app.build_console_context(login)
