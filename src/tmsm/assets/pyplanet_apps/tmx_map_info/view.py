"""Compact ManiaLink widget showing TMX info for the current map."""
from pyplanet.views.template import TemplateView


class MapInfoWidget(TemplateView):
    template_name = "tmx_map_info/widget.xml"
    # Top-right corner. Quad in widget.xml uses halign="right" so its 80-unit
    # width extends LEFT from this anchor, keeping the box inside the screen.
    widget_x = 160.0
    widget_y = 30.0

    def __init__(self, app):
        super().__init__(app.context.ui)
        self.app = app
        self.id = "tmsm_tmx_map_info_widget"
        self.data: dict = {}

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update({
            "widget_x": self.widget_x,
            "widget_y": self.widget_y,
            "data": self.data,
        })
        return ctx

    def set_data(self, data: dict) -> None:
        self.data = data
