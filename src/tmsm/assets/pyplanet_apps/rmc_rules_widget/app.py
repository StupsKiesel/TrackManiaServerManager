"""RMC rules widget.

Shows the active Random Challenge objective and core run rules.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from pyplanet.views.template import TemplateView

from pyplanet.apps.tmsm.widget_engine import AnimDir, DriveMode
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase

logger = logging.getLogger(__name__)


class RmcRulesWidget(WidgetAppBase):
    name = "pyplanet.apps.tmsm.rmc_rules_widget"
    label = "rmc_rules_widget"

    WIDGET_KEY = "rmc_rules"
    WIDGET_NAME = "RMC Rules"
    WIDGET_DESCRIPTION = "Current RMC goal and run rules."
    WIDGET_ICON = "book"
    WIDGET_TEMPLATE = "rmc_rules_widget/widget.xml"

    WIDGET_DEFAULT_X = -122.0
    WIDGET_DEFAULT_Y = 70.0
    WIDGET_DEFAULT_W = 58.0
    WIDGET_DEFAULT_H = 18.0

    WIDGET_REFRESH_SECONDS = 1.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_DRIVE_MODE = DriveMode.FIXED
    # Keep transitions instant but allow frame-script concealment to move
    # off-screen when widget_force_hidden is true.
    WIDGET_ANIM_DIR = AnimDir.RIGHT
    WIDGET_ANIM_DURATION_MS = 0
    WIDGET_ANIM_IN_DELAY_MS = 0
    WIDGET_ANIM_OUT_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "f0aa33ff"

    @staticmethod
    def _goal_label(key: str) -> str:
        return {
            "bronze": "Bronze",
            "silver": "Silver",
            "gold": "Gold",
            "at": "AT",
        }.get(str(key or "").strip().lower(), "AT")

    def _active_mode(self):
        gm = getattr(self.instance.apps, "apps", {}).get("tmsm_gamemodes")
        if gm is None:
            return None
        mode = getattr(gm, "_active", None)
        if mode is None or str(getattr(mode, "key", "") or "") != "random_challenge":
            return None
        return mode

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        mode = self._active_mode()
        if mode is None:
            return {
                "widget_force_hidden": True,
                "title_right": "inactive",
                "goal_text": "AT",
                "timer_text": "--:--.--- / --:--.---",
                "rules_line_1": "First goal clear advances to next map.",
                "rules_line_2": "1 free skip vote per run.",
                "rules_line_3": "Broken-map skip vote is always available.",
            }

        run = dict(getattr(mode, "_state", {}).get("run") or {})
        goal_key = "at"
        try:
            goal_key = str(mode._goal_medal(run))
        except Exception:
            goal_key = str(run.get("goal_medal") or "at")
        try:
            remaining_ms = int(mode._remaining_ms_now())
            total_ms = int(getattr(mode, "RUN_DURATION_MS", 0) or 0)
            timer_text = f"{mode._fmt_ms(remaining_ms)} / {mode._fmt_ms(total_ms)}"
        except Exception:
            timer_text = "--:--.--- / --:--.---"

        active = bool(run.get("active"))
        paused = bool(run.get("paused"))
        if not active:
            state_text = "idle"
        elif paused:
            state_text = "paused"
        else:
            state_text = "running"

        free_left = 0 if bool(run.get("free_skip_used")) else 1
        return {
            "widget_force_hidden": (not active),
            "title_right": state_text,
            "goal_text": self._goal_label(goal_key),
            "timer_text": timer_text,
            "rules_line_1": "First goal clear advances to next map.",
            "rules_line_2": f"Free skips left this run: {free_left}",
            "rules_line_3": "Use /rmc status for full run details.",
        }

    async def on_start(self) -> None:
        await super().on_start()
        # Outside an active RMC mode the widget must not render at all.
        await self._hide_view()

    async def _online_logins(self) -> list[str]:
        try:
            return [
                p.login for p in self.instance.player_manager.online
                if getattr(p, "login", None)
            ]
        except Exception:
            return []

    async def _hide_view(self) -> None:
        if self.view is None:
            return
        logins = await self._online_logins()
        try:
            if logins:
                await TemplateView.hide(self.view, player_logins=logins)
            else:
                await TemplateView.hide(self.view)
        except Exception:
            logger.exception("rmc_rules_widget: hide failed")

    async def _refresh_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.WIDGET_REFRESH_SECONDS or 1.0)
                if self.view is None:
                    continue
                mode = self._active_mode()
                if mode is None:
                    await self._hide_view()
                    continue
                logins = await self._online_logins()
                try:
                    if logins:
                        await self.view.display(player_logins=logins)
                    else:
                        await self.view.display()
                except Exception:
                    logger.exception("rmc_rules_widget: refresh display failed")
        except asyncio.CancelledError:
            pass
