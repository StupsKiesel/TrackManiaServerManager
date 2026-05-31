"""Evolution mode.

A slowly-rotating environment (vista) mix. Active categories are stored as
"slots"; the picker draws random TMX maps whose environment is in the
current slot set. Every ``vote_every_n_maps`` podiums we run a player vote
that picks a *new* environment to add to the mix; when the slot pool
exceeds ``slot_count`` we drop the oldest entry.

Design intent: small steps. Map style changes gradually rather than
swinging every map.
"""
from __future__ import annotations

import logging
import random
from typing import Any

from pyplanet.apps.tmsm.tmx_browser.tmx import _ENVIRONMENTS

from ..base import ConfigField, GameMode, GameModeContext, register
from ..picker import reject_difficulty, reject_tags, downloadable

logger = logging.getLogger(__name__)


# Sentinel used in the on_podium_start guard so we don't double-pick when
# multiple callbacks fire close together (e.g. server restarts mid-podium).
_PICK_BUSY = "_evolution_pick_busy"


@register
class EvolutionMode(GameMode):
    key = "evolution"
    name = "Evolution"
    description = "Slowly rotating environment mix - players vote in a new vista each cycle."
    icon = "leaf"
    color = "0c4"
    category = "rotation"

    # ---- config -------------------------------------------------------

    def default_config(self) -> dict[str, Any]:
        return {
            "slot_count":         3,
            "vote_every_n_maps":  1,
            "vote_duration_s":    25,
            "max_pick_attempts":  8,
            "block_lunatic":      True,
            "block_kacky":        True,
        }

    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField.make("slot_count", "Active vista slots", "int",
                             default=3, min=1, max=6,
                             help="How many vistas are simultaneously eligible."),
            ConfigField.make("vote_every_n_maps", "Vote every N maps", "int",
                             default=1, min=1, max=10,
                             help="A new vista is voted in every N podiums."),
            ConfigField.make("vote_duration_s", "Vote duration (s)", "int",
                             default=25, min=5, max=120),
            ConfigField.make("max_pick_attempts", "Max TMX pick attempts", "int",
                             default=8, min=1, max=30,
                             help="Random map rolls before giving up this cycle."),
            ConfigField.make("block_lunatic", "Block Lunatic / Impossible", "bool",
                             default=True),
            ConfigField.make("block_kacky", "Block Kacky tag", "bool",
                             default=True),
        ]

    # ---- lifecycle ----------------------------------------------------

    def __init__(self, ctx: GameModeContext) -> None:
        super().__init__(ctx)
        self._config: dict[str, Any] = self.default_config()
        # Mode-private state (persisted via ctx.save_state).
        # slots: list[int]  - env ids in insertion order; oldest at [0]
        # maps_since_vote: int
        # last_track_id: int | None
        self._state: dict[str, Any] = {
            "slots": [],
            "maps_since_vote": 0,
            "last_track_id": None,
        }
        self._busy: bool = False

    async def on_enable(self, config: dict[str, Any]) -> None:
        self._config = {**self.default_config(), **(config or {})}
        persisted = self.ctx.load_state()
        if persisted.get("slots"):
            self._state.update(persisted)
        else:
            # Bootstrap with one random environment from the game's enum.
            envs = self._env_options()
            if envs:
                self._state["slots"] = [random.choice(envs)[0]]
        self._save()
        self._update_status()
        # Kick off: queue a map immediately so the operator sees activity.
        await self._pick_and_jukebox(triggered_by="enable")

    async def on_disable(self) -> None:
        # Cancel any in-flight vote we own.
        try:
            if self.ctx.votes.is_active:
                await self.ctx.votes.cancel()
        except Exception:
            logger.exception("evolution: cancel vote on disable failed")

    async def on_podium_start(self) -> None:
        # Decide whether to run a vote this cycle.
        self._state["maps_since_vote"] += 1
        n = max(1, int(self._config.get("vote_every_n_maps") or 1))
        if self._state["maps_since_vote"] >= n:
            await self._start_vote()
        else:
            # No vote this round - just queue the next map from the existing mix.
            await self._pick_and_jukebox(triggered_by="podium")
        self._save()
        self._update_status()

    # ---- status / helpers --------------------------------------------

    def status_lines(self) -> list[str]:
        names = [self._env_name(e) for e in self._state["slots"]]
        n = max(1, int(self._config.get("vote_every_n_maps") or 1))
        return [
            f"Active vistas: {', '.join(names) if names else '(none)'}",
            f"Slot budget: {len(self._state['slots'])} / {self._config['slot_count']}",
            f"Maps since last vote: {self._state['maps_since_vote']} / {n}",
        ]

    def _env_options(self) -> list[tuple[int, str]]:
        envs = _ENVIRONMENTS.get(self.ctx.game, {})
        # Drop "Custom" (id=0) - random custom maps are too unpredictable.
        return [(k, v) for k, v in sorted(envs.items()) if k != 0]

    def _env_name(self, env_id: int) -> str:
        return _ENVIRONMENTS.get(self.ctx.game, {}).get(int(env_id), f"#{env_id}")

    def _save(self) -> None:
        self.ctx.save_state(self._state)

    def _update_status(self) -> None:
        self.ctx.set_status(self.status_lines())

    # ---- vote --------------------------------------------------------

    async def _start_vote(self) -> None:
        envs = self._env_options()
        if not envs:
            await self._pick_and_jukebox(triggered_by="no_env_options")
            return
        # Candidates = envs not already in slots; if everything is in the
        # mix, just vote on the full list so something can rotate.
        active = set(int(e) for e in self._state["slots"])
        pool = [(eid, name) for (eid, name) in envs if eid not in active] or envs
        # Cap to 6 options to keep the vote panel readable.
        if len(pool) > 6:
            pool = random.sample(pool, 6)
        options = [{"value": eid, "label": name} for (eid, name) in pool]
        await self.ctx.votes.start(
            key="evolution:add_env",
            title="Vote a new vista into the rotation",
            options=options,
            duration_s=int(self._config.get("vote_duration_s") or 25),
            on_finish=self._on_vote_finished,
        )
        self.ctx.chat("$0c4>> $fffEvolution:$z vote on the next vista is live.")

    async def _on_vote_finished(self, result: dict[str, Any]) -> None:
        winner = result.get("winner")
        if winner is None:
            await self._pick_and_jukebox(triggered_by="vote_empty")
            return
        winner = int(winner)
        slots = [int(s) for s in self._state["slots"] if int(s) != winner]
        slots.append(winner)
        cap = max(1, int(self._config.get("slot_count") or 3))
        # Drop oldest until within budget.
        while len(slots) > cap:
            dropped = slots.pop(0)
            self.ctx.chat(
                f"$0c4>> $fffEvolution:$z $888dropped$z {self._env_name(dropped)}"
            )
        self._state["slots"] = slots
        self._state["maps_since_vote"] = 0
        self._save()
        self._update_status()
        self.ctx.chat(
            f"$0c4>> $fffEvolution:$z added $0c4{self._env_name(winner)}$z"
            f" - active mix: {', '.join(self._env_name(e) for e in slots)}"
        )
        await self._pick_and_jukebox(triggered_by="vote_result")

    # ---- picking -----------------------------------------------------

    async def _pick_and_jukebox(self, triggered_by: str) -> None:
        if self._busy:
            return
        slots = [int(s) for s in self._state["slots"]]
        if not slots:
            return
        self._busy = True
        try:
            validators = [downloadable()]
            if self._config.get("block_lunatic"):
                validators.append(reject_difficulty("Lunatic", "Impossible"))
            if self._config.get("block_kacky"):
                validators.append(reject_tags("Kacky"))

            # The TMX v2 API takes a single environment per call. Roll one
            # env per attempt, weighted uniformly across the active slots.
            attempts = max(1, int(self._config.get("max_pick_attempts") or 8))
            chosen_row = None
            for _ in range(attempts):
                env = random.choice(slots)
                row = await self.ctx.picker.pick_random(
                    filters={"environment": env},
                    validators=validators,
                    excluded_tmx_ids=[int(self._state.get("last_track_id") or 0)],
                    max_attempts=2,
                )
                if row is not None:
                    chosen_row = row
                    break
            if chosen_row is None:
                logger.warning("evolution: picker found nothing after %d attempts "
                               "(slots=%s, trigger=%s)",
                               attempts, slots, triggered_by)
                await self.ctx.notify(
                    "Evolution: no valid map found, retry on next podium",
                    severity="warning",
                )
                return

            installed = await self.ctx.picker.install(chosen_row, juke_next=True)
            if installed is None:
                await self.ctx.notify(
                    "Evolution: failed to add picked map to server",
                    severity="error",
                )
                return
            self._state["last_track_id"] = int(chosen_row.get("track_id") or 0)
            self._save()
            self.ctx.chat(
                f"$0c4>> $fffEvolution:$z next map "
                f"$0c4{chosen_row.get('name')}$z by {chosen_row.get('author')}"
                f" [{chosen_row.get('environment')}]"
            )
        finally:
            self._busy = False
