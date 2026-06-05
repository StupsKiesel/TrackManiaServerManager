"""Karma widget with built-in voting logic.

Implements the core vote handling from contrib karma directly in this app so
servers can keep the legacy contrib widget disabled while using the new HUD.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from pyplanet.apps.contrib.karma.models import Karma as KarmaModel
from pyplanet.apps.core.statistics.models import Score
from pyplanet.contrib.setting import Setting

from pyplanet.apps.tmsm.widget_engine import AnimDir, DriveMode
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase


logger = logging.getLogger(__name__)


class KarmaWidgetApp(WidgetAppBase):
    name = "pyplanet.apps.tmsm.karma_widget"
    label = "karma_widget"

    WIDGET_KEY = "karma_widget"
    WIDGET_NAME = "Karma"
    WIDGET_DESCRIPTION = "Current map karma score and vote percentage."
    WIDGET_ICON = "heart"
    WIDGET_TEMPLATE = "karma_widget/karma.xml"

    WIDGET_DEFAULT_X = 112.0
    WIDGET_DEFAULT_Y = 67.0
    WIDGET_DEFAULT_W = 46.0
    WIDGET_DEFAULT_H = 11.0

    # Keep refresh event-driven. Periodic full re-renders reset the frame
    # script and can look like a pop-in instead of a smooth show/hide.
    WIDGET_REFRESH_SECONDS = 0.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_DRIVE_MODE = DriveMode.FIXED
    WIDGET_ANIM_DIR = AnimDir.RIGHT
    WIDGET_ANIM_DURATION_MS = 250
    WIDGET_ANIM_IN_DELAY_MS = 0
    WIDGET_ANIM_OUT_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "ee5577ff"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._queued_refresh: asyncio.Task | None = None
        self._lock = asyncio.Lock()

        self.current_votes: list[Any] = []
        self.current_karma: float = 0.0
        self.current_karma_percentage: float = 0.0
        self.current_karma_positive: float = 0.0
        self.current_karma_negative: float = 0.0

        self.setting_expanded_voting = Setting(
            "expanded_voting",
            "Expanded voting",
            Setting.CAT_BEHAVIOUR,
            type=bool,
            description="Use additional votes (-, +, +-, +++, ---) in addition to ++/--.",
            default=False,
        )
        self.setting_finishes_before_voting = Setting(
            "finishes_before_voting",
            "Finishes before voting",
            Setting.CAT_BEHAVIOUR,
            type=int,
            description="Required amount of map finishes before voting is allowed.",
            default=0,
        )

    async def on_start(self) -> None:
        await super().on_start()

        if self.view is not None:
            self.view.subscribe("vote_up", self._on_vote_up)
            self.view.subscribe("vote_down", self._on_vote_down)

        await self.context.setting.register(
            self.setting_finishes_before_voting,
            self.setting_expanded_voting,
        )

        try:
            self.context.signals.listen("maniaplanet:map_begin", self._on_map_begin)
            self.context.signals.listen("maniaplanet:map_start", self._on_refresh_signal)
            self.context.signals.listen("maniaplanet:player_connect", self._on_refresh_signal)
        except Exception:
            pass

        await self._reload_votes_for_current_map()
        await self._calculate_karma()

    async def on_stop(self) -> None:
        if self._queued_refresh is not None:
            self._queued_refresh.cancel()
            self._queued_refresh = None
        await super().on_stop()

    async def _on_map_begin(self, **kwargs) -> None:
        await self._reload_votes_for_current_map()
        await self._calculate_karma()
        self._queue_refresh()

    async def _on_refresh_signal(self, **kwargs) -> None:
        self._queue_refresh()

    def _queue_refresh(self) -> None:
        if self.view is None:
            return
        if self._queued_refresh is not None and not self._queued_refresh.done():
            return

        async def _flush() -> None:
            try:
                # Coalesce event bursts while keeping the HUD reactive.
                await asyncio.sleep(0.12)
                if self.view is not None:
                    await self.view.refresh()
            except Exception:
                pass
            finally:
                self._queued_refresh = None

        self._queued_refresh = asyncio.create_task(_flush())

    @staticmethod
    def _vote_text(score: float | int | None) -> str:
        if score is None:
            return "--"
        try:
            s = float(score)
        except (TypeError, ValueError):
            return "--"
        if s >= 1.0:
            return "++"
        if s >= 0.5:
            return "+"
        if s <= -1.0:
            return "--"
        if s <= -0.5:
            return "-"
        return "+-"

    def _current_map(self):
        return getattr(self.instance.map_manager, "current_map", None)

    async def _reload_votes_for_current_map(self) -> None:
        current_map = self._current_map()
        if current_map is None:
            self.current_votes = []
            return
        try:
            rows = await KarmaModel.objects.execute(
                KarmaModel.select().where(KarmaModel.map_id == current_map.get_id())
            )
            self.current_votes = list(rows)
        except Exception:
            logger.exception("karma_widget: failed to reload votes")
            self.current_votes = []

    async def _calculate_karma(self) -> None:
        total_score = 0.0
        total_abs = 0.0
        self.current_karma_positive = 0.0
        self.current_karma_negative = 0.0

        for vote in self.current_votes:
            score = getattr(vote, "score", 0)
            expanded = getattr(vote, "expanded_score", None)
            if expanded is not None:
                score = expanded
            score = float(score or 0)

            total_score += score
            total_abs += abs(score)
            if score > 0:
                self.current_karma_positive += score

        self.current_karma_negative = total_abs - self.current_karma_positive
        self.current_karma = total_score
        self.current_karma_percentage = (
            (self.current_karma_positive / total_abs) if total_abs > 0 else 0.0
        )

    async def _score_from_token(self, token: str) -> tuple[float, float] | None:
        expanded = bool(await self.setting_expanded_voting.get_value())
        if not expanded and token not in {"++", "--"}:
            return None

        normal_score = -1.0
        expanded_score = -1.0
        if token in {"++", "+++"}:
            normal_score = 1.0
            expanded_score = 1.0
        elif token == "+":
            normal_score = 1.0
            expanded_score = 0.5
        elif token in {"+-", "-+"}:
            expanded_score = 0.0
        elif token == "-":
            expanded_score = -0.5

        return normal_score, expanded_score

    async def _player_can_vote(self, player) -> tuple[bool, str | None]:
        finishes_required = int(await self.setting_finishes_before_voting.get_value() or 0)
        if finishes_required <= 0:
            return True, None

        current_map = self._current_map()
        if current_map is None:
            return False, "$f80No current map; vote rejected."

        player_finishes = await Score.objects.count(
            Score.select()
            .where(Score.map_id == current_map.get_id())
            .where(Score.player_id == player.get_id())
        )
        if int(player_finishes) < finishes_required:
            msg = (
                "$f80You have to finish this map at least "
                f"$fff{finishes_required}$f80 times before voting."
            )
            return False, msg
        return True, None

    async def _cast_vote_token(self, player, token: str) -> None:
        async with self._lock:
            current_map = self._current_map()
            if current_map is None:
                return

            valid, message = await self._player_can_vote(player)
            if not valid:
                if message:
                    try:
                        await self.instance.chat(message, player)
                    except Exception:
                        pass
                return

            mapped = await self._score_from_token(token)
            if mapped is None:
                return
            normal_score, expanded_score = mapped

            player_id = int(player.get_id())
            existing = next(
                (v for v in self.current_votes if int(getattr(v, "player_id", 0) or 0) == player_id),
                None,
            )

            if existing is not None:
                old_expanded = getattr(existing, "expanded_score", None)
                old_score = getattr(existing, "score", None)
                changed = (
                    (old_expanded is not None and float(old_expanded) != float(expanded_score))
                    or (old_expanded is None and float(old_score or 0) != float(expanded_score))
                )
                if not changed:
                    return

                existing.score = normal_score
                existing.expanded_score = expanded_score
                await existing.save()
            else:
                new_vote = KarmaModel(
                    map=current_map,
                    player=player,
                    score=normal_score,
                    expanded_score=expanded_score,
                )
                await new_vote.save()
                self.current_votes.append(new_vote)

            await self._calculate_karma()

        self._queue_refresh()

    async def _on_vote_up(self, player, action=None, values=None, **kwargs) -> None:
        await self._cast_vote_token(player, "++")

    async def _on_vote_down(self, player, action=None, values=None, **kwargs) -> None:
        await self._cast_vote_token(player, "--")

    def _my_vote_for_login(self, login: str) -> str:
        if not login:
            return "--"
        for p in list(getattr(self.instance.player_manager, "online", []) or []):
            if str(getattr(p, "login", "") or "") != login:
                continue
            pid = int(p.get_id())
            vote = next(
                (v for v in self.current_votes if int(getattr(v, "player_id", 0) or 0) == pid),
                None,
            )
            if vote is None:
                return "--"
            exp = getattr(vote, "expanded_score", None)
            return self._vote_text(exp if exp is not None else getattr(vote, "score", None))
        return "--"

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        p = (self.current_karma_percentage * 100.0) if self.current_votes else 0.0
        bar_pct = max(0.0, min(100.0, (p + 100.0) / 2.0))
        score = float(self.current_karma or 0.0)
        score_color = "3f8" if score >= 0 else "f66"

        return {
            "score": score,
            "percent": p,
            "positive": float(self.current_karma_positive or 0.0),
            "negative": float(self.current_karma_negative or 0.0),
            "count": len(self.current_votes),
            "my_vote": self._my_vote_for_login(login),
            "source": "LOCAL",
            "bar_pct": bar_pct,
            "score_color": score_color,
            "percent_text": f"{p:+.1f}%",
            "score_text": f"{score:+.1f}",
        }
