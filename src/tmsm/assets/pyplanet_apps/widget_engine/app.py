"""Widget engine host app.

Owns the registry, the `WidgetEngine` façade, storage, and the inbound
signal contract.

Signals exposed (namespace `widget_engine`):

    register             — addon hands over a WidgetEntry (+ self)
    request_register     — engine asks late-loaded addons to register again
    refresh              — re-render one or all widgets

Slice 2: storage-backed positions/behaviour via the `we_widget` table.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

from pyplanet.apps.config import AppConfig
from pyplanet.contrib.command import Command
from pyplanet.core.events import Signal
from pyplanet.views.template import TemplateView

from .engine import WidgetEngine
from .registry import GbxReplacement, Phase, WidgetEntry, WidgetKind
from .storage import WidgetStorage
from .views import WidgetEditOverlayView, WidgetEngineManagerView

_MANIALINK_OPEN_RE = re.compile(r"<\s*manialink\b[^>]*>", re.IGNORECASE)
_MANIALINK_CLOSE_RE = re.compile(r"<\s*/\s*manialink\s*>", re.IGNORECASE)

logger = logging.getLogger(__name__)


# Signals emitted/consumed by the engine. The setup pass registers all of
# them up front so listeners attaching before the engine app is loaded
# don't race the `get_signal` call.
_ENGINE_SIGNALS = (
    "register",
    "request_register",
    "refresh",
)

_TMSM_WIDGETS_SIGNALS = (
    "register",
    "request_register",
    "refresh",
    "position_changed",
    "runtime_override_set",
    "runtime_override_clear",
    "runtime_override_clear_owner",
    "runtime_layout_apply",
    "runtime_layout_clear_owner",
    "runtime_layout_clear_all",
)


# Phase signals to listen for (slice 3). Order does not matter; the engine
# only refreshes when the resolved phase actually changes.
_PHASE_SIGNALS: tuple[tuple[str, Phase], ...] = (
    ("maniaplanet:loading_map_start", Phase.LOADING_MAP),
    ("maniaplanet:map_start",         Phase.PRE_RACE),
    ("trackmania:warmup_start",       Phase.WARMUP),
    ("trackmania:warmup_end",         Phase.PRE_RACE),
    ("trackmania:start_countdown",    Phase.PRE_RACE),
    ("trackmania:start_line",         Phase.IN_RACE),
    ("maniaplanet:podium_start",      Phase.IN_PODIUM),
    ("maniaplanet:podium_end",        Phase.POST_RACE),
    ("maniaplanet:map_end",           Phase.POST_RACE),
)


class WidgetsApp(AppConfig):
    name = "pyplanet.apps.tmsm.widget_engine"
    label = "widget_engine"

    app_dependencies = ["core.maniaplanet"]
    game_dependencies = ["trackmania", "trackmania_next"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Installed widgets — have a `we_widget` row and render in-game.
        self._entries: dict[str, WidgetEntry] = {}
        # Available widgets — every entry an addon has announced via the
        # `widget_engine:register` signal. Superset of `_entries`. Used by
        # the Add picker to offer uninstalled widgets for re-installation.
        self._available: dict[str, WidgetEntry] = {}
        # Addons set on_start of their own AppConfig — the host stores the
        # reference so the engine can call `view.display()` on refresh.
        self._widget_apps: dict[str, AppConfig] = {}
        self.storage = WidgetStorage(self.instance)
        self.engine = WidgetEngine(self)
        self.manager_view: WidgetEngineManagerView | None = None
        self.edit_overlay_view: WidgetEditOverlayView | None = None
        # GBX manialink id replacements: manialink_id -> widget key.
        self._replacements: dict[str, str] = {}
        # Per-player opt-out for replacement widgets: key -> {login,...}.
        self._replace_disabled: dict[str, set[str]] = {}
        # Server-wide override of `hide_ui_modules` per replacement widget.
        # widget_key -> tuple(ids). Missing key = inherit addon manifest;
        # empty tuple = explicit "hide nothing".
        self._ui_modules_overrides: dict[str, tuple[str, ...]] = {}
        # owner -> set of (login, key) transient overlays set through
        # tmsm_widgets:runtime_override_set with a specific login.
        self._runtime_owner_transients: dict[str, set[tuple[str, str]]] = {}

    # ---- monitor compatibility API -----------------------------------

    def get_ui_offset(self, login: str) -> dict[str, float]:
        return self.engine.get_ui_offset(login)

    async def set_ui_offset(self, login: str, x: float, y: float) -> None:
        await self.engine.set_ui_offset(login, x, y)

    async def clear_ui_offset(self, login: str) -> None:
        await self.engine.clear_ui_offset(login)

    def get_ui_stretch(self, login: str) -> float:
        return self.engine.get_ui_stretch(login)

    async def set_ui_stretch(self, login: str, value: float) -> None:
        await self.engine.set_ui_stretch(login, value)

    async def on_init(self) -> None:
        # Register our signals BEFORE any other app's on_start runs, so
        # listeners in other addons (hub, notification_engine, widgets)
        # bind directly instead of going through pyplanet's broken
        # `finish_reservations` path (Signal has no .connect method).
        for code in _ENGINE_SIGNALS:
            try:
                self.context.signals.register_signal(
                    Signal(code=code, namespace="widget_engine")
                )
            except Exception:
                logger.debug("widget_engine: signal %s already registered", code)
        for code in _TMSM_WIDGETS_SIGNALS:
            try:
                self.context.signals.register_signal(
                    Signal(code=code, namespace="tmsm_widgets")
                )
            except Exception:
                logger.debug("widget_engine: legacy signal %s already registered", code)

    async def on_start(self) -> None:
        self.context.signals.listen("widget_engine:register", self._on_register)
        self.context.signals.listen("widget_engine:refresh", self._on_refresh)
        # Legacy/compat namespace bridge.
        self.context.signals.listen("tmsm_widgets:register", self._on_register)
        self.context.signals.listen("tmsm_widgets:refresh", self._on_refresh)
        self.context.signals.listen("tmsm_widgets:runtime_override_set", self._on_runtime_override_set)
        self.context.signals.listen("tmsm_widgets:runtime_override_clear", self._on_runtime_override_clear)
        self.context.signals.listen("tmsm_widgets:runtime_override_clear_owner", self._on_runtime_override_clear_owner)
        self.context.signals.listen("tmsm_widgets:runtime_layout_apply", self._on_runtime_layout_apply)
        self.context.signals.listen("tmsm_widgets:runtime_layout_clear_owner", self._on_runtime_layout_clear_owner)
        self.context.signals.listen("tmsm_widgets:runtime_layout_clear_all", self._on_runtime_layout_clear_all)
        # Race phase tracking (slice 3). The handler closes over the Phase
        # value so we don't need one handler per phase.
        for code, ph in _PHASE_SIGNALS:
            self.context.signals.listen(code, self._make_phase_handler(ph))
        try:
            await self.storage.load()
        except Exception:
            logger.exception("widget_engine: storage load failed; defaults only")
        # Hydrate engine-wide flags from persisted settings.
        try:
            top = self.storage.setting_get("strip_prefer_top")
            if top is not None:
                self.engine.strip_prefer_top = top in ("1", "true", "True")
            thick = self.storage.setting_get("strip_thickness")
            if thick is not None:
                self.engine.strip_thickness = float(thick)
        except Exception:
            logger.exception("widget_engine: settings hydrate failed")
        # Hydrate per-player replacement opt-out lists.
        try:
            for skey, sval in (self.storage.settings_all() or {}).items():
                if not skey.startswith("replace_disabled:"):
                    continue
                widget_key = skey.split(":", 1)[1]
                logins = {l for l in (sval or "").split(",") if l}
                if logins:
                    self._replace_disabled[widget_key] = logins
        except Exception:
            logger.exception("widget_engine: replacement opt-outs hydrate failed")
        # Hydrate per-replacement UI module overrides (JSON list).
        try:
            for skey, sval in (self.storage.settings_all() or {}).items():
                if not skey.startswith("repl_ui_modules:"):
                    continue
                widget_key = skey.split(":", 1)[1]
                try:
                    parsed = json.loads(sval or "[]")
                except Exception:
                    logger.exception(
                        "widget_engine: repl_ui_modules JSON decode '%s' failed",
                        skey,
                    )
                    continue
                if isinstance(parsed, list):
                    self._ui_modules_overrides[widget_key] = tuple(
                        str(x) for x in parsed if x
                    )
        except Exception:
            logger.exception("widget_engine: UI modules overrides hydrate failed")
        # Player connect: re-assert replacements for that player after the
        # original manialink lands. Each replacement entry carries its own
        # delay so addons can tune ordering vs PyPlanet's own override.
        try:
            self.context.signals.listen(
                "maniaplanet:player_connect", self._on_player_connect
            )
        except Exception:
            logger.exception(
                "widget_engine: player_connect listen failed"
            )
        # Ask any widget apps that started before us to (re-)register now.
        try:
            sig = self.context.signals.get_signal("widget_engine:request_register")
            await sig.send_robust({}, raw=True)
        except Exception:
            logger.exception("widget_engine: emit request_register failed")
        try:
            sig = self.context.signals.get_signal("tmsm_widgets:request_register")
            await sig.send_robust({}, raw=True)
        except Exception:
            logger.debug("widget_engine: emit legacy request_register failed")
        # Slice 8: manager view + admin command + hub tile. All three need
        # the tmsm_ui app to be loaded — without it the manager view can't
        # be constructed and `//widget` would have nothing to open.
        try:
            ui_app = self.instance.apps.apps.get("tmsm_ui")
        except Exception:
            ui_app = None
        if ui_app is None:
            logger.warning(
                "widget_engine: tmsm_ui not loaded — //widget command and "
                "manager view disabled; engine runs in render-only mode.",
            )
            self.manager_view = None
            self.edit_overlay_view = None
        else:
            try:
                cmd = Command(
                    command="widget", target=self._cmd_widget,
                    admin=True,
                    description="Widget engine admin: list | set | disable | enable | reset",
                )
                cmd.add_param("args", nargs="*", type=str, required=False)
                await self.instance.command_manager.register(cmd)
            except Exception:
                logger.exception("widget_engine: /widget command registration failed")
            try:
                self.manager_view = WidgetEngineManagerView(self)
            except Exception:
                logger.exception("widget_engine: manager view init failed")
                self.manager_view = None
            try:
                self.edit_overlay_view = WidgetEditOverlayView(self)
            except Exception:
                logger.exception("widget_engine: edit overlay view init failed")
                self.edit_overlay_view = None
            await self._register_hub_tile()
        # Bootstrap phase: if no signal has fired yet (server already running
        # mid-session when engine starts), default to PRE_RACE so consumers
        # don't see "?" until the next lifecycle signal.
        if self.engine.current_phase is None:
            try:
                await self.engine.set_phase(Phase.PRE_RACE)
            except Exception:
                logger.exception("widget_engine: phase bootstrap failed")
        # Delayed re-render after startup: when PyPlanet restarts mid-race,
        # widget apps' view.show() may run before `player_manager.online`
        # is hydrated, so the manialinks are sent to nobody. Re-display
        # everything once the player list has had time to settle.
        try:
            asyncio.ensure_future(self._delayed_initial_widget_push())
        except Exception:
            logger.exception("widget_engine: schedule delayed widget push failed")
        logger.info("widget_engine: ready (slice 12 — settings + copy-to)")

    async def _delayed_initial_widget_push(self) -> None:
        """Periodic re-render after engine start. The widget addons may
        register and call view.show() before the dedicated server has
        returned its initial GetPlayerList, leaving manialinks delivered
        to nobody. We retry every few seconds until at least one
        successful per-player render lands, since `maniaplanet:player_connect`
        does NOT fire for players that were already on the dedicated
        server when PyPlanet restarted.
        """
        for delay in (1.0, 2.0, 4.0, 6.0, 10.0):
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            logins = self._online_logins()
            keys = list(self._widget_apps.keys())
            logger.debug(
                "widget_engine: delayed push attempt — players=%d widgets=%d",
                len(logins), len(keys),
            )
            if not logins or not keys:
                continue
            try:
                await self._refresh_all()
                logger.debug("widget_engine: delayed push succeeded")
                return
            except Exception:
                logger.exception("widget_engine: delayed widget push failed")

    # ---- hub tile -----------------------------------------------------

    async def _register_hub_tile(self) -> None:
        # Optional integration: only attempt registration when the hub app
        # itself is loaded. Without it the tile would be a no-op and the
        # tmsm_hub:register signal would never be consumed.
        try:
            hub_app = self.instance.apps.apps.get("tmsm_hub")
        except Exception:
            hub_app = None
        if hub_app is None:
            logger.info("widget_engine: tmsm_hub app not loaded; skipping hub tile (use //widget)")
            return
        try:
            from pyplanet.apps.tmsm.hub.registry import HubAppEntry, Role, Status
        except Exception:
            logger.debug("widget_engine: hub registry not importable; skipping tile")
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("widget_engine: tmsm_hub:register not available")
            return
        try:
            entry = HubAppEntry(
                key="widget_engine",
                name="Widget Engine",
                icon="cog",
                color="15f",
                description="Configure registered widgets per phase",
                role=Role.ADMIN,
                status=Status.WIP,
                order=210,
                command="we",
                author="tmsm",
                version="0.1",
                open=self._hub_open,
            )
            await sig.send_robust({"entry": entry}, raw=True)
        except Exception:
            logger.exception("widget_engine: hub registration failed")

    async def _hub_open(self, player) -> None:
        await self._open_manager(player.login)

    async def _open_manager(self, login: str) -> None:
        if self.manager_view is None:
            await self.instance.chat("$f80widget_engine: manager view unavailable", login)
            return
        try:
            await self.manager_view.display(player_logins=[login])
            self.manager_view._visible = True
            self.manager_view._visible_logins.add(login)
        except Exception:
            logger.exception("widget_engine: open manager failed")

    # ---- edit overlay failsafe ---------------------------------------

    def edit_overlay_context(self, login: str) -> dict[str, object]:
        key = self.engine._editing.get(login)
        if not key:
            return {
                "overlay_enabled": False,
                "overlay_x": 0.0,
                "overlay_y": 0.0,
                "overlay_w": 1.0,
                "overlay_h": 1.0,
            }
        resolved = self.engine.resolve(key, login)
        if resolved is None:
            return {
                "overlay_enabled": False,
                "overlay_x": 0.0,
                "overlay_y": 0.0,
                "overlay_w": 1.0,
                "overlay_h": 1.0,
            }
        return {
            "overlay_enabled": True,
            "overlay_x": float(resolved.x),
            "overlay_y": float(resolved.y),
            "overlay_w": max(1.0, float(resolved.w)),
            "overlay_h": max(1.0, float(resolved.h)),
        }

    async def refresh_edit_overlay(self, login: str) -> None:
        if not login or self.edit_overlay_view is None:
            return
        try:
            await self.edit_overlay_view.display(player_logins=[login])
        except Exception:
            logger.exception("widget_engine: refresh edit overlay failed")

    async def show_edit_overlay(self, login: str) -> None:
        await self.refresh_edit_overlay(login)

    async def hide_edit_overlay(self, login: str) -> None:
        if not login or self.edit_overlay_view is None:
            return
        try:
            await TemplateView.hide(self.edit_overlay_view, player_logins=[login])
        except Exception:
            logger.exception("widget_engine: hide edit overlay failed")
            await self.instance.chat("$f80widget_engine: failed to open manager", login)

    # ---- registration -------------------------------------------------

    async def _on_register(self, signal=None, source=None, **payload):  # noqa: ARG002
        entry = payload.get("entry")
        widget_app = payload.get("app")
        if not isinstance(entry, WidgetEntry):
            logger.warning("widget_engine: register: invalid payload %r", payload)
            return
        # Always announce as available so the Add picker can offer it.
        first_available = entry.key not in self._available
        self._available[entry.key] = entry
        if widget_app is not None:
            self._widget_apps[entry.key] = widget_app
        if first_available:
            logger.debug(
                "widget_engine: available '%s' (%s, kind=%s)",
                entry.key, entry.name, entry.kind.value,
            )
        # Decide whether this widget is installed:
        #   - row present in storage → user installed it previously, keep it
        #   - tombstone present     → user uninstalled it, leave available only
        #   - neither               → first-time discovery, auto-install
        has_row = self.storage.get(entry.key) is not None
        if has_row:
            self._register_entry(entry)
            return
        if self.storage.is_removed(entry.key):
            return  # user opt-out; the Add picker is the only way back in
        # First-time auto-install.
        try:
            await self.storage.ensure_row(entry)
        except Exception:
            logger.exception("widget_engine: ensure_row '%s' failed", entry.key)
            return
        self._register_entry(entry)

    def _register_entry(self, entry: WidgetEntry) -> None:
        first = entry.key not in self._entries
        self._entries[entry.key] = entry
        logger.debug(
            "widget_engine: %s '%s' (%s, kind=%s)",
            "installed" if first else "updated",
            entry.key, entry.name, entry.kind.value,
        )
        # GBX manialink-id replacement: claim the id and schedule a push
        # to all currently-online players. New connects get pushed via
        # _on_player_connect.
        repl = entry.gbx_replace
        if isinstance(repl, GbxReplacement) and repl.manialink_id:
            prev = self._replacements.get(repl.manialink_id)
            if prev and prev != entry.key:
                logger.warning(
                    "widget_engine: manialink id '%s' claimed by '%s' "
                    "already owned by '%s'; new owner wins",
                    repl.manialink_id, entry.key, prev,
                )
            self._replacements[repl.manialink_id] = entry.key
            try:
                asyncio.ensure_future(
                    self._delayed_initial_push(entry.key)
                )
            except Exception:
                logger.exception(
                    "widget_engine: schedule initial replacement push for '%s' failed",
                    entry.key,
                )
            if repl.manialink_id and self.get_effective_hide_ui_modules(entry.key):
                try:
                    asyncio.ensure_future(
                        self._reconcile_all_ui_modules()
                    )
                except Exception:
                    logger.exception(
                        "widget_engine: schedule UI modules reconcile for '%s' failed",
                        entry.key,
                    )

    async def install_widget(self, key: str) -> bool:
        """Promote an available widget to installed: clear tombstone, seed
        a default `we_widget` row, mirror into `_entries`, re-render.

        Returns True on success, False if the key is not available.
        """
        entry = self._available.get(key)
        if entry is None:
            return False
        await self.storage.clear_tombstone(key)
        try:
            await self.storage.ensure_row(entry)
        except Exception:
            logger.exception("widget_engine: install '%s' ensure_row failed", key)
            return False
        self._register_entry(entry)
        await self._redisplay(key)
        return True

    async def uninstall_widget(self, key: str) -> bool:
        """Demote an installed widget to available-only: hide UI, wipe
        `we_widget` + every `we_phase_override` row, write a tombstone,
        drop from `_entries`. The addon's runtime registration is kept so
        the Add picker can still offer the widget without a restart.
        """
        if key not in self._entries:
            return False
        widget_app = self._widget_apps.get(key)
        view = getattr(widget_app, "view", None) if widget_app else None
        if view is not None:
            try:
                await view.hide()
            except Exception:
                logger.exception("widget_engine: uninstall '%s' hide failed", key)
        try:
            await self.storage.delete_widget(key)
        except Exception:
            logger.exception("widget_engine: uninstall '%s' delete failed", key)
            return False
        await self.storage.add_tombstone(key)
        self._entries.pop(key, None)
        logger.info("widget_engine: uninstalled '%s'", key)
        return True

    # ---- refresh ------------------------------------------------------

    async def _on_refresh(self, signal=None, source=None, **payload):  # noqa: ARG002
        key = payload.get("key")
        if key and key in self._widget_apps:
            keys = [key]
        else:
            keys = list(self._widget_apps.keys())
        logins = self._online_logins()
        for k in keys:
            app = self._widget_apps.get(k)
            view = getattr(app, "view", None) if app else None
            if view is None:
                continue
            entry = self._entries.get(k)
            # POPUP widgets are shown on demand (or are GBX-replacement
            # only). Routine refreshes must not push their persistent
            # frame manialink — that would render them as normal widgets
            # alongside their GBX replacement.
            if entry is not None and entry.kind == WidgetKind.POPUP:
                continue
            try:
                if logins:
                    await view.display(player_logins=logins)
                else:
                    await view.display()
            except Exception:
                logger.exception("widget_engine: refresh '%s' failed", k)

    @staticmethod
    def _unwrap_payload(kwargs: dict) -> dict:
        src = kwargs.get("source")
        if isinstance(src, dict):
            return src
        out = dict(kwargs)
        out.pop("signal", None)
        out.pop("source", None)
        return out

    async def _on_runtime_override_set(self, **kwargs):
        payload = self._unwrap_payload(kwargs)
        owner = str(payload.get("owner") or "").strip()
        key = str(payload.get("widget_key") or payload.get("key") or "").strip()
        login = str(payload.get("login") or "").strip()
        if not owner or key not in self._entries:
            return
        patch: dict[str, object] = {}
        for col in ("x", "y", "w", "h", "drive_mode", "anim_dir", "anim_duration_ms"):
            if col in payload and payload[col] is not None:
                patch[col] = payload[col]
        delay = payload.get("anim_delay_ms")
        if delay is not None:
            patch["anim_in_delay_ms"] = delay
            patch["anim_out_delay_ms"] = delay
        if payload.get("enabled") is not None:
            patch["disabled"] = not bool(payload.get("enabled"))
        pos = payload.get("pos")
        if isinstance(pos, dict):
            for col in ("x", "y", "w", "h"):
                if pos.get(col) is not None:
                    patch[col] = pos.get(col)
        if not patch:
            return

        if login:
            await self.engine.set_transient(login, key, patch, ttl_s=None)
            self._runtime_owner_transients.setdefault(owner, set()).add((login, key))
            return
        await self.engine.set_runtime_layout(owner, key, patch)

    async def _on_runtime_override_clear(self, **kwargs):
        payload = self._unwrap_payload(kwargs)
        owner = str(payload.get("owner") or "").strip()
        key = str(payload.get("widget_key") or payload.get("key") or "").strip()
        login = str(payload.get("login") or "").strip()
        if not owner or key not in self._entries:
            return
        if login:
            await self.engine.clear_transient(login, key)
            refs = self._runtime_owner_transients.get(owner)
            if refs is not None:
                refs.discard((login, key))
                if not refs:
                    self._runtime_owner_transients.pop(owner, None)
            return
        await self.engine.clear_runtime_layout(owner, key)

    async def _on_runtime_override_clear_owner(self, **kwargs):
        payload = self._unwrap_payload(kwargs)
        owner = str(payload.get("owner") or "").strip()
        if not owner:
            return
        refs = list(self._runtime_owner_transients.pop(owner, set()))
        for login, key in refs:
            try:
                await self.engine.clear_transient(login, key)
            except Exception:
                logger.exception(
                    "widget_engine: clear owner transient '%s' '%s'/'%s' failed",
                    owner, login, key,
                )
        await self.engine.clear_runtime_owner(owner)

    async def _on_runtime_layout_apply(self, **kwargs):
        payload = self._unwrap_payload(kwargs)
        owner = str(payload.get("owner") or "").strip()
        rows = payload.get("widgets") or payload.get("entries") or []
        if not owner or not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("widget_key") or row.get("key") or "").strip()
            if key not in self._entries:
                continue
            patch = {
                k: v for k, v in row.items()
                if k in {
                    "x", "y", "w", "h",
                    "drive_mode", "anim_dir",
                    "anim_duration_ms", "anim_in_delay_ms", "anim_out_delay_ms",
                    "disabled",
                }
            }
            if not patch:
                continue
            await self.engine.set_runtime_layout(owner, key, patch)

    async def _on_runtime_layout_clear_owner(self, **kwargs):
        payload = self._unwrap_payload(kwargs)
        owner = str(payload.get("owner") or "").strip()
        if not owner:
            return
        await self.engine.clear_runtime_owner(owner)

    async def _on_runtime_layout_clear_all(self, **kwargs):  # noqa: ARG002
        await self.engine.clear_runtime_all()

    async def _redisplay(self, key: str) -> None:
        app = self._widget_apps.get(key)
        view = getattr(app, "view", None) if app else None
        entry = self._entries.get(key)
        is_popup = entry is not None and entry.kind == WidgetKind.POPUP
        if view is not None and not is_popup:
            # Always target each online login explicitly. TM2020 ignores
            # broadcast SendDisplayManialinkPage for views that were never
            # delivered to the player as a per-login manialink before, so
            # the global broadcast path leaves widgets invisible until a
            # per-player display is forced. Per-player display works in
            # every scenario, so we use it as the single path.
            logins = self._online_logins()
            resolved = self.engine.resolve(key, logins[0]) if logins else None
            logger.debug(
                "widget_engine: redisplay '%s' players=%d disabled=%s phase=%s",
                key, len(logins),
                resolved.disabled if resolved else "n/a",
                self.engine.current_phase.value if self.engine.current_phase else "?",
            )
            try:
                if logins:
                    await view.display(player_logins=logins)
                else:
                    await view.display()
            except Exception:
                logger.exception("widget_engine: redisplay '%s' failed", key)
            # Per-player re-render for anyone whose own view of this widget
            # depends on per-login state (debug overlay, edit mode). The
            # global display path uses login="" which strips that state.
            seen: set[str] = set()
            for login in self._debug_logins_for(key):
                if login in seen:
                    continue
                seen.add(login)
                await self._redisplay_for(key, login)
            for login, editing_key in self.engine._editing.items():
                if editing_key != key or login in seen:
                    continue
                seen.add(login)
                await self._redisplay_for(key, login)
        # GBX replacements live in their own manialink, not in the
        # widget engine's PyPlanet view. Push so editor changes
        # (position/size/anim) take effect immediately.
        if entry and entry.gbx_replace:
            try:
                await self.push_replacement(key)
            except Exception:
                logger.exception(
                    "widget_engine: redisplay push replacement '%s' failed",
                    key,
                )

    def _debug_logins_for(self, key: str) -> list[str]:
        logins: list[str] = []
        for login, keys in self.engine._debug.items():
            if "*" in keys or key in keys:
                logins.append(login)
        return logins

    async def _redisplay_for(self, key: str, login: str) -> None:
        app = self._widget_apps.get(key)
        view = getattr(app, "view", None) if app else None
        if view is None or not login:
            return
        entry = self._entries.get(key)
        if entry is not None and entry.kind == WidgetKind.POPUP:
            return
        try:
            await view.display(player_logins=[login])
        except Exception:
            logger.exception("widget_engine: redisplay '%s' for '%s' failed", key, login)

    async def _refresh_all(self) -> None:
        for k in list(self._widget_apps.keys()):
            await self._redisplay(k)
        # Re-push every active manialink replacement after a refresh /
        # phase change. Default-mode UI often resets on phase boundaries.
        for key in list(self._replacements.values()):
            try:
                await self.push_replacement(key)
            except Exception:
                logger.exception(
                    "widget_engine: refresh push replacement '%s' failed", key,
                )
            try:
                await self._reconcile_all_ui_modules()
            except Exception:
                logger.exception(
                    "widget_engine: refresh reconcile UI modules for '%s' failed",
                    key,
                )

    async def _refresh_phase_change(
        self, outgoing: set[str], incoming: set[str],
    ) -> None:
        """Refresh ONLY widgets whose visibility actually changed at the
        phase boundary. Continuing widgets keep their existing manialink
        and running ManiaScript — re-displaying them would replace the
        manialink and cause a visible flicker for every widget on screen.
        """
        affected = outgoing | incoming
        for k in list(self._widget_apps.keys()):
            if k not in affected:
                continue
            await self._redisplay(k)
        # GBX replacements: re-push only the affected ones. Continuing
        # replacements (e.g. an active podium countdown) must not be
        # rebuilt because their baked-in remaining-ms / client-side
        # state would reset.
        for key in list(self._replacements.values()):
            if key not in affected:
                continue
            try:
                await self.push_replacement(key)
            except Exception:
                logger.exception(
                    "widget_engine: phase-change push replacement '%s' failed",
                    key,
                )
            try:
                await self._reconcile_all_ui_modules()
            except Exception:
                logger.exception(
                    "widget_engine: phase-change reconcile UI modules for '%s' failed",
                    key,
                )

    # ---- gbx manialink-id replacement --------------------------------

    def _online_logins(self) -> list[str]:
        try:
            return [
                p.login for p in list(self.instance.player_manager.online)
                if getattr(p, "login", None)
            ]
        except Exception:
            return []

    def is_replacement_enabled(self, login: str, key: str) -> bool:
        if not login or key not in self._entries:
            return False
        return login not in self._replace_disabled.get(key, set())

    def has_ui_modules_override(self, key: str) -> bool:
        return key in self._ui_modules_overrides

    def get_effective_hide_ui_modules(self, key: str) -> tuple[str, ...]:
        """Effective list of UI module ids to hide for replacement `key`.
        Returns the server-wide override if one is set (including an
        explicit empty tuple), otherwise falls back to the addon's
        manifest declaration."""
        if key in self._ui_modules_overrides:
            return self._ui_modules_overrides[key]
        entry = self._entries.get(key)
        if entry is None or not entry.gbx_replace:
            return ()
        return tuple(entry.gbx_replace.hide_ui_modules or ())

    async def set_ui_modules_override(
        self, key: str, modules: list[str] | tuple[str, ...] | None,
    ) -> None:
        """Persist or clear the server-wide UI modules override for
        replacement `key`. `None` resets to the addon manifest default.
        Applies the visibility delta in-flight: ids that were hidden but
        no longer are get re-shown; new ids get hidden."""
        entry = self._entries.get(key)
        if entry is None or not entry.gbx_replace:
            return
        previous = self.get_effective_hide_ui_modules(key)
        if modules is None:
            self._ui_modules_overrides.pop(key, None)
            try:
                await self.storage.setting_delete(f"repl_ui_modules:{key}")
            except Exception:
                logger.exception(
                    "widget_engine: clear repl_ui_modules '%s' failed", key,
                )
        else:
            cleaned = tuple(dict.fromkeys(str(m).strip() for m in modules if str(m).strip()))
            self._ui_modules_overrides[key] = cleaned
            try:
                await self.storage.setting_set(
                    f"repl_ui_modules:{key}", json.dumps(list(cleaned)),
                )
            except Exception:
                logger.exception(
                    "widget_engine: persist repl_ui_modules '%s' failed", key,
                )
        new_effective = self.get_effective_hide_ui_modules(key)
        prev_set = set(previous)
        new_set = set(new_effective)
        to_show = tuple(prev_set - new_set)
        to_hide = tuple(new_set - prev_set)
        any_enabled = any(
            self.is_replacement_enabled(login, k)
            for k in (key,)
            for login in self._online_logins()
        )
        if to_show:
            await self._apply_ui_modules_visibility(to_show, visible=True)
        if to_hide and any_enabled:
            await self._apply_ui_modules_visibility(to_hide, visible=False)
        # Normalize globally in case multiple replacement keys overlap on
        # the same UI module ids.
        await self._reconcile_all_ui_modules()

    async def set_replacement_enabled(
        self, login: str, key: str, enabled: bool,
    ) -> None:
        if not login or key not in self._entries:
            return
        entry = self._entries[key]
        if not (entry.gbx_replace and entry.gbx_replace.manialink_id):
            return
        disabled = self._replace_disabled.setdefault(key, set())
        changed = (login in disabled) if enabled else (login not in disabled)
        if enabled:
            disabled.discard(login)
        else:
            disabled.add(login)
        if changed:
            try:
                await self.storage.setting_set(
                    f"replace_disabled:{key}", ",".join(sorted(disabled)),
                )
            except Exception:
                logger.exception(
                    "widget_engine: persist replace_disabled '%s' failed", key,
                )
        await self.push_replacement(key, logins=[login])
        if self.get_effective_hide_ui_modules(key):
            await self._reconcile_all_ui_modules()

    async def _on_player_connect(self, player=None, **kwargs):  # noqa: ARG002
        login = getattr(player, "login", None)
        if not login:
            return
        # Re-display every registered widget to the connecting player. The
        # original view.show() at engine startup may have run before this
        # player's login was known, so the manialink was never sent.
        for key in list(self._widget_apps.keys()):
            try:
                await self._redisplay_for(key, login)
            except Exception:
                logger.exception(
                    "widget_engine: on_connect redisplay '%s' for '%s' failed",
                    key, login,
                )
        if not self._replacements:
            return
        # Re-assert visibility policy too. Do not blindly hide: some widgets
        # may be disabled for all online players and their modules must stay
        # visible in that case.
        try:
            await self._reconcile_all_ui_modules()
        except Exception:
            logger.exception("widget_engine: on_connect reconcile UI modules failed")
        # Push each replacement after its own delay so the original
        # manialink lands first and our XML overwrites it client-side.
        for key in list(self._replacements.values()):
            entry = self._entries.get(key)
            if entry is None or not entry.gbx_replace:
                continue
            delay = float(entry.gbx_replace.connect_delay_s or 0.0)
            asyncio.ensure_future(
                self._delayed_push_replacement(key, login, delay)
            )

    async def _delayed_push_replacement(
        self, key: str, login: str, delay: float,
    ) -> None:
        if delay > 0:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
        try:
            await self.push_replacement(key, logins=[login])
        except Exception:
            logger.exception(
                "widget_engine: delayed push '%s' for '%s' failed", key, login,
            )

    async def _delayed_initial_push(self, key: str) -> None:
        """Initial push runs after PyPlanet startup. At that moment
        `player_manager.online` may not be hydrated yet, so an immediate
        push would target an empty list and the player would have to
        manually refresh. Wait a few seconds, then retry; also re-push
        once more later as a safety net for slow startups."""
        for delay in (2.0, 6.0):
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            try:
                logins = self._online_logins()
                if not logins:
                    continue
                await self.push_replacement(key)
                return
            except Exception:
                logger.exception(
                    "widget_engine: initial push '%s' failed", key,
                )
                return

    async def push_replacement(
        self, key: str, logins: Optional[list[str]] = None,
    ) -> None:
        """Render `key`'s replacement XML and push it via GBX, owning the
        configured manialink id. When `logins` is None, pushes to every
        currently-online player (per-player to allow per-login data)."""
        entry = self._entries.get(key)
        if entry is None or not entry.gbx_replace:
            return
        manialink_id = entry.gbx_replace.manialink_id
        if not manialink_id:
            return
        widget_app = self._widget_apps.get(key)
        builder = getattr(widget_app, "build_replacement_xml", None)
        if widget_app is None or not callable(builder):
            logger.warning(
                "widget_engine: '%s' missing build_replacement_xml; skip push",
                key,
            )
            return
        targets = list(logins) if logins is not None else self._online_logins()
        # Out-of-phase: clear the replacement instead of rebuilding it.
        # The current phase may be None (host hasn't reported one yet); in
        # that case fall through and push normally.
        out_of_phase = (
            entry.visible_phases is not None
            and self.engine.current_phase is not None
            and self.engine.current_phase not in entry.visible_phases
        )
        for login in targets:
            if not login:
                continue
            resolved = self.engine.resolve(key, login)
            # Per-player opt-out OR out-of-phase: send an empty manialink
            # so the override is cleared. The default UI will not
            # necessarily come back until the next mode-script refresh.
            if out_of_phase or (resolved is not None and bool(resolved.disabled)) or not self.is_replacement_enabled(login, key):
                xml = f'<manialink id="{manialink_id}" version="3"></manialink>'
                if out_of_phase:
                    logger.debug(
                        "widget_engine: clearing replacement '%s' id=%s for %s "
                        "(out of phase: current=%s allowed=%s)",
                        key, manialink_id, login,
                        self.engine.current_phase.value if self.engine.current_phase else "?",
                        ",".join(p.value for p in (entry.visible_phases or ())),
                    )
            else:
                try:
                    body = await builder(login)
                except Exception:
                    logger.exception(
                        "widget_engine: build_replacement_xml '%s' for '%s' failed",
                        key, login,
                    )
                    continue
                xml = self._wrap_replacement_xml(body or "", manialink_id)
                if entry.gbx_replace.chrome:
                    xml = self._inject_replacement_chrome(xml, entry, resolved)
            try:
                await self.instance.gbx(
                    "SendDisplayManialinkPageToLogin",
                    login, xml, 0, False,
                )
                logger.debug(
                    "widget_engine: pushed replacement '%s' id=%s to %s (%d bytes)",
                    key, manialink_id, login, len(xml),
                )
            except Exception:
                logger.exception(
                    "widget_engine: gbx push '%s' for '%s' failed", key, login,
                )

    @staticmethod
    def _wrap_replacement_xml(body: str, manialink_id: str) -> str:
        """Ensure the outgoing XML is a `<manialink>` document with the
        engine-owned id. Accepts a bare body or a full manialink — in
        the latter case the id is rewritten to `manialink_id` so the
        addon can't accidentally fight a different id."""
        body = (body or "").strip()
        if not body:
            return f'<manialink id="{manialink_id}" version="3"></manialink>'
        if _MANIALINK_OPEN_RE.search(body):
            body = _MANIALINK_OPEN_RE.sub(
                f'<manialink id="{manialink_id}" version="3">', body, count=1,
            )
            if not _MANIALINK_CLOSE_RE.search(body):
                body = body + "</manialink>"
            return body
        return f'<manialink id="{manialink_id}" version="3">{body}</manialink>'

    @staticmethod
    def _inject_replacement_chrome(xml: str, entry, resolved=None) -> str:
        """Wrap the manialink body in the standard widget chrome (bg
        quad + accent strip, positioned at resolved x/y, sized resolved
        w/h) inside an `we_anim` frame, and append a ManiaScript that
        handles slide-in/out animation and optional hold-to-show hotkey.

        When `resolved` is None falls back to entry defaults — happens
        for the very first push before the resolver has a row.
        """
        from .registry import AnimDir
        repl = entry.gbx_replace
        open_match = _MANIALINK_OPEN_RE.search(xml)
        close_match = _MANIALINK_CLOSE_RE.search(xml)
        if not open_match or not close_match:
            return xml
        body_start = open_match.end()
        body_end = close_match.start()
        inner = xml[body_start:body_end]

        if resolved is not None:
            x = float(resolved.x)
            y = float(resolved.y)
            w = float(resolved.w)
            h = float(resolved.h)
            anim_dir = resolved.anim_dir
            anim_dur = int(resolved.anim_duration_ms)
            in_delay = int(resolved.anim_in_delay_ms)
            out_delay = int(resolved.anim_out_delay_ms)
        else:
            x = float(entry.default_x)
            y = float(entry.default_y)
            w = float(entry.default_w)
            h = float(entry.default_h)
            anim_dir = entry.animation.direction
            anim_dur = int(entry.animation.duration_ms)
            in_delay = int(entry.animation.in_delay_ms)
            out_delay = int(entry.animation.out_delay_ms)
        bg = entry.bg_color or "40404080"
        strip_color = entry.strip_color or "ffae00"

        # Strip edge opposite the slide direction (matches normal-widget
        # convention in resolved._strip_edge).
        edge_map = {
            AnimDir.RIGHT: "left",
            AnimDir.LEFT: "right",
            AnimDir.UP: "bottom",
            AnimDir.DOWN: "top",
            AnimDir.NONE: "left",
        }
        strip_edge = edge_map.get(anim_dir, "left") if entry.strip_enabled else ""
        strip_t = 1.0

        # Off-screen anchor for the slide-out tween. Margin must clear
        # the visible screen edge — TM2020 script-unit viewport is
        # ~160x90, so 60u extra past the widget's own size is plenty.
        margin = 60.0
        if anim_dir == AnimDir.RIGHT:
            off_x, off_y = w + margin, 0.0
        elif anim_dir == AnimDir.LEFT:
            off_x, off_y = -(w + margin), 0.0
        elif anim_dir == AnimDir.UP:
            off_x, off_y = 0.0, h + margin
        elif anim_dir == AnimDir.DOWN:
            off_x, off_y = 0.0, -(h + margin)
        else:
            off_x, off_y = 0.0, 0.0

        # Build the chrome quads.
        bg_quad = (
            f'<quad pos="0 0" z-index="1" size="{w} {h}" '
            f'bgcolor="{bg}" halign="left" valign="top"/>'
        )
        strip_quad = ""
        if strip_edge == "left":
            strip_quad = (
                f'<quad pos="-{strip_t} 0" z-index="2" size="{strip_t} {h}" '
                f'bgcolor="{strip_color}" halign="left" valign="top"/>'
            )
        elif strip_edge == "right":
            strip_quad = (
                f'<quad pos="{w} 0" z-index="2" size="{strip_t} {h}" '
                f'bgcolor="{strip_color}" halign="left" valign="top"/>'
            )
        elif strip_edge == "top":
            strip_quad = (
                f'<quad pos="0 {strip_t}" z-index="2" size="{w} {strip_t}" '
                f'bgcolor="{strip_color}" halign="left" valign="top"/>'
            )
        elif strip_edge == "bottom":
            strip_quad = (
                f'<quad pos="0 -{h}" z-index="2" size="{w} {strip_t}" '
                f'bgcolor="{strip_color}" halign="left" valign="top"/>'
            )

        body_frame = (
            f'<frame id="we_body" pos="0 0">{bg_quad}{strip_quad}'
            f'<frame pos="0 0" z-index="3">{inner}</frame>'
            f'</frame>'
        )
        anim_frame = f'<frame id="we_anim" pos="0 0">{body_frame}</frame>'
        outer = (
            f'<frame id="we_root" pos="{x} {y}" z-index="40">{anim_frame}</frame>'
        )

        hotkey = repl.hotkey or ""
        anim_enabled = anim_dir != AnimDir.NONE
        # Initial hidden state: only when there's a hotkey to bring it
        # back. Without a hotkey, widget is always visible.
        start_hidden = bool(hotkey)

        # ManiaScript: handle slide-in/out via AnimMgr; hotkey toggles
        # the Concealing state with the OS auto-repeat compensation.
        sl = []
        sl.append('<script><!--\n')
        sl.append('main() {\n')
        sl.append('  declare CMlFrame WeAnim <=> (Page.GetFirstChild("we_anim") as CMlFrame);\n')
        sl.append('  if (WeAnim == Null) return;\n')
        sl.append(
            f'  declare Text TweenHidden = "<frame pos=\\"{off_x} {off_y}\\"/>";\n'
        )
        sl.append('  declare Text TweenVisible = "<frame pos=\\"0 0\\"/>";\n')
        sl.append(f'  declare Integer AnimDur = {anim_dur};\n')
        sl.append(f'  declare Integer InDelay = {in_delay};\n')
        sl.append(f'  declare Integer OutDelay = {out_delay};\n')
        sl.append(
            f'  declare Boolean AnimEnabled = {"True" if anim_enabled else "False"};\n'
        )
        sl.append('  declare Boolean Concealed = False;\n')
        if start_hidden:
            sl.append('  AnimMgr.Add(WeAnim, TweenHidden, 0, CAnimManager::EAnimManagerEasing::Linear);\n')
            sl.append('  Concealed = True;\n')
        sl.append('  declare Boolean InRepeat = False;\n')
        sl.append('  declare Integer LastKey = -10000;\n')
        sl.append('  while (True) {\n')
        sl.append('    yield;\n')
        sl.append('    foreach (Event in PendingEvents) {\n')
        sl.append('      if (Event.Type == CMlScriptEvent::Type::MouseClick && Event.Control.HasClass("toggleSpec")) {\n')
        sl.append('        declare CMlFrame Row <=> Event.Control.Parent;\n')
        sl.append('        if (Row != Null) {\n')
        sl.append('          SetSpectateTarget(Row.DataAttributeGet("login"));\n')
        sl.append('        }\n')
        sl.append('      }\n')
        if hotkey:
            sl.append('      if (Event.Type == CMlScriptEvent::Type::KeyPress) {\n')
            sl.append(f'        if (Event.KeyName == "{hotkey}") {{\n')
            sl.append('          if (!Concealed && Now - LastKey < 150) { InRepeat = True; }\n')
            sl.append('          LastKey = Now;\n')
            sl.append('          if (Concealed) {\n')
            sl.append('            Concealed = False;\n')
            sl.append('            InRepeat = False;\n')
            sl.append('            if (InDelay > 0) sleep(InDelay);\n')
            sl.append('            if (AnimEnabled) AnimMgr.Add(WeAnim, TweenVisible, AnimDur, CAnimManager::EAnimManagerEasing::QuadInOut);\n')
            sl.append('            else AnimMgr.Add(WeAnim, TweenVisible, 0, CAnimManager::EAnimManagerEasing::Linear);\n')
            sl.append('          }\n')
            sl.append('        }\n')
            sl.append('      }\n')
        sl.append('    }\n')
        if hotkey:
            sl.append('    if (!Concealed) {\n')
            sl.append('      declare Integer Timeout = 500;\n')
            sl.append('      if (InRepeat) { Timeout = 150; }\n')
            sl.append('      if (Now - LastKey > Timeout) {\n')
            sl.append('        Concealed = True;\n')
            sl.append('        InRepeat = False;\n')
            sl.append('        if (OutDelay > 0) sleep(OutDelay);\n')
            sl.append('        if (AnimEnabled) AnimMgr.Add(WeAnim, TweenHidden, AnimDur, CAnimManager::EAnimManagerEasing::QuadInOut);\n')
            sl.append('        else AnimMgr.Add(WeAnim, TweenHidden, 0, CAnimManager::EAnimManagerEasing::Linear);\n')
            sl.append('      }\n')
            sl.append('    }\n')
        sl.append('  }\n')
        sl.append('}\n')
        sl.append('--></script>')
        script = "".join(sl)

        return xml[:body_start] + outer + script + xml[body_end:]

    async def _apply_ui_modules_visibility(
        self, ids: tuple[str, ...] | list[str], visible: bool,
    ) -> None:
        """Server-wide hide/show of title-pack ManiaScript UI modules via
        the modescript callback `Common.UIModules.SetProperties`. Used to
        make room for a widget that replaces a default UI (e.g. the
        TimeAttack TAB scoreboard, module id `Race_ScoresTable3`).
        """
        ids = [i for i in (ids or ()) if i]
        if not ids:
            return
        payload = {
            "uimodules": [
                {"id": mid, "visible": visible, "visible_update": True}
                for mid in ids
            ]
        }
        try:
            await self.instance.gbx(
                "TriggerModeScriptEventArray",
                "Common.UIModules.SetProperties",
                [json.dumps(payload)],
            )
            logger.debug(
                "widget_engine: Common.UIModules.SetProperties ids=%s visible=%s",
                ids, visible,
            )
        except Exception:
            logger.exception(
                "widget_engine: TriggerModeScriptEventArray "
                "Common.UIModules.SetProperties failed (ids=%s, visible=%s)",
                ids, visible,
            )

    async def _reconcile_ui_modules_for(self, key: str) -> None:
        """Re-show the title-pack UI modules of replacement `key` if no
        online player currently has the replacement enabled; otherwise
        keep them hidden. Compensates for the fact that
        `Common.UIModules.SetProperties` is server-wide — a player who
        disables the replacement would otherwise see neither the custom
        manialink nor the default UI."""
        entry = self._entries.get(key)
        if entry is None or not entry.gbx_replace:
            return
        ids = self.get_effective_hide_ui_modules(key)
        if not ids:
            return
        any_enabled = any(
            self.is_replacement_enabled(login, key)
            for login in self._online_logins()
        )
        await self._apply_ui_modules_visibility(ids, visible=not any_enabled)

    async def _reconcile_all_ui_modules(self) -> None:
        """Compute desired visibility for every known module id globally.

        `Common.UIModules.SetProperties` is server-wide, so per-key toggles can
        race when multiple replacements reference the same module id. We hide an
        id iff at least one replacement that lists it is enabled for any online
        player; otherwise we show it.
        """
        online = self._online_logins()
        desired_hidden: dict[str, bool] = {}
        keys = tuple(dict.fromkeys(self._replacements.values()))
        for key in keys:
            entry = self._entries.get(key)
            if entry is None or not entry.gbx_replace:
                continue
            ids = self.get_effective_hide_ui_modules(key)
            if not ids:
                continue
            any_enabled = any(self.is_replacement_enabled(login, key) for login in online)
            for mid in ids:
                if not mid:
                    continue
                desired_hidden[mid] = bool(desired_hidden.get(mid, False) or any_enabled)

        if not desired_hidden:
            return

        to_show = tuple(mid for mid, hidden in desired_hidden.items() if not hidden)
        to_hide = tuple(mid for mid, hidden in desired_hidden.items() if hidden)
        if to_show:
            await self._apply_ui_modules_visibility(to_show, visible=True)
        if to_hide:
            await self._apply_ui_modules_visibility(to_hide, visible=False)

    # ---- phase --------------------------------------------------------

    def _make_phase_handler(self, phase: Phase):
        async def _handler(signal=None, source=None, **payload):  # noqa: ARG001
            await self.engine.set_phase(phase)
        return _handler

    # ---- chat command -------------------------------------------------

    async def _cmd_widget(self, player, data, **kwargs):  # noqa: ARG002
        argv = list(getattr(data, "args", None) or [])
        sub = argv[0].lower() if argv else "list"
        rest = argv[1:]
        handler = {
            "list":    self._sub_list,
            "ls":      self._sub_list,
            "set":     self._sub_set,
            "disable": self._sub_disable,
            "enable":  self._sub_enable,
            "reset":   self._sub_reset,
            "phase":   self._sub_phase,
            "pset":    self._sub_pset,
            "pclear":  self._sub_pclear,
            "plist":   self._sub_plist,
            "tset":    self._sub_tset,
            "tclear":  self._sub_tclear,
            "tlist":   self._sub_tlist,
            "rset":    self._sub_rset,
            "rclear":  self._sub_rclear,
            "rlist":   self._sub_rlist,
            "umap":    self._sub_umap,
            "ushow":   self._sub_ushow,
            "debug":   self._sub_debug,
            "edit":    self._sub_edit,
            "done":    self._sub_done,
            "config":  self._sub_config,
            "manage":  self._sub_config,
            "help":    self._sub_help,
        }.get(sub)
        if handler is None:
            await self.instance.chat(f"$f80widget_engine: unknown subcommand '{sub}'", player.login)
            await self._sub_help(player, rest)
            return
        try:
            await handler(player, rest)
        except Exception:
            logger.exception("widget_engine: /widget %s failed", sub)
            await self.instance.chat("$f80widget_engine: command failed (see log)", player.login)

    async def _sub_help(self, player, rest):  # noqa: ARG002
        for line in (
            "$0afwidget_engine commands:",
            "$fff//widget list$888 — show registered widgets and their stored rows",
            "$fff//widget set <key> <x> <y> <w> <h>$888 — set position/size",
            "$fff//widget disable <key>$888 — hide widget (master kill-switch)",
            "$fff//widget enable <key>$888 — un-hide widget",
            "$fff//widget reset <key>$888 \u2014 restore code defaults",
            "$fff//widget phase [<name>]$888 \u2014 show or override current phase",
            "$fff//widget plist [<key>]$888 \u2014 list phase overrides",
            "$fff//widget pset <key> <phase> <x> <y> <w> <h>$888 \u2014 set phase position override",
            "$fff//widget pclear <key> <phase>$888 \u2014 remove a phase override",
            "$fff//widget tset <key> <ttl_s> <x> <y> <w> <h>$888 \u2014 transient overlay for you (TTL seconds, 0=none)",
            "$fff//widget tclear <key>$888 \u2014 clear your transient overlay for this widget",
            "$fff//widget tlist$888 \u2014 list your live transient overlays",
            "$fff//widget rset <owner> <key> <x> <y> <w> <h> [disabled0|1]$888 \u2014 set temporary runtime layout patch",
            "$fff//widget rclear <owner> [<key>]$888 \u2014 clear temporary runtime patch (owner or owner+key)",
            "$fff//widget rlist [<owner>]$888 \u2014 list active temporary runtime layout patches",
            "$fff//widget debug [<key>|all|off]$888 \u2014 toggle per-player debug overlay",
            "$fff//widget edit <key>$888 \u2014 enter edit mode (bypasses hide rules; auto-enables debug)",
            "$fff//widget done$888 \u2014 leave edit mode",
            "$fff//widget config$888 \u2014 open the Widget Engine manager window",
        ):
            await self.instance.chat(line, player.login)

    async def _sub_list(self, player, rest):  # noqa: ARG002
        if not self._entries:
            await self.instance.chat("$f80widget_engine: no widgets registered", player.login)
            return
        cur = self.engine.current_phase
        await self.instance.chat(
            f"$0afwidget_engine: {len(self._entries)} registered, "
            f"{len(self.storage.all())} stored, phase=$fff{cur.value if cur else '?'}",
            player.login,
        )
        for entry in sorted(self._entries.values(), key=lambda e: e.key):
            row = self.storage.get(entry.key) or {}
            x = row.get("x", entry.default_x)
            y = row.get("y", entry.default_y)
            w = row.get("w", entry.default_w)
            h = row.get("h", entry.default_h)
            tag = " $f80[disabled]" if row.get("disabled") else ""
            if entry.visible_phases is not None:
                phases = ",".join(p.value for p in entry.visible_phases) or "none"
                tag += f" $aaa[phases={phases}]"
            await self.instance.chat(
                f"$fff{entry.key}$888  pos={x:.1f},{y:.1f}  size={w:.1f}x{h:.1f}{tag}",
                player.login,
            )

    async def _sub_set(self, player, rest):
        if len(rest) != 5:
            await self.instance.chat(
                "$f80usage: //widget set <key> <x> <y> <w> <h>", player.login,
            )
            return
        key = rest[0]
        if key not in self._entries:
            await self.instance.chat(f"$f80widget_engine: unknown widget '{key}'", player.login)
            return
        try:
            x, y, w, h = (float(v) for v in rest[1:])
        except ValueError:
            await self.instance.chat("$f80widget_engine: x/y/w/h must be numbers", player.login)
            return
        await self.storage.set_position(key, x, y, w, h)
        await self._redisplay(key)
        await self.instance.chat(
            f"$0afwidget_engine: '{key}' → pos={x:.1f},{y:.1f} size={w:.1f}x{h:.1f}",
            player.login,
        )

    async def _sub_disable(self, player, rest):
        key = rest[0] if rest else ""
        if key not in self._entries:
            await self.instance.chat("$f80usage: //widget disable <key>", player.login)
            return
        await self.storage.set_disabled(key, True)
        await self._redisplay(key)
        await self.instance.chat(f"$0afwidget_engine: '{key}' disabled", player.login)

    async def _sub_enable(self, player, rest):
        key = rest[0] if rest else ""
        if key not in self._entries:
            await self.instance.chat("$f80usage: //widget enable <key>", player.login)
            return
        await self.storage.set_disabled(key, False)
        await self._redisplay(key)
        await self.instance.chat(f"$0afwidget_engine: '{key}' enabled", player.login)

    async def _sub_reset(self, player, rest):
        key = rest[0] if rest else ""
        entry = self._entries.get(key)
        if entry is None:
            await self.instance.chat("$f80usage: //widget reset <key>", player.login)
            return
        # Reset re-applies the entry's code defaults to the stored row.
        await self.storage.set_position(
            key, entry.default_x, entry.default_y, entry.default_w, entry.default_h,
        )
        await self.storage.set_drive_mode(key, entry.drive_mode)
        await self.storage.set_animation(
            key,
            direction=entry.animation.direction,
            duration_ms=entry.animation.duration_ms,
            in_delay_ms=entry.animation.in_delay_ms,
            out_delay_ms=entry.animation.out_delay_ms,
        )
        await self.storage.set_disabled(key, False)
        await self._redisplay(key)
        await self.instance.chat(f"$0afwidget_engine: '{key}' reset to code defaults", player.login)

    async def _sub_phase(self, player, rest):
        if not rest:
            cur = self.engine.current_phase
            names = ", ".join(p.value for p in Phase)
            await self.instance.chat(
                f"$0afwidget_engine: phase=$fff{cur.value if cur else '?'}$0af  "
                f"$888({names})",
                player.login,
            )
            return
        name = rest[0].lower()
        try:
            phase = Phase(name)
        except ValueError:
            await self.instance.chat(
                f"$f80widget_engine: unknown phase '{name}'", player.login,
            )
            return
        await self.engine.set_phase(phase)
        await self.instance.chat(
            f"$0afwidget_engine: phase forced to $fff{phase.value}", player.login,
        )

    # ---- phase override subcommands -----------------------------------

    def _parse_phase(self, name: str) -> Optional[Phase]:
        try:
            return Phase(name.lower())
        except ValueError:
            return None

    def _resolve_widget_key_cmd(self, raw: str) -> str:
        key = str(raw or "").strip()
        aliases = {
            "local_records_widget": "local_rankings",
            "local_records": "local_rankings",
            "local_rankings_widget": "local_rankings",
            "live_records_widget": "live_rankings",
            "live_records": "live_rankings",
            "live_rankings_widget": "live_rankings",
            "best_cps_widget": "best_cps",
            "best_cps2_widget": "best_cps2",
            "karma": "karma_widget",
        }
        return aliases.get(key, key)

    async def _sub_pset(self, player, rest):
        if len(rest) != 6:
            await self.instance.chat(
                "$f80usage: //widget pset <key> <phase> <x> <y> <w> <h>",
                player.login,
            )
            return
        key, phase_name = rest[0], rest[1]
        if key not in self._entries:
            await self.instance.chat(
                f"$f80widget_engine: unknown widget '{key}'", player.login,
            )
            return
        phase = self._parse_phase(phase_name)
        if phase is None:
            await self.instance.chat(
                f"$f80widget_engine: unknown phase '{phase_name}'", player.login,
            )
            return
        try:
            x, y, w, h = (float(v) for v in rest[2:])
        except ValueError:
            await self.instance.chat(
                "$f80widget_engine: x/y/w/h must be numbers", player.login,
            )
            return
        await self.storage.phase_set(
            key, phase, {"x": x, "y": y, "w": w, "h": h},
        )
        await self._redisplay(key)
        await self.instance.chat(
            f"$0afwidget_engine: '{key}' @ {phase.value} "
            f"\u2192 pos={x:.1f},{y:.1f} size={w:.1f}x{h:.1f}",
            player.login,
        )

    async def _sub_pclear(self, player, rest):
        if len(rest) != 2:
            await self.instance.chat(
                "$f80usage: //widget pclear <key> <phase>", player.login,
            )
            return
        key, phase_name = rest[0], rest[1]
        if key not in self._entries:
            await self.instance.chat(
                f"$f80widget_engine: unknown widget '{key}'", player.login,
            )
            return
        phase = self._parse_phase(phase_name)
        if phase is None:
            await self.instance.chat(
                f"$f80widget_engine: unknown phase '{phase_name}'", player.login,
            )
            return
        await self.storage.phase_clear(key, phase)
        await self._redisplay(key)
        await self.instance.chat(
            f"$0afwidget_engine: cleared override '{key}' @ {phase.value}",
            player.login,
        )

    async def _sub_plist(self, player, rest):
        rows = self.storage.phase_all()
        if rest:
            key_filter = rest[0]
            rows = {k: v for k, v in rows.items() if k[0] == key_filter}
        if not rows:
            await self.instance.chat(
                "$0afwidget_engine: no phase overrides", player.login,
            )
            return
        await self.instance.chat(
            f"$0afwidget_engine: {len(rows)} phase override(s)", player.login,
        )
        for (key, phase_value), r in sorted(rows.items()):
            cells = []
            for col in ("x", "y", "w", "h", "drive_mode", "anim_dir", "disabled"):
                v = r.get(col)
                if v is None:
                    continue
                if isinstance(v, float):
                    cells.append(f"{col}={v:.1f}")
                else:
                    cells.append(f"{col}={v}")
            await self.instance.chat(
                f"$fff{key}$888 @ $aaa{phase_value}$888  " + " ".join(cells),
                player.login,
            )

    # ---- transient override subcommands (slice 5) ---------------------

    async def _sub_tset(self, player, rest):
        if len(rest) != 6:
            await self.instance.chat(
                "$f80usage: //widget tset <key> <ttl_s> <x> <y> <w> <h>",
                player.login,
            )
            return
        key = rest[0]
        if key not in self._entries:
            await self.instance.chat(
                f"$f80widget_engine: unknown widget '{key}'", player.login,
            )
            return
        try:
            ttl = float(rest[1])
            x, y, w, h = (float(v) for v in rest[2:])
        except ValueError:
            await self.instance.chat(
                "$f80widget_engine: ttl_s and x/y/w/h must be numbers", player.login,
            )
            return
        await self.engine.set_transient(
            player.login, key,
            {"x": x, "y": y, "w": w, "h": h},
            ttl_s=ttl if ttl > 0 else None,
        )
        ttl_txt = f"{ttl:.1f}s" if ttl > 0 else "no-expiry"
        await self.instance.chat(
            f"$0afwidget_engine: transient '{key}' \u2192 "
            f"pos={x:.1f},{y:.1f} size={w:.1f}x{h:.1f}  ($888{ttl_txt}$0af)",
            player.login,
        )

    async def _sub_tclear(self, player, rest):
        key = rest[0] if rest else ""
        if key not in self._entries:
            await self.instance.chat(
                "$f80usage: //widget tclear <key>", player.login,
            )
            return
        await self.engine.clear_transient(player.login, key)
        await self.instance.chat(
            f"$0afwidget_engine: transient '{key}' cleared", player.login,
        )

    async def _sub_tlist(self, player, rest):  # noqa: ARG002
        rows = self.engine.transient_all(login=player.login)
        if not rows:
            await self.instance.chat(
                "$0afwidget_engine: no transient overlays for you", player.login,
            )
            return
        await self.instance.chat(
            f"$0afwidget_engine: {len(rows)} transient overlay(s)", player.login,
        )
        for (_login, key), patch in sorted(rows.items()):
            cells = []
            for col in ("x", "y", "w", "h", "drive_mode", "anim_dir", "disabled"):
                v = patch.get(col)
                if v is None:
                    continue
                if isinstance(v, float):
                    cells.append(f"{col}={v:.1f}")
                else:
                    cells.append(f"{col}={v}")
            await self.instance.chat(
                f"$fff{key}$888  " + " ".join(cells), player.login,
            )

    # ---- runtime layout subcommands (slice 5.5) ----------------------

    async def _sub_rset(self, player, rest):
        if len(rest) not in (6, 7):
            await self.instance.chat(
                "$f80usage: //widget rset <owner> <key> <x> <y> <w> <h> [disabled0|1]",
                player.login,
            )
            return
        owner, key = rest[0], self._resolve_widget_key_cmd(rest[1])
        if key not in self._entries:
            await self.instance.chat(
                f"$f80widget_engine: unknown widget '{key}'", player.login,
            )
            return
        try:
            x, y, w, h = (float(v) for v in rest[2:6])
        except ValueError:
            await self.instance.chat(
                "$f80widget_engine: x/y/w/h must be numbers", player.login,
            )
            return
        patch: dict[str, object] = {"x": x, "y": y, "w": w, "h": h}
        if len(rest) == 7:
            raw = str(rest[6]).strip().lower()
            patch["disabled"] = raw in {"1", "true", "yes", "on"}
        await self.engine.set_runtime_layout(owner, key, patch)
        await self.instance.chat(
            f"$0afwidget_engine: runtime[{owner}] '{key}' -> "
            f"pos={x:.1f},{y:.1f} size={w:.1f}x{h:.1f}",
            player.login,
        )

    async def _sub_rclear(self, player, rest):
        if len(rest) not in (1, 2):
            await self.instance.chat(
                "$f80usage: //widget rclear <owner> [<key>]", player.login,
            )
            return
        owner = rest[0]
        if len(rest) == 1:
            await self.engine.clear_runtime_owner(owner)
            await self.instance.chat(
                f"$0afwidget_engine: runtime owner '{owner}' cleared", player.login,
            )
            return
        key = self._resolve_widget_key_cmd(rest[1])
        if key not in self._entries:
            await self.instance.chat(
                f"$f80widget_engine: unknown widget '{key}'", player.login,
            )
            return
        await self.engine.clear_runtime_layout(owner, key)
        await self.instance.chat(
            f"$0afwidget_engine: runtime[{owner}] '{key}' cleared", player.login,
        )

    async def _sub_rlist(self, player, rest):
        owner = rest[0] if rest else None
        rows = self.engine.runtime_layout_all(owner=owner)
        if not rows:
            await self.instance.chat(
                "$0afwidget_engine: no runtime layout patches", player.login,
            )
            return
        await self.instance.chat(
            f"$0afwidget_engine: {len(rows)} runtime patch(es)", player.login,
        )
        for (own, key), patch in sorted(rows.items()):
            cells = []
            for col in ("x", "y", "w", "h", "drive_mode", "anim_dir", "disabled"):
                v = patch.get(col)
                if v is None:
                    continue
                if isinstance(v, float):
                    cells.append(f"{col}={v:.1f}")
                else:
                    cells.append(f"{col}={v}")
            await self.instance.chat(
                f"$fff{own}$888/{key}  " + " ".join(cells),
                player.login,
            )

    # ---- debug overlay subcommand (slice 6) ---------------------------

    async def _sub_debug(self, player, rest):
        # No arg: show current state.
        if not rest:
            keys = self.engine.debug_keys(player.login)
            if not keys:
                await self.instance.chat(
                    "$0afwidget_engine: debug off  ($888//widget debug <key>|all|off$0af)",
                    player.login,
                )
                return
            shown = "all" if "*" in keys else ", ".join(sorted(keys))
            await self.instance.chat(
                f"$0afwidget_engine: debug on for $fff{shown}", player.login,
            )
            return
        target = rest[0].lower()
        if target == "off":
            await self.engine.clear_debug(player.login)
            await self.instance.chat(
                "$0afwidget_engine: debug cleared", player.login,
            )
            return
        if target == "all":
            on = "*" not in self.engine.debug_keys(player.login)
            await self.engine.set_debug(player.login, "*", on)
            await self.instance.chat(
                f"$0afwidget_engine: debug for all widgets {'on' if on else 'off'}",
                player.login,
            )
            return
        key = rest[0]
        if key not in self._entries:
            await self.instance.chat(
                f"$f80widget_engine: unknown widget '{key}'", player.login,
            )
            return
        on = not self.engine.is_debug(player.login, key)
        await self.engine.set_debug(player.login, key, on)
        await self.instance.chat(
            f"$0afwidget_engine: debug '{key}' {'on' if on else 'off'}", player.login,
        )

    # ---- UI modules diagnostics --------------------------------------

    async def _sub_umap(self, player, rest):
        """List which replacement widgets currently hide specific UI modules.

        Usage:
          //widget umap
          //widget umap Race_Chrono Race_Chrono2 Race_ChronoTable
        """
        modules = tuple(rest) if rest else (
            "Race_Chrono",
            "Race_Chrono2",
            "Race_ChronoTable",
            "Race_Checkpoint",
            "Race_Countdown",
            "Race_HUD",
            "Race_HUD_BigMessage",
        )
        await self.instance.chat(
            "$0afwidget_engine: checking UI module owners for $fff"
            + ", ".join(modules),
            player.login,
        )
        online = self._online_logins()
        hits = 0
        for key in sorted(self._entries.keys()):
            entry = self._entries.get(key)
            if entry is None or not entry.gbx_replace:
                continue
            effective = tuple(self.get_effective_hide_ui_modules(key) or ())
            if not effective:
                continue
            matched = tuple(m for m in modules if m in effective)
            if not matched:
                continue
            hits += 1
            source = "override" if self.has_ui_modules_override(key) else "default"
            enabled_logins = [lg for lg in online if self.is_replacement_enabled(lg, key)]
            enabled_info = (
                f"enabled_for={len(enabled_logins)}/{len(online)}"
                if online else "enabled_for=0/0"
            )
            active = "active_hide=yes" if bool(enabled_logins) else "active_hide=no"
            await self.instance.chat(
                f"$fff{key}$888 source={source} {enabled_info} {active} hides="
                + ",".join(matched),
                player.login,
            )
        if hits == 0:
            await self.instance.chat(
                "$0afwidget_engine: no registered replacement currently hides those modules",
                player.login,
            )

    async def _sub_ushow(self, player, rest):
        """Force-show UI modules server-wide once.

        Useful to validate whether hidden title-pack modules are the root cause.
        """
        modules = tuple(rest) if rest else (
            "Race_Chrono",
            "Race_Chrono2",
            "Race_ChronoTable",
            "Race_Checkpoint",
            "Race_Countdown",
            "Race_HUD",
            "Race_HUD_BigMessage",
        )
        await self._apply_ui_modules_visibility(modules, visible=True)
        await self.instance.chat(
            "$0afwidget_engine: forced visible (once): $fff" + ", ".join(modules),
            player.login,
        )

    # ---- edit mode subcommands (slice 7) ------------------------------

    async def _sub_edit(self, player, rest):
        key = rest[0] if rest else ""
        if key not in self._entries:
            await self.instance.chat("$f80usage: //widget edit <key>", player.login)
            return
        prev = self.engine.editing_key(player.login)
        if prev == key:
            await self.instance.chat(
                f"$0afwidget_engine: already editing '{key}'  ($888//widget done$0af)",
                player.login,
            )
            return
        await self.engine.enter_edit(player.login, key)
        msg = f"$0afwidget_engine: editing $fff{key}"
        if prev is not None and prev != key:
            msg += f"$0af  (left '{prev}')"
        await self.instance.chat(msg, player.login)

    async def _sub_done(self, player, rest):  # noqa: ARG002
        prev = await self.engine.exit_edit(player.login)
        if prev is None:
            await self.instance.chat(
                "$0afwidget_engine: not in edit mode", player.login,
            )
            return
        await self.instance.chat(
            f"$0afwidget_engine: stopped editing $fff{prev}", player.login,
        )

    async def _sub_config(self, player, rest):  # noqa: ARG002
        await self._open_manager(player.login)
