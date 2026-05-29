"""Bottom-right reload button + AUTO watcher checkbox, master-admins only."""
from pyplanet.views.template import TemplateView


class ReloadButtonView(TemplateView):
    template_name = "dev_reload/widget.xml"
    widget_x = 160.0
    widget_y = -82.0

    def __init__(self, app):
        super().__init__(app.context.ui)
        self.app = app
        self.id = "tmsm_dev_reload_button"
        self.subscribe("reload", self._on_click)
        self.subscribe("toggle_watch", self._on_toggle_watch)

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx["widget_x"] = self.widget_x
        ctx["widget_y"] = self.widget_y
        ctx["watch_active"] = bool(self.app.watch_active)
        return ctx

    async def _on_click(self, player, action, values, **kwargs):
        await self.app.handle_reload_click(player)

    async def _on_toggle_watch(self, player, action, values, **kwargs):
        await self.app.handle_toggle_watch(player)
