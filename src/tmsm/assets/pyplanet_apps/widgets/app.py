"""tmsm widgets — global widget framework.

Widgets are registered by other apps via the ``tmsm_widgets:register``
signal. They are HUD overlays (clock, race info, popup notifications,
etc.) that:

  * persist on-screen (kind=PERSISTENT) or pop up on demand (kind=POPUP)
  * resolve their position from defaults < global override < per-player override
  * can be moved in-game via the position editor (admin command ``/widgets``)
  * hide themselves client-side when named or raw ManiaScript conditions match
  * animate show/hide with configurable direction, duration, and delay

Signals exposed (namespace ``tmsm_widgets``)::

    register            entry=WidgetEntry             register or replace widget
    refresh             (none)                        re-render the editor
    popup               key=str, login=str            trigger a popup widget
    position_changed    key=str, scope=str, login=?   announce edit took effect
    edit_mode           login=str, active=bool        editor open / closed for player
    runtime_override_set owner,key,login?,enabled?,x?,y?,w?,h?,drive_mode?,anim_* temporary override
    runtime_override_clear owner,key,login?           clear one temporary override
    runtime_override_clear_owner owner                clear all overrides for owner

Commands::

    /widgets            open the position editor (admin)
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import Any

from pyplanet.apps.config import AppConfig
from pyplanet.apps.tmsm.hub.registry import HubAppEntry, Role, Status
from pyplanet.contrib.command import Command
from pyplanet.core.events import Signal

from .presets import (
    PresetRegistry,
    WidgetPreset,
    build_preset_from_snapshot,
    default_presets_dir,
)
from .registry import WidgetEntry, WidgetKind
from .storage import WidgetStorage, default_defaults_path
from .views import WidgetEditorView

logger = logging.getLogger(__name__)


def _normalize_color_hex(value: Any) -> str:
    """Coerce a user-supplied color string into a ManiaPlanet-safe hex.

    Accepts RGB/RGBA hex with optional ``#``/``$`` prefix and any case.
    Returns ``""`` for empty/invalid input — callers treat empty as
    "no override". Anything that isn't 3/4/6/8 hex chars is dropped so
    the renderer never emits an unparseable ``bgcolor`` (which the game
    renders as fully transparent).
    """
    if value is None:
        return ""
    s = str(value).strip().lstrip("#").lstrip("$")
    if not s:
        return ""
    if len(s) not in (3, 4, 6, 8):
        return ""
    try:
        int(s, 16)
    except ValueError:
        return ""
    return s.lower()

_ANIM_DIR_OPTIONS = ("none", "left", "right", "up", "down")
_DRIVE_MODE_OPTIONS = (
    "fixed",
    "hide_while_driving",
    "only_shown_while_driving",
)
_STATE_MODE_OPTIONS = (
    "all",
    "loading_map",
    "warmup",
    "pre_race",
    "in_race",
    "in_podium",
    "post_race",
)
_STATE_MODE_ORDER = list(_STATE_MODE_OPTIONS)
_GROUP_MODE_OPTIONS = (
    "priority_active",
    "first_visible",
    "fixed_member",
)
_GROUP_RUNTIME_DEBOUNCE_SEC = 0.2

# Editor list pagination — rows per page in the Widgets/Groups list.
_EDITOR_PAGE_SIZE = 9


class WidgetsApp(AppConfig):
    name = "pyplanet.apps.tmsm.widgets"
    label = "tmsm_widgets"
    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_hub"]
    game_dependencies = ["trackmania", "trackmania_next"]

    HUB_KEY = "widgets"
    HUB_NAME = "Widgets"
    HUB_ICON = "object-group"
    HUB_COLOR = "15f"
    HUB_DESCRIPTION = "Position and configure on-screen widgets."
    HUB_ROLE = Role.PLAYER
    HUB_STATUS = Status.BETA
    HUB_ORDER = 30
    HUB_COMMAND = "widgets"
    EDGE_CENTER_DEAD_ZONE = 1.0

    @staticmethod
    def _edge_side(x: float, w: float, dead_zone: float = 1.0) -> int:
        """Classify a widget as left/center/right based on its center X."""
        cx = float(x) + float(w) * 0.5
        if cx > dead_zone:
            return 1
        if cx < -dead_zone:
            return -1
        return 0

    @classmethod
    def _apply_edge_offset_x(cls, x: float, w: float, edge_x: float) -> float:
        """Apply horizontal calibration as a side-aware translation.

        Positive edge values push widgets away from center
        (left widgets further left, right widgets further right), while
        centered widgets (within a dead zone) stay in place.
        """
        side = cls._edge_side(x, w, cls.EDGE_CENTER_DEAD_ZONE)
        if side > 0:
            return float(x) + float(edge_x)
        if side < 0:
            return float(x) - float(edge_x)
        return float(x)

    @classmethod
    def _remove_edge_offset_x(cls, x_eff: float, w: float, edge_x: float) -> float:
        """Inverse of :meth:`_apply_edge_offset_x` for stored coordinates."""
        side = cls._edge_side(x_eff, w, cls.EDGE_CENTER_DEAD_ZONE)
        if side > 0:
            return float(x_eff) - float(edge_x)
        if side < 0:
            return float(x_eff) + float(edge_x)
        return float(x_eff)

    @staticmethod
    def _apply_unstretch_y(y: float, stretch: float) -> float:
        """Compensate display stretch by compressing/expanding Y around center.

        Positive values compress Y toward center (useful when UI appears too
        tall/stretched). Negative values expand Y.
        """
        denom = max(0.2, 1.0 + float(stretch) / 100.0)
        return float(y) / denom

    @staticmethod
    def _remove_unstretch_y(y_eff: float, stretch: float) -> float:
        """Inverse of :meth:`_apply_unstretch_y` for stored coordinates."""
        denom = max(0.2, 1.0 + float(stretch) / 100.0)
        return float(y_eff) * denom

    @staticmethod
    def _apply_unstretch_h(h: float, stretch: float) -> float:
        """Apply the same vertical compensation factor to widget height."""
        denom = max(0.2, 1.0 + float(stretch) / 100.0)
        return float(h) / denom

    @staticmethod
    def _unstretch_scale_y(stretch: float) -> float:
        """Y-scale factor used to de-stretch widget content."""
        return 1.0 / max(0.2, 1.0 + float(stretch) / 100.0)

    @staticmethod
    def _default_drive_mode(entry: WidgetEntry) -> str:
        named = [str(n or "").strip() for n in entry.hide_rule.named]
        if any(n.startswith("speed_below:") for n in named):
            return "only_shown_while_driving"
        if any(n.startswith("speed_above:") for n in named):
            return "hide_while_driving"
        return "fixed"

    @staticmethod
    def _default_state_mode(entry: WidgetEntry) -> str:
        named = [str(n or "").strip() for n in entry.hide_rule.named]
        if any(n == "in_race" for n in named):
            return "in_race"
        return "all"

    @staticmethod
    def _normalize_state_modes(raw_modes: Any) -> list[str]:
        if isinstance(raw_modes, str):
            items = [raw_modes]
        else:
            try:
                items = [str(x) for x in (raw_modes or [])]
            except Exception:
                items = []
        modes = [m for m in items if m in _STATE_MODE_OPTIONS]
        if not modes:
            return ["all"]
        if "all" in modes:
            return ["all"]
        ordered = [m for m in _STATE_MODE_ORDER if m in modes and m != "all"]
        return ordered or ["all"]

    @staticmethod
    def _normalize_group_key(raw: Any) -> str:
        return str(raw or "").strip()

    @staticmethod
    def _normalize_editor_tab(raw: Any) -> str:
        tab = str(raw or "widgets").strip()
        return tab if tab in ("widgets", "groups", "frame", "presets") else "widgets"

    @staticmethod
    def _normalize_group_mode(raw: Any) -> str:
        mode = str(raw or "priority_active").strip()
        return mode if mode in _GROUP_MODE_OPTIONS else "priority_active"

    def _group_cfg(self, group_key: str) -> dict[str, Any]:
        key = self._normalize_group_key(group_key)
        if not key:
            return {
                "key": "",
                "anchor_x": 0.0,
                "anchor_y": 0.0,
                "anchor_w": 18.0,
                "anchor_h": 8.0,
                "mode": "priority_active",
                "max_visible": 1,
                "runtime_prev_enabled": True,
                "runtime_next_enabled": True,
                "runtime_auto_enabled": True,
                "runtime_pin_enabled": True,
                "fixed_widget_key": "",
            }
        cfg = self.storage.group_by_key(key) or {"key": key}
        out = dict(cfg)
        out["key"] = key
        out["anchor_x"] = float(out.get("anchor_x", 0.0) or 0.0)
        out["anchor_y"] = float(out.get("anchor_y", 0.0) or 0.0)
        out["anchor_w"] = float(out.get("anchor_w", 18.0) or 18.0)
        out["anchor_h"] = float(out.get("anchor_h", 8.0) or 8.0)
        out["mode"] = self._normalize_group_mode(out.get("mode"))
        try:
            out["max_visible"] = max(1, int(out.get("max_visible", 1) or 1))
        except (TypeError, ValueError):
            out["max_visible"] = 1
        for fld in (
            "runtime_prev_enabled",
            "runtime_next_enabled",
            "runtime_auto_enabled",
            "runtime_pin_enabled",
        ):
            out[fld] = bool(out.get(fld, True))
        out["fixed_widget_key"] = str(out.get("fixed_widget_key") or "")
        return out

    def _group_runtime_row(self, login: str, group_key: str) -> dict[str, Any]:
        return self._group_runtime.setdefault(login, {}).setdefault(group_key, {
            "manual_index": None,
            "manual_until": None,
            "last_action_at": 0.0,
        })

    def _group_runtime_active(self, login: str, group_key: str) -> bool:
        row = self._group_runtime.get(login, {}).get(group_key)
        if not row:
            return False
        until = row.get("manual_until")
        if until is not None and float(until) < time.monotonic():
            row["manual_until"] = None
            row["manual_index"] = None
            return False
        return row.get("manual_index") is not None

    def _group_runtime_debounced(self, login: str, group_key: str) -> bool:
        row = self._group_runtime_row(login, group_key)
        now = time.monotonic()
        last = float(row.get("last_action_at", 0.0) or 0.0)
        if now - last < _GROUP_RUNTIME_DEBOUNCE_SEC:
            return True
        row["last_action_at"] = now
        return False

    def _group_usage_map(self) -> dict[str, list[str]]:
        usage: dict[str, list[str]] = {}
        for widget_key in sorted(self.entries.keys()):
            beh = self.resolve_behavior(widget_key)
            gk = self._normalize_group_key(beh.get("group_key"))
            if not gk:
                continue
            usage.setdefault(gk, []).append(widget_key)
        return usage

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entries: dict[str, WidgetEntry] = {}
        self.storage: WidgetStorage = WidgetStorage(self.instance)
        self.presets: PresetRegistry = PresetRegistry(default_presets_dir())
        self.editor: WidgetEditorView | None = None
        # logins currently in editor mode (positions are draggable in their UI)
        self._editing: set[str] = set()
        # default scope chosen in the editor: "global" or "player"
        self._scope: dict[str, str] = {}  # login -> scope
        # currently selected widget in the editor: login -> widget key
        self._selected: dict[str, str] = {}
        # currently selected group in the editor: login -> group key
        self._selected_group: dict[str, str] = {}
        # active editor tab: login -> widgets|groups
        self._editor_tab: dict[str, str] = {}
        # step size for nudge buttons (manialink units)
        self._step: dict[str, float] = {}
        # per-login debug overlay (master-only); when on, every widget
        # renders its debug chip + status label.
        self._debug: set[str] = set()
        # currently open combo-box in editor, per login (e.g. "drivemode__clock")
        self._open_combo: dict[str, str] = {}
        # groups delete confirmation arm (login -> group_key)
        self._armed_del_group: dict[str, str] = {}
        # per-login ephemeral group runtime state
        self._group_runtime: dict[str, dict[str, dict[str, Any]]] = {}
        # owner -> (login_or_* + "::" + widget_key) -> runtime override payload
        # payload keys: enabled?, x?, y?, w?, h?, drive_mode?, anim_*, __seq
        self._runtime_overrides: dict[str, dict[str, dict[str, Any]]] = {}
        self._runtime_seq: int = 0
        # pagination page index per list (login -> 0-based page)
        self._widgets_page: dict[str, int] = {}
        self._groups_page: dict[str, int] = {}
        self._phase: str = "pre_race"

    # ---- lifecycle -----------------------------------------------------

    async def on_init(self) -> None:
        for code in ("register", "refresh", "popup",
                     "position_changed", "edit_mode", "request_register",
                     "group_next", "group_prev", "group_set_auto", "group_pin",
                     "runtime_override_set", "runtime_override_clear",
                     "runtime_override_clear_owner",
                     "presets_changed", "preset_applied"):
            try:
                self.context.signals.register_signal(
                    Signal(code=code, namespace="tmsm_widgets")
                )
            except Exception:
                logger.exception("widgets: failed to register signal tmsm_widgets:%s", code)

    async def on_start(self) -> None:
        # DB-backed storage: seed the global table from bundled defaults
        # on first boot, then populate in-memory caches from the DB.
        try:
            await self.storage.seed_defaults(default_defaults_path())
            await self.storage.load()
        except Exception:
            logger.exception("widgets: storage init failed; in-memory only")

        try:
            self.presets.reload()
        except Exception:
            logger.exception("widgets: preset registry load failed")

        self.editor = WidgetEditorView(self)
        self.editor.handle_catch_all = self._editor_catch_all  # type: ignore[assignment]

        try:
            await self.instance.command_manager.register(
                Command(command="widgets", target=self._cmd_widgets,
                        description="Open the tmsm widget position editor."),
            )
        except Exception:
            logger.exception("widgets: /widgets command registration failed")

        self.context.signals.listen("tmsm_widgets:register", self._on_register)
        self.context.signals.listen("tmsm_widgets:popup", self._on_popup_signal)
        self.context.signals.listen("tmsm_widgets:refresh", self._on_refresh)
        self.context.signals.listen("tmsm_widgets:group_next", self._on_group_next_signal)
        self.context.signals.listen("tmsm_widgets:group_prev", self._on_group_prev_signal)
        self.context.signals.listen("tmsm_widgets:group_set_auto", self._on_group_set_auto_signal)
        self.context.signals.listen("tmsm_widgets:group_pin", self._on_group_pin_signal)
        self.context.signals.listen("tmsm_widgets:runtime_override_set", self._on_runtime_override_set_signal)
        self.context.signals.listen("tmsm_widgets:runtime_override_clear", self._on_runtime_override_clear_signal)
        self.context.signals.listen("tmsm_widgets:runtime_override_clear_owner", self._on_runtime_override_clear_owner_signal)
        self.context.signals.listen("maniaplanet:player_disconnect", self._on_player_disconnect)
        self.context.signals.listen("maniaplanet:player_connect", self._on_player_connect)
        self.context.signals.listen("maniaplanet:loading_map_start", self._on_phase_loading)
        self.context.signals.listen("maniaplanet:map_start", self._on_phase_pre)
        self.context.signals.listen("trackmania:warmup_start", self._on_phase_warmup)
        self.context.signals.listen("trackmania:warmup_end", self._on_phase_pre)
        self.context.signals.listen("trackmania:start_countdown", self._on_phase_pre)
        self.context.signals.listen("trackmania:start_line", self._on_phase_race)
        self.context.signals.listen("trackmania:waypoint", self._on_phase_race)
        self.context.signals.listen("maniaplanet:podium_start", self._on_phase_podium)
        self.context.signals.listen("maniaplanet:podium_end", self._on_phase_post)
        self.context.signals.listen("maniaplanet:map_end", self._on_phase_post)

        # Ask any widget-providing app that started before us (e.g. tmsm_hub)
        # to register itself now that our `register` signal has a listener.
        try:
            sig = self.context.signals.get_signal("tmsm_widgets:request_register")
            await sig.send_robust({}, raw=True)
        except Exception:
            logger.exception("widgets: emit tmsm_widgets:request_register failed")

        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
            entry = HubAppEntry(
                key=self.HUB_KEY,
                name=self.HUB_NAME,
                icon=self.HUB_ICON,
                color=self.HUB_COLOR,
                description=self.HUB_DESCRIPTION,
                role=self.HUB_ROLE,
                status=self.HUB_STATUS,
                tags=[],
                order=self.HUB_ORDER,
                command=self.HUB_COMMAND,
                author="tmsm",
                version="0.1",
                open=self._hub_open,
            )
            await sig.send_robust({"entry": entry}, raw=True)
        except KeyError:
            logger.info("widgets: tmsm_hub:register not available")
        except Exception:
            logger.exception("widgets: hub registration failed")

        logger.info("widgets: started; editor available via //widgets")

    async def _hub_open(self, player) -> None:
        await self._open_editor(player.login)

    async def on_stop(self) -> None:
        if self.editor is not None:
            try:
                await self.editor.destroy()
            except Exception:
                logger.exception("widgets: editor destroy failed")

    # ---- registry ------------------------------------------------------

    async def _on_register(self, entry: WidgetEntry | None = None, **kwargs) -> None:
        if entry is None or not isinstance(entry, WidgetEntry):
            logger.warning("widgets: tmsm_widgets:register received invalid payload: %r", entry)
            return
        self.entries[entry.key] = entry
        logger.info("widgets: registered '%s' (%s, kind=%s)",
                    entry.key, entry.name, entry.kind.value)
        # If anyone has the editor open, refresh it so the new widget shows.
        if self._editing:
            await self._refresh_editor_for(list(self._editing))

    async def _on_refresh(self, **kwargs) -> None:
        if self._editing:
            await self._refresh_editor_for(list(self._editing))

    async def _on_popup_signal(self, key: str | None = None, login: str | None = None,
                                **kwargs) -> None:
        if not key or not login:
            return
        entry = self.entries.get(key)
        if entry is None or entry.popup_trigger is None:
            logger.info("widgets: popup '%s' for %s skipped (not registered as popup)",
                        key, login)
            return
        if self.is_widget_disabled(key):
            logger.info("widgets: popup '%s' for %s skipped (widget disabled)",
                        key, login)
            return
        try:
            await entry.popup_trigger(login)
        except Exception:
            logger.exception("widgets: popup trigger raised for '%s'", key)

    async def _on_group_next_signal(self, group_key: str | None = None,
                                    login: str | None = None, **kwargs) -> None:
        if not group_key or not login:
            return
        await self.group_next(group_key, login)

    async def _on_group_prev_signal(self, group_key: str | None = None,
                                    login: str | None = None, **kwargs) -> None:
        if not group_key or not login:
            return
        await self.group_prev(group_key, login)

    async def _on_group_set_auto_signal(self, group_key: str | None = None,
                                        login: str | None = None, **kwargs) -> None:
        if not group_key or not login:
            return
        await self.group_set_auto(group_key, login)

    async def _on_group_pin_signal(self, group_key: str | None = None,
                                   login: str | None = None,
                                   widget_key: str | None = None,
                                   ttl: float | None = None, **kwargs) -> None:
        if not group_key or not login:
            return
        await self.group_pin(group_key, login, widget_key=widget_key, ttl=ttl)

    @staticmethod
    def _runtime_scope(login: str | None, widget_key: str) -> str:
        return f"{str(login or '*')}::{widget_key}"

    def _runtime_override_for(self, key: str, login: str | None) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        scopes = [self._runtime_scope(None, key)]
        if login:
            scopes.insert(0, self._runtime_scope(login, key))
        for rows in self._runtime_overrides.values():
            for scope in scopes:
                row = rows.get(scope)
                if row:
                    candidates.append(row)
        if not candidates:
            return {}
        best = max(candidates, key=lambda r: int(r.get("__seq", 0) or 0))
        return dict(best)

    async def _on_runtime_override_set_signal(self,
                                              owner: str | None = None,
                                              widget_key: str | None = None,
                                              login: str | None = None,
                                              enabled: bool | None = None,
                                              x: float | None = None,
                                              y: float | None = None,
                                              w: float | None = None,
                                              h: float | None = None,
                                              drive_mode: str | None = None,
                                              anim_dir: str | None = None,
                                              anim_duration_ms: int | None = None,
                                              anim_delay_ms: int | None = None,
                                              pos: dict[str, Any] | None = None,
                                              **kwargs) -> None:
        if not owner or not widget_key:
            return
        await self.set_runtime_override(
            owner=owner,
            widget_key=widget_key,
            login=login,
            enabled=enabled,
            x=x,
            y=y,
            w=w,
            h=h,
            drive_mode=drive_mode,
            anim_dir=anim_dir,
            anim_duration_ms=anim_duration_ms,
            anim_delay_ms=anim_delay_ms,
            pos=pos,
        )

    async def _on_runtime_override_clear_signal(self,
                                                owner: str | None = None,
                                                widget_key: str | None = None,
                                                login: str | None = None,
                                                **kwargs) -> None:
        if not owner or not widget_key:
            return
        await self.clear_runtime_override(owner=owner, widget_key=widget_key, login=login)

    async def _on_runtime_override_clear_owner_signal(self,
                                                      owner: str | None = None,
                                                      **kwargs) -> None:
        if not owner:
            return
        await self.clear_runtime_owner(owner)

    async def set_runtime_override(self,
                                   *,
                                   owner: str,
                                   widget_key: str,
                                   login: str | None = None,
                                   enabled: bool | None = None,
                                   x: float | None = None,
                                   y: float | None = None,
                                   w: float | None = None,
                                   h: float | None = None,
                                   drive_mode: str | None = None,
                                   anim_dir: str | None = None,
                                   anim_duration_ms: int | None = None,
                                   anim_delay_ms: int | None = None,
                                   pos: dict[str, Any] | None = None) -> None:
        if not owner or not widget_key:
            return
        row: dict[str, Any] = {}
        if enabled is not None:
            row["enabled"] = bool(enabled)
        src = dict(pos or {})
        if x is not None:
            src["x"] = x
        if y is not None:
            src["y"] = y
        if w is not None:
            src["w"] = w
        if h is not None:
            src["h"] = h
        for fld in ("x", "y", "w", "h"):
            if fld not in src:
                continue
            try:
                row[fld] = float(src[fld])
            except (TypeError, ValueError):
                continue
        if drive_mode is not None:
            dm = str(drive_mode or "").strip().lower()
            if dm in _DRIVE_MODE_OPTIONS:
                row["drive_mode"] = dm
        if anim_dir is not None:
            ad = str(anim_dir or "").strip().lower()
            if ad in _ANIM_DIR_OPTIONS:
                row["anim_dir"] = ad
        if anim_duration_ms is not None:
            try:
                row["anim_duration_ms"] = max(0, int(anim_duration_ms))
            except (TypeError, ValueError):
                pass
        if anim_delay_ms is not None:
            try:
                row["anim_delay_ms"] = max(0, int(anim_delay_ms))
            except (TypeError, ValueError):
                pass
        if not row:
            return
        self._runtime_seq += 1
        row["__seq"] = self._runtime_seq
        rows = self._runtime_overrides.setdefault(str(owner), {})
        rows[self._runtime_scope(login, widget_key)] = row
        await self._refresh_runtime_targets(login)

    async def clear_runtime_override(self,
                                     *,
                                     owner: str,
                                     widget_key: str,
                                     login: str | None = None) -> None:
        rows = self._runtime_overrides.get(str(owner))
        if not rows:
            return
        scope = self._runtime_scope(login, widget_key)
        if scope not in rows:
            return
        rows.pop(scope, None)
        if not rows:
            self._runtime_overrides.pop(str(owner), None)
        await self._refresh_runtime_targets(login)

    async def clear_runtime_owner(self, owner: str) -> None:
        if str(owner) not in self._runtime_overrides:
            return
        self._runtime_overrides.pop(str(owner), None)
        await self._refresh_runtime_targets(None)

    async def _refresh_runtime_targets(self, login: str | None) -> None:
        if login:
            await self._refresh_all_widget_frames(login)
            return
        try:
            online = list(self.instance.player_manager.online)
        except Exception:
            online = []
        for p in online:
            lg = getattr(p, "login", None)
            if not lg:
                continue
            await self._refresh_all_widget_frames(lg)

    async def group_next(self, group_key: str, login: str) -> None:
        group_key = self._normalize_group_key(group_key)
        if not group_key:
            return
        if not self._group_cfg(group_key).get("runtime_next_enabled", True):
            return
        if self._group_runtime_debounced(login, group_key):
            return
        keys = self._group_candidate_keys(group_key, login)
        if len(keys) <= 1:
            return
        row = self._group_runtime_row(login, group_key)
        idx = row.get("manual_index")
        if idx is None or not isinstance(idx, int):
            idx = 0
        else:
            idx = (idx + 1) % len(keys)
        row["manual_index"] = idx
        row["manual_until"] = None
        await self._refresh_group_for_login(group_key, login)

    async def group_prev(self, group_key: str, login: str) -> None:
        group_key = self._normalize_group_key(group_key)
        if not group_key:
            return
        if not self._group_cfg(group_key).get("runtime_prev_enabled", True):
            return
        if self._group_runtime_debounced(login, group_key):
            return
        keys = self._group_candidate_keys(group_key, login)
        if len(keys) <= 1:
            return
        row = self._group_runtime_row(login, group_key)
        idx = row.get("manual_index")
        if idx is None or not isinstance(idx, int):
            idx = len(keys) - 1
        else:
            idx = (idx - 1) % len(keys)
        row["manual_index"] = idx
        row["manual_until"] = None
        await self._refresh_group_for_login(group_key, login)

    async def group_set_auto(self, group_key: str, login: str) -> None:
        group_key = self._normalize_group_key(group_key)
        if not group_key:
            return
        if not self._group_cfg(group_key).get("runtime_auto_enabled", True):
            return
        row = self._group_runtime_row(login, group_key)
        row["manual_index"] = None
        row["manual_until"] = None
        await self._refresh_group_for_login(group_key, login)

    async def group_pin(self, group_key: str, login: str,
                        widget_key: str | None = None,
                        ttl: float | None = None) -> None:
        group_key = self._normalize_group_key(group_key)
        if not group_key:
            return
        if not self._group_cfg(group_key).get("runtime_pin_enabled", True):
            return
        if self._group_runtime_debounced(login, group_key):
            return
        keys = self._group_candidate_keys(group_key, login)
        if not keys:
            return
        if widget_key and widget_key in keys:
            idx = keys.index(widget_key)
        else:
            row0 = self._group_runtime_row(login, group_key)
            cur = row0.get("manual_index")
            idx = int(cur) if isinstance(cur, int) and 0 <= int(cur) < len(keys) else 0
        row = self._group_runtime_row(login, group_key)
        row["manual_index"] = idx
        ttl_sec = max(0.5, float(ttl or 10.0))
        row["manual_until"] = time.monotonic() + ttl_sec
        await self._refresh_group_for_login(group_key, login)

    async def _refresh_group_for_login(self, group_key: str, login: str) -> None:
        for widget_key in sorted(self.entries.keys()):
            beh = self.resolve_behavior(widget_key, login=login)
            if self._normalize_group_key(beh.get("group_key")) != group_key:
                continue
            app = self._find_widget_app(widget_key)
            if app is None or app.view is None:
                continue
            try:
                await app.view.display(player_logins=[login])
            except Exception:
                logger.exception("widgets: refresh group '%s' member '%s' failed", group_key, widget_key)

    async def _on_player_disconnect(self, player, **kwargs) -> None:
        login = getattr(player, "login", None)
        if not login:
            return
        self._editing.discard(login)
        self._scope.pop(login, None)
        self._selected.pop(login, None)
        self._selected_group.pop(login, None)
        self._editor_tab.pop(login, None)
        self._step.pop(login, None)
        self._open_combo.pop(login, None)
        self._armed_del_group.pop(login, None)
        self._group_runtime.pop(login, None)

    async def _on_player_connect(self, player, **kwargs) -> None:
        # Persistent widgets are only sent at WidgetAppBase.on_start() to
        # players already online. A player who connects (or reconnects)
        # later otherwise never gets the manialink pushed to them.
        login = getattr(player, "login", None)
        if not login:
            return
        for entry in self.entries.values():
            if entry.kind != WidgetKind.PERSISTENT:
                continue
            app = self._find_widget_app(entry.key)
            if app is None or app.view is None:
                continue
            try:
                await app.view.display(player_logins=[login])
            except Exception:
                logger.exception("widgets: reconnect push '%s' failed", entry.key)

    async def _set_phase(self, phase: str) -> None:
        if phase == self._phase:
            return
        self._phase = phase
        try:
            online = list(self.instance.player_manager.online)
        except Exception:
            online = []
        for p in online:
            login = getattr(p, "login", None)
            if login:
                await self._refresh_all_widget_frames(login)

    async def _on_phase_pre(self, **kwargs) -> None:
        await self._set_phase("pre_race")

    async def _on_phase_loading(self, **kwargs) -> None:
        await self._set_phase("loading_map")

    async def _on_phase_warmup(self, **kwargs) -> None:
        await self._set_phase("warmup")

    async def _on_phase_race(self, **kwargs) -> None:
        await self._set_phase("in_race")

    async def _on_phase_podium(self, **kwargs) -> None:
        await self._set_phase("in_podium")

    async def _on_phase_post(self, **kwargs) -> None:
        await self._set_phase("post_race")

    def _state_visible(self, state_modes: Any) -> bool:
        modes = self._normalize_state_modes(state_modes)
        if "all" in modes:
            return True
        return self._phase in modes

    # ---- public API for widget views -----------------------------------

    def resolve_position(self, key: str, login: str) -> dict[str, float]:
        out = self._resolve_position_raw(key, login)
        # When the widget is assigned to a group, the group's anchor takes
        # precedence over the per-widget stored position so all members
        # render inside the same slot. Each widget keeps its own w/h.
        beh = self._resolve_behavior_core(key, login=login)
        gk = self._normalize_group_key(beh.get("group_key")) if beh else ""
        if gk:
            cfg = self._group_cfg(gk)
            out["x"] = float(cfg.get("anchor_x", out.get("x", 0.0)))
            out["y"] = float(cfg.get("anchor_y", out.get("y", 0.0)))
        rt = self._runtime_override_for(key, login)
        for fld in ("x", "y", "w", "h"):
            if fld in rt:
                out[fld] = float(rt[fld])
        cal = self.storage.get_ui_offset(login)
        out["x"] = self._apply_edge_offset_x(
            float(out.get("x", 0.0)),
            float(out.get("w", 0.0)),
            float(cal.get("x", 0.0)),
        )
        y_uns = self._apply_unstretch_y(
            float(out.get("y", 0.0)),
            float(cal.get("stretch", 0.0)),
        )
        out["y"] = y_uns + float(cal.get("y", 0.0))
        return out

    def _resolve_position_raw(self, key: str, login: str) -> dict[str, float]:
        entry = self.entries.get(key)
        if entry is None:
            return {}
        defaults = {
            "x": entry.default_x,
            "y": entry.default_y,
            "w": entry.default_w,
            "h": entry.default_h,
        }
        if not self.allow_personal(key):
            return {**defaults, **self.storage.global_pos(key)}
        return self.storage.resolve(key, login, defaults)

    def _effective_to_raw(self, login: str, key: str, pos: dict[str, float]) -> dict[str, float]:
        """Convert rendered/screen position into stored raw position."""
        cal = self.storage.get_ui_offset(login)
        out = dict(pos)
        w = 0.0
        try:
            cur = self._resolve_position_raw(key, login)
            w = float(cur.get("w", 0.0))
        except Exception:
            w = 0.0
        if "x" in out:
            x_no_edge = self._remove_edge_offset_x(
                float(out["x"]),
                w,
                float(cal.get("x", 0.0)),
            )
            out["x"] = x_no_edge
        if "y" in out:
            y_no_off = float(out["y"]) - float(cal.get("y", 0.0))
            out["y"] = self._remove_unstretch_y(
                y_no_off,
                float(cal.get("stretch", 0.0)),
            )
        return out

    def resolve_behavior(self, key: str, login: str | None = None) -> dict[str, Any]:
        out = self._resolve_behavior_core(key, login)
        if not out:
            return {}
        out["group_visible"] = self._is_group_winner(key, login)
        return out

    def _resolve_behavior_core(self, key: str, login: str | None = None) -> dict[str, Any]:
        entry = self.entries.get(key)
        if entry is None:
            return {}
        default_mode = self._default_drive_mode(entry)
        default_state = self._default_state_mode(entry)
        defaults = {
            "hide_while_driving": default_mode == "hide_while_driving",
            "drive_mode": default_mode,
            "state_modes": [default_state],
            "group_key": self._normalize_group_key(getattr(entry, "group_key", "")),
            "group_member_enabled": True,
            "group_priority": int(getattr(entry, "group_priority", 0) or 0),
            "group_order": int(getattr(entry, "group_order", 0) or 0),
            "anim_dir": entry.animation.direction,
            "anim_duration_ms": entry.animation.duration_ms,
            "anim_delay_ms": entry.animation.delay_ms,
            "allow_personal": bool(entry.allow_personal),
            "widget_disabled": False,
        }
        out = self.storage.resolve_behavior(key, defaults, login=login)
        drive_mode = str(out.get("drive_mode") or default_mode)
        if drive_mode not in _DRIVE_MODE_OPTIONS:
            drive_mode = default_mode
        if "hide_while_driving" in out:
            if bool(out.get("hide_while_driving", False)):
                drive_mode = "hide_while_driving"
            elif drive_mode == "hide_while_driving":
                drive_mode = "fixed"
        state_modes = self._normalize_state_modes(out.get("state_modes") or [default_state])
        group_key = self._normalize_group_key(out.get("group_key"))
        group_member_enabled = bool(out.get("group_member_enabled", True))
        try:
            group_priority = int(out.get("group_priority", getattr(entry, "group_priority", 0) or 0))
        except (TypeError, ValueError):
            group_priority = int(getattr(entry, "group_priority", 0) or 0)
        try:
            group_order = int(out.get("group_order", getattr(entry, "group_order", 0) or 0))
        except (TypeError, ValueError):
            group_order = int(getattr(entry, "group_order", 0) or 0)
        anim_dir = str(out.get("anim_dir") or entry.animation.direction)
        if "@" in anim_dir:
            anim_dir = anim_dir.split("@", 1)[0]
        if anim_dir not in _ANIM_DIR_OPTIONS:
            anim_dir = entry.animation.direction
        hide = drive_mode == "hide_while_driving"
        show = drive_mode == "only_shown_while_driving"
        out["hide_while_driving"] = hide
        out["show_while_driving"] = show
        out["drive_mode"] = drive_mode
        out["state_modes"] = state_modes
        out["state_mode"] = state_modes[0] if state_modes else "all"
        out["state_visible"] = self._state_visible(state_modes)
        out["group_key"] = group_key
        out["group_member_enabled"] = group_member_enabled
        out["group_priority"] = group_priority
        out["group_order"] = group_order
        out["anim_dir"] = anim_dir
        rt = self._runtime_override_for(key, login)
        out["runtime_enabled"] = bool(rt.get("enabled", True))
        if "drive_mode" in rt:
            dm = str(rt.get("drive_mode") or "").strip().lower()
            if dm in _DRIVE_MODE_OPTIONS:
                drive_mode = dm
                hide = drive_mode == "hide_while_driving"
                show = drive_mode == "only_shown_while_driving"
                out["hide_while_driving"] = hide
                out["show_while_driving"] = show
                out["drive_mode"] = drive_mode
        if "anim_dir" in rt:
            ad = str(rt.get("anim_dir") or "").lower()
            if ad in _ANIM_DIR_OPTIONS:
                out["anim_dir"] = ad
        if "anim_duration_ms" in rt:
            try:
                out["anim_duration_ms"] = max(0, int(rt.get("anim_duration_ms") or 0))
            except (TypeError, ValueError):
                pass
        if "anim_delay_ms" in rt:
            try:
                out["anim_delay_ms"] = max(0, int(rt.get("anim_delay_ms") or 0))
            except (TypeError, ValueError):
                pass
        # Per-widget strip_prefer_top override (master-admin). Falls back to
        # the widget's class default WIDGET_STRIP_PREFER_TOP when unset.
        ov = self.storage.strip_prefer_top.get(key)
        if ov is None:
            app = self._find_widget_app(key)
            ov = bool(getattr(app, "WIDGET_STRIP_PREFER_TOP", False)) if app is not None else False
        out["strip_prefer_top"] = bool(ov)
        out["widget_disabled"] = bool(out.get("widget_disabled", False))
        return out

    def _group_members(self, group_key: str, login: str | None = None) -> list[tuple[WidgetEntry, dict[str, Any]]]:
        group_key = self._normalize_group_key(group_key)
        if not group_key:
            return []
        members: list[tuple[WidgetEntry, dict[str, Any]]] = []
        for e in self.entries.values():
            beh = self._resolve_behavior_core(e.key, login=login)
            if self._normalize_group_key(beh.get("group_key")) != group_key:
                continue
            members.append((e, beh))
        members.sort(
            key=lambda it: (
                -int(it[1].get("group_priority", 0) or 0),
                int(it[1].get("group_order", 0) or 0),
                str(getattr(it[0], "key", "")),
            )
        )
        return members

    def _group_visible_members(self, group_key: str, login: str | None = None) -> list[tuple[WidgetEntry, dict[str, Any]]]:
        out: list[tuple[WidgetEntry, dict[str, Any]]] = []
        for cand, beh in self._group_members(group_key, login=login):
            if cand.kind != WidgetKind.PERSISTENT:
                continue
            if not bool(getattr(cand, "enabled", True)):
                continue
            if not beh:
                continue
            if bool(beh.get("widget_disabled", False)):
                continue
            if not bool(beh.get("runtime_enabled", True)):
                continue
            if not bool(beh.get("group_member_enabled", True)):
                continue
            if not bool(beh.get("state_visible", True)):
                continue
            out.append((cand, beh))
        return out

    def _group_ordered_keys_for_mode(self, group_key: str,
                                     login: str | None = None) -> list[str]:
        visible = self._group_visible_members(group_key, login=login)
        if not visible:
            return []
        cfg = self._group_cfg(group_key)
        mode = self._normalize_group_mode(cfg.get("mode"))
        if mode == "first_visible":
            ordered = sorted(
                visible,
                key=lambda it: (
                    int(it[1].get("group_order", 0) or 0),
                    -int(it[1].get("group_priority", 0) or 0),
                    str(it[0].key),
                )
            )
        elif mode == "fixed_member":
            fixed = str(cfg.get("fixed_widget_key") or "")
            ordered = list(visible)
            if fixed:
                ordered.sort(
                    key=lambda it: (
                        0 if it[0].key == fixed else 1,
                        -int(it[1].get("group_priority", 0) or 0),
                        int(it[1].get("group_order", 0) or 0),
                        str(it[0].key),
                    )
                )
        else:
            ordered = list(visible)
        return [cand.key for cand, _ in ordered]

    def _group_candidate_keys(self, group_key: str, login: str) -> list[str]:
        group_key = self._normalize_group_key(group_key)
        if not group_key:
            return []
        keys = self._group_ordered_keys_for_mode(group_key, login=login)
        row = self._group_runtime_row(login, group_key)
        if not keys:
            row["manual_index"] = None
            row["manual_until"] = None
            return []
        if self._group_runtime_active(login, group_key):
            idx = row.get("manual_index")
            if not isinstance(idx, int) or idx < 0:
                idx = 0
            if idx >= len(keys):
                idx = idx % len(keys)
                row["manual_index"] = idx
            keys = keys[idx:] + keys[:idx]
        return keys

    def _group_visible_keys(self, group_key: str, login: str) -> set[str]:
        keys = self._group_candidate_keys(group_key, login)
        if not keys:
            return set()
        cfg = self._group_cfg(group_key)
        try:
            max_visible = max(1, int(cfg.get("max_visible", 1) or 1))
        except (TypeError, ValueError):
            max_visible = 1
        return set(keys[:max_visible])

    def _is_group_winner(self, key: str, login: str | None) -> bool:
        entry = self.entries.get(key)
        if entry is None:
            return False
        beh_self = self._resolve_behavior_core(key, login=login)
        group_key = self._normalize_group_key(beh_self.get("group_key"))
        if not group_key or not login:
            return True
        visible_keys = self._group_visible_keys(group_key, login)
        if not visible_keys:
            return True
        return key in visible_keys

    def allow_personal(self, key: str) -> bool:
        """Effective personalization flag (class default + DB override)."""
        beh = self.resolve_behavior(key)
        return bool(beh.get("allow_personal", True))

    def is_widget_disabled(self, key: str) -> bool:
        """True if a master admin has disabled this widget in the global config.

        Disabled widgets never render, never popup, and are excluded from
        group rotations — even if their providing app is installed and
        currently registered."""
        beh = self.resolve_behavior(key)
        return bool(beh.get("widget_disabled", False))

    def is_editing(self, login: str) -> bool:
        return login in self._editing

    def can_edit_widget(self, key: str, login: str) -> bool:
        """Whether ``login`` is allowed to interact with this widget in
        the editor. Masters always may; everyone else only if the widget
        is enabled AND opted into personalization. Used by the widget
        frame to gate the EditorOn override that bypasses ForceHidden,
        so disabled widgets never pop up for impersonated viewers."""
        try:
            from pyplanet.apps.tmsm.ui import perms as _perms
            if _perms.is_master(login):
                return True
        except Exception:
            pass
        if self.is_widget_disabled(key):
            return False
        return self.allow_personal(key)

    def is_debug(self, login: str, key: str | None = None) -> bool:
        """Whether the master-admin debug overlay is on for ``login``.

        When ``key`` is given, the overlay is only active for the widget
        currently selected in that login's editor.
        """
        if login not in self._debug:
            return False
        if key is None:
            return True
        return self._selected.get(login) == key

    async def set_ui_offset(self, login: str, x: float, y: float) -> None:
        """Set a per-login monitor calibration offset and repaint widgets."""
        await self.storage.set_ui_offset(login, x, y)
        await self._refresh_all_widget_frames(login)

    async def clear_ui_offset(self, login: str) -> None:
        """Clear per-login monitor calibration and repaint widgets."""
        await self.storage.clear_ui_offset(login)
        await self._refresh_all_widget_frames(login)

    async def set_ui_stretch(self, login: str, stretch: float) -> None:
        """Set per-login display stretch compensation and repaint widgets."""
        await self.storage.set_ui_stretch(login, stretch)
        await self._refresh_all_widget_frames(login)

    def get_ui_stretch(self, login: str) -> float:
        """Current per-login display stretch compensation percentage."""
        return float(self.storage.get_ui_offset(login).get("stretch", 0.0))

    def get_ui_scale_y(self, login: str) -> float:
        """Current per-login vertical content scale factor."""
        return self._unstretch_scale_y(self.get_ui_stretch(login))

    def get_ui_offset(self, login: str) -> dict[str, float]:
        """Current per-login monitor calibration offset."""
        return self.storage.get_ui_offset(login)

    # ---- global strip settings (master-admin) --------------------------

    def get_global_strip_color_override(self) -> str:
        """Empty string = no override; each widget keeps its own strip color.
        Non-empty rgba/rgb string = applied to every widget's strip."""
        return str(getattr(self.storage, "strip_color_override", "") or "")

    def get_global_strip_thickness(self) -> float:
        return float(getattr(self.storage, "strip_thickness", 1.0) or 1.0)

    def get_global_bg_color_override(self) -> str:
        """Empty string = no override; each widget keeps its own bg color.
        Non-empty rgba/rgb string = applied to every widget's frame bg."""
        return str(getattr(self.storage, "bg_color_override", "") or "")

    async def set_global_strip_color_override(self, value: str) -> None:
        self.storage.strip_color_override = _normalize_color_hex(value)
        await self.storage.set_theme_override("__frame__", "strip_color", self.storage.strip_color_override)

    async def set_global_strip_thickness(self, value: float) -> None:
        try:
            v = float(value)
        except (TypeError, ValueError):
            v = 1.0
        self.storage.strip_thickness = max(0.0, min(5.0, v))
        await self.storage.set_theme_override("__frame__", "strip_thickness", "{:.3f}".format(self.storage.strip_thickness))

    async def set_global_bg_color_override(self, value: str) -> None:
        self.storage.bg_color_override = _normalize_color_hex(value)
        await self.storage.set_theme_override("__frame__", "bg_color", self.storage.bg_color_override)

    def get_strip_prefer_top(self, key: str) -> bool | None:
        """Per-widget override; None means "use the widget's class default"."""
        return self.storage.strip_prefer_top.get(key)

    async def set_strip_prefer_top(self, key: str, value: bool | None) -> None:
        if value is None:
            self.storage.strip_prefer_top.pop(key, None)
        else:
            self.storage.strip_prefer_top[key] = bool(value)
        await self.storage.set_behavior(key, {"strip_prefer_top": value})

    # ---- commands ------------------------------------------------------

    async def _cmd_widgets(self, player, data, **kwargs) -> None:
        login = player.login
        if login in self._editing:
            await self._close_editor(login)
        else:
            await self._open_editor(login)

    async def _open_editor(self, login: str) -> None:
        self._editing.add(login)
        # Only master admins can edit global config. Everyone else is
        # locked to personal scope and only sees widgets that allow it.
        is_master = await self._login_is_master(login)
        if not is_master:
            self._scope[login] = "player"
        else:
            self._scope.setdefault(login, "global")
        self._step.setdefault(login, 1.0)
        self._editor_tab.setdefault(login, "widgets")
        if self._selected.get(login) is None and self.entries:
            self._selected[login] = sorted(self.entries.keys())[0]
        if self._selected_group.get(login) is None:
            groups = self.storage.list_groups()
            self._selected_group[login] = groups[0]["key"] if groups else ""
        try:
            sig = self.context.signals.get_signal("tmsm_widgets:edit_mode")
            await sig.send_robust({"login": login, "active": True}, raw=True)
        except Exception:
            pass
        await self._refresh_editor_for([login])
        await self._refresh_all_widget_frames(login)

    async def _close_editor(self, login: str) -> None:
        self._editing.discard(login)
        self._open_combo.pop(login, None)
        self._armed_del_group.pop(login, None)
        try:
            sig = self.context.signals.get_signal("tmsm_widgets:edit_mode")
            await sig.send_robust({"login": login, "active": False}, raw=True)
        except Exception:
            pass
        if self.editor is not None:
            # BaseView.hide() destroys the underlying manialink (and nulls
            # its data), which would break the next display(). Use the raw
            # TemplateView per-player hide so we keep the editor alive.
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.editor, player_logins=[login])
            except Exception:
                logger.exception("widgets: editor per-player hide failed")
        await self._refresh_all_widget_frames(login)

    # ---- editor render -------------------------------------------------

    async def _refresh_editor_for(self, logins: list[str]) -> None:
        if self.editor is None or not logins:
            return
        try:
            await self.editor.display(player_logins=logins)
        except Exception:
            logger.exception("widgets: editor display failed")

    async def _refresh_all_widget_frames(self, login: str) -> None:
        """Refresh every widget view for one player (so edit-mode UI toggles)."""
        for entry in self.entries.values():
            app = self._find_widget_app(entry.key)
            if app is None or app.view is None:
                continue
            try:
                await app.view.display(player_logins=[login])
            except Exception:
                logger.exception("widgets: refresh '%s' failed", entry.key)

    def _find_widget_app(self, key: str):
        """Walk PyPlanet's app registry for a WidgetAppBase with this key."""
        try:
            apps = self.instance.apps.apps.values()
        except Exception:
            return None
        for app in apps:
            if getattr(app, "WIDGET_KEY", None) == key:
                return app
        return None

    # ---- editor actions ------------------------------------------------

    async def _editor_catch_all(self, player, action, values, **kwargs) -> None:
        login = player.login
        # PyPlanet strips the "<id>__" prefix before calling catch-all,
        # so we receive "<verb>__<arg>" here.
        try:
            verb, arg = action.split("__", 1)
        except ValueError:
            logger.warning("widgets editor: unrecognised action %s", action)
            return
        handler = getattr(self, f"_act_{verb}", None)
        if handler is None:
            logger.warning("widgets editor: no handler for verb '%s'", verb)
            return
        try:
            await handler(login, arg, values or {})
        except Exception:
            logger.exception("widgets editor: action '%s' raised", action)

    async def _act_select(self, login: str, key: str, _values: dict) -> None:
        if key in self.entries:
            self._selected[login] = key
            self._open_combo.pop(login, None)
            await self._refresh_editor_for([login])
            # debug overlay follows the selection -> repaint widget frames
            if login in self._debug:
                await self._refresh_all_widget_frames(login)

    async def _act_mode(self, login: str, arg: str, _values: dict) -> None:
        if not arg.startswith("tab__"):
            return
        tab = self._normalize_editor_tab(arg[len("tab__"):])
        if tab == "groups" and not await self._login_is_master(login):
            await self._toast(login, "groups tab is master-admin only", "warning")
            return
        if tab == "frame" and not await self._login_is_master(login):
            await self._toast(login, "frame tab is master-admin only", "warning")
            return
        self._editor_tab[login] = tab
        self._open_combo.pop(login, None)
        self._armed_del_group.pop(login, None)
        await self._refresh_editor_for([login])

    async def _act_selectgroup(self, login: str, group_key: str, _values: dict) -> None:
        if self.storage.group_by_key(group_key) is None:
            return
        self._selected_group[login] = group_key
        self._armed_del_group.pop(login, None)
        await self._refresh_editor_for([login])

    def _next_group_key(self) -> str:
        existing = {g.get("key", "") for g in self.storage.list_groups()}
        idx = 1
        while True:
            candidate = f"group_{idx}"
            if candidate not in existing:
                return candidate
            idx += 1

    async def _act_newgroup(self, login: str, _arg: str, _values: dict) -> None:
        if not await self._login_is_master(login):
            await self._toast(login, "groups are master-admin only", "warning")
            return
        key = self._next_group_key()
        await self.storage.set_group(key, {
            "label": key.replace("_", " ").title(),
            "description": "",
            "order": len(self.storage.list_groups()),
        })
        self._selected_group[login] = key
        self._editor_tab[login] = "groups"
        self._armed_del_group.pop(login, None)
        await self._refresh_editor_for([login])
        await self._toast(login, f"group created: {key}", "success")

    async def _act_savegroup(self, login: str, group_key: str, values: dict) -> None:
        if not await self._login_is_master(login):
            await self._toast(login, "groups are master-admin only", "warning")
            return
        key = group_key or self._selected_group.get(login, "")
        if not key or self.storage.group_by_key(key) is None:
            await self._toast(login, "no group selected", "warning")
            return
        label = values.get(f"entry_group_{key}_label")
        desc = values.get(f"entry_group_{key}_description")
        order_raw = values.get(f"entry_group_{key}_order")
        mode_raw = values.get(f"entry_group_{key}_mode")
        fixed_raw = values.get(f"entry_group_{key}_fixed_widget_key")
        max_visible_raw = values.get(f"entry_group_{key}_max_visible")
        patch: dict[str, Any] = {}
        if label is not None:
            patch["label"] = str(label).strip() or key
        if desc is not None:
            patch["description"] = str(desc).strip()
        if order_raw is not None and order_raw != "":
            try:
                patch["order"] = int(float(order_raw))
            except (TypeError, ValueError):
                patch["order"] = 0
        for fld in ("anchor_x", "anchor_y", "anchor_w", "anchor_h"):
            raw = values.get(f"entry_group_{key}_{fld}")
            if raw is None or raw == "":
                continue
            try:
                patch[fld] = float(raw)
            except (TypeError, ValueError):
                continue
        if mode_raw is not None:
            patch["mode"] = self._normalize_group_mode(mode_raw)
        if fixed_raw is not None:
            patch["fixed_widget_key"] = "" if str(fixed_raw) == "__none__" else str(fixed_raw)
        if max_visible_raw is not None and max_visible_raw != "":
            try:
                patch["max_visible"] = max(1, int(float(max_visible_raw)))
            except (TypeError, ValueError):
                patch["max_visible"] = 1
        for fld in (
            "runtime_prev_enabled",
            "runtime_next_enabled",
            "runtime_auto_enabled",
            "runtime_pin_enabled",
        ):
            raw = values.get(f"entry_group_{key}_{fld}")
            if raw is not None:
                patch[fld] = bool(raw)
        if not patch:
            await self._toast(login, "nothing to save", "warning")
            return
        await self.storage.set_group(key, patch)
        self._armed_del_group.pop(login, None)
        await self._refresh_editor_for([login])
        for wk in self._group_usage_map().get(key, []):
            await self._refresh_widget_for_all(wk)
        await self._toast(login, f"group saved: {key}", "success")

    async def _act_groupmode(self, login: str, arg: str, _values: dict) -> None:
        mode = ""
        if arg.endswith("__toggle"):
            key = arg[:-len("__toggle")]
            combo_name = f"groupmode__{key}"
            if self._open_combo.get(login) == combo_name:
                self._open_combo.pop(login, None)
            else:
                self._open_combo[login] = combo_name
            await self._refresh_editor_for([login])
            return
        if "__pick__" in arg:
            key, mode = arg.split("__pick__", 1)
            self._open_combo.pop(login, None)
        elif "__set__" in arg:
            key, mode = arg.split("__set__", 1)
        else:
            return
        if not await self._login_is_master(login):
            await self._toast(login, "groups are master-admin only", "warning")
            return
        if self.storage.group_by_key(key) is None:
            return
        mode = self._normalize_group_mode(mode)
        await self.storage.set_group(key, {"mode": mode})
        await self._refresh_editor_for([login])
        for widget_key in self.entries.keys():
            beh = self.resolve_behavior(widget_key)
            if self._normalize_group_key(beh.get("group_key")) == key:
                await self._refresh_widget_for_all(widget_key)

    async def _act_groupfixed(self, login: str, arg: str, _values: dict) -> None:
        value = ""
        if arg.endswith("__toggle"):
            key = arg[:-len("__toggle")]
            combo_name = f"groupfixed__{key}"
            if self._open_combo.get(login) == combo_name:
                self._open_combo.pop(login, None)
            else:
                self._open_combo[login] = combo_name
            await self._refresh_editor_for([login])
            return
        if "__pick__" in arg:
            key, value = arg.split("__pick__", 1)
            self._open_combo.pop(login, None)
        elif "__set__" in arg:
            key, value = arg.split("__set__", 1)
        else:
            return
        if not await self._login_is_master(login):
            await self._toast(login, "groups are master-admin only", "warning")
            return
        if self.storage.group_by_key(key) is None:
            return
        fixed = "" if value in ("", "__none__") else value
        await self.storage.set_group(key, {"fixed_widget_key": fixed})
        await self._refresh_editor_for([login])
        for widget_key in self.entries.keys():
            beh = self.resolve_behavior(widget_key)
            if self._normalize_group_key(beh.get("group_key")) == key:
                await self._refresh_widget_for_all(widget_key)

    async def _act_groupmember(self, login: str, key: str, _values: dict) -> None:
        if key not in self.entries:
            return
        if not await self._login_is_master(login):
            await self._toast(login, "global config is master-admin only", "warning")
            return
        cur = self.resolve_behavior(key)
        new_val = not bool(cur.get("group_member_enabled", True))
        await self.storage.set_behavior(key, {"group_member_enabled": new_val})
        await self._refresh_editor_for([login])
        await self._refresh_widget_for_all(key)

    async def _act_groupruntime(self, login: str, arg: str, _values: dict) -> None:
        if "|" not in arg:
            return
        key, flag = arg.split("|", 1)
        if not await self._login_is_master(login):
            await self._toast(login, "groups are master-admin only", "warning")
            return
        cfg = self.storage.group_by_key(key)
        if cfg is None:
            return
        fld = {
            "prev": "runtime_prev_enabled",
            "next": "runtime_next_enabled",
            "auto": "runtime_auto_enabled",
            "pin": "runtime_pin_enabled",
        }.get(flag)
        if fld is None:
            return
        cur = bool(cfg.get(fld, True))
        await self.storage.set_group(key, {fld: not cur})
        await self._refresh_editor_for([login])

    async def _act_groupnudge(self, login: str, arg: str, _values: dict) -> None:
        """Anchor move/resize: arg = '<group_key>|<verb>'.

        verbs: left, right, up, down (move anchor x/y),
               w_inc, w_dec, h_inc, h_dec (resize anchor w/h)."""
        if "|" not in arg:
            return
        key, verb = arg.split("|", 1)
        if not await self._login_is_master(login):
            await self._toast(login, "groups are master-admin only", "warning")
            return
        cfg = self.storage.group_by_key(key)
        if cfg is None:
            return
        step = float(self._step.get(login, 1.0) or 1.0)
        anchor_x = float(cfg.get("anchor_x", 0.0) or 0.0)
        anchor_y = float(cfg.get("anchor_y", 0.0) or 0.0)
        anchor_w = float(cfg.get("anchor_w", 18.0) or 18.0)
        anchor_h = float(cfg.get("anchor_h", 8.0) or 8.0)
        patch: dict[str, Any] = {}
        if verb == "left":
            patch["anchor_x"] = anchor_x - step
        elif verb == "right":
            patch["anchor_x"] = anchor_x + step
        elif verb == "up":
            patch["anchor_y"] = anchor_y + step
        elif verb == "down":
            patch["anchor_y"] = anchor_y - step
        elif verb == "w_inc":
            patch["anchor_w"] = max(1.0, anchor_w + step)
        elif verb == "w_dec":
            patch["anchor_w"] = max(1.0, anchor_w - step)
        elif verb == "h_inc":
            patch["anchor_h"] = max(1.0, anchor_h + step)
        elif verb == "h_dec":
            patch["anchor_h"] = max(1.0, anchor_h - step)
        else:
            return
        await self.storage.set_group(key, patch)
        await self._refresh_editor_for([login])
        for wk in self._group_usage_map().get(key, []):
            await self._refresh_widget_for_all(wk)

    async def _act_groupmemberorder(self, login: str, arg: str, _values: dict) -> None:
        """Move a member in a group's order. arg = '<group_key>|<widget_key>|<up|down>'."""
        parts = arg.split("|")
        if len(parts) != 3:
            return
        group_key, widget_key, direction = parts
        if not await self._login_is_master(login):
            await self._toast(login, "global config is master-admin only", "warning")
            return
        if widget_key not in self.entries:
            return
        if self.storage.group_by_key(group_key) is None:
            return
        members = self._group_usage_map().get(group_key, [])
        ordered = sorted(
            members,
            key=lambda k: (
                int(self.resolve_behavior(k).get("group_order", 0) or 0),
                k,
            ),
        )
        if widget_key not in ordered:
            return
        idx = ordered.index(widget_key)
        swap_idx = idx - 1 if direction == "up" else idx + 1 if direction == "down" else -1
        if swap_idx < 0 or swap_idx >= len(ordered):
            return
        a, b = ordered[idx], ordered[swap_idx]
        ordered[idx], ordered[swap_idx] = b, a
        for i, k in enumerate(ordered):
            await self.storage.set_behavior(k, {"group_order": i})
        await self._refresh_editor_for([login])
        for k in ordered:
            await self._refresh_widget_for_all(k)

    async def _act_groupmemberkey(self, login: str, arg: str, _values: dict) -> None:
        """Toggle group_member_enabled on a specific widget from the Groups tab.

        arg = '<widget_key>'. Mirrors _act_groupmember but addressable from
        the groups-tab member list where a different widget is the target."""
        if arg not in self.entries:
            return
        if not await self._login_is_master(login):
            await self._toast(login, "global config is master-admin only", "warning")
            return
        cur = self.resolve_behavior(arg)
        new_val = not bool(cur.get("group_member_enabled", True))
        await self.storage.set_behavior(arg, {"group_member_enabled": new_val})
        await self._refresh_editor_for([login])
        await self._refresh_widget_for_all(arg)

    async def _act_widgetspage(self, login: str, arg: str, _values: dict) -> None:
        await self._page_action(login, self._widgets_page, arg)

    async def _act_groupspage(self, login: str, arg: str, _values: dict) -> None:
        await self._page_action(login, self._groups_page, arg)

    async def _page_action(self, login: str, store: dict[str, int], arg: str) -> None:
        cur = int(store.get(login, 0) or 0)
        if arg == "prev":
            store[login] = max(0, cur - 1)
        elif arg == "next":
            store[login] = cur + 1
        elif arg == "first":
            store[login] = 0
        elif arg == "last":
            store[login] = 10_000
        elif arg.startswith("page__"):
            try:
                n = int(arg[len("page__"):])
            except ValueError:
                return
            store[login] = max(0, n - 1)
        else:
            return
        await self._refresh_editor_for([login])

    async def _act_delgroup(self, login: str, group_key: str, _values: dict) -> None:
        if not await self._login_is_master(login):
            await self._toast(login, "groups are master-admin only", "warning")
            return
        key = group_key or self._selected_group.get(login, "")
        if not key or self.storage.group_by_key(key) is None:
            await self._toast(login, "no group selected", "warning")
            return
        usage = self._group_usage_map()
        members = usage.get(key, [])
        if members and self._armed_del_group.get(login) != key:
            self._armed_del_group[login] = key
            await self._refresh_editor_for([login])
            await self._toast(
                login,
                f"{key} has {len(members)} widget(s). Click Delete again to confirm unassign + delete.",
                "warning",
            )
            return
        touched: list[str] = []
        for widget_key in sorted(self.entries.keys()):
            beh = self.resolve_behavior(widget_key)
            if self._normalize_group_key(beh.get("group_key")) != key:
                continue
            await self.storage.set_behavior(widget_key, {"group_key": ""})
            touched.append(widget_key)
        await self.storage.delete_group(key)
        groups = self.storage.list_groups()
        self._selected_group[login] = groups[0]["key"] if groups else ""
        self._armed_del_group.pop(login, None)
        await self._refresh_editor_for([login])
        for widget_key in touched:
            await self._refresh_widget_for_all(widget_key)
        await self._toast(login, f"group deleted: {key}", "success")

    async def _act_scope(self, login: str, scope: str, _values: dict) -> None:
        # radio_group emits "scope__set__<value>" -> arg arrives as "set__<value>"
        if scope.startswith("set__"):
            scope = scope[len("set__"):]
        if scope == "global" and not await self._login_is_master(login):
            await self._toast(login, "global config is master-admin only", "warning")
            return
        if scope == "player":
            sel = self._selected.get(login)
            entry = self.entries.get(sel) if sel else None
            if entry is not None and not self.allow_personal(sel):
                await self._toast(login, f"{entry.name}: personalization is disabled", "warning")
                return
        if scope in ("global", "player"):
            self._scope[login] = scope
            await self._refresh_editor_for([login])

    async def _login_is_master(self, login: str) -> bool:
        try:
            from pyplanet.apps.tmsm.ui import perms as _perms
            return _perms.is_master(login)
        except Exception:
            return False

    async def _act_step(self, login: str, step: str, _values: dict) -> None:
        opts = [0.25, 0.5, 1.0, 2.0, 5.0, 10.0]
        cur = self._step.get(login, 1.0)
        if step in ("inc", "dec"):
            # snap to nearest preset, then move one slot
            idx = min(range(len(opts)), key=lambda i: abs(opts[i] - cur))
            idx = max(0, min(len(opts) - 1, idx + (1 if step == "inc" else -1)))
            self._step[login] = opts[idx]
        else:
            try:
                self._step[login] = max(0.1, float(step))
            except ValueError:
                return
        await self._refresh_editor_for([login])

    async def _act_nudge(self, login: str, direction: str, _values: dict) -> None:
        key = self._selected.get(login)
        if not key or key not in self.entries:
            return
        step = self._step.get(login, 1.0)
        cur = self._resolve_position_raw(key, login)
        dx = dy = 0.0
        if direction == "left":  dx = -step
        elif direction == "right": dx = step
        elif direction == "up":    dy = step
        elif direction == "down":  dy = -step
        else: return
        await self._write_pos(login, key, {"x": cur.get("x", 0) + dx,
                                          "y": cur.get("y", 0) + dy})

    async def _act_set(self, login: str, key: str, values: dict) -> None:
        if key not in self.entries:
            return
        new_pos: dict[str, float] = {}
        for field in ("x", "y", "w", "h"):
            raw = values.get(f"widget_{key}_{field}")
            if raw is None or raw == "":
                continue
            try:
                new_pos[field] = float(raw)
            except (TypeError, ValueError):
                continue
        beh_patch: dict[str, Any] = {}
        for field, caster in (("anim_duration_ms", int), ("anim_delay_ms", int)):
            raw = values.get(f"entry_widget_{key}_{field}")
            if raw is None or raw == "":
                continue
            try:
                beh_patch[field] = caster(float(raw))
            except (TypeError, ValueError):
                continue
        for field in ("group_priority", "group_order"):
            raw = values.get(f"entry_widget_{key}_{field}")
            if raw is None or raw == "":
                continue
            try:
                beh_patch[field] = int(float(raw))
            except (TypeError, ValueError):
                continue
        if not new_pos and not beh_patch:
            await self._toast(login, f"No values to apply for '{key}'", "warning")
            return
        if new_pos:
            await self._write_pos(login, key, new_pos)
        if beh_patch:
            scope = self._scope.get(login, "global")
            if scope == "player":
                dropped = False
                for fld in ("group_priority", "group_order"):
                    if fld in beh_patch:
                        beh_patch.pop(fld, None)
                        dropped = True
                if dropped:
                    await self._toast(login, "group priority/order are global-only", "warning")
                if not self.allow_personal(key):
                    await self._toast(login, "personalization is disabled", "warning")
                elif beh_patch:
                    await self.storage.set_player_behavior(key, login, beh_patch)
                    await self._refresh_editor_for([login])
                    await self._refresh_widget_for_all(key)
            elif not await self._login_is_master(login):
                await self._toast(login, "global config is master-admin only", "warning")
            else:
                await self.storage.set_behavior(key, beh_patch)
                await self._refresh_editor_for([login])
                await self._refresh_widget_for_all(key)
        entry = self.entries.get(key)
        label = entry.name if entry else key
        await self._toast(login, f"{label}: settings saved", "success", source="widgets")

    async def _act_setdir(self, login: str, arg: str, _values: dict) -> None:
        # radio_group emits "setdir__set__<value>" -> arg arrives as "set__<value>"
        if arg.startswith("set__"):
            arg = arg[len("set__"):]
        try:
            key, direction = arg.split("|", 1)
        except ValueError:
            return
        if key not in self.entries or direction not in _ANIM_DIR_OPTIONS:
            return
        scope = self._scope.get(login, "global")
        is_master = await self._login_is_master(login)
        if scope == "player":
            if not self.allow_personal(key):
                await self._toast(login, "personalization is disabled", "warning")
                return
            await self.storage.set_player_behavior(key, login, {"anim_dir": direction})
        else:
            if not is_master:
                await self._toast(login, "global config is master-admin only", "warning")
                return
            beh = self.resolve_behavior(key)
            await self.storage.set_behavior(key, {
                "anim_dir": direction,
            })
        await self._refresh_editor_for([login])
        await self._refresh_widget_for_all(key)

    async def _act_groupkey(self, login: str, arg: str, _values: dict) -> None:
        value = ""
        if arg.endswith("__toggle"):
            key = arg[:-len("__toggle")]
            combo_name = f"groupkey__{key}"
            if self._open_combo.get(login) == combo_name:
                self._open_combo.pop(login, None)
            else:
                self._open_combo[login] = combo_name
            await self._refresh_editor_for([login])
            return
        if "__pick__" in arg:
            key, value = arg.split("__pick__", 1)
            self._open_combo.pop(login, None)
        elif "__set__" in arg:
            key, value = arg.split("__set__", 1)
        else:
            return
        if key not in self.entries:
            return
        if not await self._login_is_master(login):
            await self._toast(login, "global config is master-admin only", "warning")
            return
        chosen = "" if value in ("", "__none__") else self._normalize_group_key(value)
        if chosen and self.storage.group_by_key(chosen) is None:
            await self._toast(login, "select a registered group", "warning")
            return
        await self.storage.set_behavior(key, {"group_key": chosen})
        await self._refresh_editor_for([login])
        await self._refresh_widget_for_all(key)

    async def _act_drive(self, login: str, key: str, _values: dict) -> None:
        # Backward-compatible action id kept for existing templates.
        await self._act_hidedrive(login, key, _values)

    async def _act_hidedrive(self, login: str, key: str, _values: dict) -> None:
        if key not in self.entries:
            return
        if not await self._login_is_master(login):
            await self._toast(login, "global config is master-admin only", "warning")
            return
        cur = self.resolve_behavior(key)
        current_mode = str(cur.get("drive_mode", "fixed"))
        mode = "fixed" if current_mode == "hide_while_driving" else "hide_while_driving"
        await self.storage.set_behavior(key, {
            "hide_while_driving": mode == "hide_while_driving",
            "drive_mode": mode,
        })
        await self._refresh_editor_for([login])
        await self._refresh_widget_for_all(key)

    async def _act_showdrive(self, login: str, key: str, _values: dict) -> None:
        if key not in self.entries:
            return
        if not await self._login_is_master(login):
            await self._toast(login, "global config is master-admin only", "warning")
            return
        cur = self.resolve_behavior(key)
        current_mode = str(cur.get("drive_mode", "fixed"))
        mode = "fixed" if current_mode == "only_shown_while_driving" else "only_shown_while_driving"
        await self.storage.set_behavior(key, {
            "hide_while_driving": mode == "hide_while_driving",
            "drive_mode": mode,
        })
        await self._refresh_editor_for([login])
        await self._refresh_widget_for_all(key)

    async def _act_drivemode(self, login: str, arg: str, _values: dict) -> None:
        mode = ""
        if arg.endswith("__toggle"):
            key = arg[:-len("__toggle")]
            combo_name = f"drivemode__{key}"
            if self._open_combo.get(login) == combo_name:
                self._open_combo.pop(login, None)
            else:
                self._open_combo[login] = combo_name
            await self._refresh_editor_for([login])
            return
        if "__pick__" in arg:
            key, mode = arg.split("__pick__", 1)
            self._open_combo.pop(login, None)
        elif "__set__" in arg:
            # Backward-compatible radio_group path.
            key, mode = arg.split("__set__", 1)
        else:
            return
        if key not in self.entries or mode not in _DRIVE_MODE_OPTIONS:
            return
        if not await self._login_is_master(login):
            await self._toast(login, "global config is master-admin only", "warning")
            return
        cur = self.resolve_behavior(key)
        await self.storage.set_behavior(key, {
            "hide_while_driving": mode == "hide_while_driving",
            "drive_mode": mode,
        })
        await self._refresh_editor_for([login])
        await self._refresh_widget_for_all(key)

    async def _act_statemode(self, login: str, arg: str, _values: dict) -> None:
        key = ""
        mode = ""
        if "__set__" in arg:
            # Backward-compatible single-select path.
            key, mode = arg.split("__set__", 1)
        elif "|" in arg:
            key, mode = arg.split("|", 1)
        else:
            return
        if key not in self.entries or mode not in _STATE_MODE_OPTIONS:
            return
        if not await self._login_is_master(login):
            await self._toast(login, "global config is master-admin only", "warning")
            return
        cur = self.resolve_behavior(key)
        drive_mode = str(cur.get("drive_mode", "fixed"))
        current_modes = self._normalize_state_modes(cur.get("state_modes") or ["all"])
        if "__set__" in arg:
            next_modes = [mode]
        elif mode == "all":
            next_modes = ["all"]
        else:
            if "all" in current_modes:
                current_modes = []
            if mode in current_modes:
                next_modes = [m for m in current_modes if m != mode]
            else:
                next_modes = [*current_modes, mode]
            next_modes = self._normalize_state_modes(next_modes)
        await self.storage.set_behavior(key, {
            "state_modes": next_modes,
        })
        await self._refresh_editor_for([login])
        await self._refresh_widget_for_all(key)

    async def _act_allowperson(self, login: str, key: str, _values: dict) -> None:
        if key not in self.entries:
            return
        if not await self._login_is_master(login):
            await self._toast(login, "global config is master-admin only", "warning")
            return
        new_val = not self.allow_personal(key)
        await self.storage.set_behavior(key, {"allow_personal": new_val})
        await self._refresh_editor_for([login])
        await self._refresh_widget_for_all(key)

    async def _act_widgetdisabled(self, login: str, key: str, _values: dict) -> None:
        """Toggle the master-admin kill-switch for one widget."""
        if key not in self.entries:
            return
        if not await self._login_is_master(login):
            await self._toast(login, "disable is master-admin only", "warning")
            return
        new_val = not self.is_widget_disabled(key)
        await self.storage.set_behavior(key, {"widget_disabled": new_val})
        await self._toast(
            login,
            f"widget '{key}' {'disabled' if new_val else 'enabled'}",
            "success",
        )
        await self._refresh_editor_for([login])
        # The manialink id stays the same across re-displays, so the running
        # ManiaScript keeps its prior ForceHidden value and the toggle wouldn't
        # visibly take effect. Per-player TemplateView.hide() tears down the
        # client-side instance without nulling the view's data (BaseView.hide
        # would destroy it for everyone and break future displays), then
        # display() pushes a fresh instance that restarts the script.
        app = self._find_widget_app(key)
        if app is not None and app.view is not None:
            try:
                from pyplanet.views.template import TemplateView
                online = list(self.instance.player_manager.online)
            except Exception:
                online = []
            logins = [getattr(p, "login", None) for p in online]
            logins = [pl for pl in logins if pl]
            if logins:
                try:
                    await TemplateView.hide(app.view, player_logins=logins)
                except Exception:
                    logger.exception(
                        "widgets: per-player hide on disable-toggle failed for '%s'", key,
                    )
                if not new_val:
                    for plogin in logins:
                        try:
                            await app.view.display(player_logins=[plogin])
                        except Exception:
                            logger.exception(
                                "widgets: re-display on enable-toggle failed for '%s'/%s",
                                key, plogin,
                            )

    async def _act_strippreftop(self, login: str, key: str, _values: dict) -> None:
        """Toggle WIDGET_STRIP_PREFER_TOP for one widget (master-admin only)."""
        if key not in self.entries:
            return
        if not await self._login_is_master(login):
            await self._toast(login, "strip layout is master-admin only", "warning")
            return
        cur = self.storage.strip_prefer_top.get(key)
        if cur is None:
            app = self._find_widget_app(key)
            cur = bool(getattr(app, "WIDGET_STRIP_PREFER_TOP", False)) if app is not None else False
        await self.set_strip_prefer_top(key, not bool(cur))
        await self._refresh_editor_for([login])
        await self._refresh_widget_for_all(key)

    async def _act_debug(self, login: str, _arg: str, _values: dict) -> None:
        """Toggle the per-login debug overlay (master-admin only)."""
        if not await self._login_is_master(login):
            await self._toast(login, "debug mode is master-admin only", "warning")
            return
        if login in self._debug:
            self._debug.discard(login)
        else:
            self._debug.add(login)
        await self._refresh_editor_for([login])
        await self._refresh_all_widget_frames(login)

    async def _act_dump(self, login: str, key: str, _values: dict) -> None:
        """Render the selected widget for the caller and write XML to disk
        (master-admin only). Dumps the currently-selected widget when
        ``key`` is empty or unknown."""
        if not await self._login_is_master(login):
            await self._toast(login, "dump is master-admin only", "warning")
            return
        if not key or key not in self.entries:
            key = self._selected.get(login) or ""
        if not key or key not in self.entries:
            await self._toast(login, "no widget selected to dump", "warning")
            return
        app = self._find_widget_app(key)
        if app is None or app.view is None:
            await self._toast(login, f"{key}: widget not active", "warning")
            return
        try:
            path = await app.view.dump_render(login)
        except Exception:
            logger.exception("widgets: dump of '%s' for %s failed", key, login)
            await self._toast(login, f"{key}: dump failed (see logs)", "error")
            return
        await self._toast(login, f"{key}: dumped to {path}", "success")

    async def _act_writedefaults(self, login: str, _arg: str, _values: dict) -> None:
        """Write the current global widget config snapshot into defaults.json.

        Master-admin only.
        """
        if not await self._login_is_master(login):
            await self._toast(login, "write defaults is master-admin only", "warning")
            return
        path = default_defaults_path()
        try:
            count = await self.storage.write_defaults(path)
        except Exception:
            logger.exception("widgets: write defaults failed")
            await self._toast(login, "failed writing defaults.json (see logs)", "error")
            return
        await self._toast(
            login,
            f"defaults.json updated ({count} widget positions)",
            "success",
        )

    # ---- preset API --------------------------------------------------

    def list_presets(self) -> list[WidgetPreset]:
        return self.presets.list()

    def _presets_rows_for_ui(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in self.presets.list():
            out.append({
                "key": p.key,
                "label": p.label or p.key,
                "description": p.description or "",
                "count": len(p.entries),
                "ok": bool(p.ok),
                "warnings": list(p.warnings),
                "warning_text": "; ".join(p.warnings) if p.warnings else "",
                "path": str(p.source_path) if p.source_path else "",
            })
        return out

    def get_preset(self, key: str) -> WidgetPreset | None:
        return self.presets.get(key)

    def validate_preset_for(self, preset: WidgetPreset,
                            required_keys):
        return self.presets.validate_for(preset, required_keys or [])

    def reload_presets(self) -> int:
        self.presets.reload()
        try:
            asyncio.create_task(self._emit_presets_changed())
        except Exception:
            pass
        return len(self.presets.list())

    async def _emit_presets_changed(self) -> None:
        try:
            sig = self.context.signals.get_signal("tmsm_widgets:presets_changed")
            await sig.send_robust({}, raw=True)
        except Exception:
            logger.exception("widgets: emit presets_changed failed")

    async def _emit_preset_applied(self, preset_key: str, scope: str,
                                   owner: str | None = None) -> None:
        try:
            sig = self.context.signals.get_signal("tmsm_widgets:preset_applied")
            await sig.send_robust(
                {"preset_key": preset_key, "scope": scope, "owner": owner},
                raw=True,
            )
        except Exception:
            logger.exception("widgets: emit preset_applied failed")

    def _snapshot_global_entries(self) -> list[tuple[str, dict, dict]]:
        """Build (widget_key, pos, behavior) triples mirroring the resolved
        global config — used to derive a preset snapshot."""
        out: list[tuple[str, dict, dict]] = []
        for key, entry in sorted(self.entries.items()):
            pos_defaults = {
                "x": float(entry.default_x),
                "y": float(entry.default_y),
                "w": float(entry.default_w),
                "h": float(entry.default_h),
            }
            beh_defaults: dict[str, Any] = {
                "hide_while_driving": bool(getattr(entry.hide_rule, "while_driving", False)),
                "drive_mode": "hide_while_driving" if getattr(entry.hide_rule, "while_driving", False) else "fixed",
                "state_modes": ["all"],
                "group_key": "",
                "group_member_enabled": False,
                "group_priority": 0,
                "group_order": 0,
                "anim_dir": str(getattr(entry.animation, "direction", "none") or "none"),
                "anim_duration_ms": int(getattr(entry.animation, "duration_ms", 0) or 0),
                "anim_delay_ms": int(getattr(entry.animation, "delay_ms", 0) or 0),
                "allow_personal": True,
                "strip_prefer_top": False,
                "widget_disabled": False,
            }
            pos = self.storage.resolve(key, "", pos_defaults)
            beh = self.storage.resolve_behavior(key, beh_defaults, login=None)
            out.append((key, pos, beh))
        return out

    async def snapshot_preset_from_global(self, key: str, label: str,
                                          description: str = "",
                                          *, overwrite: bool = False):
        snap = self._snapshot_global_entries()
        preset = build_preset_from_snapshot(key, label, description, snap)
        path = self.presets.save(preset, overwrite=overwrite)
        await self._emit_presets_changed()
        return path, preset

    async def apply_preset_global(self, preset: WidgetPreset) -> int:
        if not preset or not preset.ok:
            return 0
        count = 0
        for widget_key, entry in preset.entries.items():
            try:
                await self.storage.set_global(widget_key, dict(entry.pos))
                await self.storage.set_behavior(widget_key, dict(entry.behavior))
                count += 1
            except Exception:
                logger.exception("widgets: apply_preset_global '%s' failed for '%s'",
                                 preset.key, widget_key)
        if count:
            await self._emit_preset_applied(preset.key, scope="global")
            try:
                await self._refresh_runtime_targets(None)
            except Exception:
                logger.exception("widgets: refresh after preset apply failed")
        return count

    async def apply_preset_runtime(self, preset: WidgetPreset, *, owner: str,
                                   login: str | None = None) -> int:
        if not preset or not preset.ok or not owner:
            return 0
        count = 0
        for widget_key, entry in preset.entries.items():
            try:
                await self.set_runtime_override(
                    owner=owner,
                    widget_key=widget_key,
                    login=login,
                    enabled=True,
                    pos=dict(entry.pos),
                    drive_mode=entry.behavior.get("drive_mode"),
                    anim_dir=entry.behavior.get("anim_dir"),
                    anim_duration_ms=entry.behavior.get("anim_duration_ms"),
                    anim_delay_ms=entry.behavior.get("anim_delay_ms"),
                )
                count += 1
            except Exception:
                logger.exception("widgets: apply_preset_runtime '%s' failed for '%s'",
                                 preset.key, widget_key)
        if count:
            await self._emit_preset_applied(preset.key, scope="runtime", owner=owner)
        return count

    async def clear_preset_runtime(self, owner: str) -> None:
        if not owner:
            return
        await self.clear_runtime_owner(owner)

    # ---- preset action handlers (editor) -----------------------------

    _PRESET_RUNTIME_OWNER = "editor"

    async def _act_presetreload(self, login: str, _arg: str, _values: dict) -> None:
        if not await self._login_is_master(login):
            await self._toast(login, "preset reload is master-admin only", "warning")
            return
        n = self.reload_presets()
        await self._toast(login, f"reloaded presets ({n})", "success")
        await self._refresh_editor_for([login])

    async def _act_presetsnap(self, login: str, _arg: str, _values: dict) -> None:
        if not await self._login_is_master(login):
            await self._toast(login, "snapshot is master-admin only", "warning")
            return
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        key = f"snapshot_{ts}"
        label = f"Snapshot {ts}"
        try:
            path, _preset = await self.snapshot_preset_from_global(
                key, label, "Snapshot of current global widget config.",
                overwrite=False,
            )
        except FileExistsError:
            await self._toast(login, f"preset '{key}' already exists", "warning")
            return
        except Exception:
            logger.exception("widgets: snapshot failed")
            await self._toast(login, "snapshot failed (see logs)", "error")
            return
        await self._toast(login, f"saved preset {key} -> {path.name}", "success")
        await self._refresh_editor_for([login])

    async def _act_presetapply(self, login: str, arg: str, _values: dict) -> None:
        if not await self._login_is_master(login):
            await self._toast(login, "apply preset is master-admin only", "warning")
            return
        preset = self.get_preset(arg)
        if preset is None:
            await self._toast(login, f"preset '{arg}' not found", "error")
            return
        if not preset.ok:
            await self._toast(login, f"preset '{arg}' has validation errors", "error")
            return
        try:
            await self.storage.write_defaults(default_defaults_path())
        except Exception:
            logger.exception("widgets: pre-apply backup failed")
        n = await self.apply_preset_global(preset)
        await self._toast(login, f"applied preset '{preset.key}' to {n} widget(s)", "success")
        await self._refresh_editor_for([login])

    async def _act_presetapplyrt(self, login: str, arg: str, _values: dict) -> None:
        if not await self._login_is_master(login):
            await self._toast(login, "apply preset is master-admin only", "warning")
            return
        preset = self.get_preset(arg)
        if preset is None or not preset.ok:
            await self._toast(login, f"preset '{arg}' not usable", "error")
            return
        n = await self.apply_preset_runtime(preset, owner=self._PRESET_RUNTIME_OWNER)
        await self._toast(login, f"preset '{preset.key}' active for this session ({n} widget(s))", "success")

    async def _act_presetclearrt(self, login: str, _arg: str, _values: dict) -> None:
        if not await self._login_is_master(login):
            await self._toast(login, "clear preset is master-admin only", "warning")
            return
        await self.clear_preset_runtime(self._PRESET_RUNTIME_OWNER)
        await self._toast(login, "cleared temporary preset", "success")

    async def _act_presetdel(self, login: str, arg: str, _values: dict) -> None:
        if not await self._login_is_master(login):
            await self._toast(login, "delete preset is master-admin only", "warning")
            return
        if not arg:
            return
        ok = self.presets.delete(arg)
        if ok:
            await self._toast(login, f"deleted preset '{arg}'", "success")
            await self._emit_presets_changed()
        else:
            await self._toast(login, f"could not delete preset '{arg}'", "error")
        await self._refresh_editor_for([login])

    async def _toast(self, login: str, msg: str, severity: str = "info",
                     source: str = "widgets") -> None:
        try:
            sig = self.context.signals.get_signal("tmsm_status:notify")
        except Exception:
            return
        try:
            await sig.send_robust({
                "message": msg, "severity": severity,
                "login": login, "source": source,
            })
        except Exception:
            logger.exception("widgets: toast emit failed")

    async def _act_drop(self, login: str, key: str, values: dict) -> None:
        """Legacy editor path — kept for future use. Per-widget drag uses
        :meth:`handle_widget_drop` instead, called by ``WidgetView``."""
        if key not in self.entries:
            return
        new_pos: dict[str, float] = {}
        for src, dst in (("widget_drop_x", "x"), ("widget_drop_y", "y")):
            raw = values.get(src)
            if raw is None or raw == "":
                continue
            try:
                new_pos[dst] = float(raw)
            except (TypeError, ValueError):
                continue
        if new_pos:
            await self._write_pos(login, key, new_pos)

    async def _act_mdrop(self, login: str, arg: str, _values: dict) -> None:
        """Mouse-drop from the editor's drag overlay.

        ``arg`` = ``<widget_key>|<x>|<y>`` (ManiaScript-formatted floats).
        """
        parts = arg.split("|")
        if len(parts) < 3:
            return
        key, x_raw, y_raw = parts[0], parts[1], parts[2]
        if key not in self.entries:
            return
        try:
            pos = {"x": float(x_raw), "y": float(y_raw)}
        except ValueError:
            return
        await self._write_pos(login, key, self._effective_to_raw(login, key, pos))

    async def handle_widget_drop(self, login: str, action: str, _values: dict) -> None:
        """Called from each WidgetView's catch-all on drag-release or click.

        Action received here (prefix already stripped):
          ``drop__<widget_key>|<x>|<y>`` or ``click__<widget_key>``.
        """
        logger.info("widgets: widget event %s from %s", action, login)
        try:
            verb, arg = action.split("__", 1)
        except ValueError:
            return
        if verb == "dbg":
            logger.warning("[tmsm_widgets][script] %s :: %s", login, arg)
            return
        if verb == "click":
            return  # click path is informational for now
        if verb != "drop":
            return
        parts = arg.split("|")
        if len(parts) < 3:
            return
        key, x_raw, y_raw = parts[0], parts[1], parts[2]
        if key not in self.entries:
            return
        try:
            pos = {"x": float(x_raw), "y": float(y_raw)}
        except ValueError:
            return
        await self._write_pos(login, key, self._effective_to_raw(login, key, pos))

    async def _act_reset(self, login: str, key: str, _values: dict) -> None:
        if key not in self.entries:
            return
        scope = self._scope.get(login, "global")
        if scope == "player":
            await self.storage.clear_player(key, login)
        else:
            await self.storage.clear_global(key)
        await self._announce_position_changed(key, scope, login)
        await self._refresh_editor_for([login])
        await self._refresh_widget_for_all(key)

    async def _act_close(self, login: str, _arg: str, _values: dict) -> None:
        await self._close_editor(login)

    # ---- write helper --------------------------------------------------

    async def _write_pos(self, login: str, key: str, pos: dict[str, float]) -> None:
        scope = self._scope.get(login, "global")
        entry = self.entries.get(key)
        if scope == "global" and not await self._login_is_master(login):
            await self._toast(login, "global config is master-admin only", "warning")
            return
        if scope == "player" and entry is not None and not self.allow_personal(key):
            await self._toast(
                login, f"{entry.name}: personalization is disabled", "warning",
            )
            return
        if scope == "player":
            await self.storage.set_player(key, login, pos)
        else:
            await self.storage.set_global(key, pos)
        await self._announce_position_changed(key, scope, login)
        await self._refresh_editor_for([login])
        await self._refresh_widget_for_all(key)

    async def _announce_position_changed(self, key: str, scope: str, login: str) -> None:
        try:
            sig = self.context.signals.get_signal("tmsm_widgets:position_changed")
            await sig.send_robust(
                {"key": key, "scope": scope, "login": login}, raw=True,
            )
        except Exception:
            pass

    async def _refresh_widget_for_all(self, key: str) -> None:
        app = self._find_widget_app(key)
        if app is None or app.view is None:
            return
        try:
            await app.view.refresh()
        except Exception:
            logger.exception("widgets: post-edit refresh of '%s' failed", key)

    async def _refresh_all_widgets_for_all(self) -> None:
        """Refresh every widget view for every player. Used when a global
        setting (e.g. strip color override or thickness) changes."""
        for entry in self.entries.values():
            app = self._find_widget_app(entry.key)
            if app is None or app.view is None:
                continue
            try:
                await app.view.refresh()
            except Exception:
                logger.exception("widgets: global refresh of '%s' failed", entry.key)

    async def _act_saveframe(self, login: str, _arg: str, values: dict) -> None:
        if not await self._login_is_master(login):
            await self._toast(login, "frame settings are master-admin only", "warning")
            return
        color_raw = values.get("entry_frame_strip_color_override")
        thick_raw = values.get("entry_frame_strip_thickness")
        bg_raw = values.get("entry_frame_bg_color_override")
        if color_raw is not None:
            await self.set_global_strip_color_override(str(color_raw).strip())
        if thick_raw is not None:
            await self.set_global_strip_thickness(thick_raw)
        if bg_raw is not None:
            await self.set_global_bg_color_override(str(bg_raw).strip())
        await self._refresh_all_widgets_for_all()
        await self._refresh_editor_for([login])
        await self._toast(login, "frame settings saved", "success")

    async def _act_clearframecolor(self, login: str, _arg: str, _values: dict) -> None:
        if not await self._login_is_master(login):
            await self._toast(login, "frame settings are master-admin only", "warning")
            return
        await self.set_global_strip_color_override("")
        await self._refresh_all_widgets_for_all()
        await self._refresh_editor_for([login])
        await self._toast(login, "global strip color override cleared", "success")

    async def _act_clearframebg(self, login: str, _arg: str, _values: dict) -> None:
        if not await self._login_is_master(login):
            await self._toast(login, "frame settings are master-admin only", "warning")
            return
        await self.set_global_bg_color_override("")
        await self._refresh_all_widgets_for_all()
        await self._refresh_editor_for([login])
        await self._toast(login, "global bg color override cleared", "success")

    # ---- editor context (consumed by editor.xml) -----------------------

    def editor_context(self, login: str) -> dict[str, Any]:
        # Only master admins see every widget and may edit global config.
        # Everyone else only sees widgets that allow personalization.
        try:
            from pyplanet.apps.tmsm.ui import perms as _perms
            is_master = bool(_perms.is_master(login))
        except Exception:
            is_master = False
        all_keys = sorted(self.entries.keys())
        if is_master:
            keys = all_keys
        else:
            keys = [k for k in all_keys if self.allow_personal(k)]
        selected_key = self._selected.get(login)
        if selected_key not in keys:
            selected_key = keys[0] if keys else ""
            self._selected[login] = selected_key
        selected = self.entries.get(selected_key)
        scope = self._scope.get(login, "global")
        sel_allow = self.allow_personal(selected_key) if selected_key else True
        # Non-master always forced to player scope.
        if not is_master:
            scope = "player"
            self._scope[login] = scope
        elif selected is not None and not sel_allow and scope == "player":
            scope = "global"
            self._scope[login] = scope
        step = self._step.get(login, 1.0)
        tab_items = [{"key": "widgets", "label": "Widgets"}]
        if is_master:
            tab_items.append({"key": "groups", "label": "Groups"})
            tab_items.append({"key": "frame", "label": "Frame"})
            tab_items.append({"key": "presets", "label": "Presets"})
        active_tab = self._normalize_editor_tab(self._editor_tab.get(login, "widgets"))
        if active_tab in ("groups", "frame", "presets") and not is_master:
            active_tab = "widgets"
        self._editor_tab[login] = active_tab
        cal = self.storage.get_ui_offset(login)
        rows = []
        for k in keys:
            e = self.entries[k]
            raw_pos = self._resolve_position_raw(k, login)
            y_uns = self._apply_unstretch_y(
                float(raw_pos.get("y", e.default_y)),
                float(cal.get("stretch", 0.0)),
            )
            pos = {
                "x": self._apply_edge_offset_x(
                    float(raw_pos.get("x", e.default_x)),
                    float(raw_pos.get("w", e.default_w)),
                    float(cal.get("x", 0.0)),
                ),
                "y": y_uns + float(cal.get("y", 0.0)),
                "w": float(raw_pos.get("w", e.default_w)),
                "h": self._apply_unstretch_h(
                    float(raw_pos.get("h", e.default_h)),
                    float(cal.get("stretch", 0.0)),
                ),
            }
            rows.append({
                "key": k,
                "name": e.name,
                "icon": e.icon,
                "kind": e.kind.value,
                "selected": k == selected_key,
                "disabled": self.is_widget_disabled(k),
                "x": raw_pos.get("x", e.default_x),
                "y": raw_pos.get("y", e.default_y),
                "sx": pos.get("x", e.default_x),
                "sy": pos.get("y", e.default_y),
                "w": pos.get("w", e.default_w),
                "h": pos.get("h", e.default_h),
            })
        # Pagination — widgets list. Auto-jump to page containing the selected row.
        widgets_total = len(rows)
        widgets_total_pages = max(1, (widgets_total + _EDITOR_PAGE_SIZE - 1) // _EDITOR_PAGE_SIZE)
        widgets_page = int(self._widgets_page.get(login, 0) or 0)
        widgets_page = max(0, min(widgets_page, widgets_total_pages - 1))
        self._widgets_page[login] = widgets_page
        w_lo = widgets_page * _EDITOR_PAGE_SIZE
        rows = rows[w_lo:w_lo + _EDITOR_PAGE_SIZE]
        groups = self.storage.list_groups()
        selected_group_key = self._selected_group.get(login, "")
        valid_group_keys = {g["key"] for g in groups}
        if selected_group_key not in valid_group_keys:
            selected_group_key = groups[0]["key"] if groups else ""
            self._selected_group[login] = selected_group_key
        groups_rows = [
            {
                "key": g["key"],
                "name": g.get("label") or g["key"],
                "description": g.get("description") or "",
                "order": int(g.get("order", 0) or 0),
                "mode": self._normalize_group_mode(g.get("mode")),
                "selected": g["key"] == selected_group_key,
            }
            for g in groups
        ]
        selected_group = self._group_cfg(selected_group_key) if selected_group_key else None
        selected_behavior = self.resolve_behavior(selected_key, login=login) if selected_key else {}
        group_options: list[tuple[str, str]] = [("__none__", "No group")]
        for g in groups:
            label = str(g.get("label") or g["key"])
            group_options.append((g["key"], f"{label} ({g['key']})"))
        cur_group = self._normalize_group_key(selected_behavior.get("group_key", ""))
        if cur_group and cur_group not in valid_group_keys:
            group_options.append((cur_group, f"[missing] {cur_group}"))

        usage = self._group_usage_map()
        for row in groups_rows:
            members = usage.get(row["key"], [])
            row["usage_count"] = len(members)

        # Pagination — groups list. Auto-jump to page containing the selected row.
        groups_total = len(groups_rows)
        groups_total_pages = max(1, (groups_total + _EDITOR_PAGE_SIZE - 1) // _EDITOR_PAGE_SIZE)
        groups_page = int(self._groups_page.get(login, 0) or 0)
        groups_page = max(0, min(groups_page, groups_total_pages - 1))
        self._groups_page[login] = groups_page
        g_lo = groups_page * _EDITOR_PAGE_SIZE
        groups_rows = groups_rows[g_lo:g_lo + _EDITOR_PAGE_SIZE]

        group_mode_options = [
            ("priority_active", "Priority active"),
            ("first_visible", "First visible"),
            ("fixed_member", "Fixed member"),
        ]
        group_fixed_options: list[tuple[str, str]] = [("__none__", "None")]
        selected_group_members: list[str] = []
        if selected_group_key:
            selected_group_members = usage.get(selected_group_key, [])
            for k in selected_group_members:
                ent = self.entries.get(k)
                label = ent.name if ent else k
                group_fixed_options.append((k, f"{label} ({k})"))

        selected_group_mode = self._normalize_group_mode((selected_group or {}).get("mode"))
        selected_group_fixed = str((selected_group or {}).get("fixed_widget_key") or "")
        if selected_group_fixed and selected_group_fixed not in [v for v, _ in group_fixed_options]:
            group_fixed_options.append((selected_group_fixed, f"[missing] {selected_group_fixed}"))
        selected_group_max_visible = int((selected_group or {}).get("max_visible", 1) or 1)
        selected_group_anchor = {
            "x": float((selected_group or {}).get("anchor_x", 0.0) or 0.0),
            "y": float((selected_group or {}).get("anchor_y", 0.0) or 0.0),
            "w": float((selected_group or {}).get("anchor_w", 18.0) or 18.0),
            "h": float((selected_group or {}).get("anchor_h", 8.0) or 8.0),
        }
        selected_group_runtime = {
            "prev": bool((selected_group or {}).get("runtime_prev_enabled", True)),
            "next": bool((selected_group or {}).get("runtime_next_enabled", True)),
            "auto": bool((selected_group or {}).get("runtime_auto_enabled", True)),
            "pin": bool((selected_group or {}).get("runtime_pin_enabled", True)),
        }
        selected_group_usage_rows = []
        for k in selected_group_members:
            ent = self.entries.get(k)
            selected_group_usage_rows.append({
                "key": k,
                "name": ent.name if ent else k,
            })
        selected_group_usage_text = ", ".join(
            f"{r['name']} ({r['key']})" for r in selected_group_usage_rows
        )
        # Member list ordered by group_order, with enable flag + priority for visual list.
        selected_group_member_list: list[dict[str, Any]] = []
        if selected_group_members:
            ordered_members = sorted(
                selected_group_members,
                key=lambda k: (
                    int(self.resolve_behavior(k).get("group_order", 0) or 0),
                    k,
                ),
            )
            last_idx = len(ordered_members) - 1
            for i, k in enumerate(ordered_members):
                ent = self.entries.get(k)
                beh = self.resolve_behavior(k)
                selected_group_member_list.append({
                    "key": k,
                    "name": ent.name if ent else k,
                    "order": int(beh.get("group_order", 0) or 0),
                    "priority": int(beh.get("group_priority", 0) or 0),
                    "enabled": bool(beh.get("group_member_enabled", True)),
                    "can_up": i > 0,
                    "can_down": i < last_idx,
                })
        return {
            "rows": rows,
            "selected_key": selected_key,
            "selected_name": selected.name if selected else "",
            "editor_tabs": tab_items,
            "editor_tab": active_tab,
            "scope": scope,
            "allow_personal": sel_allow,
            "is_master": is_master,
            "debug": login in self._debug,
            "step": step,
            "step_options": [0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
            "drive_combo_open": self._open_combo.get(login) == f"drivemode__{selected_key}",
            "group_combo_open": self._open_combo.get(login) == f"groupkey__{selected_key}",
            "group_options": group_options,
            "groups_rows": groups_rows,
            "widgets_page": widgets_page,
            "widgets_total_pages": widgets_total_pages,
            "widgets_total": widgets_total,
            "groups_page": groups_page,
            "groups_total_pages": groups_total_pages,
            "groups_total": groups_total,
            "selected_group_key": selected_group_key,
            "selected_group": selected_group or {},
            "group_mode_options": group_mode_options,
            "group_mode_combo_open": self._open_combo.get(login) == f"groupmode__{selected_group_key}",
            "group_fixed_options": group_fixed_options,
            "group_fixed_combo_open": self._open_combo.get(login) == f"groupfixed__{selected_group_key}",
            "selected_group_usage": selected_group_members,
            "selected_group_usage_rows": selected_group_usage_rows,
            "selected_group_usage_text": selected_group_usage_text,
            "selected_group_member_list": selected_group_member_list,
            "selected_group_mode": selected_group_mode,
            "selected_group_fixed": selected_group_fixed or "__none__",
            "selected_group_max_visible": selected_group_max_visible,
            "selected_group_anchor": selected_group_anchor,
            "selected_group_runtime": selected_group_runtime,
            "selected_group_delete_armed": self._armed_del_group.get(login) == selected_group_key,
            "behavior": selected_behavior,
            "global_strip_color_override": self.get_global_strip_color_override(),
            "global_strip_thickness": self.get_global_strip_thickness(),
            "global_bg_color_override": self.get_global_bg_color_override(),
            "presets_rows": self._presets_rows_for_ui() if is_master else [],
        }
