"""BaseView, FormView — PySide6-style view base classes."""
from __future__ import annotations

import inspect
import logging
import uuid
from typing import Callable

from pyplanet.views.template import TemplateView

from .audience import Audience

logger = logging.getLogger(__name__)


class BaseView(TemplateView):
    audience: Audience = Audience.everyone()

    # Breadcrumb trail rendered in the window header by `ui.window()`. Each
    # entry is a dict with `key` and `label`. Leave empty to render no crumbs
    # (e.g. the hub itself, which has no parent). A click on a crumb fires
    # the framework-reserved signal `_crumb__<key>`; BaseView ships a default
    # `hub` handler that hides the view and emits `tmsm_hub:show` so any
    # subview can opt in to "back to hub" navigation by adding
    #     breadcrumbs = [{"key": "hub", "label": "Hub"}]
    # at the class level. Subclasses can add more entries and register their
    # own handlers via `view.connect("_crumb__<key>", handler)`.
    breadcrumbs: list[dict] = []

    def __init__(self, app):
        super().__init__(app.context.ui)
        self.app = app
        self.id = self._make_id()
        # Tracks whether show() has been called and hide() hasn't been called
        # since. Only when this is True do we re-display to joining players
        # who match the audience — otherwise a view auto-pops just because
        # someone joined, even though nobody asked for it to be shown.
        self._visible: bool = False
        # Per-login visibility: a login is in this set while the view is
        # currently rendered for them. `_on_close` removes them so a later
        # `refresh()` does not pop the window back up after they dismissed it.
        self._visible_logins: set[str] = set()
        try:
            app.context.signals.listen("maniaplanet:player_connect", self._on_player_connect)
        except Exception:
            logger.exception("BaseView: failed to register player_connect listener")
        # Auto-refresh when the impersonate app changes someone's effective
        # level — every tmsm view re-renders for the affected login.
        try:
            from .perms import subscribe_changed
            subscribe_changed(self._on_perms_changed)
        except Exception:
            logger.exception("BaseView: failed to subscribe to perms changes")
        # framework-reserved signal fired by ui.window()'s close button
        self.connect("_close", self._on_close)
        # auto-wire the default `hub` breadcrumb handler if the subclass
        # opts into the hub crumb. Subclasses may override by calling
        # connect("_crumb__hub", their_own_handler) after super().__init__().
        for c in self.breadcrumbs:
            key = c.get("key")
            if key == "hub":
                self.connect("_crumb__hub", self._on_crumb_hub)

    async def get_context_data(self):
        ctx = await super().get_context_data()
        if ctx is None:
            ctx = {}
        ctx.setdefault("view_crumbs", list(self.breadcrumbs))
        return ctx

    def _make_id(self) -> str:
        # PyPlanet's manialink callback dispatcher expects a UUID-like id.
        # Non-UUID ids are treated as stale leaked view instances.
        return str(uuid.uuid4())

    # ---- Qt-style signal API -------------------------------------------

    def connect(self, signal: str, handler: Callable) -> None:
        """Register `handler` for `signal`.

        Handler may be `async def fn(player)` or `async def fn(player, values)`
        or accept `**kwargs`. We introspect once and call accordingly.
        """
        sig = inspect.signature(handler)
        params = sig.parameters
        accepts_values = (
            "values" in params
            or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        )

        async def _adapter(player, action, values, **kwargs):
            try:
                if accepts_values:
                    await handler(player, values=values)
                else:
                    await handler(player)
            except Exception:
                logger.exception("BaseView: handler for signal '%s' raised", signal)

        self.subscribe(signal, _adapter)

    async def handle_catch_all(self, player, action, values, **kwargs):
        """Log unmatched actions for this view so missing connect() calls are visible."""
        logger.warning(
            "BaseView(%s): unmatched action '%s' by %s (known signals: %s)",
            self.__class__.__name__, action, player.login, list(self.receivers.keys()),
        )

    # ---- lifecycle -----------------------------------------------------

    async def show(self) -> None:
        self._visible = True
        if self.audience.is_global:
            try:
                await self.display()
            except Exception:
                logger.exception("BaseView.show: global display failed")
            try:
                online = list(self.app.instance.player_manager.online)
                for p in online:
                    self._visible_logins.add(p.login)
            except Exception:
                pass
            return

        try:
            online = list(self.app.instance.player_manager.online)
        except Exception:
            online = []
        targets = [p.login for p in online if self.audience.matches(p)]
        if not targets:
            logger.info(
                "BaseView(%s).show: no online players match audience; will re-display on join",
                self.__class__.__name__,
            )
            return
        try:
            await self.display(player_logins=targets)
            self._visible_logins.update(targets)
            logger.info(
                "BaseView(%s).show: displayed to %s", self.__class__.__name__, targets
            )
        except Exception:
            logger.exception("BaseView.show: targeted display failed")

    async def hide(self) -> None:
        self._visible = False
        self._visible_logins.clear()
        try:
            await self.destroy()
        except Exception:
            logger.exception("BaseView.hide: destroy failed")

    async def _on_close(self, player) -> None:
        """Default handler for ui.window()'s close button — hide for that player."""
        self._visible_logins.discard(player.login)
        # Call the underlying _ManiaLink.hide directly to avoid our destroy-on-hide override.
        try:
            await TemplateView.hide(self, player_logins=[player.login])
        except Exception:
            logger.exception("BaseView._on_close: hide failed")

    async def _on_crumb_hub(self, player) -> None:
        """Default handler for the `hub` breadcrumb: hide self, show hub."""
        self._visible_logins.discard(player.login)
        try:
            await TemplateView.hide(self, player_logins=[player.login])
        except Exception:
            logger.exception("BaseView._on_crumb_hub: hide failed")
        try:
            sig = self.app.context.signals.get_signal("tmsm_hub:show")
            await sig.send_robust({"player": player}, raw=True)
        except KeyError:
            pass
        except Exception:
            logger.exception("BaseView._on_crumb_hub: emit tmsm_hub:show failed")

    async def refresh(self) -> None:
        # Don't push the view to people who haven't asked for it; only
        # re-render for whoever currently has it open.
        if not self._visible:
            return
        # If we know exactly who has it open (per-player view), only refresh
        # those logins. Otherwise fall back to full show() (e.g. global views
        # that haven't recorded per-login state yet).
        if self._visible_logins:
            try:
                await self.display(player_logins=list(self._visible_logins))
            except Exception:
                logger.exception("BaseView.refresh: targeted display failed")
            return
        await self.show()

    async def _on_player_connect(self, player, **kwargs) -> None:
        # Only re-show to joining players if the view is currently meant
        # to be visible (an explicit show() happened and no hide() since).
        if not self._visible:
            return
        if self.audience.is_global:
            return
        if not self.audience.matches(player):
            return
        try:
            await self.display(player_logins=[player.login])
        except Exception:
            logger.exception("BaseView: per-player display failed")

    async def _on_perms_changed(self, login: str, new_level: int, real_level: int) -> None:
        """Called by tmsm_ui.perms when an impersonate override changes for
        a login. Re-render so this view reflects the new effective level."""
        if not self._visible or not login:
            return
        if login not in self._visible_logins:
            return
        try:
            await self.display(player_logins=[login])
        except Exception:
            logger.exception("BaseView: perms-changed re-display failed")


class FormView(BaseView):
    """Alias for views with `<entry>` fields. Submit handlers receive `values`."""
    pass
