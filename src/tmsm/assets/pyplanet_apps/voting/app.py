"""Player voting app (frontend/executor) backed by tmsm_voting_engine.

Supported vote types:
    * skip current map
    * extend timelimit by +5, +10 or +15 minutes
    * replay current map as next map
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

from pyplanet.apps.config import AppConfig
from pyplanet.contrib.command import Command
from pyplanet.views.template import TemplateView

from .views import VotingView, VotingWidgetView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)


class App_Voting(AppConfig):
    name = "pyplanet.apps.tmsm.voting"
    label = "voting"

    app_dependencies = [
        "core.maniaplanet", "tmsm_ui", "tmsm_hub", "tmsm_voting_engine", "widget_engine",
    ]
    game_dependencies = ["trackmania", "trackmania_next"]

    _START_COOLDOWN_S = 20
    _WIDGET_KEY = "voting_widget"
    _WIDGET_NAME = "Voting"
    _WIDGET_ICON = "check-square"
    _WIDGET_DEFAULT_X = 50.0
    _WIDGET_DEFAULT_Y = 86.0
    _WIDGET_DEFAULT_W = 58.0
    _WIDGET_DEFAULT_H = 12.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_vote_started_at: float = 0.0
        self._active_vote: dict[str, Any] | None = None
        self.view: VotingView | None = None
        self.widget_view: VotingWidgetView | None = None
        self._widget_binding: Any = None

    async def on_start(self) -> None:
        self.view = VotingView(self)
        self.view.handle_catch_all = self._view_catch_all

        self.widget_view = VotingWidgetView(self)
        self.widget_view.handle_catch_all = self._widget_catch_all
        self.widget_view.connect("vote_yes", self._widget_vote_yes)
        self.widget_view.connect("vote_no", self._widget_vote_no)
        # widget_engine renders `app.view`; provide a dedicated binding so
        # moving/editing this widget never opens the voting window UI.
        self._widget_binding = SimpleNamespace(view=self.widget_view)

        await self.instance.command_manager.register(
            Command(
                command="vote", target=self.chat_vote,
                description="Voting commands: skip, extend, replay, yes/no, 5/10/15, cancel.",
            ).add_param(name="arg1", required=False)
             .add_param(name="arg2", required=False),
            Command(command="yes", target=self.chat_yes,
                    description="Vote yes on the active vote."),
            Command(command="no", target=self.chat_no,
                    description="Vote no on the active vote."),
        )

        for code, cb in (
            ("tmsm_voting_engine:started", self._on_vote_started),
            ("tmsm_voting_engine:progress", self._on_vote_progress),
            ("tmsm_voting_engine:ended", self._on_vote_ended),
            ("tmsm_voting_engine:rejected", self._on_vote_rejected),
        ):
            self.context.signals.listen(code, cb)

        await self._register_with_hub()
        await self._register_widget_with_engine()
        self.context.signals.listen("widget_engine:request_register", self._on_widget_request_register)
        self.context.signals.listen("maniaplanet:player_connect", self._on_player_connect)
        await self._refresh_vote_widget()

    async def on_stop(self) -> None:
        if self.view is None:
            pass
        else:
            try:
                await self.view.destroy()
            except Exception:
                logger.exception("voting: view destroy failed")
            self.view = None
        if self.widget_view is None:
            return
        try:
            await self.widget_view.destroy()
        except Exception:
            logger.exception("voting: widget view destroy failed")
        self.widget_view = None
        self._widget_binding = None

    async def _on_player_connect(self, player, **kwargs) -> None:
        login = getattr(player, "login", None)
        if not login:
            return
        await self._refresh_vote_widget([login])

    def _online_logins(self) -> list[str]:
        try:
            return [
                str(p.login)
                for p in self.instance.player_manager.online
                if getattr(p, "login", None)
            ]
        except Exception:
            return []

    async def _register_widget_with_engine(self) -> None:
        try:
            from pyplanet.apps.tmsm.widget_engine.registry import (
                AnimDir, Animation, DriveMode, HideRule, WidgetEntry, WidgetKind,
            )
            sig = self.context.signals.get_signal("widget_engine:register")
        except Exception:
            logger.info("voting: widget_engine:register not available")
            return
        try:
            entry = WidgetEntry(
                key=self._WIDGET_KEY,
                name=self._WIDGET_NAME,
                description="Shows current vote, countdown, and quick yes/no buttons.",
                icon=self._WIDGET_ICON,
                default_x=self._WIDGET_DEFAULT_X,
                default_y=self._WIDGET_DEFAULT_Y,
                default_w=self._WIDGET_DEFAULT_W,
                default_h=self._WIDGET_DEFAULT_H,
                kind=WidgetKind.PERSISTENT,
                drive_mode=DriveMode.FIXED,
                hide_rule=HideRule(named=("in_menu",), raw=""),
                animation=Animation(direction=AnimDir.NONE, duration_ms=0),
                bg_color="0000",
                strip_enabled=False,
                author="tmsm",
                version="0.2",
            )
            await sig.send_robust({"entry": entry, "app": self._widget_binding or self}, raw=True)
        except Exception:
            logger.exception("voting: widget registration failed")

    async def _on_widget_request_register(self, **kwargs) -> None:
        await self._register_widget_with_engine()

    async def _refresh_vote_widget(self, logins: list[str] | None = None) -> None:
        if self.widget_view is None:
            return
        targets = logins or self._online_logins()
        if not targets:
            return

        vote_active = isinstance(self._active_vote, dict)
        if not vote_active:
            # First render hidden state so frame-script out animation can play.
            self.widget_view._visible = True
            for login in targets:
                self.widget_view._visible_logins.add(str(login))
            try:
                await self.widget_view.display(player_logins=targets)
            except Exception:
                logger.exception("voting: widget display-for-hide failed")

            max_delay_ms = 0
            for login in targets:
                try:
                    d = self._widget_hide_delay_ms(str(login))
                except Exception:
                    d = 0
                if d > max_delay_ms:
                    max_delay_ms = d
            asyncio.ensure_future(self._hide_widget_after(max_delay_ms / 1000.0, targets))
            return

        self.widget_view._visible = True
        for login in targets:
            self.widget_view._visible_logins.add(str(login))
        try:
            await self.widget_view.display(player_logins=targets)
        except Exception:
            logger.exception("voting: widget display failed")

    def _widget_hide_delay_ms(self, login: str) -> int:
        host = self.instance.apps.apps.get("widget_engine")
        if host is None or getattr(host, "engine", None) is None:
            return 0
        resolved = host.engine.resolve(self._WIDGET_KEY, login)
        if resolved is None:
            return 0
        raw_dir = getattr(getattr(resolved, "anim_dir", None), "value", None)
        anim_dir = str(raw_dir or "none").lower()
        if anim_dir == "none":
            return 0
        try:
            dur = int(getattr(resolved, "anim_duration_ms", 0) or 0)
        except (TypeError, ValueError):
            dur = 0
        try:
            out_delay = int(getattr(resolved, "anim_out_delay_ms", 0) or 0)
        except (TypeError, ValueError):
            out_delay = 0
        total = dur + out_delay
        return total if total > 0 else 0

    async def _hide_widget_after(self, delay_s: float, targets: list[str]) -> None:
        if delay_s > 0:
            try:
                await asyncio.sleep(delay_s)
            except asyncio.CancelledError:
                return
        # A new vote started while we were waiting; keep widget visible.
        if isinstance(self._active_vote, dict):
            return
        if self.widget_view is None:
            return
        for login in targets:
            self.widget_view._visible_logins.discard(str(login))
        try:
            await TemplateView.hide(self.widget_view, player_logins=targets)
        except Exception:
            logger.exception("voting: delayed widget hide failed")

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("voting: tmsm_hub:register not available yet")
            return

        entry = HubAppEntry(
            key="voting",
            name="Voting",
            icon="check-square",
            color="4d8",
            role=Role.PLAYER,
            order=20,
            description="Start and participate in map/time votes.",
            open=self._open,
            command="vote",
        )
        await sig.send_robust({"entry": entry}, raw=True)

    async def _open(self, player) -> None:
        if self.view is None:
            await self._chat_help(player)
            return
        # Maintain BaseView visibility bookkeeping so refresh() can update
        # only players who currently have the window open.
        self.view._visible = True
        self.view._visible_logins.add(player.login)
        await self.view.display(player_logins=[player.login])

    async def _hide_window_for(self, player) -> None:
        if self.view is None:
            return
        login = str(getattr(player, "login", "") or "")
        if not login:
            return
        self.view._visible_logins.discard(login)
        if not self.view._visible_logins:
            self.view._visible = False
        try:
            await TemplateView.hide(self.view, player_logins=[login])
        except Exception:
            logger.exception("voting: hide window failed")

    async def _refresh_view(self) -> None:
        if self.view is None:
            pass
        else:
            try:
                await self.view.refresh()
            except Exception:
                logger.exception("voting: view refresh failed")
        await self._refresh_vote_widget()

    def widget_context_for(self, login: str | None) -> dict[str, Any]:
        anchor_x = float(self._WIDGET_DEFAULT_X)
        anchor_y = float(self._WIDGET_DEFAULT_Y)
        card_w = float(self._WIDGET_DEFAULT_W)
        card_h = float(self._WIDGET_DEFAULT_H)
        anim_dir = "none"
        anim_duration_ms = 0
        anim_in_delay_ms = 0
        anim_out_delay_ms = 0
        widget_disabled = False

        bg_color = "0000"
        strip_color = "0000"
        strip_edge = "top"
        strip_thickness = 1.0

        host = self.instance.apps.apps.get("widget_engine")
        if host is not None and getattr(host, "engine", None) is not None and login:
            try:
                resolved = host.engine.resolve(self._WIDGET_KEY, login)
                if resolved is not None:
                    anchor_x = float(getattr(resolved, "x", anchor_x) or anchor_x)
                    anchor_y = float(getattr(resolved, "y", anchor_y) or anchor_y)
                    card_w = float(getattr(resolved, "w", card_w) or card_w)
                    card_h = float(getattr(resolved, "h", card_h) or card_h)
                    raw_dir = getattr(getattr(resolved, "anim_dir", None), "value", None)
                    anim_dir = str(raw_dir or anim_dir)
                    anim_duration_ms = int(getattr(resolved, "anim_duration_ms", anim_duration_ms) or anim_duration_ms)
                    anim_in_delay_ms = int(getattr(resolved, "anim_in_delay_ms", anim_in_delay_ms) or anim_in_delay_ms)
                    anim_out_delay_ms = int(getattr(resolved, "anim_out_delay_ms", anim_out_delay_ms) or anim_out_delay_ms)
                    widget_disabled = bool(getattr(resolved, "disabled", widget_disabled))
                    bg_color = str(getattr(resolved, "bg_color", bg_color) or bg_color)
                    strip_color = str(getattr(resolved, "strip_color", strip_color) or strip_color)
                    strip_edge = str(getattr(resolved, "strip_edge", strip_edge) or strip_edge)
                    strip_thickness = float(getattr(resolved, "strip_thickness", strip_thickness) or strip_thickness)
            except Exception:
                logger.exception("voting: widget resolve failed")

        off_x, off_y = {
            "none": (0.0, 0.0),
            "left": (-500.0, 0.0),
            "right": (500.0, 0.0),
            "up": (0.0, 500.0),
            "down": (0.0, -500.0),
        }.get(anim_dir, (0.0, 0.0))

        vote = self._active_vote if isinstance(self._active_vote, dict) else None
        vote_active = vote is not None
        vote_title = "No active vote"
        remaining_text = "--"
        vote_hint = ""
        if vote is not None:
            vote_title = str(vote.get("title") or "Vote")
            try:
                rem = max(0, int(vote.get("remaining") or 0))
            except (TypeError, ValueError):
                rem = 0
            remaining_text = f"{rem}s"
            mode = str(vote.get("mode") or "")
            if mode == "threshold_yes_no":
                vote_hint = "Click YES or NO"
            else:
                vote_hint = "Use /vote window for non yes/no options"

        return {
            "anchor_x": anchor_x,
            "anchor_y": anchor_y,
            "card_w": card_w,
            "card_h": card_h,
            "widget_key": self._WIDGET_KEY,
            "widget_view_id": self.widget_view.id if self.widget_view is not None else "",
            "widget_kind": "persistent",
            "widget_x": anchor_x,
            "widget_y": anchor_y,
            "widget_w": card_w,
            "widget_h": card_h,
            "widget_scale_y": 1.0,
            "widget_disabled": widget_disabled,
            "widget_hide_clauses": ["MenuOpen"],
            "widget_hide_raw": "",
            "widget_anim_dir": anim_dir,
            "widget_anim_duration_ms": anim_duration_ms,
            "widget_anim_in_delay_ms": anim_in_delay_ms,
            "widget_anim_out_delay_ms": anim_out_delay_ms,
            "widget_anim_off_x": off_x,
            "widget_anim_off_y": off_y,
            "widget_bg_color": bg_color,
            "widget_strip_color": strip_color,
            "widget_strip_edge": strip_edge,
            "widget_strip_thickness": strip_thickness,
            "widget_edit_mode": False,
            "widget_debug_mode": False,
            "widget_debug_status": "",
            "widget_debug_lines": [],
            "vote_active": vote_active,
            "vote_title": vote_title,
            "remaining_text": remaining_text,
            "vote_hint": vote_hint,
            "widget_force_hidden": widget_disabled or (not vote_active),
        }

    async def _widget_vote_yes(self, player, values=None, **kwargs) -> None:
        value = self._widget_button_value(True)
        if value is None:
            await self._notify("$fa0There is no active vote.", player)
            return
        await self._cast(player, value)

    async def _widget_vote_no(self, player, values=None, **kwargs) -> None:
        value = self._widget_button_value(False)
        if value is None:
            await self._notify("$fa0There is no active vote.", player)
            return
        await self._cast(player, value)

    def _widget_button_value(self, is_yes: bool) -> str | None:
        vote = self._active_vote if isinstance(self._active_vote, dict) else None
        if vote is None:
            return None

        mode = str(vote.get("mode") or "")
        metadata = vote.get("metadata") if isinstance(vote.get("metadata"), dict) else {}
        options = [o for o in list(vote.get("options") or []) if isinstance(o, dict)]
        if not options:
            return None

        if mode == "threshold_yes_no":
            if is_yes:
                return str(metadata.get("yes_value") or "yes")
            return str(metadata.get("no_value") or "no")

        # Fallback for plurality votes: map YES to first option and NO to last
        # option so widget buttons stay meaningful.
        pick = options[0] if is_yes else options[-1]
        value = str(pick.get("value") or "")
        return value or None

    async def _widget_catch_all(self, player, action, values, **kwargs):
        # Known button signals are bound with subscribe(); unmatched actions
        # can be ignored safely.
        return

    async def view_context(self, login: str | None) -> dict[str, Any]:
        vote = self._active_vote if isinstance(self._active_vote, dict) else None
        active = vote is not None

        title = "No active vote"
        remaining = 0
        options_ctx: list[dict[str, Any]] = []
        selected_value = ""
        total_ballots = 0

        if active and vote is not None:
            title = str(vote.get("title") or "Vote")
            try:
                remaining = max(0, int(vote.get("remaining") or 0))
            except (TypeError, ValueError):
                remaining = 0

            tally = vote.get("tally") if isinstance(vote.get("tally"), dict) else {}
            ballots = vote.get("ballots") if isinstance(vote.get("ballots"), dict) else {}
            total_ballots = len(ballots)
            if login and login in ballots:
                selected_value = str(ballots.get(login) or "")

            for idx, option in enumerate(list(vote.get("options") or [])):
                if not isinstance(option, dict):
                    continue
                value = str(option.get("value") or "")
                label = str(option.get("label") or value or f"Option {idx + 1}")
                votes = int(tally.get(option.get("value"), 0)) if tally else 0
                options_ctx.append({
                    "index": idx,
                    "value": value,
                    "label": label,
                    "votes": votes,
                    "selected": value == selected_value,
                })

        now = asyncio.get_running_loop().time()
        cooldown_left = int(self._START_COOLDOWN_S - (now - self._last_vote_started_at))
        cooldown_left = max(0, cooldown_left)

        is_operator = False
        if login:
            try:
                player = await self.instance.player_manager.get_player(
                    login=login, lock=False,
                )
            except Exception:
                player = None
            if player is not None:
                try:
                    from pyplanet.apps.tmsm.ui import perms as _perms
                    is_operator = bool(_perms.is_operator(player))
                except Exception:
                    is_operator = False

        status = ""
        if active:
            status = f"{remaining}s left, {total_ballots} vote(s)"
        elif cooldown_left > 0:
            status = f"Start cooldown: {cooldown_left}s"
        else:
            status = "Start a vote using the buttons below"

        return {
            "vote_active": active,
            "vote_title": title,
            "vote_remaining": remaining,
            "vote_options": options_ctx,
            "vote_selected": selected_value,
            "vote_status": status,
            "can_start": (not active) and cooldown_left <= 0,
            "cooldown_left": cooldown_left,
            "is_operator": is_operator,
        }

    async def _view_catch_all(self, player, action, values, **kwargs):
        if action == "start_skip":
            await self._hide_window_for(player)
            await self._start_skip_vote(player)
            return
        if action == "start_extend_5":
            await self._hide_window_for(player)
            await self._start_extend_vote(player, minutes=5)
            return
        if action == "start_extend_10":
            await self._hide_window_for(player)
            await self._start_extend_vote(player, minutes=10)
            return
        if action == "start_replay":
            await self._hide_window_for(player)
            await self._start_replay_vote(player)
            return
        if action == "cancel_vote":
            from pyplanet.apps.tmsm.ui import perms as _perms
            if not _perms.is_operator(player):
                await self._notify("$f44Only operators can cancel an active vote.", player)
                return
            await self._emit_engine("request_cancel", {"reason": f"cancelled by {player.login}"})
            return
        if action.startswith("castopt__"):
            try:
                idx = int(action.split("__", 1)[1])
            except Exception:
                return
            vote = self._active_vote if isinstance(self._active_vote, dict) else None
            if vote is None:
                await self._notify("$fa0There is no active vote.", player)
                return
            options = list(vote.get("options") or [])
            if idx < 0 or idx >= len(options):
                return
            opt = options[idx]
            if not isinstance(opt, dict):
                return
            value = str(opt.get("value") or "")
            if not value:
                return
            await self._cast(player, value)
            return

    async def _emit_engine(self, code: str, payload: dict[str, Any]) -> None:
        try:
            sig = self.context.signals.get_signal(f"tmsm_voting_engine:{code}")
        except KeyError:
            await self._notify("$f44Voting engine is not loaded.", payload.get("player"))
            return
        await sig.send_robust(payload, raw=True)

    async def _notify(self, msg: str, player=None) -> None:
        # Voting addon should be silent in in-game chat; keep the message
        # in debug logs only for troubleshooting.
        try:
            login = getattr(player, "login", None)
            logger.debug("voting notify suppressed login=%s msg=%s", login, msg)
        except Exception:
            pass

    async def _broadcast(self, msg: str) -> None:
        await self._notify(msg)

    async def _chat_help(self, player) -> None:
        await self._notify(
            "$4d8Voting:$fff /vote$4d8 opens the voting window. Chat commands: "
            "$fff/vote skip$4d8 | $fff/vote extend 5|10|15$4d8 | $fff/vote replay$4d8 | "
            "$fff/vote yes$4d8/$fffno$4d8 | $fff/vote 5$4d8/$fff10$4d8/$fff15$4d8 (when extend vote is active).",
            player,
        )

    async def chat_yes(self, player, data=None, **kwargs) -> None:
        await self._cast(player, "yes")

    async def chat_no(self, player, data=None, **kwargs) -> None:
        await self._cast(player, "no")

    async def chat_vote(self, player, data, **kwargs) -> None:
        arg1 = (getattr(data, "arg1", None) or "").strip().lower()
        arg2 = (getattr(data, "arg2", None) or "").strip().lower()
        token = arg1 or ""

        if token in ("", "help", "?"):
            await self._open(player)
            return

        if token in ("yes", "y", "+", "up"):
            await self._cast(player, "yes")
            return
        if token in ("no", "n", "-", "down"):
            await self._cast(player, "no")
            return
        if token in ("5", "+5", "extend5"):
            await self._cast(player, "extend_5")
            return
        if token in ("10", "+10", "extend10"):
            await self._cast(player, "extend_10")
            return
        if token in ("15", "+15", "extend15"):
            await self._cast(player, "extend_15")
            return

        if token == "cancel":
            from pyplanet.apps.tmsm.ui import perms as _perms
            if not _perms.is_operator(player):
                await self._notify("$f44Only operators can cancel an active vote.", player)
                return
            await self._emit_engine("request_cancel", {"reason": f"cancelled by {player.login}"})
            return

        if token == "skip":
            await self._start_skip_vote(player)
            return
        if token == "extend":
            if arg2 in ("15", "+15", "extend15"):
                await self._start_extend_vote(player, minutes=15)
            elif arg2 in ("10", "+10", "extend10"):
                await self._start_extend_vote(player, minutes=10)
            else:
                await self._start_extend_vote(player, minutes=5)
            return
        if token in ("replay", "again"):
            await self._start_replay_vote(player)
            return

        # Accept '/vote cast <value>' style fallback.
        if token == "cast" and arg2:
            await self._cast(player, arg2)
            return

        await self._chat_help(player)

    async def _cast(self, player, raw_value: str) -> None:
        value = str(raw_value or "").strip().lower()
        if not value:
            await self._chat_help(player)
            return
        await self._emit_engine("request_cast", {
            "login": player.login,
            "value": value,
        })

    async def _start_skip_vote(self, player) -> None:
        if not await self._can_start_vote(player):
            return
        await self._emit_engine("request_start", {
            "key": "skip_map",
            "title": "Skip current map?",
            "mode": "threshold_yes_no",
            "options": [
                {"value": "yes", "label": "Yes"},
                {"value": "no", "label": "No"},
            ],
            "duration_s": 20,
            "pass_ratio": 0.55,
            "initiator": player.login,
            "metadata": {"action": "skip_map", "yes_value": "yes", "no_value": "no"},
        })

    async def _start_replay_vote(self, player) -> None:
        if not await self._can_start_vote(player):
            return
        await self._emit_engine("request_start", {
            "key": "replay_next",
            "title": "Play current map again as next map?",
            "mode": "threshold_yes_no",
            "options": [
                {"value": "yes", "label": "Yes"},
                {"value": "no", "label": "No"},
            ],
            "duration_s": 20,
            "pass_ratio": 0.55,
            "initiator": player.login,
            "metadata": {"action": "replay_next", "yes_value": "yes", "no_value": "no"},
        })

    async def _start_extend_vote(self, player, minutes: int = 5) -> None:
        if not await self._can_start_vote(player):
            return
        req = int(minutes)
        if req >= 15:
            mins = 15
        elif req >= 10:
            mins = 10
        else:
            mins = 5
        await self._emit_engine("request_start", {
            "key": f"extend_time_{mins}",
            "title": f"Extend timelimit by +{mins} min?",
            "mode": "threshold_yes_no",
            "options": [
                {"value": "yes", "label": "Yes"},
                {"value": "no", "label": "No"},
            ],
            "duration_s": 20,
            "pass_ratio": 0.55,
            "initiator": player.login,
            "metadata": {
                "action": "extend_time",
                "extend_minutes": mins,
                "yes_value": "yes",
                "no_value": "no",
            },
        })

    async def _can_start_vote(self, player) -> bool:
        now = asyncio.get_running_loop().time()
        left = int(self._START_COOLDOWN_S - (now - self._last_vote_started_at))
        if left > 0:
            await self._notify(f"$fa0Please wait {left}s before starting another vote.", player)
            return False
        return True

    @staticmethod
    def _unwrap(kwargs: dict[str, Any]) -> dict[str, Any]:
        src = kwargs.get("source")
        if isinstance(src, dict):
            return src
        out = dict(kwargs)
        out.pop("signal", None)
        out.pop("source", None)
        return out

    async def _on_vote_started(self, **kwargs) -> None:
        payload = self._unwrap(kwargs)
        vote = payload.get("vote") or {}
        if not isinstance(vote, dict):
            return
        self._active_vote = vote
        self._last_vote_started_at = asyncio.get_running_loop().time()

        title = str(vote.get("title") or "Vote")
        opts = [str(o.get("label") or o.get("value")) for o in list(vote.get("options") or []) if isinstance(o, dict)]
        await self._broadcast(
            f"$4d8Vote started:$fff {title}$4d8 ({'/'.join(opts)}). "
            f"Use $fff/vote yes/no$4d8 or $fff/vote 5|10|15$4d8 depending on vote."
        )
        await self._refresh_view()

    async def _on_vote_progress(self, **kwargs) -> None:
        payload = self._unwrap(kwargs)
        vote = payload.get("vote")
        if isinstance(vote, dict):
            # Guard against out-of-order progress/ended delivery: a stale
            # timeout snapshot must not re-show the widget.
            try:
                remaining = int(vote.get("remaining") or 0)
            except (TypeError, ValueError):
                remaining = 0
            if remaining <= 0:
                self._active_vote = None
                await self._refresh_view()
                return
            self._active_vote = vote
            await self._refresh_view()

    async def _on_vote_rejected(self, **kwargs) -> None:
        payload = self._unwrap(kwargs)
        reason = str(payload.get("reason") or "rejected")
        req = payload.get("request") if isinstance(payload.get("request"), dict) else {}
        login = req.get("login")
        if login:
            try:
                player = await self.instance.player_manager.get_player(login=login, lock=False)
            except Exception:
                player = None
            if player is not None:
                await self._notify(f"$f44Vote action rejected: {reason}", player)
        await self._refresh_view()

    async def _on_vote_ended(self, **kwargs) -> None:
        payload = self._unwrap(kwargs)
        result = payload.get("result")
        if not isinstance(result, dict):
            return
        self._active_vote = None

        cancelled = bool(result.get("cancelled", False))
        if cancelled:
            await self._broadcast(f"$fa0Vote cancelled ($fff{result.get('reason', 'cancelled')}$fa0).")
            await self._refresh_view()
            return

        winner = str(result.get("winner") or "")
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        action = str(metadata.get("action") or "")
        await self._broadcast(f"$4d8Vote result:$fff winner = {winner or '-'}$4d8.")

        try:
            if action == "skip_map" and winner == "yes":
                await self.instance.gbx("NextMap")
                await self._broadcast("$4d8Vote passed:$fff skipping current map.")
                await self._refresh_view()
                return

            if action == "replay_next" and winner == "yes":
                cur = getattr(self.instance.map_manager, "current_map", None)
                if cur is None:
                    await self._broadcast("$f44Replay vote passed but current map is unavailable.")
                    return
                # Prefer the jukebox queue when available so the replay is
                # announced naturally on podium_start.
                jb = self.instance.apps.apps.get("jukebox")
                if jb is not None and hasattr(jb, "insert_map"):
                    jb.insert_map(None, cur, 0)
                    await self._broadcast("$4d8Vote passed:$fff current map queued as next map.")
                else:
                    await self.instance.map_manager.set_next_map(cur)
                    await self._broadcast("$4d8Vote passed:$fff next map set to current map.")
                await self._refresh_view()
                return

            if action == "extend_time":
                mins_raw = metadata.get("extend_minutes")
                mins = None
                try:
                    if mins_raw is not None:
                        mins = int(mins_raw)
                except (TypeError, ValueError):
                    mins = None

                if mins in (5, 10):
                    if winner == "yes":
                        ok = await self._extend_timelimit_minutes(mins)
                        await self._broadcast(
                            f"$4d8Vote passed:$fff timelimit +{mins} min."
                            if ok else f"$f44Could not extend timelimit (+{mins})."
                        )
                    else:
                        await self._broadcast("$fa0Vote ended with no timelimit change.")
                elif winner == "extend_5":
                    ok = await self._extend_timelimit_minutes(5)
                    await self._broadcast("$4d8Vote passed:$fff timelimit +5 min." if ok else "$f44Could not extend timelimit (+5).")
                elif winner == "extend_10":
                    ok = await self._extend_timelimit_minutes(10)
                    await self._broadcast("$4d8Vote passed:$fff timelimit +10 min." if ok else "$f44Could not extend timelimit (+10).")
                else:
                    await self._broadcast("$fa0Vote ended with no timelimit change.")
                await self._refresh_view()
                return
        except Exception:
            logger.exception("voting: applying vote result failed")
            await self._broadcast("$f44Applying vote result failed (see server log).")
        finally:
            await self._refresh_view()

    async def _timelimit_unit_hint(self) -> str:
        try:
            info = await self.instance.gbx("GetModeScriptInfo")
        except Exception:
            return ""
        if not isinstance(info, dict):
            return ""
        for p in list(info.get("ParamDescs") or []):
            if not isinstance(p, dict):
                continue
            name = str(p.get("Name") or "")
            if "timelimit" not in name.lower():
                continue
            txt = f"{p.get('Type', '')} {p.get('Desc', '')}".lower()
            if "millisecond" in txt:
                return "milliseconds"
            if "second" in txt:
                return "seconds"
            if "minute" in txt:
                return "minutes"
        return ""

    @staticmethod
    def _normalize_timelimit_seconds(raw: Any, unit_hint: str = "") -> int | None:
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None
        if val <= 0:
            return None
        hint = str(unit_hint or "").lower()
        if hint == "minutes":
            secs = val * 60.0
        elif hint == "milliseconds":
            secs = val / 1000.0
        else:
            secs = val
        out = int(round(secs))
        return out if out > 0 else None

    @staticmethod
    def _encode_timelimit_value(seconds: int, unit_hint: str = "") -> int:
        hint = str(unit_hint or "").lower()
        if hint == "minutes":
            return int(round(seconds / 60.0))
        if hint == "milliseconds":
            return int(round(seconds * 1000.0))
        return int(seconds)

    async def _extend_timelimit_minutes(self, add_minutes: int) -> bool:
        try:
            settings = await self.instance.gbx("GetModeScriptSettings")
        except Exception:
            settings = None
        if not isinstance(settings, dict):
            return False

        key = None
        for k in ("S_TimeLimit", "TimeLimit", "S_TimeLimitSeconds"):
            if k in settings:
                key = k
                break
        if key is None:
            for k in settings.keys():
                if "timelimit" in str(k).lower():
                    key = str(k)
                    break
        if key is None:
            return False

        hint = await self._timelimit_unit_hint()
        cur_s = self._normalize_timelimit_seconds(settings.get(key), hint)
        if cur_s is None:
            return False
        new_s = max(1, min(86400, cur_s + int(add_minutes) * 60))
        payload = {key: self._encode_timelimit_value(new_s, hint)}
        try:
            await self.instance.gbx("SetModeScriptSettings", payload)
            return True
        except Exception:
            logger.exception("voting: SetModeScriptSettings failed for timelimit extend")
            return False