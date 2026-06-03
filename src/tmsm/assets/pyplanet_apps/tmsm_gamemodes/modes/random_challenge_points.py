"""Random Challenge Points mode.

Multiplayer variant of random challenge:
* first N finishers are tracked each map
* finishers that beat Author Time earn points
* once M finishers are known, force next map (no rounds flow)
* players can trigger one free-skip vote per map via chat (!skip)
"""
from __future__ import annotations

import math
import time
from typing import Any

from ..base import ConfigField, register
from .random_challenge import RandomChallengeMode


@register
class RandomChallengePointsMode(RandomChallengeMode):
    key = "random_challenge_points"
    name = "Random Challenge Points"
    description = "AT points for top finishers, then immediate next map; !skip vote included."
    icon = "trophy"
    color = "fc0"
    category = "rotation"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._advance_busy: bool = False

    def default_config(self) -> dict[str, Any]:
        return {
            **super().default_config(),
            "apply_widget_layout": True,
            "hide_all_widgets_first": True,
            "required_widgets_csv": "random_mx_points",
            "extra_widgets_csv": "",
            "random_mx_points_x": -126.0,
            "random_mx_points_y": 70.0,
            "random_mx_points_w": 58.0,
            "random_mx_points_h": 22.0,
            "random_mx_points_drive_mode": "fixed",
            "random_mx_points_anim_dir": "left",
            "random_mx_points_anim_duration_ms": 180,
            "random_mx_points_anim_delay_ms": 0,
            "mode_duration_minutes": 60,
            "scored_finishers": 3,
            "finishers_to_advance": 3,
            "points_csv": "3,2,1",
            "skip_vote_duration_s": 12,
            "skip_vote_min_players": 2,
            "skip_vote_pass_pct_online": 50,
        }

    def config_schema(self) -> list[ConfigField]:
        return super().config_schema() + [
            ConfigField.make(
                "apply_widget_layout",
                "Apply points widget layout",
                "bool",
                default=True,
                help="Temporarily show/hide widgets while this mode is active.",
            ),
            ConfigField.make(
                "hide_all_widgets_first",
                "Hide all widgets first",
                "bool",
                default=True,
                help="Hide every registered widget before enabling the mode widget set.",
            ),
            ConfigField.make(
                "required_widgets_csv",
                "Required widgets",
                "str",
                default="random_mx_points",
                help="Comma-separated widget keys always enabled for this mode.",
            ),
            ConfigField.make(
                "extra_widgets_csv",
                "Extra widgets",
                "str",
                default="",
                help="Comma-separated optional widget keys to also enable in this mode.",
            ),
            ConfigField.make(
                "random_mx_points_x",
                "Points widget X",
                "int",
                default=-126,
                min=-200,
                max=200,
            ),
            ConfigField.make(
                "random_mx_points_y",
                "Points widget Y",
                "int",
                default=70,
                min=-200,
                max=200,
            ),
            ConfigField.make(
                "random_mx_points_w",
                "Points widget W",
                "int",
                default=58,
                min=10,
                max=200,
            ),
            ConfigField.make(
                "random_mx_points_h",
                "Points widget H",
                "int",
                default=22,
                min=6,
                max=80,
            ),
            ConfigField.make(
                "random_mx_points_drive_mode",
                "Points widget drive mode",
                "str",
                default="fixed",
                help="fixed|hide_while_driving|only_shown_while_driving",
            ),
            ConfigField.make(
                "random_mx_points_anim_dir",
                "Points widget anim dir",
                "str",
                default="left",
                help="Animation direction: none|left|right|up|down",
            ),
            ConfigField.make(
                "random_mx_points_anim_duration_ms",
                "Points widget anim duration",
                "int",
                default=180,
                min=0,
                max=2000,
            ),
            ConfigField.make(
                "random_mx_points_anim_delay_ms",
                "Points widget anim delay",
                "int",
                default=0,
                min=0,
                max=2000,
            ),
            ConfigField.make(
                "mode_duration_minutes",
                "Mode duration (min)",
                "int",
                default=60,
                min=1,
                max=1440,
                help="Mode auto-stops when this timer reaches zero.",
            ),
            ConfigField.make(
                "scored_finishers",
                "Scored finishers",
                "int",
                default=3,
                min=1,
                max=16,
                help="Only the first N finishers are eligible for AT points.",
            ),
            ConfigField.make(
                "finishers_to_advance",
                "Finishers to advance",
                "int",
                default=3,
                min=1,
                max=32,
                help="Advance to next map after this many finishers are known.",
            ),
            ConfigField.make(
                "points_csv",
                "Points table",
                "str",
                default="3,2,1",
                help="Comma-separated points for places (e.g. 5,3,2,1).",
            ),
            ConfigField.make(
                "skip_vote_duration_s",
                "Skip vote duration (s)",
                "int",
                default=12,
                min=5,
                max=45,
            ),
            ConfigField.make(
                "skip_vote_min_players",
                "Skip vote min players",
                "int",
                default=2,
                min=1,
                max=64,
            ),
            ConfigField.make(
                "skip_vote_pass_pct_online",
                "Skip vote pass % (online)",
                "int",
                default=50,
                min=1,
                max=100,
                help="Required YES share of online players for vote skip.",
            ),
        ]

    async def on_enable(self, config: dict[str, Any]) -> None:
        await super().on_enable(config)
        self._state.setdefault("points", {})
        self._state.setdefault("current_finishers", [])
        self._state.setdefault("skip_vote_used_on_map", "")
        self._state.setdefault("current_map_uid", "")
        self._state.setdefault("best_finish_ms_by_login", {})
        now = int(time.time())
        self._state["mode_started_ts"] = now
        self._state["mode_end_ts"] = now + max(60, self._duration_seconds())
        await self._apply_widget_layout_overrides()
        self._save()
        self._update_status()

    async def on_disable(self) -> None:
        # Best-effort restore (orchestrator also clears this mode owner).
        await self.ctx.clear_widget_overrides()
        await super().on_disable()

    async def on_map_begin(self, map_obj) -> None:
        if await self._check_time_limit():
            return
        uid = str(getattr(map_obj, "uid", "") or "") if map_obj is not None else ""
        self._state["current_map_uid"] = uid
        self._state["current_finishers"] = []
        self._state["skip_vote_used_on_map"] = ""
        self._state["best_finish_ms_by_login"] = {}
        self._save()
        self._update_status()

    def status_lines(self) -> list[str]:
        base = [
            "Mode: AT points, immediate next map after finisher threshold",
            f"Finishers this map: {len(self._finishers())} / {self._advance_target()}",
            f"Time left: {self._remaining_text()}",
        ]
        top = self._top_points(5)
        if top:
            base.append("Top points: " + ", ".join(f"{n} {p}" for n, p in top))
        else:
            base.append("Top points: (no points yet)")
        return base

    def _finishers(self) -> list[str]:
        out: list[str] = []
        for v in (self._state.get("current_finishers") or []):
            login = str(v or "").strip()
            if login:
                out.append(login)
        return out

    def _advance_target(self) -> int:
        return max(1, int(self._config.get("finishers_to_advance") or 3))

    def _scored_finishers(self) -> int:
        return max(1, int(self._config.get("scored_finishers") or 3))

    def _points_table(self) -> list[int]:
        raw = str(self._config.get("points_csv") or "3,2,1")
        vals: list[int] = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                vals.append(max(0, int(part)))
            except (TypeError, ValueError):
                continue
        return vals or [3, 2, 1]

    def _points_for_place(self, place: int) -> int:
        table = self._points_table()
        if place <= 0:
            return 0
        if place <= len(table):
            return table[place - 1]
        return table[-1]

    def _current_author_time(self) -> int:
        m = getattr(self.ctx.instance.map_manager, "current_map", None)
        if m is None:
            return 0
        try:
            return int(getattr(m, "time_author", 0) or 0)
        except Exception:
            return 0

    def _is_author_finish(self, score_ms: int) -> bool:
        at = self._current_author_time()
        return at > 0 and score_ms > 0 and score_ms <= at

    def _points_state(self) -> dict[str, dict[str, Any]]:
        p = self._state.setdefault("points", {})
        if isinstance(p, dict):
            return p
        self._state["points"] = {}
        return self._state["points"]

    def _best_finish_state(self) -> dict[str, int]:
        raw = self._state.setdefault("best_finish_ms_by_login", {})
        if isinstance(raw, dict):
            return raw
        self._state["best_finish_ms_by_login"] = {}
        return self._state["best_finish_ms_by_login"]

    def _duration_seconds(self) -> int:
        mins = max(1, int(self._config.get("mode_duration_minutes") or 60))
        return mins * 60

    def _remaining_seconds(self) -> int:
        end_ts = int(self._state.get("mode_end_ts") or 0)
        if end_ts <= 0:
            return 0
        return max(0, end_ts - int(time.time()))

    @staticmethod
    def _fmt_mmss(total_s: int) -> str:
        total_s = max(0, int(total_s))
        m = total_s // 60
        s = total_s % 60
        return f"{m:02d}:{s:02d}"

    @staticmethod
    def _fmt_ms_delta(ms: int) -> str:
        ms = max(0, int(ms))
        m = ms // 60000
        s = (ms % 60000) // 1000
        mm = ms % 1000
        return f"{m}:{s:02d}.{mm:03d}"

    def _remaining_text(self) -> str:
        return self._fmt_mmss(self._remaining_seconds())

    async def _check_time_limit(self) -> bool:
        if self._remaining_seconds() > 0:
            return False
        self.ctx.chat("$fc0>> $fffRC Points:$z mode time reached, stopping mode.")
        app = getattr(self.ctx, "_app", None)
        if app is not None:
            try:
                await app._deactivate(announce=True)
            except Exception:
                pass
        return True

    def _add_points(self, login: str, nickname: str, delta: int) -> int:
        pts = self._points_state()
        rec = dict(pts.get(login) or {})
        rec["nickname"] = str(nickname or login)
        rec["points"] = int(rec.get("points") or 0) + int(delta)
        pts[login] = rec
        return int(rec["points"])

    def _top_points(self, n: int) -> list[tuple[str, int]]:
        pts = self._points_state()
        rows: list[tuple[str, int]] = []
        for rec in pts.values():
            name = str(rec.get("nickname") or "player")
            p = int(rec.get("points") or 0)
            rows.append((name, p))
        rows.sort(key=lambda x: (-x[1], x[0].lower()))
        return rows[:max(1, int(n))]

    async def on_player_finish(self, player=None, **kwargs) -> None:
        if await self._check_time_limit():
            return
        if not bool(kwargs.get("is_end_race", False)):
            return
        login = str(getattr(player, "login", "") or "")
        if not login:
            return
        finishers = self._finishers()
        if login in finishers:
            return

        finishers.append(login)
        self._state["current_finishers"] = finishers

        place = len(finishers)
        score_ms = 0
        for k in ("lap_time", "race_time", "time"):
            try:
                score_ms = int(kwargs.get(k) or 0)
            except (TypeError, ValueError):
                score_ms = 0
            if score_ms > 0:
                break

        if score_ms > 0:
            best = self._best_finish_state()
            prev = int(best.get(login) or 0)
            if prev <= 0 or score_ms < prev:
                best[login] = score_ms

        if place <= self._scored_finishers() and self._is_author_finish(score_ms):
            delta = self._points_for_place(place)
            if delta > 0:
                nick = str(getattr(player, "nickname", login) or login)
                total = self._add_points(login, nick, delta)
                self.ctx.chat(
                    f"$fc0>> $fffRC Points:$z {nick} got AT in P{place} "
                    f"(+{delta}, total {total})"
                )

        self._save()
        self._update_status()

        if len(finishers) >= self._advance_target():
            await self._advance_map("finishers")

    async def on_player_chat(self, player=None, text: str = "", **kwargs) -> None:
        if await self._check_time_limit():
            return
        msg = str(text or "").strip().lower()
        if not msg:
            return
        token = msg
        if token.startswith("/") or token.startswith("!"):
            token = token[1:]
        if token not in {"skip", "voteskip", "free_skip", "freeskip"}:
            return

        uid = str(self._state.get("current_map_uid") or "")
        if not uid:
            m = getattr(self.ctx.instance.map_manager, "current_map", None)
            uid = str(getattr(m, "uid", "") or "") if m is not None else ""
            self._state["current_map_uid"] = uid
        if uid and str(self._state.get("skip_vote_used_on_map") or "") == uid:
            await self.ctx.notify("Skip vote already used on this map.", "warning",
                                  login=getattr(player, "login", None))
            return
        if self.ctx.votes.is_active:
            await self.ctx.notify("Another vote is already running.", "warning",
                                  login=getattr(player, "login", None))
            return

        online = list(getattr(self.ctx.instance.player_manager, "online", []) or [])
        min_players = max(1, int(self._config.get("skip_vote_min_players") or 2))
        if len(online) < min_players:
            await self.ctx.notify(
                f"Need at least {min_players} online players to start skip vote.",
                "warning",
                login=getattr(player, "login", None),
            )
            return

        self._state["skip_vote_used_on_map"] = uid
        self._save()
        who = str(getattr(player, "nickname", "A player") or "A player")
        self.ctx.chat(f"$fc0>> $fffRC Points:$z {who} started a free-skip vote.")
        await self.ctx.votes.start(
            key=f"rc_points:skip:{uid or 'map'}",
            title="Free skip this map?",
            options=[
                {"value": "yes", "label": "Yes, skip now"},
                {"value": "no", "label": "No, keep playing"},
            ],
            duration_s=max(5, int(self._config.get("skip_vote_duration_s") or 12)),
            on_finish=self._on_skip_vote_finished,
        )

    async def _on_skip_vote_finished(self, result: dict[str, Any]) -> None:
        tally = dict(result.get("tally") or {})
        yes = int(tally.get("yes") or 0)
        no = int(tally.get("no") or 0)
        online = list(getattr(self.ctx.instance.player_manager, "online", []) or [])
        online_count = max(1, len(online))
        pass_pct = max(1, min(100, int(self._config.get("skip_vote_pass_pct_online") or 50)))
        needed = max(1, int(math.ceil((online_count * pass_pct) / 100.0)))

        if yes >= needed and yes >= no:
            self.ctx.chat(
                f"$fc0>> $fffRC Points:$z skip vote passed ({yes}/{online_count}), switching map."
            )
            await self._advance_map("skip_vote")
            return

        self.ctx.chat(
            f"$fc0>> $fffRC Points:$z skip vote failed ({yes} yes, {no} no; "
            f"need {needed} yes)."
        )

    async def _advance_map(self, reason: str) -> None:
        if self._advance_busy:
            return
        if await self._check_time_limit():
            return
        self._advance_busy = True
        try:
            ok = await self._pick_and_jukebox(triggered_by=f"points:{reason}")
            if not ok:
                return
            await self.ctx.instance.gbx("NextMap")
            self.ctx.chat("$fc0>> $fffRC Points:$z advancing to next map.")
        finally:
            self._advance_busy = False

    async def _apply_widget_layout_overrides(self) -> None:
        if not bool(self._config.get("apply_widget_layout", True)):
            return
        # Start from a clean owner state, then build a mode-specific layout.
        await self.ctx.clear_widget_overrides()

        widgets_app = None
        try:
            widgets_app = getattr(self.ctx.instance.apps, "apps", {}).get("tmsm_widgets")
        except Exception:
            widgets_app = None

        entries = getattr(widgets_app, "entries", {}) if widgets_app is not None else {}

        def _parse_csv(raw: Any) -> list[str]:
            out: list[str] = []
            for part in str(raw or "").split(","):
                k = str(part or "").strip()
                if not k:
                    continue
                if entries and k not in entries:
                    continue
                if k not in out:
                    out.append(k)
            return out

        required = _parse_csv(self._config.get("required_widgets_csv", "random_mx_points"))
        extras = _parse_csv(self._config.get("extra_widgets_csv", ""))
        enabled_keys = list(required)
        for k in extras:
            if k not in enabled_keys:
                enabled_keys.append(k)

        # Hide every registered widget first when requested.
        if bool(self._config.get("hide_all_widgets_first", True)) and entries:
            for key in sorted(entries.keys()):
                await self.ctx.set_widget_override(str(key), enabled=False)

        # Then enable only the configured mode widgets.
        for key in enabled_keys:
            await self.ctx.set_widget_override(key, enabled=True)

        # Dedicated reconfiguration for the points HUD widget.
        if "random_mx_points" in enabled_keys:
            try:
                px = float(self._config.get("random_mx_points_x", -126.0) or -126.0)
            except (TypeError, ValueError):
                px = -126.0
            try:
                py = float(self._config.get("random_mx_points_y", 70.0) or 70.0)
            except (TypeError, ValueError):
                py = 70.0
            try:
                pw = float(self._config.get("random_mx_points_w", 58.0) or 58.0)
            except (TypeError, ValueError):
                pw = 58.0
            try:
                ph = float(self._config.get("random_mx_points_h", 22.0) or 22.0)
            except (TypeError, ValueError):
                ph = 22.0
            dm = str(
                self._config.get("random_mx_points_drive_mode", "fixed") or "fixed"
            ).strip().lower()
            if dm not in ("fixed", "hide_while_driving", "only_shown_while_driving"):
                dm = "fixed"
            ad = str(self._config.get("random_mx_points_anim_dir", "left") or "left").strip().lower()
            if ad not in ("none", "left", "right", "up", "down"):
                ad = "left"
            try:
                anim_dur = int(self._config.get("random_mx_points_anim_duration_ms", 180) or 180)
            except (TypeError, ValueError):
                anim_dur = 180
            try:
                anim_delay = int(self._config.get("random_mx_points_anim_delay_ms", 0) or 0)
            except (TypeError, ValueError):
                anim_delay = 0
            await self.ctx.set_widget_override(
                "random_mx_points",
                enabled=True,
                x=px,
                y=py,
                w=pw,
                h=ph,
                drive_mode=dm,
                anim_dir=ad,
                anim_duration_ms=max(0, anim_dur),
                anim_delay_ms=max(0, anim_delay),
            )

    def widget_snapshot(self, login: str) -> dict[str, Any]:
        points = self._points_state()
        rows: list[dict[str, Any]] = []
        for plogin, rec in points.items():
            rows.append({
                "login": str(plogin or ""),
                "nickname": str(rec.get("nickname") or plogin or "player"),
                "points": int(rec.get("points") or 0),
            })
        rows.sort(key=lambda r: (-int(r["points"]), str(r["nickname"]).lower()))
        for idx, row in enumerate(rows, start=1):
            row["rank"] = idx

        mine = next((r for r in rows if str(r.get("login") or "") == str(login)), None)
        my_rank = int(mine.get("rank") or 0) if mine else 0
        my_points = int(mine.get("points") or 0) if mine else 0

        at_ms = self._current_author_time()
        best = int(self._best_finish_state().get(str(login)) or 0)
        if at_ms <= 0 or best <= 0:
            at_delta_text = "--"
        elif best <= at_ms:
            at_delta_text = f"AT reached (+{self._fmt_ms_delta(at_ms - best)})"
        else:
            at_delta_text = f"Need {self._fmt_ms_delta(best - at_ms)} faster"

        top_rows = rows[:5]
        return {
            "active": True,
            "time_left_s": self._remaining_seconds(),
            "time_left_text": self._remaining_text(),
            "my_rank": my_rank,
            "my_points": my_points,
            "at_delta_text": at_delta_text,
            "top_rows": top_rows,
        }
