"""RMC mode.

Cooperative multiplayer adaptation of Random Map Challenge:
- one fixed goal medal per run
- 60 minute in-race timer
- first goal clear advances to next map
- one global free-skip vote, unlimited operator-initiated broken-map votes
- persistent broken-map exclusion across runs
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import Any

from ..base import ConfigField, GameMode, GameModeContext, register
from ..picker import downloadable, min_awards, reject_difficulty, reject_tags

logger = logging.getLogger(__name__)


# Medal substyles for `style="MedalsBig"` quads in templates.
MEDAL_SUBSTYLE = {
    "at": "MedalNadeo",
    "gold": "MedalGold",
    "silver": "MedalSilver",
    "bronze": "MedalBronze",
}


GOAL_ORDER = ["bronze", "silver", "gold", "at"]
GOAL_LABELS = {
    "bronze": "Bronze",
    "silver": "Silver",
    "gold": "Gold",
    "at": "AT",
}


@register
class RandomChallengeMode(GameMode):
    key = "random_challenge"
    name = "RMC"
    description = "Co-op RMC: one shared 60 min run with first-clear map advance."
    icon = "random"
    color = "fa0"
    category = "rotation"

    RUN_DURATION_MS = 60 * 60 * 1000
    BROKEN_SKIP_VOTE_SECONDS = 30
    FREE_SKIP_VOTE_SECONDS = 30
    VOTE_THRESHOLD_RATIO = 0.60

    def __init__(self, ctx: GameModeContext) -> None:
        super().__init__(ctx)
        self._config: dict[str, Any] = self.default_config()
        self._state: dict[str, Any] = {
            "broken_maps": {},
            "run": {},
            "history_track_ids": [],
            "last_track_id": None,
            "timelimit_restore": {},
        }
        self._busy = False
        self._round_ending = False
        self._run_task: asyncio.Task | None = None
        self._current_map = None
        self._in_race_started_at_monotonic: float | None = None
        # Logins that were actively racing the moment the operator hit Pause.
        # On Resume only these players are force-moved back into the race;
        # anyone else who self-spec'd in the meantime stays free to choose.
        self._paused_active_logins: set[str] = set()

    def default_config(self) -> dict[str, Any]:
        return {
            "goal_medal": "at",
            "run_duration_min": 60,
            "resume_behavior": "current",  # current|next
            "max_pick_attempts": 10,
            "block_lunatic": True,
            "block_kacky": True,
            "skip_duplicate_maps": True,
            "history_size": 200,
            "filter_low_effort": True,
            "filter_untagged": True,
            "max_author_time_sec": 180,
            "required_widgets_csv": "rmc_rules,rmc_operator",
            "extra_widgets_csv": "",
        }

    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField.make(
                "goal_medal",
                "Goal medal for next run",
                "choice",
                default="at",
                help="Locked when a run starts. Default is AT.",
                choices=[
                    ("silver", "Silver"),
                    ("gold", "Gold"),
                    ("at", "AT"),
                ],
            ),
            ConfigField.make(
                "run_duration_min",
                "Run duration",
                "choice",
                default=60,
                help="Total challenge time for a new run. 30-minute steps (5 min option for testing).",
                choices=[
                    (5,   "5 min (test)"),
                    (30,  "30 min"),
                    (60,  "1 h"),
                    (90,  "1 h 30"),
                    (120, "2 h"),
                    (150, "2 h 30"),
                    (180, "3 h"),
                    (210, "3 h 30"),
                    (240, "4 h"),
                ],
            ),
            ConfigField.make(
                "resume_behavior",
                "Resume behavior after pause",
                "choice",
                default="current",
                help="Operator can still override by command.",
                choices=[
                    ("current", "Resume current map"),
                    ("next", "Go to next map"),
                ],
            ),
            ConfigField.make(
                "max_pick_attempts",
                "Max TMX pick attempts",
                "int",
                default=10,
                min=1,
                max=60,
                help="How many random rolls to try before giving up this cycle.",
            ),
            ConfigField.make(
                "max_author_time_sec",
                "Max author time (seconds)",
                "int",
                default=180,
                min=30,
                max=600,
                help="Reject maps with AT above this threshold.",
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
                "skip_duplicate_maps",
                "Skip duplicates in recent history",
                "bool",
                default=True,
            ),
            ConfigField.make(
                "history_size",
                "Recent history size",
                "int",
                default=200,
                min=10,
                max=5000,
            ),
            ConfigField.make(
                "filter_low_effort",
                "Filter low-effort maps",
                "bool",
                default=True,
                help="Require at least one award.",
            ),
            ConfigField.make(
                "filter_untagged",
                "Filter untagged maps",
                "bool",
                default=True,
                help="Reject maps with no tags.",
            ),
            ConfigField.make(
                "required_widgets_csv",
                "Required widgets (csv)",
                "str",
                default="rmc_rules,rmc_operator",
                help="Used by gamemodes widget set editor.",
            ),
            ConfigField.make(
                "extra_widgets_csv",
                "Extra widgets (csv)",
                "str",
                default="",
                help="Used by gamemodes widget set editor.",
            ),
        ]

    async def on_enable(self, config: dict[str, Any]) -> None:
        self._config = {**self.default_config(), **(config or {})}
        self._load_state()
        self._ensure_run_defaults()
        await self._set_mode_timelimit_zero()
        if self._run_task is None or self._run_task.done():
            self._run_task = asyncio.ensure_future(self._run_tick_loop())

        run = self._state.setdefault("run", {})
        # Do NOT auto-start a run on enable. Finished runs (or fresh mode
        # activations) stay idle until an operator presses Start RMC or runs
        # `//rmc start`, otherwise the challenge would silently restart every
        # time the timer expires or the pool boots.
        if bool(run.get("active")):
            self._in_race_started_at_monotonic = None
        self._update_status()

    async def on_disable(self) -> None:
        self._commit_race_elapsed()
        await self._force_all_spectator(False)
        await self._restore_mode_timelimit()
        # Stopping the mode should clear transient run progress so a later
        # activation starts from a clean 60:00 / 0-clears state.
        run = self._state.setdefault("run", {})
        run["active"] = False
        run["paused"] = False
        run["remaining_race_ms"] = self._configured_run_duration_ms()
        run["maps_cleared"] = 0
        run["secondary_cleared"] = 0
        run["free_skip_used"] = False
        run["free_skips"] = 0
        run["broken_skips"] = 0
        run["current_map"] = {}
        run["pending_track_id"] = 0
        run["pending_row"] = {}
        run["last_event"] = ""
        self._state["history_track_ids"] = []
        self._state["last_track_id"] = None
        self._in_race_started_at_monotonic = None
        if self._run_task is not None:
            self._run_task.cancel()
            self._run_task = None
        self._save()
        await self._refresh_rmc_widgets()

    async def _refresh_rmc_widgets(self) -> None:
        """Force re-render of RMC widgets after lifecycle transitions.

        Their templates use `widget_force_hidden`, which is evaluated on
        render. Explicit refresh avoids stale visibility after stop/finish.
        """
        # Directly drive the widget apps' refresh loops so they execute their
        # active-aware display/hide logic immediately, not on the next tick.
        apps_map = getattr(self.ctx._app.instance.apps, "apps", {}) or {}
        for app_label in ("rmc_operator_widget", "rmc_rules_widget"):
            widget_app = apps_map.get(app_label)
            if widget_app is None:
                continue
            try:
                if hasattr(widget_app, "_active_mode") and widget_app._active_mode() is None:
                    if hasattr(widget_app, "_hide_view"):
                        await widget_app._hide_view()
                        continue
                view = getattr(widget_app, "view", None)
                if view is None:
                    continue
                try:
                    online_logins = [
                        p.login for p in self.ctx.instance.player_manager.online
                        if getattr(p, "login", None)
                    ]
                except Exception:
                    online_logins = []
                if online_logins:
                    await view.display(player_logins=online_logins)
                else:
                    await view.display()
            except Exception:
                logger.exception("rmc: direct widget refresh '%s' failed", app_label)

        try:
            sig_new = self.ctx._app.context.signals.get_signal("widget_engine:refresh")
        except Exception:
            sig_new = None
        try:
            sig_old = self.ctx._app.context.signals.get_signal("tmsm_widgets:refresh")
        except Exception:
            sig_old = None
        for key in ("rmc_operator", "rmc_rules"):
            payload = {"key": key}
            if sig_new is not None:
                try:
                    await sig_new.send_robust(payload, raw=True)
                except Exception:
                    pass
            if sig_old is not None:
                try:
                    await sig_old.send_robust(payload, raw=True)
                except Exception:
                    pass

    async def on_map_begin(self, map_obj) -> None:
        self._current_map = map_obj
        run = self._state.setdefault("run", {})
        if not bool(run.get("active")):
            return

        run["current_map"] = {
            "uid": str(getattr(map_obj, "uid", "") or ""),
            "name": str(getattr(map_obj, "name", "") or ""),
            "author": str(getattr(map_obj, "author_nickname", "") or ""),
            "track_id": int(run.get("pending_track_id") or 0),
            "goal_time_ms": int(self._goal_time_for_map(map_obj, self._goal_medal(run))),
            "secondary_time_ms": int(self._goal_time_for_map(
                map_obj, self._secondary_medal(self._goal_medal(run))
            )),
            "first_clear_login": "",
            "first_clear_time_ms": 0,
            "cleared": False,
            "secondary_cleared": False,
        }
        run["pending_track_id"] = 0
        if not bool(run.get("paused")):
            self._in_race_started_at_monotonic = time.monotonic()
        self._save()
        self._update_status()

    async def on_map_end(self, map_obj) -> None:
        self._commit_race_elapsed()
        self._save()
        self._update_status()

    async def on_podium_start(self) -> None:
        run = self._state.setdefault("run", {})
        if not bool(run.get("active")):
            return
        if self._remaining_ms_now() <= 0:
            await self._finish_run("Time is over")
            return
        await self._pick_and_jukebox(triggered_by="podium")

    async def on_player_finish(self, player=None, **kwargs) -> None:
        run = self._state.setdefault("run", {})
        if not bool(run.get("active")) or bool(run.get("paused")):
            return
        if kwargs.get("is_end_race") is False:
            return
        if self._remaining_ms_now() <= 0:
            await self._finish_run("Time is over")
            return

        login = str(getattr(player, "login", "") or "")
        if not login:
            return
        nickname = str(getattr(player, "nickname", "") or login)

        try:
            score = int(kwargs.get("race_time") or kwargs.get("lap_time") or 0)
        except (TypeError, ValueError):
            score = 0
        if score <= 0:
            return

        # Count every valid finish (used only by future stats app).
        self._bump_contribution(login, nickname, "finishes", 1)

        cmap = dict(run.get("current_map") or {})
        if bool(cmap.get("cleared")):
            return

        # Always recompute goal/secondary thresholds from the live config so
        # changing the goal medal mid-run takes effect immediately.
        goal_key = self._goal_medal(run)
        if self._current_map is not None:
            goal_ms = int(self._goal_time_for_map(self._current_map, goal_key))
            secondary_ms = int(self._goal_time_for_map(
                self._current_map, self._secondary_medal(goal_key)
            ))
        else:
            goal_ms = int(cmap.get("goal_time_ms") or 0)
            secondary_ms = int(cmap.get("secondary_time_ms") or 0)
        cmap["goal_time_ms"] = goal_ms
        cmap["secondary_time_ms"] = secondary_ms
        if goal_ms <= 0:
            return

        # Track secondary medal (one notch easier) independently, but only
        # bump the global counter once per map across all players. Driving
        # the goal-medal time directly must NOT count toward the secondary
        # counter — those are two separate achievements.
        if (
            secondary_ms > 0
            and not bool(cmap.get("secondary_cleared"))
            and score <= secondary_ms
            and score > goal_ms
        ):
            cmap["secondary_cleared"] = True
            cmap["secondary_clear_login"] = login
            cmap["secondary_clear_time_ms"] = int(score)
            run["current_map"] = cmap
            run["secondary_cleared"] = int(run.get("secondary_cleared") or 0) + 1
            self._bump_contribution(login, nickname, "secondary_clears", 1)
            self._save()
            self._update_status()
            # Make the secondary-skip button light up immediately.
            await self._refresh_rmc_widgets()

        if score <= goal_ms:
            await self._on_first_goal_clear(login=login, nickname=nickname, time_ms=score, goal_ms=goal_ms)

    async def on_player_chat(self, player=None, text: str = "", **kwargs) -> None:
        raw = str(text or "").strip()
        if not raw:
            return
        cmd = raw.lower()
        if not (cmd.startswith("//rmc") or cmd.startswith("/rmc")):
            return

        login = str(getattr(player, "login", "") or "")
        if cmd in {"//rmc", "/rmc", "//rmc help", "/rmc help"}:
            self.ctx.chat("$fa0RMC:$z //rmc start | pause | play [current|next] | stop | vote skip | vote broken | vote secondary | status", login=login)
            return

        if not self._is_operator(player):
            await self.ctx.notify("RMC: operator only command.", severity="warning", login=login)
            return

        if cmd in {"//rmc status", "/rmc status"}:
            for line in self.status_lines():
                self.ctx.chat(f"$fa0RMC:$z {line}", login=login)
            return

        if cmd in {"//rmc start", "/rmc start"}:
            await self._start_new_run(announce=True)
            return

        if cmd in {"//rmc stop", "/rmc stop"}:
            app = getattr(self.ctx, "_app", None)
            if app is not None and hasattr(app, "_deactivate"):
                await app._deactivate()
            else:
                await self._finish_run("Stopped by operator")
            return

        if cmd in {"//rmc pause", "/rmc pause"}:
            await self._pause_run()
            return

        if cmd.startswith("//rmc play") or cmd.startswith("/rmc play"):
            behavior = ""
            parts = cmd.split()
            if len(parts) >= 3:
                behavior = parts[2]
            await self._resume_run(behavior=behavior)
            return

        if cmd in {"//rmc vote skip", "/rmc vote skip", "//rmc skip", "/rmc skip"}:
            await self._start_skip_vote(vote_kind="free")
            return

        if cmd in {"//rmc vote broken", "/rmc vote broken", "//rmc broken", "/rmc broken"}:
            await self._start_skip_vote(vote_kind="broken")
            return

        if cmd in {"//rmc vote secondary", "/rmc vote secondary",
                   "//rmc secondary", "/rmc secondary"}:
            await self._start_skip_vote(vote_kind="secondary")
            return

    def status_lines(self) -> list[str]:
        run = self._state.setdefault("run", {})
        goal = self._goal_medal(run)
        rem = self._remaining_ms_now()
        broken = self._state.get("broken_maps") or {}
        state = "running" if bool(run.get("active")) and not bool(run.get("paused")) else (
            "paused" if bool(run.get("active")) else "idle"
        )
        cmap = dict(run.get("current_map") or {})
        cmap_name = str(cmap.get("name") or "-")
        cmap_goal = int(cmap.get("goal_time_ms") or 0)
        free_left = 0 if bool(run.get("free_skip_used")) else 1
        return [
            f"State: {state}",
            f"Timer: {self._fmt_ms(rem)} / {self._fmt_ms(self._configured_run_duration_ms())}",
            f"Goal: {GOAL_LABELS.get(goal, goal.upper())}",
            f"Maps cleared: {int(run.get('maps_cleared') or 0)}",
            f"Skips: free-left={free_left}, broken-skips={int(run.get('broken_skips') or 0)}",
            f"Current: {cmap_name} (goal {self._fmt_ms(cmap_goal) if cmap_goal > 0 else '-'})",
            f"Broken table: {len(broken)} maps",
        ]

    # ---- run lifecycle -------------------------------------------------

    async def _start_new_run(self, *, announce: bool) -> None:
        self._commit_race_elapsed()
        await self._set_mode_timelimit_zero()
        await self._force_all_spectator(False)
        self._state["run"] = {
            "active": True,
            "paused": False,
            "remaining_race_ms": self._configured_run_duration_ms(),
            "goal_medal": self._normalized_goal(self._config.get("goal_medal")),
            "maps_cleared": 0,
            "secondary_cleared": 0,
            "free_skip_used": False,
            "free_skips": 0,
            "broken_skips": 0,
            "current_map": {},
            "pending_track_id": 0,
            "pending_row": {},
            "last_event": "",
            "started_at": datetime.datetime.utcnow().isoformat(),
            "contributions": {},  # login -> {nickname, goal_clears, secondary_clears, finishes, best_delta_ms, total_clear_time_ms}
        }
        self._state["history_track_ids"] = []
        self._state["last_track_id"] = None
        self._in_race_started_at_monotonic = None
        self._save()
        self._update_status()

        picked = await self._pick_and_jukebox(triggered_by="run_start")
        if picked:
            try:
                await self.ctx.instance.gbx("NextMap")
            except Exception:
                logger.exception("rmc: NextMap on run start failed")
        if announce:
            self.ctx.chat("$fa0>> $fffRMC:$z new run started.")
        await self._refresh_rmc_widgets()

    async def _finish_run(self, reason: str) -> None:
        run = self._state.setdefault("run", {})
        if not bool(run.get("active")):
            return
        self._commit_race_elapsed()
        await self._force_all_spectator(False)
        run["active"] = False
        run["paused"] = False
        run["last_event"] = str(reason or "")
        # Persist stats first so the results view can read the new run_id.
        run_id = await self._persist_run_stats(reason=reason)
        if run_id:
            run["last_run_id"] = int(run_id)
        self._save()
        self._update_status()
        await self._restore_mode_timelimit()
        self.ctx.chat(
            f"$fa0>> $fffRMC:$z run finished ({reason}). "
            f"Clears: {int(run.get('maps_cleared') or 0)}"
        )
        await self._refresh_rmc_widgets()
        # Open the end-of-run results panel for everyone online.
        try:
            app = self.ctx._app
            if app is not None and hasattr(app, "show_rmc_results") and run_id:
                await app.show_rmc_results(int(run_id))
        except Exception:
            logger.exception("rmc: opening results view failed")

    async def _persist_run_stats(self, *, reason: str) -> int | None:
        """Write `rmc_run`, `rmc_run_player`, and update `rmc_player_totals`.

        Returns the new `rmc_run.id` on success, or None if persistence
        failed (logged + skipped so it never blocks the run lifecycle).
        """
        try:
            from ..models import RmcPlayerTotals, RmcRun, RmcRunPlayer
        except Exception:
            logger.exception("rmc: stats models import failed")
            return None

        run = self._state.setdefault("run", {})
        contribs = dict(run.get("contributions") or {})
        goal = self._goal_medal(run)
        secondary = self._secondary_medal(goal)
        maps_cleared = int(run.get("maps_cleared") or 0)
        secondary_cleared = int(run.get("secondary_cleared") or 0)
        # Participants = anyone who recorded at least one finish.
        players_count = sum(
            1 for r in contribs.values()
            if int((r or {}).get("finishes") or 0) > 0
        )

        started_at = self._parse_iso_dt(run.get("started_at"))
        finished_at = datetime.datetime.utcnow()
        duration_ms = self._configured_run_duration_ms() - max(0, self._remaining_ms_now())
        if duration_ms < 0:
            duration_ms = 0

        try:
            run_row = await RmcRun.objects.create(
                RmcRun,
                started_at=started_at or finished_at,
                finished_at=finished_at,
                duration_ms=int(duration_ms),
                goal_medal=goal,
                secondary_medal=secondary,
                reason=str(reason or "")[:64],
                maps_cleared=maps_cleared,
                secondary_cleared=secondary_cleared,
                players_count=players_count,
            )
        except Exception:
            logger.exception("rmc: failed to insert rmc_run row")
            return None

        for login, raw in contribs.items():
            r = raw or {}
            try:
                await RmcRunPlayer.objects.create(
                    RmcRunPlayer,
                    run=run_row,
                    login=str(login)[:100],
                    nickname=str(r.get("nickname") or login)[:150],
                    goal_clears=int(r.get("goal_clears") or 0),
                    secondary_clears=int(r.get("secondary_clears") or 0),
                    finishes=int(r.get("finishes") or 0),
                    best_delta_ms=(
                        int(r["best_delta_ms"])
                        if r.get("best_delta_ms") is not None else None
                    ),
                    total_clear_time_ms=int(r.get("total_clear_time_ms") or 0),
                )
            except Exception:
                logger.exception("rmc: failed to insert rmc_run_player row for %s", login)

            try:
                existing = None
                try:
                    existing = await RmcPlayerTotals.objects.get(
                        RmcPlayerTotals, RmcPlayerTotals.login == str(login)
                    )
                except RmcPlayerTotals.DoesNotExist:
                    existing = None
                if existing is None:
                    await RmcPlayerTotals.objects.create(
                        RmcPlayerTotals,
                        login=str(login)[:100],
                        nickname=str(r.get("nickname") or login)[:150],
                        runs_played=1,
                        goal_clears=int(r.get("goal_clears") or 0),
                        secondary_clears=int(r.get("secondary_clears") or 0),
                        finishes=int(r.get("finishes") or 0),
                        last_played_at=finished_at,
                    )
                else:
                    existing.nickname = str(r.get("nickname") or existing.nickname)[:150]
                    existing.runs_played = int(existing.runs_played or 0) + 1
                    existing.goal_clears = int(existing.goal_clears or 0) + int(r.get("goal_clears") or 0)
                    existing.secondary_clears = int(existing.secondary_clears or 0) + int(r.get("secondary_clears") or 0)
                    existing.finishes = int(existing.finishes or 0) + int(r.get("finishes") or 0)
                    existing.last_played_at = finished_at
                    await RmcPlayerTotals.objects.update(existing)
            except Exception:
                logger.exception("rmc: failed to upsert rmc_player_totals for %s", login)

        return int(run_row.id) if run_row is not None else None

    @staticmethod
    def _parse_iso_dt(raw: Any) -> datetime.datetime | None:
        if not raw:
            return None
        try:
            return datetime.datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return None

    async def _pause_run(self) -> None:
        run = self._state.setdefault("run", {})
        if not bool(run.get("active")) or bool(run.get("paused")):
            return
        self._commit_race_elapsed()
        run["paused"] = True
        # Snapshot whoever is actively racing right now so resume can put
        # exactly these players back into the race. Everyone else online
        # gets force-spec'd too, but is released (not force-played) on resume.
        self._paused_active_logins = set(self._active_player_logins())
        self._save()
        await self._force_all_spectator(True)
        self._update_status()
        self.ctx.chat("$fa0>> $fffRMC:$z paused.")
        await self._refresh_rmc_widgets()

    async def _resume_run(self, *, behavior: str = "") -> None:
        run = self._state.setdefault("run", {})
        if not bool(run.get("active")) or not bool(run.get("paused")):
            return

        chosen = str(behavior or "").strip().lower()
        if chosen not in {"current", "next"}:
            chosen = str(self._config.get("resume_behavior") or "current").strip().lower()
            if chosen not in {"current", "next"}:
                chosen = "current"

        run["paused"] = False
        self._in_race_started_at_monotonic = time.monotonic()
        self._save()
        # Force previously-driving players back into the race; release
        # everyone else from forced spectate so they stay spectating but
        # are free to self-toggle into driving whenever they want.
        try:
            online_logins = {
                str(getattr(p, "login", "") or "")
                for p in self.ctx.instance.player_manager.online
            }
            online_logins.discard("")
        except Exception:
            online_logins = set()
        to_restore = set(self._paused_active_logins) & online_logins
        to_release = online_logins - to_restore
        if to_restore:
            await self._force_spectator_for(to_restore, spectate=False)
        if to_release:
            await self._release_force_spectator_for(to_release)
        self._paused_active_logins.clear()
        if chosen == "next":
            try:
                await self.ctx.instance.gbx("NextMap")
            except Exception:
                logger.exception("rmc: NextMap on resume failed")
        self._update_status()
        self.ctx.chat(f"$fa0>> $fffRMC:$z resumed ({chosen}).")
        await self._refresh_rmc_widgets()

    async def _on_first_goal_clear(self, *, login: str, nickname: str = "", time_ms: int, goal_ms: int = 0) -> None:
        if self._round_ending:
            return
        self._round_ending = True
        try:
            run = self._state.setdefault("run", {})
            cmap = dict(run.get("current_map") or {})
            if bool(cmap.get("cleared")):
                return
            cmap["cleared"] = True
            cmap["first_clear_login"] = login
            cmap["first_clear_time_ms"] = int(time_ms)
            # NOTE: driving the goal medal time does NOT bump the secondary
            # counter — the two medal counters are tracked independently.
            run["current_map"] = cmap
            run["maps_cleared"] = int(run.get("maps_cleared") or 0) + 1
            self._bump_contribution(login, nickname or login, "goal_clears", 1)
            self._bump_contribution(login, nickname or login, "total_clear_time_ms", int(time_ms))
            if int(goal_ms) > 0:
                self._update_best_delta(login, int(goal_ms) - int(time_ms))
            self._save()
            self._update_status()
            self.ctx.chat(
                f"$fa0>> $fffRMC:$z first clear by $fa0{nickname or login}$z in {self._fmt_ms(int(time_ms))}."
            )
            try:
                await self.ctx.instance.gbx("NextMap")
            except Exception:
                logger.exception("rmc: NextMap after first clear failed")
        finally:
            self._round_ending = False

    async def _secondary_skip(self) -> bool:
        """Operator-triggered secondary skip — wired through a vote.

        Allowed only when the secondary medal has been cleared on the current
        map but the primary goal has not. This entry point no longer applies
        the skip directly; it kicks off a vote and the actual skip happens
        in ``_start_skip_vote``'s ``_on_finish`` when the vote passes.
        """
        run = self._state.setdefault("run", {})
        if not bool(run.get("active")) or bool(run.get("paused")):
            await self.ctx.notify("RMC: secondary skip not available (run not running).", severity="warning")
            return False
        cmap = dict(run.get("current_map") or {})
        if bool(cmap.get("cleared")):
            return False
        if not bool(cmap.get("secondary_cleared")):
            await self.ctx.notify(
                "RMC: secondary skip not yet available (secondary medal not achieved).",
                severity="warning",
            )
            return False
        await self._start_skip_vote(vote_kind="secondary")
        return True

    async def _apply_secondary_skip(self) -> None:
        """Actually perform the secondary skip — invoked from the vote callback."""
        run = self._state.setdefault("run", {})
        cmap = dict(run.get("current_map") or {})
        if bool(cmap.get("cleared")) or self._round_ending:
            return
        self._round_ending = True
        try:
            cmap["cleared"] = True
            cmap.setdefault("first_clear_login", "secondary_skip")
            run["current_map"] = cmap
            self._save()
            self._update_status()
            self.ctx.chat(
                "$fa0>> $fffRMC:$z secondary skip approved "
                "(secondary medal already cleared)."
            )
            try:
                await self.ctx.instance.gbx("NextMap")
            except Exception:
                logger.exception("rmc: NextMap after secondary skip failed")
        finally:
            self._round_ending = False
        await self._refresh_rmc_widgets()

    # ---- picking -------------------------------------------------------

    async def _pick_and_jukebox(self, triggered_by: str) -> bool:
        run = self._state.setdefault("run", {})
        if self._busy or not bool(run.get("active")):
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
            validators.append(self._validate_author_time)

            excluded = set()
            last_tid = int(self._state.get("last_track_id") or 0)
            if last_tid > 0:
                excluded.add(last_tid)
            if self._config.get("skip_duplicate_maps"):
                for tid in self._history_ids():
                    excluded.add(int(tid))
            for tid in self._broken_track_ids():
                excluded.add(int(tid))

            row = await self.ctx.picker.pick_random(
                filters={},
                validators=validators,
                excluded_tmx_ids=list(excluded),
                max_attempts=max(1, int(self._config.get("max_pick_attempts") or 10)),
            )
            if row is None:
                logger.warning("rmc: no valid map found (trigger=%s)", triggered_by)
                await self.ctx.notify(
                    "RMC: no valid map found, retrying next podium",
                    severity="warning",
                )
                return False

            installed = await self.ctx.picker.install(row, juke_next=True)
            if installed is None:
                await self.ctx.notify(
                    "RMC: failed to add picked map to server",
                    severity="error",
                )
                return False

            tid = int(row.get("track_id") or 0)
            self._state["last_track_id"] = tid
            history = self._history_ids()
            history.append(tid)
            cap = max(10, int(self._config.get("history_size") or 200))
            if len(history) > cap:
                history = history[-cap:]
            self._state["history_track_ids"] = history

            run["pending_track_id"] = tid
            run["pending_row"] = {
                "track_id": tid,
                "name": str(row.get("name") or ""),
                "author": str(row.get("author") or ""),
            }
            self._save()
            self._update_status()

            self.ctx.chat(
                f"$fa0>> $fffRMC:$z next map $fa0{row.get('name')}$z by {row.get('author')}"
            )
            return True
        finally:
            self._busy = False

    def _validate_author_time(self, row: dict[str, Any]) -> bool:
        raw = row.get("author_time")
        if raw is None:
            raw = row.get("authorTime")
        if raw is None:
            raw = row.get("author")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return True
        if value <= 0:
            return False
        if value > 1000:
            at_ms = int(value)
        else:
            at_ms = int(value * 1000.0)
        max_sec = max(30, int(self._config.get("max_author_time_sec") or 180))
        return at_ms <= max_sec * 1000

    # ---- votes ---------------------------------------------------------

    async def _start_skip_vote(self, *, vote_kind: str) -> None:
        run = self._state.setdefault("run", {})
        if not bool(run.get("active")):
            await self.ctx.notify("RMC: run is not active.", severity="warning")
            return
        if self.ctx.votes.is_active:
            await self.ctx.notify("RMC: another vote is already active.", severity="warning")
            return

        if vote_kind == "free" and bool(run.get("free_skip_used")):
            await self.ctx.notify("RMC: free skip already used this run.", severity="warning")
            return

        if vote_kind == "secondary":
            cmap_now = dict(run.get("current_map") or {})
            if bool(cmap_now.get("cleared")):
                await self.ctx.notify("RMC: map already cleared.", severity="warning")
                return
            if not bool(cmap_now.get("secondary_cleared")):
                await self.ctx.notify(
                    "RMC: secondary skip not yet available (secondary medal not achieved).",
                    severity="warning",
                )
                return

        participants = self._participant_logins()
        if not participants:
            await self.ctx.notify("RMC: no participating players for vote.", severity="warning")
            return

        if vote_kind == "free":
            title = "RMC vote: use the single free skip?"
            key = "rmc:free_skip"
            duration = self.FREE_SKIP_VOTE_SECONDS
        elif vote_kind == "secondary":
            sub = self._secondary_medal(self._goal_medal(run)).title()
            title = f"RMC vote: {sub} skip (advance to next map)?"
            key = "rmc:secondary_skip"
            duration = self.BROKEN_SKIP_VOTE_SECONDS
        else:
            title = "RMC vote: mark current map broken and skip?"
            key = "rmc:broken_skip"
            duration = self.BROKEN_SKIP_VOTE_SECONDS

        async def _on_finish(result: dict[str, Any]) -> None:
            winner = str(result.get("winner") or "")
            if winner != "yes":
                self.ctx.chat("$fa0>> $fffRMC:$z vote failed.")
                return
            if vote_kind == "free":
                run["free_skip_used"] = True
                run["free_skips"] = int(run.get("free_skips") or 0) + 1
                self._save()
                self._update_status()
                self.ctx.chat("$fa0>> $fffRMC:$z free skip approved.")
                try:
                    await self.ctx.instance.gbx("NextMap")
                except Exception:
                    logger.exception("rmc: NextMap after free skip failed")
                return

            if vote_kind == "secondary":
                await self._apply_secondary_skip()
                return

            run["broken_skips"] = int(run.get("broken_skips") or 0) + 1
            self._record_current_map_broken(result)
            self._save()
            self._update_status()
            self.ctx.chat("$fa0>> $fffRMC:$z broken-map skip approved.")
            try:
                await self.ctx.instance.gbx("NextMap")
            except Exception:
                logger.exception("rmc: NextMap after broken skip failed")

        await self.ctx.votes.start(
            key=key,
            title=title,
            options=[
                {"value": "yes", "label": "Yes"},
                {"value": "no", "label": "No"},
            ],
            duration_s=duration,
            pass_value="yes",
            pass_ratio=self.VOTE_THRESHOLD_RATIO,
            eligible_logins=participants,
            on_finish=_on_finish,
        )

    def _participant_logins(self) -> list[str]:
        try:
            online = list(self.ctx.instance.player_manager.online)
        except Exception:
            online = []
        out: list[str] = []
        for p in online:
            login = str(getattr(p, "login", "") or "")
            if not login:
                continue
            flow = getattr(p, "flow", None)
            if bool(getattr(flow, "is_spectator", False)):
                continue
            out.append(login)
        return out

    def _record_current_map_broken(self, result: dict[str, Any]) -> None:
        run = self._state.setdefault("run", {})
        cmap = dict(run.get("current_map") or {})
        tid = int(cmap.get("track_id") or run.get("pending_track_id") or 0)
        if tid <= 0:
            pending = dict(run.get("pending_row") or {})
            tid = int(pending.get("track_id") or 0)
        if tid <= 0:
            return

        table = self._state.setdefault("broken_maps", {})
        key = str(tid)
        now = int(time.time())
        row = dict(table.get(key) or {})
        if not row:
            row = {
                "track_id": tid,
                "first_seen_at": now,
                "broken_skip_count": 0,
            }
        row["last_seen_at"] = now
        row["broken_skip_count"] = int(row.get("broken_skip_count") or 0) + 1
        row["name"] = str(cmap.get("name") or row.get("name") or "")
        row["author"] = str(cmap.get("author") or row.get("author") or "")
        row["uid"] = str(cmap.get("uid") or row.get("uid") or "")
        tally = dict(result.get("tally") or {})
        row["last_vote_yes"] = int(tally.get("yes") or 0)
        row["last_vote_no"] = int(tally.get("no") or 0)
        table[key] = row

    # ---- helpers -------------------------------------------------------

    def _load_state(self) -> None:
        persisted = self.ctx.load_state()
        if persisted:
            self._state.update(persisted)
        self._state["broken_maps"] = dict(self._state.get("broken_maps") or {})
        self._state["history_track_ids"] = list(self._state.get("history_track_ids") or [])
        self._state["run"] = dict(self._state.get("run") or {})
        self._state["timelimit_restore"] = dict(self._state.get("timelimit_restore") or {})

    def _ensure_run_defaults(self) -> None:
        run = self._state.setdefault("run", {})
        run.setdefault("active", False)
        run.setdefault("paused", False)
        run.setdefault("remaining_race_ms", self._configured_run_duration_ms())
        run.setdefault("goal_medal", self._normalized_goal(self._config.get("goal_medal")))
        run.setdefault("maps_cleared", 0)
        run.setdefault("secondary_cleared", 0)
        run.setdefault("free_skip_used", False)
        run.setdefault("free_skips", 0)
        run.setdefault("broken_skips", 0)
        run.setdefault("current_map", {})
        run.setdefault("pending_track_id", 0)
        run.setdefault("pending_row", {})
        run.setdefault("last_event", "")

    def _save(self) -> None:
        self.ctx.save_state(self._state)

    def _update_status(self) -> None:
        self.ctx.set_status(self.status_lines())

    def _history_ids(self) -> list[int]:
        out: list[int] = []
        for v in (self._state.get("history_track_ids") or []):
            try:
                tid = int(v)
            except (TypeError, ValueError):
                continue
            if tid > 0:
                out.append(tid)
        return out

    def _broken_track_ids(self) -> list[int]:
        out: list[int] = []
        for key in (self._state.get("broken_maps") or {}).keys():
            try:
                tid = int(key)
            except (TypeError, ValueError):
                continue
            if tid > 0:
                out.append(tid)
        return out

    def _goal_medal(self, run: dict[str, Any] | None = None) -> str:
        # Always reflect the current config so changing the goal in the UI
        # takes effect immediately, even mid-run. The `run` argument is kept
        # for backwards compatibility with existing call sites.
        return self._normalized_goal(self._config.get("goal_medal"))

    def _configured_run_duration_ms(self) -> int:
        """Total run length in ms, picked from config in 30-min steps.

        Falls back to ``RUN_DURATION_MS`` for any unexpected value so a
        broken config can never produce a zero / negative timer.
        """
        try:
            minutes = int(self._config.get("run_duration_min") or 60)
        except (TypeError, ValueError):
            minutes = 60
        # Allow the 5-minute test option; otherwise clamp to 30..240 and
        # snap to the nearest 30-minute step.
        if minutes == 5:
            return 5 * 60 * 1000
        if minutes < 30:
            minutes = 30
        elif minutes > 240:
            minutes = 240
        minutes = (minutes // 30) * 30 or 30
        return minutes * 60 * 1000

    @staticmethod
    def _normalized_goal(raw: Any) -> str:
        key = str(raw or "at").strip().lower()
        # Bronze is no longer a selectable goal; clamp legacy state to silver.
        if key == "bronze":
            return "silver"
        if key not in ("silver", "gold", "at"):
            return "at"
        return key

    @staticmethod
    def _secondary_medal(goal: str) -> str:
        """One-notch-easier medal used as the secondary tracked counter."""
        key = str(goal or "at").strip().lower()
        return {
            "at": "gold",
            "gold": "silver",
            "silver": "bronze",
        }.get(key, "gold")

    # ---- per-player contribution helpers -------------------------------

    def _contrib_row(self, login: str, nickname: str) -> dict[str, Any]:
        run = self._state.setdefault("run", {})
        contribs = run.setdefault("contributions", {})
        row = contribs.get(login)
        if row is None:
            row = {
                "nickname": nickname or login,
                "goal_clears": 0,
                "secondary_clears": 0,
                "finishes": 0,
                "best_delta_ms": None,
                "total_clear_time_ms": 0,
            }
            contribs[login] = row
        elif nickname and row.get("nickname") != nickname:
            row["nickname"] = nickname
        return row

    def _bump_contribution(self, login: str, nickname: str, key: str, delta: int) -> None:
        row = self._contrib_row(login, nickname)
        row[key] = int(row.get(key) or 0) + int(delta)

    def _update_best_delta(self, login: str, delta_ms: int) -> None:
        run = self._state.setdefault("run", {})
        contribs = run.setdefault("contributions", {})
        row = contribs.get(login)
        if row is None:
            return
        cur = row.get("best_delta_ms")
        if cur is None or int(delta_ms) < int(cur):
            row["best_delta_ms"] = int(delta_ms)

    def _goal_time_for_map(self, map_obj, goal: str) -> int:
        times = {
            "bronze": int(getattr(map_obj, "time_bronze", 0) or 0),
            "silver": int(getattr(map_obj, "time_silver", 0) or 0),
            "gold": int(getattr(map_obj, "time_gold", 0) or 0),
            "at": int(getattr(map_obj, "time_author", 0) or 0),
        }
        return int(times.get(goal, 0) or 0)

    def _current_game_phase(self) -> str:
        try:
            we = self.ctx.instance.apps.apps.get("widget_engine")
            engine = getattr(we, "engine", None) if we is not None else None
            phase = getattr(engine, "current_phase", None) if engine is not None else None
            if phase is None:
                return "unknown"
            return str(getattr(phase, "value", phase) or "unknown").strip().lower()
        except Exception:
            return "unknown"

    def _is_in_race_phase(self) -> bool:
        return self._current_game_phase() == "in_race"

    def _remaining_ms_now(self) -> int:
        run = self._state.setdefault("run", {})
        base = int(run.get("remaining_race_ms") or 0)
        if not bool(run.get("active")) or bool(run.get("paused")):
            return max(0, base)
        if not self._is_in_race_phase():
            return max(0, base)
        if self._in_race_started_at_monotonic is None:
            return max(0, base)
        elapsed = int((time.monotonic() - self._in_race_started_at_monotonic) * 1000)
        return max(0, base - max(0, elapsed))

    def _commit_race_elapsed(self) -> None:
        run = self._state.setdefault("run", {})
        if not bool(run.get("active")) or bool(run.get("paused")):
            self._in_race_started_at_monotonic = None
            return
        if not self._is_in_race_phase():
            self._in_race_started_at_monotonic = None
            return
        if self._in_race_started_at_monotonic is None:
            self._in_race_started_at_monotonic = time.monotonic()
            return
        elapsed = int((time.monotonic() - self._in_race_started_at_monotonic) * 1000)
        run["remaining_race_ms"] = max(0, int(run.get("remaining_race_ms") or 0) - max(0, elapsed))
        self._in_race_started_at_monotonic = time.monotonic()

    async def _run_tick_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(1.0)
                run = self._state.setdefault("run", {})
                if bool(run.get("active")) and not bool(run.get("paused")):
                    self._commit_race_elapsed()
                    rem = self._remaining_ms_now()
                    if rem <= 0:
                        await self._finish_run("Time is over")
                self._update_status()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("rmc: run tick loop crashed")

    async def _force_all_spectator(self, force: bool) -> None:
        """Apply a server-wide ForceSpectator flip.

        Used at run start / finish / disable to release any leftover force
        from a previous pause. We deliberately do NOT force-player anyone
        during a running challenge: players must stay free to toggle
        spectator on their own. Instead we set mode 0 (UserSelectable) so
        whoever was previously forced is released.
        """
        try:
            online = list(self.ctx.instance.player_manager.online)
        except Exception:
            online = []
        for p in online:
            login = str(getattr(p, "login", "") or "")
            if not login:
                continue
            try:
                if force:
                    # 3 = SpectatorReleasePlayerSlot → actually moves a driving
                    # player into spec right now. Following up with mode 1
                    # locks them as force-spectator so they can't drive again
                    # until we release them on resume.
                    await self.ctx.instance.gbx("ForceSpectator", login, 3)
                    await self.ctx.instance.gbx("ForceSpectator", login, 1)
                else:
                    # 0 = UserSelectable: release any leftover force flag.
                    await self.ctx.instance.gbx("ForceSpectator", login, 0)
            except Exception:
                # Keep best-effort semantics; not all dedicated builds expose this equally.
                logger.exception("rmc: ForceSpectator failed for %s", login)

    async def _force_spectator_for(self, logins, *, spectate: bool) -> None:
        """Force-spec (or release) a specific set of logins.

        On release we briefly set mode 2 (force player) to physically move
        the client back into the race, then mode 0 (user selectable) so the
        player keeps the freedom to spec again afterwards.
        """
        for raw in (logins or []):
            login = str(raw or "")
            if not login:
                continue
            try:
                if spectate:
                    # 3 first physically moves a driver into spec, then 1
                    # locks the force-spec flag.
                    await self.ctx.instance.gbx("ForceSpectator", login, 3)
                    await self.ctx.instance.gbx("ForceSpectator", login, 1)
                else:
                    await self.ctx.instance.gbx("ForceSpectator", login, 2)
                    await self.ctx.instance.gbx("ForceSpectator", login, 0)
            except Exception:
                logger.exception("rmc: ForceSpectator failed for %s", login)

    async def _release_force_spectator_for(self, logins) -> None:
        """Release force-spec without moving the client back into the race.

        Used on resume for players who were spectating at pause time — they
        should stay spectating, just without the forced flag, so they can
        self-toggle into driving if they want.
        """
        for raw in (logins or []):
            login = str(raw or "")
            if not login:
                continue
            try:
                await self.ctx.instance.gbx("ForceSpectator", login, 0)
            except Exception:
                logger.exception("rmc: ForceSpectator release failed for %s", login)

    async def _set_mode_timelimit_zero(self) -> None:
        """Ensure server-side timelimit does not interfere with RMC run timer."""
        key = ""
        settings: dict[str, Any] | None = None
        try:
            raw = await self.ctx.instance.gbx("GetModeScriptSettings")
            if isinstance(raw, dict):
                settings = raw
        except Exception:
            settings = None

        if settings is not None:
            for candidate in ("S_TimeLimit", "TimeLimit", "S_TimeLimitSeconds"):
                if candidate in settings:
                    key = candidate
                    break
            if not key:
                for candidate in settings.keys():
                    if "timelimit" in str(candidate or "").lower():
                        key = str(candidate)
                        break

        # Capture the original timelimit once so we can restore it when RMC
        # stops. Do not overwrite an existing snapshot.
        restore = self._state.get("timelimit_restore") or {}
        if not restore and settings is not None and key and key in settings:
            self._state["timelimit_restore"] = {
                "key": key,
                "value": settings.get(key),
            }
            self._save()

        if key:
            try:
                await self.ctx.instance.gbx("SetModeScriptSettings", {key: 0})
                return
            except Exception:
                logger.exception("rmc: failed to set timelimit key %s to 0", key)

        # Fallback for servers where key discovery fails.
        for candidate in ("S_TimeLimit", "TimeLimit", "S_TimeLimitSeconds"):
            try:
                await self.ctx.instance.gbx("SetModeScriptSettings", {candidate: 0})
                return
            except Exception:
                continue
        logger.warning("rmc: could not set mode timelimit to 0")

    async def _restore_mode_timelimit(self) -> None:
        """Restore the timelimit value captured before RMC forced it to 0."""
        restore = self._state.get("timelimit_restore") or {}
        key = str(restore.get("key") or "").strip()
        if not key:
            return
        value = restore.get("value")
        try:
            await self.ctx.instance.gbx("SetModeScriptSettings", {key: value})
            self._state["timelimit_restore"] = {}
            self._save()
        except Exception:
            logger.exception("rmc: failed to restore timelimit key %s", key)

    @staticmethod
    def _is_operator(player) -> bool:
        try:
            return int(getattr(player, "level", 0)) >= 1
        except Exception:
            return False

    @staticmethod
    def _fmt_ms(ms: int) -> str:
        total = max(0, int(ms)) // 1000
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"
