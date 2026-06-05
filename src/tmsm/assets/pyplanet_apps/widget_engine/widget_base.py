"""Base classes for widget addons.

Subclass `WidgetAppBase`, set `WIDGET_*` attributes, drop a template that
imports `widget_engine/frame.xml`. The base handles registration,
rendering, optional auto-refresh, and per-player context assembly.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from pyplanet.apps.config import AppConfig
from pyplanet.apps.tmsm.ui.audience import Audience
from pyplanet.apps.tmsm.ui.views import BaseView

from .registry import (
    AnimDir,
    Animation,
    DriveMode,
    HideRule,
    Phase,
    WidgetEntry,
    WidgetKind,
)
from .resolved import ResolvedWidget

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Hide-rule compiler
# ─────────────────────────────────────────────────────────────────────────


_FLAG_TARGETS: dict[str, str] = {
    "in_menu": "MenuOpen",
    "in_race": "Racing",
    "spectator": "Watching",
    "paused": "TabHeld",
}


def _compile_hide_clauses(named: list[str]) -> list[str]:
    out: list[str] = []
    for raw in named or ():
        token = raw.strip()
        if not token:
            continue
        invert = token.startswith("!")
        body = token[1:] if invert else token
        expr: str | None = None
        if body in _FLAG_TARGETS:
            var = _FLAG_TARGETS[body]
            expr = f"!{var}" if invert else var
        elif body.startswith("speed_above:"):
            try:
                threshold = float(body.split(":", 1)[1])
            except ValueError:
                continue
            op = "<" if invert else ">"
            expr = f"KmH {op} {threshold}"
        elif body.startswith("speed_below:"):
            try:
                threshold = float(body.split(":", 1)[1])
            except ValueError:
                continue
            op = ">" if invert else "<"
            expr = f"KmH {op} {threshold}"
        if expr is not None:
            out.append(expr)
    return out


# Off-screen tween offset for each animation direction. `none` means the
# widget never animates and stays in place.
_ANIM_OFFSETS: dict[AnimDir, tuple[float, float]] = {
    AnimDir.NONE:  (0.0, 0.0),
    AnimDir.LEFT:  (-500.0, 0.0),
    AnimDir.RIGHT: (500.0, 0.0),
    AnimDir.UP:    (0.0, 500.0),
    AnimDir.DOWN:  (0.0, -500.0),
}


def _hide_clauses_for(entry: WidgetEntry, drive_mode: DriveMode) -> list[str]:
    """Compile the widget's hide clauses, injecting the drive-mode rule."""
    named = [n for n in entry.hide_rule.named
             if not n.strip().startswith(("speed_above:", "speed_below:"))]
    if drive_mode is DriveMode.HIDE_WHILE_DRIVING:
        named.append("speed_above:50")
    elif drive_mode is DriveMode.ONLY_SHOWN_WHILE_DRIVING:
        named.append("speed_below:50")
    return _compile_hide_clauses(named)


# ─────────────────────────────────────────────────────────────────────────
# View
# ─────────────────────────────────────────────────────────────────────────


class WidgetView(BaseView):
    """TemplateView used by every widget. The widget addon supplies the
    body template; this class injects the frame context."""

    audience: Audience = Audience.everyone()

    def __init__(self, app: "WidgetAppBase"):
        super().__init__(app)
        self.widget_app = app

    async def display(self, player_logins=None, **kwargs):
        # BaseView.refresh() and the widget addons' periodic _queue_refresh()
        # gate on `self._visible`. Persistent widgets are delivered via the
        # widget engine's per-player display path (not via BaseView.show()),
        # so we mark visibility here to keep refresh() functional.
        self._visible = True
        if player_logins:
            for login in player_logins:
                if login:
                    self._visible_logins.add(str(login))
        return await super().display(player_logins=player_logins, **kwargs)

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        return {}

    async def get_per_player_data(self, login: str) -> dict[str, Any]:
        ctx = self.widget_app.frame_context(login)
        try:
            extra = await self.widget_app.get_widget_data(login)
        except Exception:
            logger.exception("widget '%s': get_widget_data raised", self.widget_app.WIDGET_KEY)
            extra = {}
        if extra:
            ctx.update(extra)
        return ctx

    async def get_context_data(self):
        # Global display path (no per-player binding). We still need every
        # frame variable defined so the template doesn't blow up.
        ctx = await super().get_context_data() or {}
        if "widget_w" not in ctx:
            ctx.update(self.widget_app.frame_context(""))
        return ctx


# ─────────────────────────────────────────────────────────────────────────
# App base
# ─────────────────────────────────────────────────────────────────────────


class WidgetAppBase(AppConfig):
    """Subclass and set ``WIDGET_*`` attributes to register a widget."""

    app_dependencies = ["core.maniaplanet", "widget_engine"]
    game_dependencies = ["trackmania", "trackmania_next"]

    # ── identity ────────────────────────────────────────────────────────
    WIDGET_KEY: str = ""
    WIDGET_NAME: str = ""
    WIDGET_DESCRIPTION: str = ""
    WIDGET_ICON: str = "object-group"

    # ── view / template ─────────────────────────────────────────────────
    WIDGET_TEMPLATE: str = ""
    WIDGET_REFRESH_SECONDS: float = 0.0     # 0 = no auto refresh

    # ── default position / size ─────────────────────────────────────────
    WIDGET_DEFAULT_X: float = 0.0
    WIDGET_DEFAULT_Y: float = 0.0
    WIDGET_DEFAULT_W: float = 40.0
    WIDGET_DEFAULT_H: float = 10.0

    # ── kind / popup behaviour ──────────────────────────────────────────
    WIDGET_KIND: WidgetKind = WidgetKind.PERSISTENT
    WIDGET_POPUP_DURATION_MS: int = 4000

    # ── hide rule + drive mode ──────────────────────────────────────────
    WIDGET_HIDE_NAMED: list[str] = []  # e.g. ["in_menu"]
    WIDGET_HIDE_RAW: str = ""
    # fixed | hide_while_driving | only_shown_while_driving
    WIDGET_DRIVE_MODE: DriveMode = DriveMode.FIXED

    # ── animation ───────────────────────────────────────────────────────
    WIDGET_ANIM_DIR: AnimDir = AnimDir.RIGHT
    WIDGET_ANIM_DURATION_MS: int = 300
    WIDGET_ANIM_IN_DELAY_MS: int = 0
    WIDGET_ANIM_OUT_DELAY_MS: int = 0

    # ── frame look ──────────────────────────────────────────────────────
    WIDGET_BG_COLOR: str = "40404080"
    WIDGET_STRIP_COLOR: str = "ffae00"
    WIDGET_STRIP_ENABLED: bool = True

    # ── phase visibility ────────────────────────────────────────────────
    # Tuple of Phase values in which the widget renders. None = always
    # visible. Empty tuple = never visible. The engine flips the resolved
    # `disabled` flag when the current phase is not in this set.
    WIDGET_VISIBLE_PHASES: tuple[Phase, ...] | None = None

    # ── meta ────────────────────────────────────────────────────────────
    WIDGET_AUTHOR: str = "tmsm"
    WIDGET_VERSION: str = "0.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.engine = None  # type: ignore[assignment]
        self.view: WidgetView | None = None
        self._refresh_task: asyncio.Task | None = None

    # ---- registration --------------------------------------------------

    def build_entry(self) -> WidgetEntry:
        return WidgetEntry(
            key=self.WIDGET_KEY,
            name=self.WIDGET_NAME,
            description=self.WIDGET_DESCRIPTION,
            icon=self.WIDGET_ICON,
            default_x=self.WIDGET_DEFAULT_X,
            default_y=self.WIDGET_DEFAULT_Y,
            default_w=self.WIDGET_DEFAULT_W,
            default_h=self.WIDGET_DEFAULT_H,
            kind=self.WIDGET_KIND,
            popup_duration_ms=self.WIDGET_POPUP_DURATION_MS,
            drive_mode=DriveMode(self.WIDGET_DRIVE_MODE),
            hide_rule=HideRule(
                named=tuple(self.WIDGET_HIDE_NAMED),
                raw=self.WIDGET_HIDE_RAW,
            ),
            animation=Animation(
                direction=AnimDir(self.WIDGET_ANIM_DIR),
                duration_ms=int(self.WIDGET_ANIM_DURATION_MS),
                in_delay_ms=int(self.WIDGET_ANIM_IN_DELAY_MS),
                out_delay_ms=int(self.WIDGET_ANIM_OUT_DELAY_MS),
            ),
            bg_color=self.WIDGET_BG_COLOR,
            strip_color=self.WIDGET_STRIP_COLOR,
            strip_enabled=self.WIDGET_STRIP_ENABLED,
            visible_phases=(
                tuple(Phase(p) for p in self.WIDGET_VISIBLE_PHASES)
                if self.WIDGET_VISIBLE_PHASES is not None else None
            ),
            author=self.WIDGET_AUTHOR,
            version=self.WIDGET_VERSION,
        )

    # ---- lifecycle -----------------------------------------------------

    async def on_start(self) -> None:
        try:
            host = self.instance.apps.apps["widget_engine"]
        except KeyError:
            logger.exception("widget '%s': widget_engine app not loaded", self.WIDGET_KEY)
            return
        self.engine = host.engine
        try:
            self.view = WidgetView(self)
            if self.WIDGET_TEMPLATE:
                self.view.template_name = self.WIDGET_TEMPLATE
        except Exception:
            logger.exception("widget '%s': view init failed", self.WIDGET_KEY)
            return
        try:
            self.context.signals.listen(
                "widget_engine:request_register", self._on_request_register,
            )
        except Exception:
            pass
        await self._send_register(host)
        if self.WIDGET_KIND == WidgetKind.PERSISTENT:
            try:
                # Display per-player explicitly; broadcast
                # SendDisplayManialinkPage doesn't reliably deliver to
                # already-connected players on TM2020. When the player
                # list is still empty (early startup), fall back to
                # view.show() and let the engine's delayed re-render
                # catch the players once they're hydrated.
                online_logins: list[str] = []
                try:
                    online_logins = [
                        p.login for p in self.instance.player_manager.online
                        if getattr(p, "login", None)
                    ]
                except Exception:
                    online_logins = []
                if online_logins:
                    await self.view.display(player_logins=online_logins)
                else:
                    await self.view.show()
            except Exception:
                logger.exception("widget '%s': initial show failed", self.WIDGET_KEY)
            if self.WIDGET_REFRESH_SECONDS > 0:
                self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def _send_register(self, host) -> None:
        try:
            sig = self.context.signals.get_signal("widget_engine:register")
        except KeyError:
            logger.info("widget '%s': widget_engine:register not ready", self.WIDGET_KEY)
            return
        await sig.send_robust({"entry": self.build_entry(), "app": self}, raw=True)

    async def _on_request_register(self, **kwargs) -> None:
        host = self.instance.apps.apps.get("widget_engine")
        if host is not None:
            await self._send_register(host)

    async def _refresh_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.WIDGET_REFRESH_SECONDS)
                if self.view is None:
                    continue
                try:
                    online_logins = [
                        p.login for p in self.instance.player_manager.online
                        if getattr(p, "login", None)
                    ]
                except Exception:
                    online_logins = []
                try:
                    if online_logins:
                        await self.view.display(player_logins=online_logins)
                    else:
                        await self.view.display()
                except Exception:
                    logger.exception("widget '%s': refresh failed", self.WIDGET_KEY)
        except asyncio.CancelledError:
            pass

    async def on_stop(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None
        if self.view is not None:
            try:
                await self.view.destroy()
            except Exception:
                logger.exception("widget '%s': destroy failed", self.WIDGET_KEY)

    # ---- defaults subclasses may override ------------------------------

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        return {}

    # ---- per-player frame context (used by the view) -------------------

    def frame_context(self, login: str) -> dict[str, Any]:
        entry = self.build_entry()
        if self.engine is None:
            resolved = ResolvedWidget.from_entry(entry, strip_prefer_top=False, strip_thickness=1.0)
            debug_mode = False
            debug_status = ""
            debug_lines: list[str] = []
            edit_mode = False
        else:
            resolved = self.engine.resolve(self.WIDGET_KEY, login) or \
                       ResolvedWidget.from_entry(entry, strip_prefer_top=False, strip_thickness=1.0)
            edit_mode = self.engine.is_editing(login, self.WIDGET_KEY)
            # During edit mode we intentionally suppress debug visuals to keep
            # the edit overlay readable and avoid stacked helper layers.
            debug_mode = False if edit_mode else self.engine.is_debug(login, self.WIDGET_KEY)
            debug_status = self.engine.debug_status(login, self.WIDGET_KEY) if debug_mode else ""
            debug_lines = self.engine.debug_lines(login, self.WIDGET_KEY) if debug_mode else []
        anim_off = _ANIM_OFFSETS.get(resolved.anim_dir, (500.0, 0.0))
        force_hidden = bool(resolved.disabled)
        return {
            "widget_key": resolved.key,
            "widget_view_id": self.view.id if self.view else "",
            "widget_kind": entry.kind.value,
            "widget_x": resolved.x,
            "widget_y": resolved.y,
            "widget_w": resolved.w,
            "widget_h": resolved.h,
            "widget_scale_y": 1.0,
            "widget_disabled": resolved.disabled,
            "widget_force_hidden": force_hidden,
            "widget_hide_clauses": _hide_clauses_for(entry, resolved.drive_mode),
            "widget_hide_raw": entry.hide_rule.raw,
            "widget_anim_dir": resolved.anim_dir.value,
            "widget_anim_duration_ms": resolved.anim_duration_ms,
            "widget_anim_in_delay_ms": resolved.anim_in_delay_ms,
            "widget_anim_out_delay_ms": resolved.anim_out_delay_ms,
            "widget_anim_off_x": anim_off[0],
            "widget_anim_off_y": anim_off[1],
            "widget_bg_color": resolved.bg_color,
            "widget_strip_color": resolved.strip_color,
            "widget_strip_edge": resolved.strip_edge,
            "widget_strip_thickness": resolved.strip_thickness,
            "widget_edit_mode": edit_mode,
            "widget_debug_mode": debug_mode,
            "widget_debug_status": debug_status,
            "widget_debug_lines": debug_lines,
        }
