"""Reusable voting service for tmsm addons.

This module intentionally has no UI responsibilities. It manages one active
vote at a time, tracks ballots, and emits lifecycle callbacks.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict

FinishCallback = Callable[[Dict[str, Any]], Awaitable[None]]


@dataclass
class ActiveVote:
    key: str
    title: str
    options: list[dict[str, Any]]
    duration_s: int
    mode: str
    started_at: float
    ends_at: float
    eligible: set[str]
    initiator: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    ballots: dict[str, Any] = field(default_factory=dict)
    allow_revote: bool = True
    pass_ratio: float = 0.6
    on_finish: FinishCallback | None = None


class VotingService:
    """Engine used by the VotingEngineApp.

    API is intentionally simple so future apps can call it directly or via
    signals handled by the app wrapper.
    """

    def __init__(self, app) -> None:
        self.app = app
        self._vote: ActiveVote | None = None
        self._task: asyncio.Task | None = None
        self.on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self.on_ended: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    @property
    def is_active(self) -> bool:
        return self._vote is not None

    def snapshot(self) -> dict[str, Any] | None:
        vote = self._vote
        if vote is None:
            return None

        tally: dict[Any, int] = {opt["value"]: 0 for opt in vote.options}
        for choice in vote.ballots.values():
            if choice in tally:
                tally[choice] += 1

        return {
            "key": vote.key,
            "title": vote.title,
            "mode": vote.mode,
            "options": [dict(o) for o in vote.options],
            "duration": vote.duration_s,
            "remaining": max(0, int(vote.ends_at - time.time())),
            "eligible_count": len(vote.eligible),
            "eligible": sorted(vote.eligible),
            "ballots": dict(vote.ballots),
            "tally": tally,
            "initiator": vote.initiator,
            "metadata": dict(vote.metadata),
            "pass_ratio": vote.pass_ratio,
        }

    async def start(
        self,
        *,
        key: str,
        title: str,
        options: list[dict[str, Any]],
        duration_s: int,
        eligible: set[str],
        mode: str = "plurality",
        initiator: str | None = None,
        metadata: dict[str, Any] | None = None,
        allow_revote: bool = True,
        pass_ratio: float = 0.6,
        on_finish: FinishCallback | None = None,
    ) -> None:
        if self._vote is not None:
            await self.cancel(reason="replaced")

        now = time.time()
        vote = ActiveVote(
            key=key,
            title=title,
            options=[dict(o) for o in options],
            duration_s=max(1, int(duration_s)),
            mode=(mode or "plurality").strip().lower(),
            started_at=now,
            ends_at=now + max(1, int(duration_s)),
            eligible=set(eligible),
            initiator=initiator,
            metadata=dict(metadata or {}),
            allow_revote=bool(allow_revote),
            pass_ratio=max(0.0, min(1.0, float(pass_ratio))),
            on_finish=on_finish,
        )
        self._vote = vote
        self._task = asyncio.ensure_future(self._tick())

    async def cast(self, login: str, raw_value: str) -> tuple[bool, str | None]:
        vote = self._vote
        if vote is None:
            return False, "no_active_vote"

        if login not in vote.eligible:
            return False, "not_eligible"

        matched = None
        for option in vote.options:
            if str(option.get("value")) == str(raw_value):
                matched = option.get("value")
                break
        if matched is None:
            return False, "invalid_option"

        if not vote.allow_revote and login in vote.ballots:
            return False, "already_voted"

        vote.ballots[login] = matched

        if vote.eligible and all(lg in vote.ballots for lg in vote.eligible):
            await self.finish(reason="all_voted")
        return True, None

    def remove_login(self, login: str) -> bool:
        """Remove a player from eligibility/ballots during disconnect."""
        vote = self._vote
        if vote is None:
            return False
        changed = False
        if login in vote.eligible:
            vote.eligible.remove(login)
            changed = True
        if login in vote.ballots:
            vote.ballots.pop(login, None)
            changed = True
        return changed

    def should_finish_all_voted(self) -> bool:
        vote = self._vote
        if vote is None:
            return False
        return bool(vote.eligible) and all(lg in vote.ballots for lg in vote.eligible)

    async def cancel(self, *, reason: str = "cancelled") -> dict[str, Any] | None:
        return await self.finish(reason=reason, cancelled=True)

    async def finish(
        self,
        *,
        reason: str = "finished",
        cancelled: bool = False,
    ) -> dict[str, Any] | None:
        vote = self._vote
        if vote is None:
            return None

        self._vote = None
        if self._task is not None:
            self._task.cancel()
            self._task = None

        result = self._build_result(vote, reason=reason, cancelled=cancelled)
        ended_cb = self.on_ended
        if ended_cb is not None:
            try:
                await ended_cb(result)
            except Exception:
                pass
        cb = vote.on_finish
        if cb is not None:
            try:
                await cb(result)
            except Exception:
                # The wrapper app logs on lifecycle emit; keep engine resilient.
                pass
        return result

    async def shutdown(self) -> None:
        if self._vote is not None:
            await self.cancel(reason="shutdown")
        elif self._task is not None:
            self._task.cancel()
            self._task = None

    async def _tick(self) -> None:
        try:
            while self._vote is not None:
                remaining = self._vote.ends_at - time.time()
                if remaining <= 0:
                    await self.finish(reason="timeout")
                    return
                progress_cb = self.on_progress
                if progress_cb is not None:
                    try:
                        snap = self.snapshot()
                        if snap is not None:
                            await progress_cb(snap)
                    except Exception:
                        pass
                await asyncio.sleep(min(1.0, remaining))
        except asyncio.CancelledError:
            return

    def _build_result(
        self,
        vote: ActiveVote,
        *,
        reason: str,
        cancelled: bool,
    ) -> dict[str, Any]:
        tally: dict[Any, int] = {opt["value"]: 0 for opt in vote.options}
        for choice in vote.ballots.values():
            if choice in tally:
                tally[choice] += 1

        winner: Any = None
        passed: bool | None = None

        if not cancelled:
            if vote.mode == "threshold_yes_no":
                yes_key = vote.metadata.get("yes_value", "yes")
                yes_votes = tally.get(yes_key, 0)
                needed = int(math.ceil(len(vote.eligible) * vote.pass_ratio))
                passed = yes_votes >= needed
                winner = yes_key if passed else vote.metadata.get("no_value", "no")
            else:
                # Default plurality with deterministic tie break by option order.
                best = -1
                for option in vote.options:
                    value = option.get("value")
                    count = tally.get(value, 0)
                    if count > best:
                        best = count
                        winner = value
                passed = winner is not None

        return {
            "key": vote.key,
            "title": vote.title,
            "mode": vote.mode,
            "reason": reason,
            "cancelled": cancelled,
            "winner": winner,
            "passed": passed,
            "options": [dict(o) for o in vote.options],
            "ballots": dict(vote.ballots),
            "tally": tally,
            "eligible": sorted(vote.eligible),
            "eligible_count": len(vote.eligible),
            "initiator": vote.initiator,
            "metadata": dict(vote.metadata),
            "duration": vote.duration_s,
        }
