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

from pyplanet.apps.tmsm.ui import perms as _perms

try:
    from pyplanet.apps.tmsm.widgets.registry import (
        Animation as _WidgetAnim,
        HideRule as _WidgetHide,
        WidgetEntry,
        WidgetKind,
    )
    _HAS_WIDGETS = True
except Exception:
    _HAS_WIDGETS = False

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

    # ── tmsm_widgets contract (the launcher is registered as a widget) ──
    WIDGET_KEY = "hub_launcher"
    WIDGET_NAME = "Hub Launcher"
    WIDGET_DESCRIPTION = "Button that opens the tmsm hub window."
    WIDGET_ICON = "th-large"
    WIDGET_DEFAULT_X = -158.0
    WIDGET_DEFAULT_Y = -58.0
    WIDGET_DEFAULT_W = 6.0
    WIDGET_DEFAULT_H = 6.0
    # Always visible — the launcher must be reachable regardless of game state.
    WIDGET_HIDE_NAMED: list[str] = []
    WIDGET_HIDE_RAW = ""
    WIDGET_ANIM_DIR = "fade"
    WIDGET_ANIM_DURATION_MS = 200

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entries: dict[str, HubAppEntry] = {}
        self._open_for: set[str] = set()
        self._active_tab: dict[str, str] = {}
        # Per-player current grid page, keyed by (login, tab_key).
        self._page: dict[tuple[str, str], int] = {}
        self.page_size: int = 18  # 3x6 grid
        self._welcomed: set[str] = set()
        self.launcher: HubLauncherView | None = None
        self.hub: HubView | None = None
        # Cached server name from the dedicated server's <server_options><name>
        # (resolved via GBX `GetServerOptions`, which returns the runtime value
        # loaded from dedicated_cfg.txt). Refreshed at on_start and whenever
        # someone opens the hub.
        self._server_name: str = "server"
        # Track chat commands we've already wired so re-registration is a no-op.
        self._registered_commands: set[str] = set()
        # Set when the widgets app is available (resolved lazily).
        self.widgets_app = None
        self._widgets_listeners_wired: bool = False

    # ---- lifecycle -----------------------------------------------------

    async def on_init(self) -> None:
        # Register custom signals so apps can listen/send by name.
        for code in ("register", "refresh", "show", "hide", "notify"):
            try:
                self.context.signals.register_signal(
                    Signal(code=code, namespace="tmsm_hub")
                )
            except Exception:
                logger.exception("hub: failed to register signal tmsm_hub:%s", code)

    async def on_start(self) -> None:
        # Resolve the widgets app (optional dependency).
        try:
            self.widgets_app = self.instance.apps.apps.get("tmsm_widgets")
        except Exception:
            self.widgets_app = None

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
        self.context.signals.listen("tmsm_hub:notify", self._on_notify)
        self.context.signals.listen("maniaplanet:player_connect", self._on_player_connect)

        # Show the launcher to everyone (global manialink).
        await self.launcher.show()
        await self._register_launcher_widget()
        await self._refresh_server_name()
        logger.info("hub: started; launcher visible")

    async def on_stop(self) -> None:
        for view in (self.launcher, self.hub):
            if view is None:
                continue
            try:
                await view.destroy()
            except Exception:
                logger.exception("hub: destroy failed")

    # ---- tmsm_widgets contract -----------------------------------------

    @property
    def view(self):
        """Used by the widgets editor (`_find_widget_app`) to refresh the
        launcher after a position change."""
        return self.launcher

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        return {}

    def frame_context(self, login: str) -> dict[str, Any]:
        """Per-player position + edit-mode flags consumed by the
        ``tmsm_widgets/frame.xml`` macro inside ``launcher.xml``.

        Mirrors :meth:`WidgetAppBase.frame_context`. Falls back to the
        hard-coded defaults if the widgets app is not loaded.
        """
        if self.widgets_app is None:
            try:
                self.widgets_app = self.instance.apps.apps.get("tmsm_widgets")
            except Exception:
                self.widgets_app = None
        pos = {}
        if self.widgets_app is not None:
            try:
                pos = self.widgets_app.resolve_position(self.WIDGET_KEY, login)
            except Exception:
                pos = {}
        editing = False
        if self.widgets_app is not None:
            try:
                editing = bool(self.widgets_app.is_editing(login))
            except Exception:
                editing = False
        return {
            "widget_key":             self.WIDGET_KEY,
            "widget_x":               pos.get("x", self.WIDGET_DEFAULT_X),
            "widget_y":               pos.get("y", self.WIDGET_DEFAULT_Y),
            "widget_w":               pos.get("w", self.WIDGET_DEFAULT_W),
            "widget_h":               pos.get("h", self.WIDGET_DEFAULT_H),
            "widget_kind":            "persistent",
            "widget_hide_clauses":    [],
            "widget_hide_raw":        "",
            "widget_anim_dir":        self.WIDGET_ANIM_DIR,
            "widget_anim_duration_ms": self.WIDGET_ANIM_DURATION_MS,
            "widget_anim_delay_ms":   0,
            "widget_edit_mode":       editing,
            "widget_view_id":         self.launcher.id if self.launcher else self.WIDGET_KEY,
        }

    async def _register_launcher_widget(self) -> None:
        """Tell the widgets app about the launcher so it shows up in the
        position editor and reacts to position changes.

        Safe to call multiple times \u2014 widget signal listeners are wired
        exactly once via :attr:`_widgets_listeners_wired`.
        """
        if not _HAS_WIDGETS:
            logger.info("hub: tmsm_widgets not available; launcher position not configurable")
            return
        entry = WidgetEntry(
            key=self.WIDGET_KEY,
            name=self.WIDGET_NAME,
            description=self.WIDGET_DESCRIPTION,
            icon=self.WIDGET_ICON,
            default_x=self.WIDGET_DEFAULT_X,
            default_y=self.WIDGET_DEFAULT_Y,
            default_w=self.WIDGET_DEFAULT_W,
            default_h=self.WIDGET_DEFAULT_H,
            kind=WidgetKind.PERSISTENT,
            hide_rule=_WidgetHide(named=[], raw=""),
            animation=_WidgetAnim(
                direction=self.WIDGET_ANIM_DIR,
                duration_ms=self.WIDGET_ANIM_DURATION_MS,
            ),
            author="tmsm",
            version="0.1",
        )
        try:
            sig = self.context.signals.get_signal("tmsm_widgets:register")
            await sig.send_robust({"entry": entry}, raw=True)
            logger.info("hub: launcher widget registered with tmsm_widgets")
        except KeyError:
            logger.info("hub: tmsm_widgets:register signal not available yet")
        except Exception:
            logger.exception("hub: launcher widget registration failed")
        # Wire signal listeners exactly once.
        if self._widgets_listeners_wired:
            return
        try:
            self.context.signals.listen(
                "tmsm_widgets:position_changed", self._on_launcher_pos_changed,
            )
            self.context.signals.listen(
                "tmsm_widgets:edit_mode", self._on_widgets_edit_mode,
            )
            self.context.signals.listen(
                "tmsm_widgets:request_register", self._on_widgets_request_register,
            )
            self._widgets_listeners_wired = True
        except Exception:
            logger.exception("hub: failed to subscribe to widgets signals")

    async def _on_widgets_request_register(self, **kwargs) -> None:
        await self._register_launcher_widget()
        # The widgets app only becomes available after hub.on_start() has
        # already shown the launcher with default positions. Now that
        # widgets is live, re-render so resolve_position() picks up any
        # stored per-player / global override and the launcher snaps to
        # its real location instead of jumping when the editor opens.
        if self.launcher is not None:
            try:
                self.widgets_app = self.instance.apps.apps.get("tmsm_widgets")
            except Exception:
                pass
            try:
                await self.launcher.refresh()
            except Exception:
                logger.exception("hub: launcher refresh after widgets ready failed")

    async def _on_launcher_pos_changed(self, key: str | None = None,
                                       scope: str | None = None,
                                       login: str | None = None,
                                       **kwargs) -> None:
        if key != self.WIDGET_KEY or self.launcher is None:
            return
        try:
            if scope == "player" and login:
                await self.launcher.display(player_logins=[login])
            else:
                await self.launcher.refresh()
        except Exception:
            logger.exception("hub: launcher re-display after move failed")

    async def _on_widgets_edit_mode(self, login: str | None = None,
                                    active: bool | None = None, **kwargs) -> None:
        if not login or self.launcher is None:
            return
        try:
            await self.launcher.display(player_logins=[login])
        except Exception:
            logger.exception("hub: launcher re-display on edit-mode toggle failed")

    # ---- registry ------------------------------------------------------

    async def _on_register(self, entry: HubAppEntry | None = None, **kwargs) -> None:
        if entry is None:
            logger.warning("hub: tmsm_hub:register received without 'entry' payload")
            return
        if not isinstance(entry, HubAppEntry):
            logger.warning("hub: tmsm_hub:register payload not a HubAppEntry: %r", entry)
            return
        # Preserve any pending per-player notification counts across re-registers.
        existing = self.entries.get(entry.key)
        if existing is not None and existing.notifications:
            for login, n in existing.notifications.items():
                entry.notifications.setdefault(login, n)
        self.entries[entry.key] = entry
        await self._register_entry_command(entry)
        logger.info("hub: registered '%s' (%s, role=%s)", entry.key, entry.name, entry.role.label)
        if self.hub is not None and self._open_for:
            await self.hub.refresh()

    async def _register_entry_command(self, entry: HubAppEntry) -> None:
        """Auto-register `/command` for an entry that opts in. The handler
        opens the entry's `open` callback for the calling player and
        respects the entry's role-based permission level."""
        if not entry.command or entry.open is None:
            return
        if entry.command in self._registered_commands:
            return
        cmd_name = entry.command.lstrip("/")

        async def _handler(player, data, **kwargs):
            if int(entry.role) > _perms.effective_level(player):
                try:
                    await self.instance.chat(
                        f"$z$f00[hub]$z you don't have permission to use /{cmd_name}.",
                        player,
                    )
                except Exception:
                    pass
                return
            try:
                await entry.open(player)
            except Exception:
                logger.exception("hub: /%s open callback raised", cmd_name)

        try:
            await self.instance.command_manager.register(
                Command(
                    command=cmd_name,
                    target=_handler,
                    description=f"Open the '{entry.name}' hub app.",
                ),
            )
            self._registered_commands.add(entry.command)
        except Exception:
            logger.exception("hub: /%s registration failed", cmd_name)

    async def _on_notify(self, key: str | None = None, login: str | None = None,
                        count: int | None = None, delta: int | None = None,
                        **kwargs) -> None:
        """Update an entry's notification count for one player.

        Payload: `key` (entry.key), `login` (player), plus either:
          - `count` (int): set absolute count (use 0 to clear)
          - `delta` (int): increment/decrement (clamped at 0)
        """
        if not key or not login:
            return
        entry = self.entries.get(key)
        if entry is None:
            return
        if delta is not None:
            new = max(0, entry.notif_for(login) + int(delta))
        elif count is not None:
            new = max(0, int(count))
        else:
            return
        if new == 0:
            entry.notifications.pop(login, None)
        else:
            entry.notifications[login] = new
        if self.hub is not None and login in self._open_for:
            try:
                await self.hub.display(player_logins=[login])
            except Exception:
                logger.exception("hub: notify re-display failed for %s", login)

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
        # Refresh on every open so a /set_server_name takes effect in the title.
        await self._refresh_server_name()
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
        level = _perms.effective_level(player) if player is not None else _perms.effective_level(login)
        level_label = _perms.level_label(player if player is not None else login)

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

        active_tiles = entries_by_role.get(active, [])
        total_pages = max(1, (len(active_tiles) + self.page_size - 1) // self.page_size)
        page = self._page.get((login, active), 1)
        if page < 1:
            page = 1
        if page > total_pages:
            page = total_pages
        self._page[(login, active)] = page

        return {
            "active_tab": active,
            "viewer_level": level,
            "viewer_level_label": level_label,
            "visible_tabs": visible_tabs,
            "entries_by_role": entries_by_role,
            "total_entries": total,
            "page": page,
            "total_pages": total_pages,
            "page_size": self.page_size,
            "server_name": self._server_name,
            "viewer_login": login,
        }

    def _player(self, login: str):
        try:
            for p in self.instance.player_manager.online:
                if p.login == login:
                    return p
        except Exception:
            pass
        return None

    async def _refresh_server_name(self) -> None:
        """Pull the dedicated server's current <server_options><name> via GBX.

        `GetServerOptions` returns the live value (initially loaded from
        `dedicated_cfg.txt`'s `<server_options><name>`). This is the most
        reliable cross-version way to get the configured server name.
        """
        try:
            opts = await self.instance.gbx("GetServerOptions")
        except Exception:
            logger.exception("hub: GetServerOptions failed")
            return
        if not isinstance(opts, dict):
            return
        name = opts.get("Name") or opts.get("CurrentName")
        if name:
            self._server_name = str(name)

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
        # Pagination from ui.pagination(name='pg'): `pg__first`, `pg__prev`,
        # `pg__next`, `pg__last`, `pg__page__<n>`.
        if action.startswith("pg__"):
            await self._handle_pagination(player, action[len("pg__"):])
            return
        logger.debug("hub: unhandled action '%s' from %s", action, player.login)

    async def _handle_pagination(self, player, sub: str) -> None:
        tab = self._active_tab.get(player.login)
        if not tab:
            return
        tiles = [
            e for e in self.entries.values()
            if e.enabled and self._tab_for_role(e.role) == tab
        ]
        total_pages = max(1, (len(tiles) + self.page_size - 1) // self.page_size)
        cur = self._page.get((player.login, tab), 1)
        new = cur
        if sub == "first":
            new = 1
        elif sub == "prev":
            new = max(1, cur - 1)
        elif sub == "next":
            new = min(total_pages, cur + 1)
        elif sub == "last":
            new = total_pages
        elif sub.startswith("page__"):
            try:
                new = int(sub[len("page__"):])
            except ValueError:
                return
            new = max(1, min(total_pages, new))
        if new == cur:
            return
        self._page[(player.login, tab)] = new
        if self.hub is not None:
            try:
                await self.hub.display(player_logins=[player.login])
            except Exception:
                logger.exception("hub: pagination re-display failed")

    def _tab_for_role(self, role) -> str:
        for key, _label, r in _TAB_ORDER:
            if int(r) == int(role):
                return key
        return _TAB_ORDER[0][0]

    async def _open_entry(self, player, key: str) -> None:
        entry = self.entries.get(key)
        if entry is None:
            logger.warning("hub: open requested for unknown key '%s'", key)
            return
        if int(entry.role) > _perms.effective_level(player):
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
