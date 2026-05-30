"""Views for the database master tool."""
from __future__ import annotations

from pyplanet.apps.tmsm.ui.views import BaseView


class DbView(BaseView):
    template_name = "tmsm_db/db.xml"
    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            mode="tables",
            title="Database",
            status="", status_color="aaa",
            is_master=False,
            # tables mode
            tables=[], tables_count=0,
            search_tables="",
            tbl_page=0, tbl_total_pages=1,
            # rows mode
            table_key="", table_name="", table_app="",
            columns=[], rows=[], rows_count=0,
            search_rows="",
            row_page=0, row_total_pages=1,
            pk_field="id",
            # edit mode
            edit_pk="", edit_fields=[], dirty_count=0,
        )
        return ctx

    async def get_per_player_data(self, login):
        player = next(
            (p for p in self.app.instance.player_manager.online if p.login == login),
            None,
        )
        if player is None:
            return {}
        return await self.app.db_context(player)
