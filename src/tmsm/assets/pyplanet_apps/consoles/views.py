"""GbxConsoleView and PypConsoleView — shared template, kind-specific context."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class BaseConsoleView(BaseView):
    template_name = "tmsm_consoles/console.xml"
    kind: str = "gbx"
    title: str = "Console"
    prompt: str = "$"
    hint: str = ""

    def __init__(self, app):
        super().__init__(app)
        self.hide_click = False

    async def get_context_data(self):
        ctx = await super().get_context_data()
        # safe defaults so the template renders even without per-player data
        ctx.update(
            title=self.title,
            kind=self.kind,
            prompt=self.prompt,
            hint=self.hint,
            lines=[],
            input_value="",
            last_status="",
            last_status_color="aaa",
            log_path="",
        )
        return ctx

    async def get_per_player_data(self, login):
        return self.app.build_console_context(self.kind, login)


class GbxConsoleView(BaseConsoleView):
    kind = "gbx"
    title = "Game server console (XML-RPC)"
    prompt = "$"
    hint = "MethodName arg1 arg2  —  args parsed as int/bool/str (use \"quotes\" for spaces)"


class PypConsoleView(BaseConsoleView):
    kind = "pyp"
    title = "PyPlanet console (chat commands)"
    prompt = ">"
    hint = "/help, //admin, /mapinfo, ...  —  runs as you, output appears in chat + log"
