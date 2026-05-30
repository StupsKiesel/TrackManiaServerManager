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
            tpl = await self.get_template()
            body = await tpl.render(**ctx)
            import os
            os.makedirs("/tmp/tmsm_widget_dump", exist_ok=True)
            with open(f"/tmp/tmsm_widget_dump/{self.widget_app.WIDGET_KEY}_{login}.xml", "w") as f:
                f.write(body)
        except Exception:
            logger.exception("widget dump failed")
        return ctx

    async def handle_catch_all(self, player, action, values, **kwargs):
        # Forward drag-drop actions emitted by the widget's frame ManiaScript
        # to the widgets app, which owns the storage.
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
    WIDGET_HIDE_NAMED: list[str] = ["speed_above:50"]  # e.g. ["in_menu", "speed_above:50"]
    WIDGET_HIDE_RAW: str = ""                # raw ManiaScript bool expression

    # ── animation ───────────────────────────────────────────────────────
    WIDGET_ANIM_DIR: str = "fade"            # fade | up | down | left | right | none
    WIDGET_ANIM_DURATION_MS: int = 300
    WIDGET_ANIM_DELAY_MS: int = 0

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
                named=list(self.WIDGET_HIDE_NAMED),
                raw=self.WIDGET_HIDE_RAW,
            ),
            animation=Animation(
                direction=self.WIDGET_ANIM_DIR,
                duration_ms=self.WIDGET_ANIM_DURATION_MS,
                delay_ms=self.WIDGET_ANIM_DELAY_MS,
            ),
            author=self.WIDGET_AUTHOR,
            version=self.WIDGET_VERSION,
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

    # ---- per-player frame context (used by the view) -------------------

    def frame_context(self, login: str) -> dict[str, Any]:
        entry = self.build_entry()
        pos = self.widgets_app.resolve_position(self.WIDGET_KEY, login) if self.widgets_app else {}
        return {
            "widget_key": entry.key,
            "widget_x": pos.get("x", entry.default_x),
            "widget_y": pos.get("y", entry.default_y),
            "widget_w": pos.get("w", entry.default_w),
            "widget_h": pos.get("h", entry.default_h),
            "widget_kind": entry.kind.value,
            "widget_hide_clauses": _compile_hide_clauses(entry.hide_rule.named),
            "widget_hide_raw": entry.hide_rule.raw,
            "widget_anim_dir": entry.animation.direction,
            "widget_anim_duration_ms": entry.animation.duration_ms,
            "widget_anim_delay_ms": entry.animation.delay_ms,
            "widget_edit_mode": bool(self.widgets_app and self.widgets_app.is_editing(login)),
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
