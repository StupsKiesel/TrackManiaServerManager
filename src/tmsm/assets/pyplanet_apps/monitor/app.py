"""monitor - per-player HUD edge calibration.

Applies a translation-only calibration offset (no scaling/stretching) so
widgets can be aligned to monitor/window edges across aspect ratios.
"""
from __future__ import annotations

import logging
import re

from pyplanet.apps.config import AppConfig
from pyplanet.contrib.command import Command

from .views import MonitorView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role

    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)


class MonitorApp(AppConfig):
    name = "pyplanet.apps.tmsm.monitor"
    label = "monitor"
    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_widgets"]
    game_dependencies = ["trackmania", "trackmania_next", "shootmania"]

    EDGE_MIN = -20
    EDGE_MAX = 100
    STRETCH_MIN = -40
    STRETCH_MAX = 100

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.view: MonitorView | None = None

    async def on_start(self) -> None:
        try:
            self.view = MonitorView(self)
            self.view.handle_catch_all = self._catch_all  # type: ignore[assignment]
        except Exception:
            logger.exception("monitor: view init failed")
            self.view = None

        try:
            await self.instance.command_manager.register(
                Command(
                    command="monitor",
                    target=self._cmd_monitor,
                    description="Open monitor calibration (player HUD edge fit).",
                )
            )
        except Exception:
            logger.exception("monitor: command registration failed")

        await self._register_with_hub()

    async def on_stop(self) -> None:
        if self.view is not None:
            try:
                await self.view.destroy()
            except Exception:
                logger.exception("monitor: destroy failed")
            self.view = None

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("monitor: tmsm_hub:register not available yet")
            return
        entry = HubAppEntry(
            key="monitor",
            name="Monitor",
            icon="monitor",
            color="2aa",
            role=Role.PLAYER,
            order=32,
            description="Calibrate widget edge fit for your monitor.",
            open=self._open,
            command="monitor",
        )
        await sig.send_robust({"entry": entry}, raw=True)

    def _widgets_app(self):
        try:
            return self.instance.apps.apps.get("tmsm_widgets")
        except Exception:
            return None

    def _edge_value(self, login: str) -> int:
        wa = self._widgets_app()
        if wa is None:
            return 0
        try:
            off = wa.get_ui_offset(login)
            return int(round(float(off.get("x", 0.0))))
        except Exception:
            return 0

    async def _set_edge_value(self, login: str, edge: int) -> None:
        wa = self._widgets_app()
        if wa is None:
            return
        edge = max(self.EDGE_MIN, min(self.EDGE_MAX, int(edge)))
        try:
            off = wa.get_ui_offset(login)
            y = float(off.get("y", 0.0))
            await wa.set_ui_offset(login, float(edge), y)
        except Exception:
            logger.exception("monitor: set edge failed for %s", login)

    def _stretch_value(self, login: str) -> int:
        wa = self._widgets_app()
        if wa is None:
            return 0
        try:
            return int(round(float(wa.get_ui_stretch(login))))
        except Exception:
            return 0

    async def _set_stretch_value(self, login: str, stretch: int) -> None:
        wa = self._widgets_app()
        if wa is None:
            return
        stretch = max(self.STRETCH_MIN, min(self.STRETCH_MAX, int(stretch)))
        try:
            await wa.set_ui_stretch(login, float(stretch))
        except Exception:
            logger.exception("monitor: set stretch failed for %s", login)

    async def _reset_calibration(self, login: str) -> None:
        wa = self._widgets_app()
        if wa is None:
            return
        try:
            await wa.clear_ui_offset(login)
            await wa.set_ui_stretch(login, 0.0)
        except Exception:
            logger.exception("monitor: reset calibration failed for %s", login)

    async def _open(self, player) -> None:
        if self.view is None:
            return
        login = player.login
        try:
            await self.view.display(player_logins=[login])
            self.view._visible_logins.add(login)
            self.view._visible = bool(self.view._visible_logins)
        except Exception:
            logger.exception("monitor: open failed")

    async def _refresh(self, login: str) -> None:
        if self.view is None or login not in self.view._visible_logins:
            return
        try:
            await self.view.display(player_logins=[login])
        except Exception:
            logger.exception("monitor: refresh failed")

    async def _cmd_monitor(self, player, data, **kwargs) -> None:
        await self._open(player)

    async def _catch_all(self, player, action, values, **kwargs) -> None:
        login = player.login

        if action == "_close" or action.startswith("_crumb__"):
            if self.view is not None:
                self.view._visible_logins.discard(login)
                self.view._visible = bool(self.view._visible_logins)
                try:
                    from pyplanet.views.template import TemplateView

                    await TemplateView.hide(self.view, player_logins=[login])
                except Exception:
                    logger.exception("monitor: hide on close failed")
            return

        if action == "edgefit__inc":
            await self._set_edge_value(login, self._edge_value(login) + 1)
            await self._refresh(login)
            return
        if action == "edgefit__dec":
            await self._set_edge_value(login, self._edge_value(login) - 1)
            await self._refresh(login)
            return

        if action == "stretchfit__inc":
            await self._set_stretch_value(login, self._stretch_value(login) + 1)
            await self._refresh(login)
            return
        if action == "stretchfit__dec":
            await self._set_stretch_value(login, self._stretch_value(login) - 1)
            await self._refresh(login)
            return

        if action == "calibration__reset":
            await self._reset_calibration(login)
            await self._refresh(login)
            return

    async def view_context(self, login: str) -> dict:
        edge = self._edge_value(login)
        stretch = self._stretch_value(login)
        if edge == 0:
            note = "No offset. Widgets use stored positions as-is."
        elif edge > 0:
            note = "Positive edge fit pushes left widgets left and right widgets right."
        else:
            note = "Negative edge fit pulls widgets toward center."
        if stretch > 0:
            note2 = "Vertical unstretch compresses Y and widget height (counteracts tall stretch)."
        elif stretch < 0:
            note2 = "Negative vertical unstretch expands Y and widget height."
        else:
            note2 = "Vertical unstretch at 0% leaves Y and height unchanged."
        return {
            "edgefit": edge,
            "stretchfit": stretch,
            "note": note,
            "note2": note2,
        }
