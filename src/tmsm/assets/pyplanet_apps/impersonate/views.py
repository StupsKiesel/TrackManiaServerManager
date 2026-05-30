"""Views for the impersonate addon."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class ImpersonatePickerView(BaseView):
    template_name = "tmsm_impersonate/picker.xml"
    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            active=False,
            effective=3,
            effective_label="master",
            real=3,
            real_label="master",
            is_player=False,
            is_operator=False,
            is_admin=False,
            is_master=True,
        )
        return ctx

    async def get_per_player_data(self, login):
        return self.app.view_context(login)
