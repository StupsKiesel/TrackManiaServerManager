"""notification_engine — toast notification app.

Listens on signal ``notification_engine:notify`` and renders a stack of
toast cards in the top-right of each targeted player's screen. Cards
slide in from the right and out to the left.

The stack's anchor (x, y, width) is registered with ``widget_engine`` as
the ``notifications`` widget, so admins can move/resize it from the
widget engine manager UI.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Iterable, Optional

from pyplanet.apps.config import AppConfig
from pyplanet.core.events import Signal

from .registry import Action, Notification, SEVERITY_THEME, Severity
from .views import NotificationEngineView

logger = logging.getLogger(__name__)

# Constants ----------------------------------------------------------------
MAX_VISIBLE = 4              # cards visible at once per player; oldest dropped on overflow
ANIM_MS = 250                # slide-in / slide-out duration
DEFAULT_DURATION_MS = 4000   # auto-dismiss timeout when no actions

# Widget registration (widget_engine) --------------------------------------
WIDGET_KEY = "notifications"
WIDGET_NAME = "Notifications"
WIDGET_ICON = "info"
# Anchor = top-left of the stack area. Cards are CARD_W wide and extend to
# the right; up to MAX_VISIBLE * (CARD_H + GAP) downward.
DEFAULT_X = 79.0             # right-aligned (screen ~160 wide / 2 = 80)
DEFAULT_Y = 89.0             # near top edge
DEFAULT_W = 80.0
DEFAULT_H = 50.0             # MAX_VISIBLE * (11 + 1.5) ≈ 50


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


class NotificationEngineApp(AppConfig):
    name = "pyplanet.apps.tmsm.notification_engine"
    label = "notification_engine"
    app_dependencies = ["core.maniaplanet", "tmsm_ui", "widget_engine"]
    game_dependencies = ["trackmania", "trackmania_next"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.view: Optional[NotificationEngineView] = None
        # login -> ordered list of currently visible notifications
        self._stacks: dict[str, list[Notification]] = {}
        # (login, nid) -> task driving its enter/idle/leave lifecycle
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}
        # widget_engine host app reference, populated on_start.
        self.widget_engine_app = None  # type: ignore[assignment]

    # ---- lifecycle -----------------------------------------------------

    async def on_init(self) -> None:
        for code in ("notify", "dismiss"):
            try:
                self.context.signals.register_signal(
                    Signal(code=code, namespace="notification_engine")
                )
            except Exception:
                logger.exception(
                    "notification_engine: failed to register signal notification_engine:%s",
                    code,
                )

    async def on_start(self) -> None:
        self.view = NotificationEngineView(self)
        self.view.handle_catch_all = self._catch_all  # type: ignore[assignment]

        self.context.signals.listen("notification_engine:notify", self._on_notify)
        self.context.signals.listen("notification_engine:dismiss", self._on_dismiss)
        self.context.signals.listen(
            "maniaplanet:player_disconnect", self._on_disconnect,
        )

        # Register with widget_engine so the manager can move the stack
        # anchor (x/y/w). We do NOT pass `app: self` because the engine's
        # per-widget render path expects a WidgetAppBase-style view; we
        # handle our own refresh through `widget_engine:refresh`.
        self.widget_engine_app = self.instance.apps.apps.get("widget_engine")
        try:
            from pyplanet.apps.tmsm.widget_engine.registry import (
                WidgetEntry, WidgetKind, DriveMode, Animation, AnimDir, HideRule,
            )
            entry = WidgetEntry(
                key=WIDGET_KEY,
                name=WIDGET_NAME,
                description="Anchor point for transient toast notifications.",
                icon=WIDGET_ICON,
                default_x=DEFAULT_X, default_y=DEFAULT_Y,
                default_w=DEFAULT_W, default_h=DEFAULT_H,
                kind=WidgetKind.PERSISTENT,
                drive_mode=DriveMode.FIXED,
                hide_rule=HideRule(),
                animation=Animation(direction=AnimDir.NONE, duration_ms=0),
                strip_enabled=False,
                author="tmsm", version="0.1",
            )
            sig = self.context.signals.get_signal("widget_engine:register")
            await sig.send_robust({"entry": entry, "app": self}, raw=True)
        except KeyError:
            logger.info(
                "notification_engine: widget_engine:register not available"
            )
        except Exception:
            logger.exception("notification_engine: widget registration failed")

        # Re-announce on request from the widget_engine.
        try:
            self.context.signals.listen(
                "widget_engine:request_register", self._on_request_register,
            )
        except Exception:
            logger.exception(
                "notification_engine: request_register listen failed"
            )

    async def on_stop(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()
        self._stacks.clear()
        if self.view is not None:
            try:
                await self.view.destroy()
            except Exception:
                pass

    # ---- signal entry points -------------------------------------------

    @staticmethod
    def _unwrap(kwargs: dict) -> dict:
        """PyPlanet wraps `send_robust(dict)` payloads as `source=<dict>`.
        Accept either a wrapped source or already-flat kwargs."""
        src = kwargs.get("source")
        if isinstance(src, dict):
            return src
        out = dict(kwargs)
        out.pop("signal", None)
        out.pop("source", None)
        return out

    async def _on_notify(self, **kwargs) -> None:
        payload = self._unwrap(kwargs)
        if not payload.get("message"):
            logger.warning(
                "notification_engine: notify called without 'message' (payload=%r)",
                payload,
            )
            return
        try:
            await self.notify(**payload)
        except Exception:
            logger.exception("notification_engine: notify handler raised")

    async def _on_dismiss(self, **kwargs) -> None:
        payload = self._unwrap(kwargs)
        nid = payload.get("id")
        login = payload.get("login")
        if not nid:
            return
        targets: Iterable[str] = [login] if login else list(self._stacks.keys())
        for lg in targets:
            await self._begin_leave(lg, nid)

    async def _on_disconnect(self, player, **kwargs) -> None:
        login = getattr(player, "login", None)
        if not login:
            return
        self._stacks.pop(login, None)
        for key in [k for k in self._tasks if k[0] == login]:
            self._tasks.pop(key).cancel()

    async def _on_request_register(self, **kwargs) -> None:
        host = self.instance.apps.apps.get("widget_engine")
        if host is None:
            return
        try:
            from pyplanet.apps.tmsm.widget_engine.registry import (
                WidgetEntry, WidgetKind, DriveMode, Animation, AnimDir, HideRule,
            )
            entry = WidgetEntry(
                key=WIDGET_KEY,
                name=WIDGET_NAME,
                description="Anchor point for transient toast notifications.",
                icon=WIDGET_ICON,
                default_x=DEFAULT_X, default_y=DEFAULT_Y,
                default_w=DEFAULT_W, default_h=DEFAULT_H,
                kind=WidgetKind.PERSISTENT,
                drive_mode=DriveMode.FIXED,
                hide_rule=HideRule(),
                animation=Animation(direction=AnimDir.NONE, duration_ms=0),
                strip_enabled=False,
                author="tmsm", version="0.1",
            )
            sig = self.context.signals.get_signal("widget_engine:register")
            await sig.send_robust({"entry": entry, "app": self}, raw=True)
        except Exception:
            logger.exception(
                "notification_engine: re-register on request_register failed"
            )

    # ---- public API (also reachable via signal) ------------------------

    async def notify(
        self,
        message: str,
        severity: str | Severity = Severity.INFO,
        *,
        audience: str | None = "global",
        login: str | Iterable[str] | None = None,
        duration_ms: int = DEFAULT_DURATION_MS,
        button: bool | str | list[dict] | None = False,
        id: str | None = None,
        source: str = "",
        icon: str | None = None,
        color: str | None = None,
        **_extra: Any,
    ) -> str:
        """Show a toast to the resolved targets. Returns the notification id."""
        if not message:
            return ""
        sev = Severity(severity) if not isinstance(severity, Severity) else severity
        actions = self._coerce_actions(button)
        nid = id or f"n{uuid.uuid4().hex[:10]}"

        targets = self._resolve_targets(audience, login)
        if not targets:
            logger.info("notification_engine: notify '%s' had no targets", nid)
            return nid

        for lg in targets:
            notif = Notification(
                nid=nid,
                message=message,
                severity=sev,
                icon=icon,
                color=color,
                duration_ms=int(duration_ms),
                actions=actions,
                source=source,
                state="enter",
                created_ms=_now_ms(),
            )
            await self._enqueue(lg, notif)
        return nid

    # ---- internal: stack management ------------------------------------

    async def _enqueue(self, login: str, notif: Notification) -> None:
        stack = self._stacks.setdefault(login, [])

        # Replace existing notification with the same id (deduplication).
        for i, existing in enumerate(stack):
            if existing.nid == notif.nid:
                self._cancel_task(login, notif.nid)
                stack[i] = notif
                await self._refresh(login)
                self._spawn_lifecycle(login, notif.nid)
                return

        # Overflow: drop the oldest without animation.
        while len(stack) >= MAX_VISIBLE:
            old = stack.pop(0)
            self._cancel_task(login, old.nid)

        stack.append(notif)
        await self._refresh(login)
        self._spawn_lifecycle(login, notif.nid)

    def _spawn_lifecycle(self, login: str, nid: str) -> None:
        self._cancel_task(login, nid)
        task = asyncio.ensure_future(self._lifecycle(login, nid))
        self._tasks[(login, nid)] = task

    def _cancel_task(self, login: str, nid: str) -> None:
        task = self._tasks.pop((login, nid), None)
        if task and not task.done():
            task.cancel()

    async def _lifecycle(self, login: str, nid: str) -> None:
        try:
            # The card was rendered with state="enter"; the client's
            # ManiaScript drives the slide-in. We deliberately do NOT
            # refresh between enter and idle — that would re-send the
            # whole manialink and reset the script before the enter
            # animation finishes painting, causing the toast to "pop"
            # into view.
            await asyncio.sleep(ANIM_MS / 1000.0)
            notif = self._find(login, nid)
            if notif is None:
                return
            # Flip in-memory state so any subsequent refresh (caused by
            # another toast arriving) renders this card as "idle" and the
            # script leaves it at X=0 instead of re-running the enter anim.
            notif.state = "idle"

            if not notif.actions:
                await asyncio.sleep(max(0, notif.duration_ms) / 1000.0)
                await self._begin_leave(login, nid)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "notification_engine: lifecycle for %s/%s failed", login, nid,
            )

    async def _begin_leave(self, login: str, nid: str) -> None:
        notif = self._find(login, nid)
        if notif is None or notif.state == "leave":
            return
        notif.state = "leave"
        await self._refresh(login)
        await asyncio.sleep(ANIM_MS / 1000.0)
        await self._remove(login, nid)

    async def _remove(self, login: str, nid: str) -> None:
        stack = self._stacks.get(login)
        if stack is not None:
            self._stacks[login] = [n for n in stack if n.nid != nid]
        # Don't cancel our own task here — _remove is awaited from within
        # _lifecycle for normal expiry, and cancelling self would also cancel
        # the _refresh call below, leaving the last toast on screen.
        current = asyncio.current_task()
        owned = self._tasks.get((login, nid))
        if owned is not None and owned is not current:
            owned.cancel()
        self._tasks.pop((login, nid), None)
        await self._refresh(login)

    def _find(self, login: str, nid: str) -> Notification | None:
        for n in self._stacks.get(login, ()):
            if n.nid == nid:
                return n
        return None

    # ---- refresh / context ---------------------------------------------

    async def _refresh(self, login: str) -> None:
        if self.view is None:
            return
        self.view._visible = True
        try:
            ctx = self.context_for(login)
            logger.debug(
                "[notification_engine][refresh] login=%s anchor=(%.1f,%.1f) "
                "card_w=%.1f notifs=%d states=%s nids=%s",
                login,
                ctx.get("anchor_x", 0.0),
                ctx.get("anchor_y", 0.0),
                ctx.get("card_w", 0.0),
                len(ctx.get("notifications", [])),
                [n["state"] for n in ctx.get("notifications", [])],
                [n["nid"] for n in ctx.get("notifications", [])],
            )
            await self.view.display(player_logins=[login])
        except Exception:
            logger.exception(
                "notification_engine: display failed for %s", login,
            )

    def context_for(self, login: str) -> dict[str, Any]:
        rows = []
        for i, n in enumerate(self._stacks.get(login, [])):
            theme_color, theme_icon = SEVERITY_THEME[n.severity]
            rows.append({
                "nid": n.nid,
                "message": n.message,
                "severity": n.severity.value,
                "color": n.color or theme_color,
                "icon": n.icon or theme_icon,
                "state": n.state,        # enter | idle | leave
                "source": n.source,
                "slot": i,                # 0 = topmost
                "actions": [
                    {"label": a.label, "action": a.action, "variant": a.variant}
                    for a in n.actions
                ],
            })
        # Anchor + card width are resolved through widget_engine so the
        # in-game manager can move/resize the stack.
        anchor_x, anchor_y, card_w = DEFAULT_X, DEFAULT_Y, DEFAULT_W
        host = self.widget_engine_app or self.instance.apps.apps.get("widget_engine")
        if host is not None and getattr(host, "engine", None) is not None:
            try:
                resolved = host.engine.resolve(WIDGET_KEY, login)
                if resolved is not None:
                    anchor_x = float(resolved.x)
                    anchor_y = float(resolved.y)
                    card_w = float(resolved.w)
            except Exception:
                logger.exception(
                    "notification_engine: anchor resolve failed for %s", login,
                )
        return {
            "notifications": rows,
            "anchor_x": anchor_x,
            "anchor_y": anchor_y,
            "card_w": card_w,
            "render_nonce": uuid.uuid4().hex,
        }

    # ---- click dispatch -------------------------------------------------

    async def _catch_all(self, player, action, values, **kwargs) -> None:
        try:
            verb, arg = action.split("__", 1)
        except ValueError:
            return
        if verb == "dismiss":
            await self._begin_leave(player.login, arg)
        elif verb == "act":
            # act__<nid>__<action_id>; for now we just treat any action as dismiss
            # plus emit a signal so callers can react.
            try:
                nid, act_id = arg.split("__", 1)
            except ValueError:
                return
            try:
                await self.context.signals.get_signal(
                    "notification_engine:dismiss"
                ).send_robust({
                    "id": nid, "login": player.login, "action": act_id,
                })
            except Exception:
                logger.exception(
                    "notification_engine: dismiss signal emit failed"
                )
            await self._begin_leave(player.login, nid)

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _coerce_actions(button: bool | str | list[dict] | None) -> list[Action]:
        if not button:
            return []
        if button is True:
            return [Action(label="OK", action="dismiss", variant="primary")]
        if isinstance(button, str):
            return [Action(label=button, action="dismiss", variant="primary")]
        if isinstance(button, list):
            out: list[Action] = []
            for spec in button:
                if not isinstance(spec, dict):
                    continue
                out.append(Action(
                    label=spec.get("label", "OK"),
                    action=spec.get("action", "dismiss"),
                    variant=spec.get("variant", "primary"),
                ))
            return out
        return []

    def _resolve_targets(
        self, audience: str | None, login: str | Iterable[str] | None,
    ) -> list[str]:
        if login:
            if isinstance(login, str):
                return [login]
            return [str(x) for x in login]
        try:
            online = list(self.instance.player_manager.online)
        except Exception:
            return []
        if audience in (None, "global", "everyone"):
            return [p.login for p in online]
        # PyPlanet: Player.LEVEL_PLAYER=0, LEVEL_OPERATOR=1, LEVEL_ADMIN=2, LEVEL_MASTER=3
        if audience == "ops":
            return [p.login for p in online if getattr(p, "level", 0) >= 1]
        if audience == "admins":
            return [p.login for p in online if getattr(p, "level", 0) >= 2]
        return [p.login for p in online]
