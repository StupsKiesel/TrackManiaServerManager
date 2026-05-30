"""Shared base class for tiny placeholder addons.

Each placeholder addon subclasses :class:`WipAppBase` and sets four class
attributes (`HUB_KEY`, `HUB_NAME`, `HUB_ICON`, `HUB_ROLE`). The base takes
care of:
  * registering a tile with the hub on startup,
  * showing a per-player 'work in progress. Soon™' dialog when the tile
    is clicked,
  * dismissing the dialog and reopening the hub when the user clicks OK.

The dialog uses the shared template at ``tmsm_hub/wip.xml`` so addons
ship no template of their own.
"""
from __future__ import annotations

import logging

from pyplanet.apps.config import AppConfig
from pyplanet.apps.tmsm.hub.registry import HubAppEntry, Role, Status
from pyplanet.apps.tmsm.ui.views import BaseView
from pyplanet.views.template import TemplateView

logger = logging.getLogger(__name__)


class _WipView(BaseView):
    """Per-app 'work in progress' dialog. Shares one template, but each
    app gets its own subclass so PyPlanet can address the view by id."""

    template_name = "tmsm_hub/wip.xml"

    def __init__(self, app):
        super().__init__(app)
        self.hide_click = False

    async def get_context_data(self):
        ctx = await super().get_context_data()
        ctx.update(
            hub_app_name=self.app.HUB_NAME,
            hub_app_icon=self.app.HUB_ICON,
        )
        return ctx


def _make_view_class(label: str) -> type[_WipView]:
    """Create a unique view subclass per app so PyPlanet's view registry
    keeps them apart (id is derived from the class module+name)."""
    cls = type(
        f"WipView_{label}",
        (_WipView,),
        {"__module__": f"pyplanet.apps.tmsm.{label}.app"},
    )
    return cls


class WipAppBase(AppConfig):
    """Subclass and set the ``HUB_*`` attributes."""

    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_hub"]
    game_dependencies = ["trackmania", "trackmania_next"]

    HUB_KEY: str = ""
    HUB_NAME: str = ""
    HUB_ICON: str = "cog"
    HUB_COLOR: str = "15f"          # 3-digit hex accent
    HUB_DESCRIPTION: str = ""
    HUB_ROLE: Role = Role.PLAYER
    HUB_STATUS: Status = Status.WIP  # all placeholders are WIP by default
    HUB_ORDER: int = 100
    HUB_TAGS: tuple[str, ...] = ()
    HUB_COMMAND: str | None = None
    HUB_AUTHOR: str = "tmsm"
    HUB_VERSION: str = "0.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.view: _WipView | None = None

    async def on_start(self) -> None:
        view_cls = _make_view_class(self.label)
        try:
            self.view = view_cls(self)
            self.view.handle_catch_all = self._on_dialog_action  # type: ignore[assignment]
        except Exception:
            logger.exception("%s: view init failed", self.label)
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("%s: tmsm_hub:register signal not registered yet", self.label)
            return
        entry = HubAppEntry(
            key=self.HUB_KEY,
            name=self.HUB_NAME,
            icon=self.HUB_ICON,
            color=self.HUB_COLOR,
            description=self.HUB_DESCRIPTION or f"{self.HUB_NAME} — work in progress",
            role=self.HUB_ROLE,
            status=self.HUB_STATUS,
            tags=list(self.HUB_TAGS),
            order=self.HUB_ORDER,
            command=self.HUB_COMMAND,
            author=self.HUB_AUTHOR,
            version=self.HUB_VERSION,
            open=self._hub_open,
        )
        await sig.send_robust({"entry": entry}, raw=True)

    async def on_stop(self) -> None:
        if self.view is None:
            return
        try:
            await self.view.destroy()
        except Exception:
            logger.exception("%s: destroy failed", self.label)
        self.view = None

    async def _hub_open(self, player) -> None:
        if self.view is None:
            return
        try:
            await self.view.display(player_logins=[player.login])
        except Exception:
            logger.exception("%s: display failed for %s", self.label, player.login)

    async def _on_dialog_action(self, player, action, values, **kwargs) -> None:
        # info_dialog uses the signal name `wip__ok`; the window's close
        # button fires `_close`.
        if not (action.endswith("__ok")
                or action.endswith("__close")
                or action == "_close"):
            return
        if self.view is not None:
            try:
                await TemplateView.hide(self.view, player_logins=[player.login])
            except Exception:
                logger.exception("%s: hide failed", self.label)
        try:
            sig = self.context.signals.get_signal("tmsm_hub:show")
            await sig.send_robust({"player": player}, raw=True)
        except Exception:
            logger.exception("%s: hub:show failed", self.label)
