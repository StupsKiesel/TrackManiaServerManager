"""Base classes for apps that register a widget.

Typical usage::

    from datetime import datetime
    from pyplanet.apps.tmsm.widgets.widget_base import WidgetAppBase
    from pyplanet.apps.tmsm.widgets import HideRule

    class ClockWidget(WidgetAppBase):
        name = "pyplanet.apps.tmsm.clock_widget"
        label = "clock_widget"

        WIDGET_KEY = "clock"
        WIDGET_NAME = "Clock"
        WIDGET_TEMPLATE = "tmsm_clock_widget/clock.xml"
        WIDGET_DEFAULT_X = 158
        WIDGET_DEFAULT_Y = -85
        WIDGET_DEFAULT_W = 25
        WIDGET_DEFAULT_H = 8
        WIDGET_REFRESH_SECONDS = 1.0
        WIDGET_HIDE_NAMED = ["in_menu"]

        async def get_widget_data(self, login):
            return {"now": datetime.now().strftime("%H:%M:%S")}

The template must call the shared ``widgets.frame()`` macro::

    {% import 'tmsm_widgets/frame.xml' as widgets with context %}
    {% call widgets.frame() %}
        <label text="{{ now }}" />
    {% endcall %}
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from pyplanet.apps.config import AppConfig
from pyplanet.apps.tmsm.ui.audience import Audience
from pyplanet.apps.tmsm.ui.views import BaseView

from .registry import Animation, HideRule, WidgetEntry, WidgetKind

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
    """Turn declared hide-rule names into ManiaScript boolean expressions.

    Each returned string is a complete bool expression that, when True,
    means the widget should be hidden. The frame script ORs them all
    together. Unknown names are skipped silently.
    """
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
# widget never animates and stays in place (hide rules are ignored client-side).
# `fade` falls back to a horizontal slide for now since proper opacity tween
# on a CMlFrame doesn't propagate to children.
_ANIM_OFFSETS: dict[str, tuple[float, float]] = {
    "none":  (0.0, 0.0),
    "left":  (-500.0, 0.0),
    "right": (500.0, 0.0),
    "up":    (0.0, 500.0),
    "down":  (0.0, -500.0),
    "fade":  (500.0, 0.0),
}


def _anim_offset(direction: str) -> tuple[float, float]:
    return _ANIM_OFFSETS.get((direction or "right").lower(), (500.0, 0.0))


# ─────────────────────────────────────────────────────────────────────────
# View
# ─────────────────────────────────────────────────────────────────────────


class WidgetView(BaseView):
    """Base TemplateView used by every widget.

    Subclasses set ``template_name`` (or rely on ``WidgetAppBase`` to set it).
    ``get_per_player_data`` injects resolved position + edit flag so the
    ``widgets.frame()`` macro can render position-aware. Subclasses override
    ``get_widget_data(login)`` to provide their own template variables.
    """

    audience: Audience = Audience.everyone()

    def __init__(self, app, widget_app: "WidgetAppBase"):
        super().__init__(app)
        self.widget_app = widget_app

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
        try:
            getter = getattr(self.widget_app, "get_debug_status", None)
            status = await getter(login) if getter else ""
        except Exception:
            logger.exception(
                "widget '%s': get_debug_status raised", self.widget_app.WIDGET_KEY,
            )
            status = ""
        ctx["widget_debug_status"] = status or ""
        return ctx

    async def dump_render(self, login: str, out_dir: str = "/tmp/tmsm_widget_dump") -> str:
        """Render this widget for ``login`` and write the XML to ``out_dir``.

        Returns the absolute path of the written file. Raises on failure
        so callers can surface the error in a toast.
        """
        import os
        ctx = await self.get_per_player_data(login)
        tpl = await self.get_template()
        body = await tpl.render(**ctx)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{self.widget_app.WIDGET_KEY}_{login}.xml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    async def handle_catch_all(self, player, action, values, **kwargs):
        # Forward drag-drop AND debug actions emitted by the widget's frame
        # ManiaScript to the widgets app, which owns the storage and the
        # script-debug log path.
        widgets_app = getattr(self.widget_app, "widgets_app", None)
        if widgets_app is not None:
            try:
                await widgets_app.handle_widget_drop(player.login, action, values or {})
                return
            except Exception:
                logger.exception("widget '%s': drop forward failed", self.widget_app.WIDGET_KEY)
        await super().handle_catch_all(player, action, values, **kwargs)


# ─────────────────────────────────────────────────────────────────────────
# App base
# ─────────────────────────────────────────────────────────────────────────


class WidgetAppBase(AppConfig):
    """Subclass and set ``WIDGET_*`` attributes to register a widget."""

    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_widgets"]
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

    # ── hide rule ───────────────────────────────────────────────────────
    # Default: hide while the player is driving fast so widgets don't clutter
    # the screen during a run. Override per-widget if a widget should remain
    # visible at speed (e.g. a speedometer).
    WIDGET_HIDE_NAMED: list[str] = []  # e.g. ["in_menu", "speed_above:50"]
    WIDGET_HIDE_RAW: str = ""                # raw ManiaScript bool expression
    # Convenience flag — when True, appends `speed_above:50` to the hide rule.
    WIDGET_HIDE_WHILE_DRIVING: bool = True

    # ── animation ───────────────────────────────────────────────────────
    # direction: none | left | right | up | down | fade
    # `none` disables the hide animation entirely (widget is fully static).
    # The four side directions slide the widget off-screen in that direction.
    WIDGET_ANIM_DIR: str = "right"
    WIDGET_ANIM_DURATION_MS: int = 300
    WIDGET_ANIM_DELAY_MS: int = 0

    # ── personalization ────────────────────────────────────────────────
    # When False, players cannot set a personal override for this widget;
    # only the admin-set global position applies.
    WIDGET_ALLOW_PERSONAL: bool = True

    # ── debug ──────────────────────────────────────────────────────────
    # One-line status shown in the master-admin debug overlay. Static by
    # default; override async `get_debug_status(login)` for live values.
    WIDGET_DEBUG_STATUS: str = ""

    # ── meta ────────────────────────────────────────────────────────────
    WIDGET_AUTHOR: str = "tmsm"
    WIDGET_VERSION: str = "0.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.widgets_app = None  # type: ignore[assignment]
        self.view: WidgetView | None = None
        self._refresh_task: asyncio.Task | None = None
        # popup state: login -> (asyncio.Task hide-after, expiry timestamp)
        self._popup_hide_tasks: dict[str, asyncio.Task] = {}

    # ---- registration --------------------------------------------------

    def build_entry(self) -> WidgetEntry:
        named = list(self.WIDGET_HIDE_NAMED)
        if self.WIDGET_HIDE_WHILE_DRIVING and not any(
            n.strip().startswith("speed_above:") for n in named
        ):
            named.append("speed_above:50")
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
            hide_rule=HideRule(
                named=named,
                raw=self.WIDGET_HIDE_RAW,
            ),
            animation=Animation(
                direction=self.WIDGET_ANIM_DIR,
                duration_ms=self.WIDGET_ANIM_DURATION_MS,
                delay_ms=self.WIDGET_ANIM_DELAY_MS,
            ),
            author=self.WIDGET_AUTHOR,
            version=self.WIDGET_VERSION,
            allow_personal=self.WIDGET_ALLOW_PERSONAL,
            popup_trigger=self._trigger_popup if self.WIDGET_KIND == WidgetKind.POPUP else None,
        )

    # ---- lifecycle -----------------------------------------------------

    async def on_start(self) -> None:
        try:
            self.widgets_app = self.instance.apps.apps["tmsm_widgets"]
        except Exception:
            logger.exception("widget '%s': tmsm_widgets app not found", self.WIDGET_KEY)
            return
        try:
            self.view = self._build_view()
        except Exception:
            logger.exception("widget '%s': view init failed", self.WIDGET_KEY)
            return
        # If widgets ever re-broadcasts its readiness (e.g. reload), re-register.
        try:
            self.context.signals.listen(
                "tmsm_widgets:request_register", self._on_widgets_request_register,
            )
        except Exception:
            pass
        await self._send_register()
        if self.WIDGET_KIND == WidgetKind.PERSISTENT:
            try:
                await self.view.show()
            except Exception:
                logger.exception("widget '%s': initial show failed", self.WIDGET_KEY)
            if self.WIDGET_REFRESH_SECONDS > 0:
                self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def _send_register(self) -> None:
        try:
            sig = self.context.signals.get_signal("tmsm_widgets:register")
        except KeyError:
            logger.info("widget '%s': tmsm_widgets:register signal not registered yet", self.WIDGET_KEY)
            return
        await sig.send_robust({"entry": self.build_entry()}, raw=True)

    async def _on_widgets_request_register(self, **kwargs) -> None:
        await self._send_register()

    async def on_stop(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None
        for t in self._popup_hide_tasks.values():
            t.cancel()
        self._popup_hide_tasks.clear()
        if self.view is not None:
            try:
                await self.view.destroy()
            except Exception:
                logger.exception("widget '%s': destroy failed", self.WIDGET_KEY)

    # ---- defaults subclasses may override ------------------------------

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        return {}

    async def get_debug_status(self, login: str) -> str:
        """One-line widget-defined status pushed into the debug overlay.

        Override to surface state that helps diagnose the widget in the
        master-admin debug overlay (e.g. ``"q=3 next=2.4s"`` for a queue,
        ``"cp=4 Δ=-0.213"`` for a CP delta). Keep it short — the
        ManiaScript loop decorates it with a universal ``R/H <tick>ms``
        prefix automatically. Return ``""`` for no status.
        """
        return self.WIDGET_DEBUG_STATUS

    # ---- per-player frame context (used by the view) -------------------

    def frame_context(self, login: str) -> dict[str, Any]:
        entry = self.build_entry()
        pos = self.widgets_app.resolve_position(self.WIDGET_KEY, login) if self.widgets_app else {}
        behavior = self.widgets_app.resolve_behavior(self.WIDGET_KEY, login=login) if self.widgets_app else {
            "hide_while_driving": True,
            "anim_dir": entry.animation.direction,
            "anim_duration_ms": entry.animation.duration_ms,
            "anim_delay_ms": entry.animation.delay_ms,
        }
        # Rebuild hide-clause list from the DB-resolved hide_while_driving flag
        # so the editor can toggle it live (without restarting the app).
        named = [n for n in entry.hide_rule.named if not n.strip().startswith("speed_above:")]
        if behavior.get("hide_while_driving", True):
            named.append("speed_above:50")
        off_x, off_y = _anim_offset(behavior.get("anim_dir", entry.animation.direction))
        return {
            "widget_key": entry.key,
            "widget_x": pos.get("x", entry.default_x),
            "widget_y": pos.get("y", entry.default_y),
            "widget_w": pos.get("w", entry.default_w),
            "widget_h": pos.get("h", entry.default_h),
            "widget_kind": entry.kind.value,
            "widget_hide_clauses": _compile_hide_clauses(named),
            "widget_hide_raw": entry.hide_rule.raw,
            "widget_anim_dir": behavior.get("anim_dir", entry.animation.direction),
            "widget_anim_duration_ms": int(behavior.get("anim_duration_ms", entry.animation.duration_ms)),
            "widget_anim_delay_ms": int(behavior.get("anim_delay_ms", entry.animation.delay_ms)),
            "widget_anim_off_x": off_x,
            "widget_anim_off_y": off_y,
            "widget_edit_mode": bool(self.widgets_app and self.widgets_app.is_editing(login)),
            "widget_debug_mode": bool(self.widgets_app and self.widgets_app.is_debug(login, entry.key)),
            "widget_view_id": self.view.id if self.view else entry.key,
        }

    # ---- popup ---------------------------------------------------------

    async def _trigger_popup(self, login: str) -> None:
        """Show this widget to one player for ``WIDGET_POPUP_DURATION_MS`` ms."""
        if self.view is None:
            return
        # Cancel any in-flight hide.
        old = self._popup_hide_tasks.pop(login, None)
        if old is not None:
            old.cancel()
        try:
            await self.view.display(player_logins=[login])
        except Exception:
            logger.exception("widget '%s': popup display failed", self.WIDGET_KEY)
            return

        async def _hide_later():
            try:
                await asyncio.sleep(self.WIDGET_POPUP_DURATION_MS / 1000)
                await self.view.destroy()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("widget '%s': popup hide failed", self.WIDGET_KEY)
            finally:
                self._popup_hide_tasks.pop(login, None)

        self._popup_hide_tasks[login] = asyncio.create_task(_hide_later())

    # ---- internals -----------------------------------------------------

    def _build_view(self) -> WidgetView:
        if not self.WIDGET_TEMPLATE:
            raise RuntimeError(f"widget '{self.WIDGET_KEY}': WIDGET_TEMPLATE not set")
        widget_app = self
        template_name = self.WIDGET_TEMPLATE

        class _DynView(WidgetView):
            pass

        _DynView.template_name = template_name
        _DynView.__name__ = f"WidgetView_{self.WIDGET_KEY}"
        return _DynView(self, widget_app)

    async def _refresh_loop(self) -> None:
        interval = max(0.2, float(self.WIDGET_REFRESH_SECONDS))
        try:
            while True:
                await asyncio.sleep(interval)
                if self.view is None:
                    continue
                try:
                    await self.view.refresh()
                except Exception:
                    logger.exception("widget '%s': periodic refresh failed", self.WIDGET_KEY)
        except asyncio.CancelledError:
            pass
