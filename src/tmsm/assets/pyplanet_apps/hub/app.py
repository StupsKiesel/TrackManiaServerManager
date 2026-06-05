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

import asyncio
import logging
import time
from typing import Any

from pyplanet.apps.config import AppConfig
from pyplanet.contrib.command import Command
from pyplanet.core.events import Signal

from .registry import HubAppEntry, Role
from .views import HubLauncherView, HubView

from pyplanet.apps.tmsm.ui import perms as _perms

try:
    from pyplanet.apps.tmsm.widget_engine.registry import (
        AnimDir as _WeAnimDir,
        Animation as _WeAnim,
        DriveMode as _WeDriveMode,
        HideRule as _WeHide,
        WidgetEntry as _WeWidgetEntry,
        WidgetKind as _WeWidgetKind,
    )
    _HAS_WE = True
except Exception:
    _HAS_WE = False

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

    # ── widget_engine contract (the launcher is registered as a widget) ─
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
    WIDGET_ANIM_DIR = "none"
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
        self._server_name_refresh_task: asyncio.Task | None = None
        self._server_name_last_refresh: float = 0.0
        # Track chat commands we've already wired so re-registration is a no-op.
        self._registered_commands: set[str] = set()
        # Resolved on_start: reference to the widget_engine app, or None.
        # When None, the launcher widget is disabled and /hub is the only
        # way to open the hub.
        self.widget_engine_app = None
        self._widget_engine_listeners_wired: bool = False

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
        # Resolve the widget_engine app (optional). When missing the hub
        # is reachable only through the `/hub` chat command \u2014 no launcher.
        try:
            self.widget_engine_app = self.instance.apps.apps.get("widget_engine")
        except Exception:
            self.widget_engine_app = None
        if not _HAS_WE or self.widget_engine_app is None:
            logger.warning(
                "hub: widget_engine not available \u2014 launcher disabled; "
                "use /hub to open the hub.",
            )
            self.launcher = None
        else:
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

        # Show the launcher to everyone (global manialink) \u2014 only when the
        # widget_engine is available.
        if self.launcher is not None:
            await self.launcher.show()
            await self._register_launcher_widget()
        await self._refresh_server_name()
        logger.info(
            "hub: started; launcher %s",
            "visible" if self.launcher is not None else "disabled (no widget_engine)",
        )

    async def on_stop(self) -> None:
        for view in (self.launcher, self.hub):
            if view is None:
                continue
            try:
                await view.destroy()
            except Exception:
                logger.exception("hub: destroy failed")

    # ---- widget_engine contract ---------------------------------------

    @property
    def view(self):
        """Used by the widget_engine to refresh the launcher after a
        position change (engine looks up ``app.view`` to call display)."""
        return self.launcher

    def _anim_offsets(self, anim_dir: str) -> tuple[float, float]:
        if anim_dir == "left":
            return (-500.0, 0.0)
        if anim_dir == "right":
            return (500.0, 0.0)
        if anim_dir == "up":
            return (0.0, 500.0)
        if anim_dir == "down":
            return (0.0, -500.0)
        return (0.0, 0.0)

    def _default_frame_context(self, view_id: str) -> dict[str, Any]:
        anim_off = self._anim_offsets(self.WIDGET_ANIM_DIR)
        return {
            "widget_key":              self.WIDGET_KEY,
            "widget_view_id":          view_id,
            "widget_kind":             "persistent",
            "widget_x":                self.WIDGET_DEFAULT_X,
            "widget_y":                self.WIDGET_DEFAULT_Y,
            "widget_w":                self.WIDGET_DEFAULT_W,
            "widget_h":                self.WIDGET_DEFAULT_H,
            "widget_hide_clauses":     [],
            "widget_hide_raw":         "",
            "widget_anim_dir":         self.WIDGET_ANIM_DIR,
            "widget_anim_duration_ms": self.WIDGET_ANIM_DURATION_MS,
            "widget_anim_in_delay_ms": 0,
            "widget_anim_out_delay_ms": 0,
            "widget_anim_off_x":       anim_off[0],
            "widget_anim_off_y":       anim_off[1],
            "widget_bg_color":         "40404080",
            "widget_strip_color":      "ffae00",
            "widget_strip_edge":       "",
            "widget_strip_thickness":  1.0,
            "widget_edit_mode":        False,
            "widget_debug_mode":       False,
            "widget_debug_status":     "",
            "widget_debug_lines":      [],
            "widget_force_hidden":     False,
            "widget_scale_y":          1.0,
        }

    def frame_context(self, login: str) -> dict[str, Any]:
        """Per-player frame context consumed by ``widget_engine/frame.xml``
        inside ``launcher.xml``. Falls back to defaults if the widget
        engine is not available.
        """
        view_id = self.launcher.id if self.launcher is not None else self.WIDGET_KEY
        ctx = self._default_frame_context(view_id)
        engine = getattr(self.widget_engine_app, "engine", None) if self.widget_engine_app else None
        if engine is None:
            return ctx
        try:
            resolved = engine.resolve(self.WIDGET_KEY, login)
        except Exception:
            resolved = None
        if resolved is None:
            return ctx
        anim_dir = getattr(resolved.anim_dir, "value", resolved.anim_dir)
        anim_off = self._anim_offsets(str(anim_dir))
        try:
            edit_mode = bool(engine.is_editing(login, self.WIDGET_KEY))
        except Exception:
            edit_mode = False
        try:
            debug_mode = bool(engine.is_debug(login, self.WIDGET_KEY))
        except Exception:
            debug_mode = False
        if edit_mode:
            debug_mode = False
        debug_status = ""
        debug_lines: list[Any] = []
        if debug_mode:
            try:
                debug_status = engine.debug_status(login, self.WIDGET_KEY) or ""
                debug_lines = engine.debug_lines(login, self.WIDGET_KEY) or []
            except Exception:
                pass
        ctx.update({
            "widget_x":                 resolved.x,
            "widget_y":                 resolved.y,
            "widget_w":                 resolved.w,
            "widget_h":                 resolved.h,
            "widget_anim_dir":          str(anim_dir),
            "widget_anim_duration_ms":  getattr(resolved, "anim_duration_ms", self.WIDGET_ANIM_DURATION_MS),
            "widget_anim_in_delay_ms":  getattr(resolved, "anim_in_delay_ms", 0),
            "widget_anim_out_delay_ms": getattr(resolved, "anim_out_delay_ms", 0),
            "widget_anim_off_x":        anim_off[0],
            "widget_anim_off_y":        anim_off[1],
            "widget_bg_color":          getattr(resolved, "bg_color", "0000"),
            "widget_strip_color":       getattr(resolved, "strip_color", "fff8"),
            "widget_strip_edge":        getattr(resolved, "strip_edge", "none"),
            "widget_strip_thickness":   getattr(resolved, "strip_thickness", 0.0),
            "widget_edit_mode":         edit_mode,
            "widget_debug_mode":        debug_mode,
            "widget_debug_status":      debug_status,
            "widget_debug_lines":       debug_lines,
        })
        return ctx

    async def _register_launcher_widget(self) -> None:
        """Tell widget_engine about the launcher so it shows up in the
        manager and reacts to position changes. Safe to call multiple
        times \u2014 the signal listeners are wired exactly once via
        :attr:`_widget_engine_listeners_wired`.
        """
        if not _HAS_WE or self.widget_engine_app is None or self.launcher is None:
            return
        entry = _WeWidgetEntry(
            key=self.WIDGET_KEY,
            name=self.WIDGET_NAME,
            description=self.WIDGET_DESCRIPTION,
            icon=self.WIDGET_ICON,
            kind=_WeWidgetKind.PERSISTENT,
            drive_mode=_WeDriveMode.FIXED,
            default_x=self.WIDGET_DEFAULT_X,
            default_y=self.WIDGET_DEFAULT_Y,
            default_w=self.WIDGET_DEFAULT_W,
            default_h=self.WIDGET_DEFAULT_H,
            hide_rule=_WeHide(),
            animation=_WeAnim(
                direction=_WeAnimDir.NONE,
                duration_ms=self.WIDGET_ANIM_DURATION_MS,
            ),
            author="tmsm",
            version="0.1",
        )
        try:
            sig = self.context.signals.get_signal("widget_engine:register")
            await sig.send_robust({"entry": entry, "app": self}, raw=True)
            logger.info("hub: launcher widget registered with widget_engine")
        except KeyError:
            logger.info("hub: widget_engine:register signal not available yet")
        except Exception:
            logger.exception("hub: launcher widget registration failed")
        if self._widget_engine_listeners_wired:
            return
        try:
            self.context.signals.listen(
                "widget_engine:request_register", self._on_widget_engine_request_register,
            )
            self._widget_engine_listeners_wired = True
        except Exception:
            logger.exception("hub: failed to subscribe to widget_engine signals")

    async def _on_widget_engine_request_register(self, **kwargs) -> None:
        await self._register_launcher_widget()
        if self.launcher is not None:
            try:
                await self.launcher.refresh()
            except Exception:
                logger.exception("hub: launcher refresh after widget_engine ready failed")

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
        try:
            await self.hub.display(player_logins=[player.login])
        except Exception:
            logger.exception("hub: display failed for %s", player.login)
        # Refresh server name out-of-band so button feedback is immediate.
        self._schedule_server_name_refresh(player.login)

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
                if self.launcher is not None:
                    msg = (
                        "$z$0afWelcome! Click the $fffHub$0af button "
                        "or type $fff/hub$0af to open the launcher."
                    )
                else:
                    msg = "$z$0afWelcome! Type $fff/hub$0af to open the launcher."
                await self.instance.chat(msg, player)
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

    def _schedule_server_name_refresh(self, login: str | None = None) -> None:
        # Throttle refreshes and avoid piling up concurrent GBX requests.
        if self._server_name_refresh_task is not None and not self._server_name_refresh_task.done():
            return
        if time.monotonic() - self._server_name_last_refresh < 5.0:
            return

        async def _runner() -> None:
            try:
                changed = await self._refresh_server_name()
                if changed and self.hub is not None:
                    if login and login in self._open_for:
                        await self.hub.display(player_logins=[login])
                    elif self._open_for:
                        await self.hub.display(player_logins=list(self._open_for))
            except Exception:
                logger.exception("hub: async server-name refresh failed")
            finally:
                self._server_name_last_refresh = time.monotonic()
                self._server_name_refresh_task = None

        self._server_name_refresh_task = asyncio.create_task(_runner())

    async def _refresh_server_name(self) -> bool:
        """Pull the dedicated server's current <server_options><name> via GBX.

        `GetServerOptions` returns the live value (initially loaded from
        `dedicated_cfg.txt`'s `<server_options><name>`). This is the most
        reliable cross-version way to get the configured server name.
        """
        try:
            opts = await asyncio.wait_for(self.instance.gbx("GetServerOptions"), timeout=0.75)
        except asyncio.TimeoutError:
            # Keep UI responsive when GBX is slow/unreachable.
            return False
        except Exception:
            logger.exception("hub: GetServerOptions failed")
            return False
        if not isinstance(opts, dict):
            return False
        name = opts.get("Name") or opts.get("CurrentName")
        if name and str(name) != self._server_name:
            self._server_name = str(name)
            return True
        return False

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
