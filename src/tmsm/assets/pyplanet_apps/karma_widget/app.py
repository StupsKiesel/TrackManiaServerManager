"""Karma widget.

Reads karma stats from contrib karma app when loaded. Falls back to direct
DB aggregation if the app is installed but not active.
"""
from __future__ import annotations

import asyncio
from typing import Any

from pyplanet.apps.tmsm.widgets.widget_base import WidgetAppBase


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
    WIDGET_HIDE_WHILE_DRIVING = False
    WIDGET_ANIM_DIR = "right"
    WIDGET_ANIM_DURATION_MS = 250
    WIDGET_ANIM_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "ee5577ff"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._queued_refresh: asyncio.Task | None = None

    async def on_start(self) -> None:
        await super().on_start()
        try:
            # Karma changes are driven by chat-vote messages in contrib karma.
            self.context.signals.listen("maniaplanet:player_chat", self._on_refresh_signal)
            self.context.signals.listen("maniaplanet:map_begin", self._on_refresh_signal)
            self.context.signals.listen("maniaplanet:map_start", self._on_refresh_signal)
            self.context.signals.listen("maniaplanet:player_connect", self._on_refresh_signal)
        except Exception:
            pass

    async def on_stop(self) -> None:
        if self._queued_refresh is not None:
            self._queued_refresh.cancel()
            self._queued_refresh = None
        await super().on_stop()

    def _queue_refresh(self) -> None:
        if self.view is None:
            return
        if self._queued_refresh is not None and not self._queued_refresh.done():
            return

        async def _flush() -> None:
            try:
                # Coalesce bursts of chat events while keeping the HUD reactive.
                await asyncio.sleep(0.15)
                if self.view is not None:
                    await self.view.refresh()
            except Exception:
                pass
            finally:
                self._queued_refresh = None

        self._queued_refresh = asyncio.create_task(_flush())

    async def _on_refresh_signal(self, **kwargs) -> None:
        self._queue_refresh()

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

    def _current_player_id(self, login: str) -> int | None:
        for p in list(getattr(self.instance.player_manager, "online", []) or []):
            if str(getattr(p, "login", "") or "") == login:
                try:
                    return int(p.get_id())
                except Exception:
                    return None
        return None

    async def _from_contrib(self, login: str) -> dict[str, Any] | None:
        karma_app = getattr(self.instance.apps, "apps", {}).get("karma")
        if karma_app is None:
            return None

        score = float(getattr(karma_app, "current_karma", 0.0) or 0.0)
        pct = float(getattr(karma_app, "current_karma_percentage", 0.0) or 0.0) * 100.0
        pos = float(getattr(karma_app, "current_karma_positive", 0.0) or 0.0)
        neg = float(getattr(karma_app, "current_karma_negative", 0.0) or 0.0)
        votes = list(getattr(karma_app, "current_votes", []) or [])

        my_vote = None
        my_id = self._current_player_id(login)
        if my_id is not None:
            for v in votes:
                try:
                    if int(getattr(v, "player_id", 0) or 0) != my_id:
                        continue
                    exp = getattr(v, "expanded_score", None)
                    my_vote = exp if exp is not None else getattr(v, "score", None)
                    break
                except Exception:
                    continue

        return {
            "score": score,
            "percent": pct,
            "positive": pos,
            "negative": neg,
            "count": len(votes),
            "my_vote": self._vote_text(my_vote),
            "source": "LIVE",
        }

    async def _from_db(self) -> dict[str, Any] | None:
        try:
            from pyplanet.apps.contrib.karma.models import Karma
        except Exception:
            return None

        current_map = getattr(self.instance.map_manager, "current_map", None)
        if current_map is None:
            return None
        # PyPlanet builds differ: get_id() may return an int or a model object.
        map_ref = None
        try:
            map_ref = current_map.get_id()
        except Exception:
            map_ref = getattr(current_map, "id", None)
        if hasattr(map_ref, "id"):
            map_ref = getattr(map_ref, "id", None)
        try:
            map_id = int(map_ref)
        except (TypeError, ValueError):
            return None

        try:
            rows = await Karma.objects.execute(
                Karma.select().where(Karma.map_id == map_id)
            )
        except Exception:
            return None

        votes = list(rows)
        if not votes:
            return {
                "score": 0.0,
                "percent": 0.0,
                "positive": 0.0,
                "negative": 0.0,
                "count": 0,
                "my_vote": "--",
                "source": "DB",
            }

        total_score = 0.0
        total_abs = 0.0
        pos = 0.0
        neg = 0.0
        for v in votes:
            raw = getattr(v, "expanded_score", None)
            score = float(raw if raw is not None else (getattr(v, "score", 0) or 0))
            total_score += score
            total_abs += abs(score)
            if score > 0:
                pos += score
            elif score < 0:
                neg += abs(score)

        pct = (total_score / total_abs) * 100.0 if total_abs > 0 else 0.0
        return {
            "score": total_score,
            "percent": pct,
            "positive": pos,
            "negative": neg,
            "count": len(votes),
            "my_vote": "--",
            "source": "DB",
        }

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        data = await self._from_contrib(login)
        if data is None:
            data = await self._from_db()
        if data is None:
            data = {
                "score": 0.0,
                "percent": 0.0,
                "positive": 0.0,
                "negative": 0.0,
                "count": 0,
                "my_vote": "--",
                "source": "N/A",
            }

        p = float(data.get("percent", 0.0) or 0.0)
        bar_pct = max(0.0, min(100.0, (p + 100.0) / 2.0))
        score = float(data.get("score", 0.0) or 0.0)
        score_color = "3f8" if score >= 0 else "f66"

        data.update({
            "bar_pct": bar_pct,
            "score_color": score_color,
            "percent_text": f"{p:+.1f}%",
            "score_text": f"{score:+.1f}",
        })
        return data
