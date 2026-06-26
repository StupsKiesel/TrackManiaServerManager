"""stats_tracker — the single writer behind the podium statistic widgets.

Listens to connect/disconnect/finish/scores/map callbacks and maintains the
``tmsm_stats_player`` and ``tmsm_stats_map`` tables. The podium statistic
widgets call the ``query_*`` helpers here read-only; this app never renders
anything itself.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
from typing import Any

from pyplanet.apps.config import AppConfig
from pyplanet.contrib.setting import Setting

from .storage import StatsStorage

logger = logging.getLogger(__name__)

# How often the playtime accumulator flushes elapsed session time to the DB.
FLUSH_INTERVAL_S = 60


class StatsTrackerApp(AppConfig):
    name = "pyplanet.apps.tmsm.stats_tracker"
    label = "stats_tracker"
    app_dependencies = ["core.maniaplanet"]
    game_dependencies = ["trackmania", "trackmania_next", "shootmania"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.storage = StatsStorage(self.instance)
        # login -> {"last": datetime}  (active session accumulator anchor)
        self._sessions: dict[str, dict[str, Any]] = {}
        # Latest ranked scores snapshot, used to award wins/points at podium.
        self._last_scores: list[dict[str, Any]] = []
        self._flush_task: asyncio.Task | None = None

        self.setting_visit_dedupe = Setting(
            "visit_dedupe_seconds",
            "Visit dedupe window (seconds)",
            Setting.CAT_BEHAVIOUR,
            type=int,
            description=(
                "A reconnect within this many seconds of the player's last "
                "activity is not counted as a new visit."
            ),
            default=300,
        )

    # ---- lifecycle -----------------------------------------------------

    async def on_start(self) -> None:
        try:
            await self.storage.ensure_schema()
        except Exception:
            logger.exception("stats_tracker: ensure_schema failed")

        await self.context.setting.register(self.setting_visit_dedupe)

        signals = self.context.signals
        signals.listen("maniaplanet:player_connect", self._on_connect)
        signals.listen("maniaplanet:player_disconnect", self._on_disconnect)
        signals.listen("trackmania:finish", self._on_finish)
        signals.listen("trackmania:scores", self._on_scores)
        signals.listen("maniaplanet:podium_start", self._on_podium)
        signals.listen("maniaplanet:map_begin", self._on_map_begin)
        signals.listen("maniaplanet:map_start", self._on_map_begin)

        # Seed sessions for already-connected players.
        now = self._now()
        try:
            for player in list(self.instance.player_manager.online):
                login = str(getattr(player, "login", "") or "")
                if login and not login.startswith("*"):
                    self._sessions[login] = {"last": now}
        except Exception:
            pass

        self._flush_task = asyncio.create_task(self._flush_loop())

    async def on_stop(self) -> None:
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None
        try:
            await self._flush_all()
        except Exception:
            logger.exception("stats_tracker: final flush failed")
        await super().on_stop()

    # ---- helpers -------------------------------------------------------

    @staticmethod
    def _now() -> _dt.datetime:
        return _dt.datetime.utcnow()

    @staticmethod
    def _login_of(player) -> str:
        return str(getattr(player, "login", "") or "")

    @staticmethod
    def _nick_of(player, login: str) -> str:
        return str(getattr(player, "nickname", login) or login)

    def _is_spectator(self, login: str) -> bool:
        try:
            for player in list(self.instance.player_manager.online):
                if self._login_of(player) == login:
                    flow = getattr(player, "flow", None)
                    return bool(getattr(flow, "is_spectator", False))
        except Exception:
            pass
        return False

    async def _dedupe_window(self) -> int:
        try:
            return int(await self.setting_visit_dedupe.get_value())
        except Exception:
            return 300

    # ---- connect / disconnect / playtime -------------------------------

    async def _on_connect(self, player=None, **kwargs) -> None:
        login = self._login_of(player)
        if not login or login.startswith("*"):
            return
        nickname = self._nick_of(player, login)
        now = self._now()
        try:
            window = await self._dedupe_window()
            last_seen = await self.storage.get_last_seen(login)
            counted = last_seen is None
            if last_seen is not None:
                try:
                    counted = (now - last_seen).total_seconds() > window
                except Exception:
                    counted = True
            if counted:
                await self.storage.bump_visit(login, nickname, now)
            else:
                await self.storage.touch_seen(login, nickname, now)
        except Exception:
            logger.exception("stats_tracker: connect handling failed login=%s", login)
        self._sessions[login] = {"last": now}

    async def _on_disconnect(self, player=None, **kwargs) -> None:
        login = self._login_of(player)
        if not login:
            return
        try:
            await self._flush_login(login)
        except Exception:
            logger.exception("stats_tracker: disconnect flush failed login=%s", login)
        self._sessions.pop(login, None)

    async def _flush_login(self, login: str) -> None:
        session = self._sessions.get(login)
        if not session:
            return
        now = self._now()
        last = session.get("last") or now
        elapsed = int(max(0, (now - last).total_seconds()))
        session["last"] = now
        if elapsed <= 0:
            return
        if self._is_spectator(login):
            await self.storage.add_playtime(login, 0, elapsed, now)
        else:
            await self.storage.add_playtime(login, elapsed, 0, now)

    async def _flush_all(self) -> None:
        for login in list(self._sessions.keys()):
            await self._flush_login(login)

    async def _flush_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(FLUSH_INTERVAL_S)
                try:
                    await self._flush_all()
                except Exception:
                    logger.exception("stats_tracker: periodic flush failed")
        except asyncio.CancelledError:
            pass

    # ---- finishes ------------------------------------------------------

    async def _on_finish(self, player=None, is_end_race=None, **kwargs) -> None:
        # Only reject explicit non-end-race finishes (some modes omit the flag).
        if is_end_race is False:
            return
        login = self._login_of(player)
        if not login or login.startswith("*"):
            return
        nickname = self._nick_of(player, login)
        try:
            await self.storage.bump_finish(login, nickname, self._now())
        except Exception:
            logger.exception("stats_tracker: finish handling failed login=%s", login)

    # ---- scores snapshot + podium awards -------------------------------

    async def _on_scores(self, section=None, players=None, **kwargs) -> None:
        if section == "PreEndRound":
            return
        snapshot: list[dict[str, Any]] = []
        for item in list(players or []):
            if not isinstance(item, dict):
                continue
            player = item.get("player")
            login = self._login_of(player)
            if not login or login.startswith("*"):
                continue
            try:
                best = int(item.get("best_race_time") or 0)
            except (TypeError, ValueError):
                best = 0
            snapshot.append({
                "login": login,
                "nickname": self._nick_of(player, login),
                "time": best,
            })
        if snapshot:
            self._last_scores = snapshot

    async def _on_podium(self, **kwargs) -> None:
        # Persist in-progress session time first so the Most Playtime widget
        # has fresh data at the podium (the 60s loop alone can lag a podium
        # that happens early in a session).
        try:
            await self._flush_all()
        except Exception:
            logger.exception("stats_tracker: podium flush failed")
        scores = self._last_scores
        self._last_scores = []
        if not scores:
            return
        finishers = sorted([s for s in scores if s["time"] > 0], key=lambda s: s["time"])
        non_finishers = [s for s in scores if s["time"] <= 0]
        n_fin = len(finishers)
        n_non = len(non_finishers)
        now = self._now()
        # Only count a win when at least two players were present (no solo wins).
        present = n_fin + n_non
        winner_login = finishers[0]["login"] if (finishers and present >= 2) else None
        try:
            for idx, s in enumerate(finishers):
                opponents = (n_fin - 1 - idx) + n_non
                await self.storage.award_result(
                    s["login"], s["nickname"], opponents,
                    s["login"] == winner_login, now,
                )
            for s in non_finishers:
                await self.storage.award_result(s["login"], s["nickname"], 0, False, now)
        except Exception:
            logger.exception("stats_tracker: podium award failed")

    # ---- map plays -----------------------------------------------------

    async def _on_map_begin(self, **kwargs) -> None:
        current = getattr(self.instance.map_manager, "current_map", None)
        if current is None:
            return
        uid = str(getattr(current, "uid", "") or "")
        if not uid:
            return
        name = str(getattr(current, "name", "") or "")
        author = str(getattr(current, "author_login", "") or getattr(current, "author", "") or "")
        try:
            await self.storage.bump_map_play(uid, name, author, self._now())
        except Exception:
            logger.exception("stats_tracker: map_begin handling failed uid=%s", uid)

    # ---- query API (read-only, used by the widgets) --------------------

    async def query_top_visitors(self, limit: int) -> list[dict[str, Any]]:
        return await self.storage.top_visitors(limit)

    async def query_most_playtime(self, limit: int) -> list[dict[str, Any]]:
        return await self.storage.most_playtime(limit)

    async def query_most_finishes(self, limit: int) -> list[dict[str, Any]]:
        return await self.storage.most_finishes(limit)

    async def query_top_winners(self, limit: int) -> list[dict[str, Any]]:
        return await self.storage.top_winners(limit)

    async def query_top_ranks(self, limit: int) -> list[dict[str, Any]]:
        return await self.storage.top_ranks(limit)

    async def query_most_played_maps(self, limit: int) -> list[dict[str, Any]]:
        return await self.storage.most_played_maps(limit)

    async def query_least_played_maps(self, limit: int) -> list[dict[str, Any]]:
        """Least-recently-played maps restricted to the current playlist."""
        try:
            playlist = list(self.instance.map_manager.maps)
        except Exception:
            playlist = []
        if not playlist:
            return []
        uid_meta: dict[str, dict[str, str]] = {}
        order: list[str] = []
        for m in playlist:
            uid = str(getattr(m, "uid", "") or "")
            if not uid or uid in uid_meta:
                continue
            order.append(uid)
            uid_meta[uid] = {
                "name": str(getattr(m, "name", "") or "?"),
                "author": str(getattr(m, "author_login", "") or getattr(m, "author", "") or ""),
            }
        last_played = await self.storage.map_last_played(order)
        rows: list[dict[str, Any]] = []
        for uid in order:
            rows.append({
                "uid": uid,
                "name": uid_meta[uid]["name"],
                "author": uid_meta[uid]["author"],
                "last_played_at": last_played.get(uid),
            })
        # Never-played first (None), then oldest last_played first.
        rows.sort(key=lambda r: (r["last_played_at"] is not None, r["last_played_at"] or _dt.datetime.min))
        return rows[: int(limit)]
