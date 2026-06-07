"""Generic player vote engine.

One vote at a time. Each vote has:

* ``key``        - identifier (e.g. ``"evolution:add_env"``)
* ``title``      - line shown to players
* ``options``    - list of ``{"value": ..., "label": ...}`` choices
* ``duration_s`` - countdown
* ``mode``       - currently only ``"plurality"`` (most votes wins)
* ``on_finish``  - async callback ``coro(result_dict)`` called when the
                   vote times out, all players have voted, or it is
                   manually cancelled.

The orchestrator renders the player-facing vote panel as a BaseView
(see ``views.VotePanelView``) and forwards ``vote__pick__<value>``
actions to ``cast()``.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Awaitable, Callable, Dict

logger = logging.getLogger(__name__)


FinishCallback = Callable[[Dict[str, Any]], Awaitable[None]]


class VoteEngine:
    def __init__(self, app) -> None:
        self.app = app
        self._vote: dict[str, Any] | None = None
        self._timer_task: asyncio.Task | None = None

    # ---- introspection -------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self._vote is not None

    def snapshot(self) -> dict[str, Any] | None:
        """Render-ready snapshot for the player vote panel."""
        if not self._vote:
            return None
        v = self._vote
        ballots = v["ballots"]
        tally: dict[Any, int] = {opt["value"]: 0 for opt in v["options"]}
        for choice in ballots.values():
            if choice in tally:
                tally[choice] += 1
        remaining = max(0, int(v["ends_at"] - time.time()))
        return {
            "key":       v["key"],
            "title":     v["title"],
            "options":   v["options"],
            "tally":     tally,
            "ballots":   dict(ballots),
            "total":     sum(tally.values()),
            "remaining": remaining,
            "duration":  v["duration_s"],
        }

    def has_voted(self, login: str) -> bool:
        return self._vote is not None and login in self._vote["ballots"]

    # ---- lifecycle -----------------------------------------------------

    async def start(self,
                    *,
                    key: str,
                    title: str,
                    options: list[dict[str, Any]],
                    duration_s: int,
                    mode: str = "plurality",
                    pass_value: Any | None = None,
                    pass_ratio: float | None = None,
                    eligible_logins: list[str] | None = None,
                    on_finish: FinishCallback | None = None) -> None:
        if self._vote is not None:
            logger.info("votes: a vote is already active (%s), cancelling first",
                        self._vote["key"])
            await self.cancel(emit=False)
        self._vote = {
            "key":        key,
            "title":      title,
            "options":    list(options),
            "duration_s": int(duration_s),
            "started_at": time.time(),
            "ends_at":    time.time() + int(duration_s),
            "mode":       mode,
            "pass_value": pass_value,
            "pass_ratio": pass_ratio,
            "eligible_logins": [str(x) for x in (eligible_logins or []) if str(x)],
            "ballots":    {},          # login -> chosen value
            "on_finish":  on_finish,
        }
        self._timer_task = asyncio.ensure_future(self._tick())
        await self.app._on_vote_started()

    async def cast(self, login: str, raw_value: str) -> None:
        if not self._vote:
            return
        # Match the raw string back to an option value (preserve original type).
        match = None
        for opt in self._vote["options"]:
            if str(opt["value"]) == str(raw_value):
                match = opt["value"]
                break
        if match is None:
            return
        self._vote["ballots"][login] = match

        # Threshold mode: pass immediately once the target option reaches
        # the required share over the eligible participant denominator.
        ratio = self._vote.get("pass_ratio")
        pass_value = self._vote.get("pass_value")
        if ratio is not None and pass_value is not None:
            try:
                needed_ratio = max(0.0, float(ratio))
            except (TypeError, ValueError):
                needed_ratio = 0.0
            eligible = [str(x) for x in (self._vote.get("eligible_logins") or []) if str(x)]
            eligible_total = len(set(eligible))
            if eligible_total <= 0:
                try:
                    online = list(self.app.instance.player_manager.online)
                except Exception:
                    online = []
                eligible_total = len([p for p in online if getattr(p, "login", None)])
            needed_yes = max(1, int(math.ceil(eligible_total * needed_ratio))) if eligible_total > 0 else 1
            yes_now = sum(1 for v in self._vote["ballots"].values() if v == pass_value)
            if yes_now >= needed_yes:
                await self._finish(force_winner=pass_value)
                return

        # Auto-finish when everyone currently online has voted.
        try:
            online = list(self.app.instance.player_manager.online)
        except Exception:
            online = []
        if online and all(p.login in self._vote["ballots"] for p in online):
            await self._finish()
        else:
            await self.app._on_vote_progress()

    async def cancel(self, emit: bool = True) -> None:
        if not self._vote:
            return
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None
        self._vote = None
        if emit:
            await self.app._on_vote_ended(result=None)

    async def _tick(self) -> None:
        """Drive a 1-Hz UI refresh and finish the vote when time runs out."""
        try:
            while self._vote is not None:
                remaining = self._vote["ends_at"] - time.time()
                if remaining <= 0:
                    await self._finish()
                    return
                # Refresh once per second so the countdown ticks.
                await self.app._on_vote_progress()
                await asyncio.sleep(min(1.0, remaining))
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("votes: tick loop crashed")

    async def _finish(self, force_winner: Any | None = None) -> None:
        if not self._vote:
            return
        v = self._vote
        self._vote = None
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None

        tally: dict[Any, int] = {opt["value"]: 0 for opt in v["options"]}
        for choice in v["ballots"].values():
            if choice in tally:
                tally[choice] += 1
        # Plurality with deterministic tie-break (first option in declaration
        # order wins ties so behaviour is repeatable across reloads).
        winner = force_winner
        if winner is None:
            best = -1
            for opt in v["options"]:
                if tally.get(opt["value"], 0) > best:
                    best = tally[opt["value"]]
                    winner = opt["value"]
        result = {
            "key":     v["key"],
            "winner":  winner,
            "tally":   tally,
            "total":   sum(tally.values()),
            "options": v["options"],
            "ballots": v["ballots"],
        }
        cb = v.get("on_finish")
        await self.app._on_vote_ended(result=result)
        if cb is not None:
            try:
                await cb(result)
            except Exception:
                logger.exception("votes: on_finish callback raised")
