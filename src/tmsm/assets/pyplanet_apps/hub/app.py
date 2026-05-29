"""tmsm hub — central in-game launcher app.

Other addons register tiles by sending the `tmsm_hub:register` signal with
a `HubAppEntry` payload (see `registry.py`). The hub groups tiles by role
into Player / Operator / Admin / Master tabs, filtered by the viewer's
PyPlanet permission level.

Signals exposed:
    tmsm_hub:register  payload: entry=HubAppEntry  — add/replace a tile
    tmsm_hub:refresh   payload: (none)             — re-render the hub
    tmsm_hub:show      payload: player=Player      — show hub to a player
    tmsm_hub:hide      payload: player=Player      — hide hub for a player

Chat command: `/hub` toggles the hub for the calling player.
"""
from __future__ import annotations

import logging
from typing import Any

from pyplanet.apps.config import AppConfig
from pyplanet.contrib.command import Command
from pyplanet.core.events import Signal

from .registry import HubAppEntry, Role
from .views import HubLauncherView, HubView

logger = logging.getLogger(__name__)

_TAB_ORDER = [
    ("player", "Player", Role.PLAYER),
    ("operator", "Operator", Role.OPERATOR),
    ("admin", "Admin", Role.ADMIN),
    ("master", "Master", Role.MASTER),
]


class HubApp(AppConfig):
    name = "pyplanet.apps.tmsm.hub"
    label = "tmsm_hub"
    app_dependencies = ["core.maniaplanet", "tmsm_ui"]
    game_dependencies = ["trackmania", "trackmania_next"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entries: dict[str, HubAppEntry] = {}
        self._open_for: set[str] = set()
        self._active_tab: dict[str, str] = {}
        self._welcomed: set[str] = set()
        self.launcher: HubLauncherView | None = None
        self.hub: HubView | None = None

    # ---- lifecycle -----------------------------------------------------

    async def on_init(self) -> None:
        # Register custom signals so apps can listen/send by name.
        for code in ("register", "refresh", "show", "hide"):
            try:
                self.context.signals.register_signal(
                    Signal(code=code, namespace="tmsm_hub")
                )
            except Exception:
                logger.exception("hub: failed to register signal tmsm_hub:%s", code)

    async def on_start(self) -> None:
        self.launcher = HubLauncherView(self)
        self.launcher.connect("toggle", self._on_launcher_toggle)

        self.hub = HubView(self)
        # We replace the default catch-all to route open__<key> / tab__<key>.
        self.hub.handle_catch_all = self._hub_catch_all  # type: ignore[assignment]

        # Chat command.
        try:
            await self.instance.command_manager.register(
                Command(command="hub", target=self._cmd_hub,
                        description="Toggle the tmsm hub launcher."),
            )
        except Exception:
            logger.exception("hub: /hub command registration failed")

        # Subscribe to our own signals.
        self.context.signals.listen("tmsm_hub:register", self._on_register)
        self.context.signals.listen("tmsm_hub:refresh", self._on_refresh)
        self.context.signals.listen("tmsm_hub:show", self._on_signal_show)
        self.context.signals.listen("tmsm_hub:hide", self._on_signal_hide)
        self.context.signals.listen("maniaplanet:player_connect", self._on_player_connect)

        # Show the launcher to everyone (global manialink).
        await self.launcher.show()
        logger.info("hub: started; launcher visible")

    async def on_stop(self) -> None:
        for view in (self.launcher, self.hub):
            if view is None:
                continue
            try:
                await view.destroy()
            except Exception:
                logger.exception("hub: destroy failed")

    # ---- registry ------------------------------------------------------

    async def _on_register(self, entry: HubAppEntry | None = None, **kwargs) -> None:
        if entry is None:
            logger.warning("hub: tmsm_hub:register received without 'entry' payload")
            return
        if not isinstance(entry, HubAppEntry):
            logger.warning("hub: tmsm_hub:register payload not a HubAppEntry: %r", entry)
            return
        self.entries[entry.key] = entry
        logger.info("hub: registered '%s' (%s, role=%s)", entry.key, entry.name, entry.role.label)
        if self.hub is not None and self._open_for:
            await self.hub.refresh()

    async def _on_refresh(self, **kwargs) -> None:
        if self.hub is not None and self._open_for:
            await self.hub.refresh()

    # ---- launcher / window toggle --------------------------------------

    async def _on_launcher_toggle(self, player) -> None:
        await self._toggle(player)

    async def _cmd_hub(self, player, data, **kwargs) -> None:
        await self._toggle(player)

    async def _toggle(self, player) -> None:
        if player.login in self._open_for:
            await self._hide_for(player)
        else:
            await self._show_for(player)

    async def _show_for(self, player) -> None:
        if self.hub is None:
            return
        self._open_for.add(player.login)
        self._active_tab.setdefault(player.login, self._default_tab_for(player))
        try:
            await self.hub.display(player_logins=[player.login])
        except Exception:
            logger.exception("hub: display failed for %s", player.login)

    async def _hide_for(self, player) -> None:
        if self.hub is None:
            return
        self._open_for.discard(player.login)
        try:
            from pyplanet.views.template import TemplateView
            await TemplateView.hide(self.hub, player_logins=[player.login])
        except Exception:
            logger.exception("hub: hide failed for %s", player.login)

    async def _on_signal_show(self, player=None, **kwargs) -> None:
        if player is not None:
            await self._show_for(player)

    async def _on_signal_hide(self, player=None, **kwargs) -> None:
        if player is not None:
            await self._hide_for(player)

    # ---- per-player ----------------------------------------------------

    async def _on_player_connect(self, player, **kwargs) -> None:
        # Re-display the launcher for newcomers (manialinks are usually
        # auto-restored but doing this explicitly is cheap and reliable).
        if self.launcher is not None:
            try:
                await self.launcher.display(player_logins=[player.login])
            except Exception:
                logger.exception("hub: launcher display on connect failed")
        if player.login not in self._welcomed:
            self._welcomed.add(player.login)
            try:
                await self.instance.chat(
                    "$z$0afWelcome! Click the $fffHub$0af button (bottom-right) "
                    "or type $fff/hub$0af to open the launcher.",
                    player,
                )
            except Exception:
                logger.exception("hub: welcome chat failed")

    # ---- view context --------------------------------------------------

    def build_player_context(self, login: str) -> dict[str, Any]:
        """Per-player template data for HubView."""
        player = self._player(login)
        level = int(getattr(player, "level", 0)) if player is not None else 0
        level_label = {0: "player", 1: "operator", 2: "admin", 3: "master"}[level]

        visible_tabs: list[dict[str, str]] = []
        entries_by_role: dict[str, list[HubAppEntry]] = {}
        total = 0
        for key, label, role in _TAB_ORDER:
            if int(role) > level:
                continue
            tiles = sorted(
                (e for e in self.entries.values() if int(e.role) == int(role) and e.enabled),
                key=lambda e: (e.order, e.name.lower()),
            )
            entries_by_role[key] = tiles
            total += len(tiles)
            visible_tabs.append({"key": key, "label": label})

        active = self._active_tab.get(login)
        if active not in {t["key"] for t in visible_tabs}:
            active = visible_tabs[0]["key"] if visible_tabs else "player"
            self._active_tab[login] = active

        return {
            "active_tab": active,
            "viewer_level": level,
            "viewer_level_label": level_label,
            "visible_tabs": visible_tabs,
            "entries_by_role": entries_by_role,
            "total_entries": total,
        }

    def _player(self, login: str):
        try:
            for p in self.instance.player_manager.online:
                if p.login == login:
                    return p
        except Exception:
            pass
        return None

    def _default_tab_for(self, player) -> str:
        # Always open on the Player tab — friendliest first impression.
        return _TAB_ORDER[0][0]

    # ---- hub view action routing ---------------------------------------

    async def _hub_catch_all(self, player, action, values, **kwargs) -> None:
        # Tile click: `open__<key>`
        if action.startswith("open__"):
            key = action[len("open__"):]
            await self._open_entry(player, key)
            return
        # Tab click from ui.tabs uses the action `<group>__tab__<key>` →
        # group is 'role', so action looks like `role__tab__player`.
        if action.startswith("role__tab__"):
            tab = action[len("role__tab__"):]
            self._active_tab[player.login] = tab
            if self.hub is not None:
                try:
                    await self.hub.display(player_logins=[player.login])
                except Exception:
                    logger.exception("hub: tab re-display failed")
            return
        logger.debug("hub: unhandled action '%s' from %s", action, player.login)

    async def _open_entry(self, player, key: str) -> None:
        entry = self.entries.get(key)
        if entry is None:
            logger.warning("hub: open requested for unknown key '%s'", key)
            return
        if int(entry.role) > int(getattr(player, "level", 0)):
            logger.warning("hub: %s tried to open '%s' (insufficient level)", player.login, key)
            return
        await self._hide_for(player)
        if entry.open is None:
            try:
                await self.instance.chat(
                    f"$z$ff0[hub]$z this tile ('{entry.name}') has no open handler.",
                    player,
                )
            except Exception:
                pass
            return
        try:
            await entry.open(player)
        except Exception:
            logger.exception("hub: entry '%s' open callback raised", key)
