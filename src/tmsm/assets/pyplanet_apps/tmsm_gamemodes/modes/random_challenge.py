"""Random Challenge mode.

Straightforward multiplayer adaptation of the single-player random challenge
idea: every podium, fetch a random valid TMX map and queue it as next.
"""
from __future__ import annotations

import logging
from typing import Any

from ..base import ConfigField, GameMode, GameModeContext, register
from ..picker import downloadable, min_awards, reject_difficulty, reject_tags

logger = logging.getLogger(__name__)


@register
class RandomChallengeMode(GameMode):
    key = "random_challenge"
    name = "Random Challenge"
    description = "Every podium rolls a random TMX map and queues it as next."
    icon = "random"
    color = "fa0"
    category = "rotation"

    def __init__(self, ctx: GameModeContext) -> None:
        super().__init__(ctx)
        self._config: dict[str, Any] = self.default_config()
        self._state: dict[str, Any] = {
            "last_track_id": None,
            "picked_total": 0,
            "played_track_ids": [],
        }
        self._busy: bool = False

    def default_config(self) -> dict[str, Any]:
        return {
            "autoplay_on_start": True,
            "max_pick_attempts": 10,
            "block_lunatic": True,
            "block_kacky": True,
            "avoid_direct_repeat": True,
            "skip_duplicate_maps": True,
            "history_size": 200,
            "filter_low_effort": True,
            "filter_untagged": True,
        }

    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField.make(
                "autoplay_on_start",
                "Auto-play picked map on start",
                "bool",
                default=True,
                help="When activating the mode, switch immediately to the picked map.",
            ),
            ConfigField.make(
                "max_pick_attempts",
                "Max TMX pick attempts",
                "int",
                default=10,
                min=1,
                max=40,
                help="How many random rolls to try each cycle before skipping.",
            ),
            ConfigField.make(
                "block_lunatic",
                "Block Lunatic / Impossible",
                "bool",
                default=True,
            ),
            ConfigField.make(
                "block_kacky",
                "Block Kacky tag",
                "bool",
                default=True,
            ),
            ConfigField.make(
                "avoid_direct_repeat",
                "Avoid direct repeat",
                "bool",
                default=True,
                help="Avoid immediately re-picking the same TMX track id.",
            ),
            ConfigField.make(
                "skip_duplicate_maps",
                "Skip duplicates in run",
                "bool",
                default=True,
                help="Avoid maps already picked in this run history.",
            ),
            ConfigField.make(
                "history_size",
                "History size",
                "int",
                default=200,
                min=10,
                max=2000,
                help="How many previously picked TMX ids to remember.",
            ),
            ConfigField.make(
                "filter_low_effort",
                "Filter low-effort maps",
                "bool",
                default=True,
                help="Require at least 1 award.",
            ),
            ConfigField.make(
                "filter_untagged",
                "Filter untagged maps",
                "bool",
                default=True,
                help="Reject maps with no tags.",
            ),
        ]

    async def on_enable(self, config: dict[str, Any]) -> None:
        self._config = {**self.default_config(), **(config or {})}
        persisted = self.ctx.load_state()
        if persisted:
            self._state.update(persisted)
        self._save()
        self._update_status()
        picked = await self._pick_and_jukebox(triggered_by="enable")
        if picked and bool(self._config.get("autoplay_on_start", True)):
            try:
                await self.ctx.instance.gbx("NextMap")
                self.ctx.chat("$fa0>> $fffRandom Challenge:$z starting on picked map.")
            except Exception:
                logger.exception("random_challenge: NextMap on start failed")

    async def on_podium_start(self) -> None:
        await self._pick_and_jukebox(triggered_by="podium")

    def status_lines(self) -> list[str]:
        last = self._state.get("last_track_id")
        history = self._history_ids()
        return [
            "Mode: random TMX picks every podium",
            f"Maps queued: {int(self._state.get('picked_total') or 0)}",
            f"Last TMX id: {int(last) if last else '-'}",
            f"History: {len(history)} / {max(10, int(self._config.get('history_size') or 200))}",
        ]

    def _history_ids(self) -> list[int]:
        out: list[int] = []
        for v in (self._state.get("played_track_ids") or []):
            try:
                tid = int(v)
            except (TypeError, ValueError):
                continue
            if tid > 0:
                out.append(tid)
        return out

    def _save(self) -> None:
        self.ctx.save_state(self._state)

    def _update_status(self) -> None:
        self.ctx.set_status(self.status_lines())

    async def _pick_and_jukebox(self, triggered_by: str) -> bool:
        if self._busy:
            return False
        self._busy = True
        try:
            validators = [downloadable()]
            if self._config.get("block_lunatic"):
                validators.append(reject_difficulty("Lunatic", "Impossible"))
            if self._config.get("block_kacky"):
                validators.append(reject_tags("Kacky"))
            if self._config.get("filter_low_effort"):
                validators.append(min_awards(1))
            if self._config.get("filter_untagged"):
                validators.append(lambda row: bool(row.get("tags") or []))

            excluded = []
            if self._config.get("avoid_direct_repeat"):
                last_tid = int(self._state.get("last_track_id") or 0)
                if last_tid > 0:
                    excluded.append(last_tid)
            if self._config.get("skip_duplicate_maps"):
                for tid in (self._state.get("played_track_ids") or []):
                    try:
                        t = int(tid)
                    except (TypeError, ValueError):
                        continue
                    if t > 0:
                        excluded.append(t)

            row = await self.ctx.picker.pick_random(
                filters={},
                validators=validators,
                excluded_tmx_ids=excluded,
                max_attempts=max(1, int(self._config.get("max_pick_attempts") or 10)),
            )
            if row is None:
                logger.warning("random_challenge: no valid map found (trigger=%s)",
                               triggered_by)
                await self.ctx.notify(
                    "Random Challenge: no valid map found, retrying next podium",
                    severity="warning",
                )
                return False

            installed = await self.ctx.picker.install(row, juke_next=True)
            if installed is None:
                await self.ctx.notify(
                    "Random Challenge: failed to add picked map to server",
                    severity="error",
                )
                return False

            self._state["last_track_id"] = int(row.get("track_id") or 0)
            self._state["picked_total"] = int(self._state.get("picked_total") or 0) + 1
            history = self._history_ids()
            history.append(self._state["last_track_id"])
            cap = max(10, int(self._config.get("history_size") or 200))
            if len(history) > cap:
                history = history[-cap:]
            self._state["played_track_ids"] = history
            self._save()
            self._update_status()

            self.ctx.chat(
                f"$fa0>> $fffRandom Challenge:$z next map "
                f"$fa0{row.get('name')}$z by {row.get('author')}"
            )
            return True
        finally:
            self._busy = False