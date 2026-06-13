"""RMC operator widget.

Shows run telemetry and command hints for the Random Challenge mode.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from pyplanet.views.template import TemplateView

from pyplanet.apps.tmsm.widget_engine import AnimDir, DriveMode
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase

logger = logging.getLogger(__name__)


class RmcOperatorWidget(WidgetAppBase):
    name = "pyplanet.apps.tmsm.rmc_operator_widget"
    label = "rmc_operator_widget"

    WIDGET_KEY = "rmc_operator"
    WIDGET_NAME = "RMC Operator"
    WIDGET_DESCRIPTION = "Operator status and quick command hints for RMC."
    WIDGET_ICON = "sliders-h"
    WIDGET_TEMPLATE = "rmc_operator_widget/widget.xml"

    WIDGET_DEFAULT_X = -122.0
    WIDGET_DEFAULT_Y = 50.0
    WIDGET_DEFAULT_W = 42.0
    WIDGET_DEFAULT_H = 58.0

    WIDGET_REFRESH_SECONDS = 1.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_DRIVE_MODE = DriveMode.FIXED
    # Keep transitions instant but allow frame-script concealment to move
    # off-screen when widget_force_hidden is true.
    WIDGET_ANIM_DIR = AnimDir.RIGHT
    WIDGET_ANIM_DURATION_MS = 0
    WIDGET_ANIM_IN_DELAY_MS = 0
    WIDGET_ANIM_OUT_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "8fa2bbff"

    @staticmethod
    def _fmt_mmss(ms: int) -> str:
        total = max(0, int(ms)) // 1000
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    @staticmethod
    def _goal_medal_substyle(goal: str) -> str:
        key = str(goal or "at").strip().lower()
        if key == "bronze":
            return "MedalBronze"
        if key == "silver":
            return "MedalSilver"
        if key == "gold":
            return "MedalGold"
        return "MedalNadeo"

    @staticmethod
    def _goal_minus_one_substyle(goal: str) -> str:
        key = str(goal or "at").strip().lower()
        if key == "at":
            return "MedalGold"
        if key == "gold":
            return "MedalSilver"
        if key == "silver":
            return "MedalBronze"
        return "MedalBronze"

    def _active_mode(self):
        gm = getattr(self.instance.apps, "apps", {}).get("tmsm_gamemodes")
        if gm is None:
            return None
        mode = getattr(gm, "_active", None)
        if mode is None or str(getattr(mode, "key", "") or "") != "random_challenge":
            return None
        return mode

    async def _operator_hint(self, login: str, mode) -> str:
        if mode is None:
            return "Mode inactive"
        try:
            player = await self.instance.player_manager.get_player(login=login)
            checker = getattr(mode, "_is_operator", None)
            if callable(checker) and checker(player):
                return "/rmc pause | /rmc play | /rmc vote skip"
        except Exception:
            pass
        return "/rmc status"

    async def on_start(self) -> None:
        await super().on_start()
        if self.view is None:
            return
        self.view.connect("rmc_action", self._on_rmc_action)
        self.view.connect("rmc_pause_play", self._on_rmc_pause_play)
        self.view.connect("rmc_free_skip", self._on_rmc_free_skip)
        self.view.connect("rmc_broken_skip", self._on_rmc_broken_skip)
        self.view.connect("rmc_secondary_skip", self._on_rmc_secondary_skip)
        # Outside an active RMC mode the widget must not render at all.
        # Send an empty manialink immediately so the initial display() from
        # WidgetAppBase.on_start cannot leave the widget visible.
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
            logger.exception("rmc_operator_widget: hide failed")

    async def _refresh_loop(self) -> None:
        # Replace the base refresh loop so we can actively hide the widget
        # whenever RMC is not the active game mode.
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
                    logger.exception("rmc_operator_widget: refresh display failed")
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _is_operator(mode, player) -> bool:
        checker = getattr(mode, "_is_operator", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(player))
        except Exception:
            return False

    async def _require_mode_and_operator(self, player):
        mode = self._active_mode()
        if mode is None:
            return None
        if not self._is_operator(mode, player):
            await self.instance.chat("$f80RMC: operator only.", player.login)
            return None
        return mode

    async def _refresh_view_for(self, login: str = "") -> None:
        if self.view is None:
            return
        try:
            if login:
                await self.view.display(player_logins=[login])
                return
            online_logins = [
                p.login for p in self.instance.player_manager.online
                if getattr(p, "login", None)
            ]
            if online_logins:
                await self.view.display(player_logins=online_logins)
            else:
                await self.view.display()
        except Exception:
            try:
                logger.exception("rmc_operator_widget: refresh display failed")
            except Exception:
                pass

    async def _on_rmc_action(self, player, action=None, values=None, **kwargs) -> None:  # noqa: ARG002
        mode = await self._require_mode_and_operator(player)
        if mode is None:
            return
        run = dict(getattr(mode, "_state", {}).get("run") or {})
        if bool(run.get("active")):
            gm = getattr(self.instance.apps, "apps", {}).get("tmsm_gamemodes")
            if gm is not None:
                await gm._deactivate()
            else:
                await mode._finish_run("Stopped by operator")
        else:
            await mode._start_new_run(announce=True)
        await self._refresh_view_for()

    async def _on_rmc_pause_play(self, player, action=None, values=None, **kwargs) -> None:  # noqa: ARG002
        mode = await self._require_mode_and_operator(player)
        if mode is None:
            return
        run = dict(getattr(mode, "_state", {}).get("run") or {})
        if not bool(run.get("active")):
            await mode._start_new_run(announce=True)
        elif bool(run.get("paused")):
            await mode._resume_run(behavior="current")
        else:
            await mode._pause_run()
        await self._refresh_view_for(player.login)

    async def _on_rmc_free_skip(self, player, action=None, values=None, **kwargs) -> None:  # noqa: ARG002
        mode = await self._require_mode_and_operator(player)
        if mode is None:
            return
        await mode._start_skip_vote(vote_kind="free")
        await self._refresh_view_for(player.login)

    async def _on_rmc_broken_skip(self, player, action=None, values=None, **kwargs) -> None:  # noqa: ARG002
        mode = await self._require_mode_and_operator(player)
        if mode is None:
            return
        await mode._start_skip_vote(vote_kind="broken")
        await self._refresh_view_for(player.login)

    async def _on_rmc_secondary_skip(self, player, action=None, values=None, **kwargs) -> None:  # noqa: ARG002
        mode = await self._require_mode_and_operator(player)
        if mode is None:
            return
        await mode._secondary_skip()
        await self._refresh_view_for(player.login)

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        mode = self._active_mode()
        if mode is None:
            return {
                "widget_force_hidden": True,
                "is_operator": False,
                "y_off": -2.2,
                "state_text": "inactive",
                "timer_text": "60:00",
                "goal_time_text": "00:00",
                "goal_medal_substyle": "MedalNadeo",
                "goal_prev_medal_substyle": "MedalGold",
                "goal_count_text": "",
                "goal_prev_count_text": "",
                "pause_play_btn_text": "Pause / Play",
                "maps_text": "0",
                "broken_text": "0",
                "current_text": "-",
                "meta_text": "--.--.----  by -",
                "top_action_text": "Start RMC",
                "top_action_icon": "play",
                "top_action_variant": "success",
                "free_skip_btn_text": "Free Skip (1 left)",
                "broken_skip_btn_text": "Skip broken Map",
                "secondary_skip_btn_text": "Secondary Skip",
                "secondary_skip_available": False,
                "operator_hint": "Mode inactive",
                "vote_text": "vote: idle",
            }

        run = dict(getattr(mode, "_state", {}).get("run") or {})
        cmap = dict(run.get("current_map") or {})
        active = bool(run.get("active"))
        paused = bool(run.get("paused"))
        if not active:
            state_text = "idle"
        elif paused:
            state_text = "paused"
        else:
            state_text = "running"

        free_left = 0 if bool(run.get("free_skip_used")) else 1
        broken_skips = int(run.get("broken_skips") or 0)
        remaining_ms = int(getattr(mode, "_remaining_ms_now", lambda: 0)() or 0)
        goal_ms = int(cmap.get("goal_time_ms") or 0)
        current_name = str(cmap.get("name") or "-")
        if len(current_name) > 23:
            current_name = current_name[:20] + "..."
        author = str(cmap.get("author") or "-")
        if len(author) > 14:
            author = author[:11] + "..."
        date_text = datetime.utcnow().strftime("%d-%m-%Y")

        vote_text = "vote: idle"
        try:
            if bool(mode.ctx.votes.is_active):
                vote_text = "vote: active"
        except Exception:
            pass

        goal_key = str(run.get("goal_medal") or "at")
        try:
            # Prefer the live mode helper so config changes take effect
            # immediately, regardless of any stale value locked in the run.
            if hasattr(mode, "_goal_medal"):
                goal_key = str(mode._goal_medal(run) or goal_key)
        except Exception:
            pass
        # Resolve the sub-goal medal label so the secondary-skip button reads
        # "Gold Skip" / "Silver Skip" / "Bronze Skip" instead of a generic name.
        sub_key = "gold"
        try:
            if hasattr(mode, "_secondary_medal"):
                sub_key = str(mode._secondary_medal(goal_key) or "gold")
        except Exception:
            pass
        sub_label = {
            "at":     "AT",
            "gold":   "Gold",
            "silver": "Silver",
            "bronze": "Bronze",
        }.get(sub_key.lower(), sub_key.title())
        is_active = bool(run.get("active"))

        is_operator = False
        try:
            player = await self.instance.player_manager.get_player(login=login)
            is_operator = self._is_operator(mode, player)
        except Exception:
            is_operator = False

        # Players see a compact view (timer + medals + counts) with no
        # buttons. Shift content up and shrink the frame so there's no
        # empty space where the operator buttons would otherwise live.
        y_off = -2.2 if is_operator else 7.4
        # Idle-state timer reflects the configured run length, not a hardcoded
        # 60:00 — picks up the user's chosen duration the moment they change it.
        idle_timer_text = "60:00"
        try:
            if hasattr(mode, "_configured_run_duration_ms"):
                idle_timer_text = self._fmt_mmss(int(mode._configured_run_duration_ms()))
        except Exception:
            pass

        data: dict[str, Any] = {
            # Widget stays visible whenever the RMC mode is the active
            # game mode — even before the operator presses Start. The button
            # itself communicates whether the run is currently going.
            "widget_force_hidden": False,
            "is_operator": is_operator,
            "y_off": y_off,
            "state_text": state_text,
            "timer_text": self._fmt_mmss(remaining_ms) if is_active else idle_timer_text,
            "goal_time_text": self._fmt_mmss(goal_ms) if goal_ms > 0 else "00:00",
            "goal_medal_substyle": self._goal_medal_substyle(goal_key),
            "goal_prev_medal_substyle": self._goal_minus_one_substyle(goal_key),
            "goal_count_text": str(int(run.get("maps_cleared") or 0)) if is_active else "0",
            "goal_prev_count_text": str(int(run.get("secondary_cleared") or 0)) if is_active else "0",
            "pause_play_btn_text": "Resume" if paused else "Pause",
            # Pause logic is currently broken; hide the button entirely until fixed.
            "show_pause_btn": False,
            "maps_text": str(int(run.get("maps_cleared") or 0)),
            "broken_text": str(broken_skips),
            "current_text": current_name,
            "meta_text": f"{date_text}  by {author}",
            "top_action_text": "Stop RMC" if active else "Start RMC",
            "top_action_icon": "times" if active else "play",
            "top_action_variant": "danger" if active else "success",
            "free_skip_btn_text": f"Free Skip ({free_left} left)",
            "broken_skip_btn_text": "Skip broken Map",
            "secondary_skip_btn_text": (
                f"{sub_label} Skip (ready)"
                if (is_active and bool(cmap.get("secondary_cleared")) and not bool(cmap.get("cleared")))
                else f"{sub_label} Skip"
            ),
            "secondary_skip_available": bool(
                is_active
                and bool(cmap.get("secondary_cleared"))
                and not bool(cmap.get("cleared"))
            ),
            "operator_hint": await self._operator_hint(login, mode),
            "vote_text": vote_text,
        }
        if not is_operator:
            data["widget_h"] = 20.0
        return data
