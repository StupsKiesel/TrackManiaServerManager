"""Browse, install, update, and remove PyPlanet addons."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from .. import assets as assets_mod
from ..assets import Addon, AddonSource
from .confirm import ConfirmScreen
from .install_screen import InstallScreen


class AssetsScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("R", "refresh", "Refresh"),
        Binding("i", "install", "Install"),
        Binding("u", "update", "Update"),
        Binding("d", "remove", "Remove"),
    ]

    DEFAULT_CSS = """
    #addons-wrap { height: 1fr; }
    #addons-info { height: auto; padding: 1; color: $text-muted; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[Addon, bool]] = []  # (addon, installed?)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Container(id="addons-wrap"):
            yield DataTable(id="addons", cursor_type="row", zebra_stripes=True)
        yield Vertical(Static("", id="addons-info"))
        yield Footer()

    def on_mount(self) -> None:
        self.title = "PyPlanet addons"
        table = self.query_one(DataTable)
        table.add_columns("", "Name", "Source", "Author", "Description")
        self.refresh_rows()

    def refresh_rows(self) -> None:
        installed = {a.name for a in assets_mod.list_installed()}
        bundled = assets_mod.list_bundled()
        community = assets_mod.list_catalog()
        self.rows = []
        for a in bundled:
            self.rows.append((a, a.name in installed))
        for a in community:
            # community 'multi' addons are tracked as "<name>:<sub>" — count any prefix-match as installed.
            is_installed = a.name in installed or any(
                rec.startswith(a.name + ":") for rec in installed
            )
            self.rows.append((a, is_installed))

        table = self.query_one(DataTable)
        prev = table.cursor_row if table.row_count else 0
        table.clear()
        for addon, inst in self.rows:
            mark = "[green]●[/green]" if inst else "[grey50]○[/grey50]"
            source = "tmsm" if addon.source is AddonSource.BUNDLED else "git"
            if addon.multi:
                source += " (multi)"
            table.add_row(mark, addon.name, source, addon.author or "—",
                          addon.description or "—")
        if table.row_count:
            table.move_cursor(row=min(prev, table.row_count - 1))
        self._update_info()

    def on_data_table_row_highlighted(self, _e: DataTable.RowHighlighted) -> None:
        self._update_info()

    def _selected(self) -> tuple[Addon, bool] | None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        return self.rows[table.cursor_row]

    def _update_info(self) -> None:
        info = self.query_one("#addons-info", Static)
        sel = self._selected()
        if sel is None:
            info.update(
                "No addons. Bundled addons live in src/tmsm/assets/pyplanet_apps/, "
                "community addons in src/tmsm/assets/catalog.json."
            )
            return
        addon, inst = sel
        lines = [
            f"[b]{addon.name}[/b]  ({addon.source.value})",
        ]
        if addon.author:
            lines.append(f"  author:  {addon.author}")
        if addon.repo:
            lines.append(f"  repo:    {addon.repo}  ({addon.ref})")
        if addon.description:
            lines.append(f"  about:   {addon.description}")
        if addon.notes:
            lines.append(f"  [yellow]notes:   {addon.notes}[/yellow]")
        status = "[green]installed[/green]" if inst else "[grey50]not installed[/grey50]"
        lines.append(f"  status:  {status}")
        lines.append("")
        lines.append("[dim]i=install · u=update · d=remove · esc=back[/dim]")
        lines.append("[dim]After install, uncomment the entry in a pool's settings/apps.py to activate it for that pool.[/dim]")
        info.update("\n".join(lines))

    # --- actions ---

    def action_refresh(self) -> None:
        self.refresh_rows()

    def action_install(self) -> None:
        sel = self._selected()
        if sel is None:
            return
        addon, inst = sel
        if inst:
            self.notify(f"{addon.name} is already installed.", severity="warning")
            return

        def runner(log) -> None:
            assets_mod.install_addon(addon, log)

        def after(_r) -> None:
            self.refresh_rows()

        self.app.push_screen(
            InstallScreen(title=f"Install addon: {addon.name}", runner=runner),
            after,
        )

    def action_update(self) -> None:
        sel = self._selected()
        if sel is None:
            return
        addon, inst = sel
        if not inst:
            self.notify(f"{addon.name} is not installed.", severity="warning")
            return
        if addon.source is AddonSource.BUNDLED:
            self.notify("Bundled addons update with tmsm itself.",
                        severity="information")
            return

        # For multi addons there may be several records — update by name prefix.
        installed = assets_mod.list_installed()
        names = [r.name for r in installed
                 if r.name == addon.name or r.name.startswith(addon.name + ":")]

        def runner(log) -> None:
            for n in names:
                assets_mod.update_addon(n, log)

        def after(_r) -> None:
            self.refresh_rows()

        self.app.push_screen(
            InstallScreen(title=f"Update addon: {addon.name}", runner=runner),
            after,
        )

    def action_remove(self) -> None:
        sel = self._selected()
        if sel is None:
            return
        addon, inst = sel
        if not inst:
            self.notify(f"{addon.name} is not installed.", severity="warning")
            return

        installed = assets_mod.list_installed()
        names = [r.name for r in installed
                 if r.name == addon.name or r.name.startswith(addon.name + ":")]

        def confirmed(ok: bool) -> None:
            if not ok:
                return

            def runner(log) -> None:
                for n in names:
                    assets_mod.remove_addon(n, log)

            def after(_r) -> None:
                self.refresh_rows()

            self.app.push_screen(
                InstallScreen(title=f"Remove addon: {addon.name}", runner=runner),
                after,
            )

        self.app.push_screen(
            ConfirmScreen(
                f"Remove '{addon.name}'? This deletes the symlink and drops the "
                f"entry from every pool's apps.py. The git cache stays on disk.",
                title="Remove addon", ok_label="Remove", destructive=True,
            ),
            confirmed,
        )
