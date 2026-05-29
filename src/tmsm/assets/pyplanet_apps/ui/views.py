"""BaseView, FormView — PySide6-style view base classes."""
from __future__ import annotations

import inspect
import logging
from typing import Callable

from pyplanet.views.template import TemplateView

from .audience import Audience

logger = logging.getLogger(__name__)


class BaseView(TemplateView):
    audience: Audience = Audience.everyone()

    def __init__(self, app):
        super().__init__(app.context.ui)
        self.app = app
        self.id = self._make_id()
        try:
            app.context.signals.listen("maniaplanet:player_connect", self._on_player_connect)
        except Exception:
            logger.exception("BaseView: failed to register player_connect listener")
        # framework-reserved signal fired by ui.window()'s close button
        self.connect("_close", self._on_close)

    def _make_id(self) -> str:
        return (
            self.__class__.__module__.replace(".", "_")
            + "__"
            + self.__class__.__name__.lower()
        )

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
            logger.info("BaseView: signal '%s' fired by %s", signal, player.login)
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
        if self.audience.is_global:
            try:
                await self.display()
            except Exception:
                logger.exception("BaseView.show: global display failed")
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
            logger.info(
                "BaseView(%s).show: displayed to %s", self.__class__.__name__, targets
            )
        except Exception:
            logger.exception("BaseView.show: targeted display failed")

    async def hide(self) -> None:
        try:
            await self.destroy()
        except Exception:
            logger.exception("BaseView.hide: destroy failed")

    async def _on_close(self, player) -> None:
        """Default handler for ui.window()'s close button — hide for that player."""
        # Call the underlying _ManiaLink.hide directly to avoid our destroy-on-hide override.
        try:
            await TemplateView.hide(self, player_logins=[player.login])
        except Exception:
            logger.exception("BaseView._on_close: hide failed")

    async def refresh(self) -> None:
        await self.show()

    async def _on_player_connect(self, player, **kwargs) -> None:
        if self.audience.is_global:
            return
        if not self.audience.matches(player):
            return
        try:
            await self.display(player_logins=[player.login])
        except Exception:
            logger.exception("BaseView: per-player display failed")


class FormView(BaseView):
    """Alias for views with `<entry>` fields. Submit handlers receive `values`."""
    pass
