"""tmsm voting engine app.

This addon provides a reusable vote orchestration backend for other addons.
It intentionally does not render UI; UI-focused apps can consume it later.
"""

from __future__ import annotations

import logging
from typing import Any

from pyplanet.apps.config import AppConfig
from pyplanet.core.events import Signal

from .engine import VotingService

logger = logging.getLogger(__name__)


class VotingEngineApp(AppConfig):
    name = "pyplanet.apps.tmsm.voting_engine"
    label = "tmsm_voting_engine"
    app_dependencies = ["core.maniaplanet"]
    game_dependencies = ["trackmania", "trackmania_next", "shootmania"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.engine = VotingService(self)
        self.engine.on_progress = self._on_engine_progress
        self.engine.on_ended = self._on_engine_ended

    async def on_init(self) -> None:
        for code in (
            "request_start",
            "request_cast",
            "request_cancel",
            "request_snapshot",
            "started",
            "progress",
            "ended",
            "rejected",
        ):
            try:
                self.context.signals.register_signal(
                    Signal(code=code, namespace="tmsm_voting_engine")
                )
            except Exception:
                logger.exception("voting_engine: signal register failed for %s", code)

    async def on_start(self) -> None:
        self.context.signals.listen("tmsm_voting_engine:request_start", self._on_request_start)
        self.context.signals.listen("tmsm_voting_engine:request_cast", self._on_request_cast)
        self.context.signals.listen("tmsm_voting_engine:request_cancel", self._on_request_cancel)
        self.context.signals.listen("tmsm_voting_engine:request_snapshot", self._on_request_snapshot)
        self.context.signals.listen("maniaplanet:player_disconnect", self._on_player_disconnect)

    async def on_stop(self) -> None:
        await self.engine.shutdown()

    @staticmethod
    def _unwrap(kwargs: dict[str, Any]) -> dict[str, Any]:
        src = kwargs.get("source")
        if isinstance(src, dict):
            return src
        out = dict(kwargs)
        out.pop("signal", None)
        out.pop("source", None)
        return out

    async def _emit(self, code: str, payload: dict[str, Any]) -> None:
        try:
            sig = self.context.signals.get_signal(f"tmsm_voting_engine:{code}")
        except KeyError:
            return
        try:
            await sig.send_robust(payload, raw=True)
        except Exception:
            logger.exception("voting_engine: emit failed for %s", code)

    def _online_player_logins(self, include_spectators: bool = False) -> set[str]:
        out: set[str] = set()
        try:
            online = list(self.instance.player_manager.online)
        except Exception:
            return out
        for p in online:
            login = getattr(p, "login", None)
            if not login:
                continue
            if not include_spectators and getattr(getattr(p, "flow", None), "is_spectator", False):
                continue
            out.add(login)
        return out

    async def _on_player_disconnect(self, player, **kwargs) -> None:
        login = getattr(player, "login", None)
        if not login or not self.engine.is_active:
            return
        if not self.engine.remove_login(login):
            return
        await self._emit("progress", {"vote": self.engine.snapshot()})
        if self.engine.should_finish_all_voted():
            await self.engine.finish(reason="all_voted")

    async def _on_engine_progress(self, snapshot: dict[str, Any]) -> None:
        await self._emit("progress", {"vote": snapshot})

    async def _on_engine_ended(self, result: dict[str, Any]) -> None:
        await self._emit("ended", {"result": result})

    async def _on_request_start(self, **kwargs) -> None:
        payload = self._unwrap(kwargs)

        if self.engine.is_active:
            await self._emit("rejected", {
                "reason": "already_active",
                "request": payload,
                "vote": self.engine.snapshot(),
            })
            return

        key = str(payload.get("key") or "vote")
        title = str(payload.get("title") or "Vote")
        options_raw = payload.get("options") or []
        if not isinstance(options_raw, list) or not options_raw:
            await self._emit("rejected", {
                "reason": "invalid_options",
                "request": payload,
            })
            return

        options: list[dict[str, Any]] = []
        for option in options_raw:
            if not isinstance(option, dict) or "value" not in option:
                continue
            options.append({
                "value": option.get("value"),
                "label": str(option.get("label") or option.get("value")),
            })
        if not options:
            await self._emit("rejected", {
                "reason": "invalid_options",
                "request": payload,
            })
            return

        eligible_raw = payload.get("eligible")
        include_spectators = bool(payload.get("include_spectators", False))
        if isinstance(eligible_raw, list):
            eligible = {str(x) for x in eligible_raw if x}
        else:
            eligible = self._online_player_logins(include_spectators=include_spectators)

        if not eligible:
            await self._emit("rejected", {
                "reason": "no_eligible_players",
                "request": payload,
            })
            return

        duration_s = int(payload.get("duration_s") or 25)
        mode = str(payload.get("mode") or "plurality")
        initiator = payload.get("initiator")
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        allow_revote = bool(payload.get("allow_revote", True))
        pass_ratio = float(payload.get("pass_ratio", 0.6))

        await self.engine.start(
            key=key,
            title=title,
            options=options,
            duration_s=max(1, duration_s),
            eligible=eligible,
            mode=mode,
            initiator=str(initiator) if initiator else None,
            metadata=metadata,
            allow_revote=allow_revote,
            pass_ratio=pass_ratio,
        )

        await self._emit("started", {"vote": self.engine.snapshot()})

    async def _on_request_cast(self, **kwargs) -> None:
        payload = self._unwrap(kwargs)
        login = payload.get("login")
        value = payload.get("value")
        if not login or value is None:
            await self._emit("rejected", {
                "reason": "invalid_cast_payload",
                "request": payload,
                "vote": self.engine.snapshot(),
            })
            return

        ok, reason = await self.engine.cast(str(login), str(value))
        if not ok:
            await self._emit("rejected", {
                "reason": reason,
                "request": payload,
                "vote": self.engine.snapshot(),
            })
            return

        await self._emit("progress", {
            "vote": self.engine.snapshot(),
            "login": str(login),
            "value": value,
        })

    async def _on_request_cancel(self, **kwargs) -> None:
        payload = self._unwrap(kwargs)
        reason = str(payload.get("reason") or "cancelled")
        result = await self.engine.cancel(reason=reason)
        if result is None:
            await self._emit("rejected", {
                "reason": "no_active_vote",
                "request": payload,
            })
            return
        await self._emit("ended", {"result": result})

    async def _on_request_snapshot(self, **kwargs) -> None:
        payload = self._unwrap(kwargs)
        await self._emit("progress", {
            "vote": self.engine.snapshot(),
            "request_id": payload.get("request_id"),
        })
