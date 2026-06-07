"""TAB Scoreboard replacement.

Replaces the default TM2020 TAB scoreboard with a custom manialink
that shows current map info, time limit, player count, and a live
ranking table. Hold Tab to display.

Data lifecycle:
 - `trackmania:scores` -> snapshot of `best_race_time` (TA) or
   `map_points` (rounds) -> stored in `self._scores`.
 - `trackmania:waypoint` -> live in-race split for TA.
 - `trackmania:give_up` -> mark DNF.
 - `maniaplanet:map_start` -> reset.

The widget engine pushes one manialink per online player, so XML can
embed per-player highlighting (the row of the viewer).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from pyplanet.apps.config import AppConfig
from pyplanet.apps.tmsm.widget_engine.registry import (
    AnimDir,
    Animation,
    GbxReplacement,
    Phase,
    WidgetEntry,
)
from pyplanet.utils import times

logger = logging.getLogger(__name__)


_MANIALINK_ID = "tmsm_tab_scoreboard"
_HIDE_UI_MODULES = ("Race_ScoresTable3", "Race_ScoresTable2", "Race_ScoresTable")

_ROW_LIMIT = 14
_REFRESH_DEBOUNCE_S = 0.2


def _xml_escape(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _country_from_zone(zone: Any) -> str:
    """`World|Europe|Germany|Berlin` -> `Germany`. Accepts plain
    strings and Zone-like objects (`.path`, `.name`, or `str(zone)`).
    Falls back to ''."""
    if not zone:
        return ""
    zone_text = ""
    if isinstance(zone, str):
        zone_text = zone
    else:
        zone_text = (
            getattr(zone, "path", None)
            or getattr(zone, "name", None)
            or str(zone)
        )
    if not zone_text:
        return ""
    parts = [p for p in str(zone_text).split("|") if p]
    if len(parts) >= 3:
        return parts[2]
    return ""


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _elapsed_ms(raw: Any, race_time: Any, kwargs: dict[str, Any], *, end_race: bool) -> int:
    """Best-effort elapsed time extraction across callback payload variants.

    On some multilap flows one field carries lap time while another carries
    full race elapsed; for end-race we pick the largest positive candidate.
    """
    candidates: list[int] = []
    if end_race:
        for key in ("race_cps", "cps"):
            seq = kwargs.get(key)
            if isinstance(seq, (list, tuple)) and seq:
                try:
                    last = int(seq[-1] or 0)
                except (TypeError, ValueError):
                    last = 0
                if last > 0:
                    candidates.append(last)
    if isinstance(raw, dict):
        for key in ("racetime", "race_time", "time", "totaltime", "total_time"):
            v = _to_int(raw.get(key), 0)
            if v > 0:
                candidates.append(v)
    for key in ("race_time", "time", "totaltime", "total_time", "lap_time"):
        v = _to_int(kwargs.get(key), 0)
        if v > 0:
            candidates.append(v)
    v = _to_int(race_time, 0)
    if v > 0:
        candidates.append(v)
    if not candidates:
        return 0
    return max(candidates) if end_race else candidates[0]


class TabScoreboard(AppConfig):
    name = "pyplanet.apps.tmsm.tab_scoreboard"
    label = "tab_scoreboard"

    app_dependencies = ["widget_engine"]

    WIDGET_KEY = "tab_scoreboard"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # login -> {nickname, score, finish, giveup, cps}
        self._scores: dict[str, dict[str, Any]] = {}
        self._is_rounds = False
        self._queued_refresh: asyncio.Task | None = None

    def _is_multi_lap_map(self) -> bool:
        current_map = getattr(self.instance.map_manager, "current_map", None)
        if current_map is None:
            return False
        try:
            return int(getattr(current_map, "num_laps", 0) or 0) > 1
        except (TypeError, ValueError):
            return False

    async def on_start(self) -> None:
        entry = WidgetEntry(
            key=self.WIDGET_KEY,
            name="TAB Scoreboard",
            description="Custom TAB scoreboard (replaces default).",
            icon="table",
            default_x=-80.0,
            default_y=40.0,
            default_w=160.0,
            default_h=80.0,
            animation=Animation(direction=AnimDir.UP, duration_ms=200),
            gbx_replace=GbxReplacement(
                manialink_id=_MANIALINK_ID,
                hide_ui_modules=_HIDE_UI_MODULES,
                hotkey="Tab",
            ),
            # The podium_scoreboard_widget owns the screen during the
            # podium phase; suppress the hold-to-show TAB scoreboard there
            # so the two manialinks don't fight over Race_ScoresTable.
            visible_phases=(
                Phase.LOADING_MAP,
                Phase.WARMUP,
                Phase.PRE_RACE,
                Phase.IN_RACE,
                Phase.POST_RACE,
            ),
        )
        try:
            sig = self.context.signals.get_signal("widget_engine:register")
        except KeyError:
            logger.warning(
                "tab_scoreboard: widget_engine:register signal missing; skipping",
            )
            return
        try:
            await sig.send_robust({"entry": entry, "app": self}, raw=True)
        except Exception:
            logger.exception("tab_scoreboard: register failed")

        for sig_name, handler in (
            ("trackmania:scores", self._on_scores),
            ("trackmania:waypoint", self._on_waypoint),
            ("trackmania:give_up", self._on_giveup),
            ("trackmania:finish", self._on_finish),
            ("maniaplanet:map_start", self._on_map_start),
            ("maniaplanet:player_connect", self._on_player_change),
            ("maniaplanet:player_disconnect", self._on_player_change),
        ):
            try:
                self.context.signals.listen(sig_name, handler)
            except Exception:
                logger.exception("tab_scoreboard: listen '%s' failed", sig_name)

    async def on_stop(self) -> None:
        if self._queued_refresh is not None:
            self._queued_refresh.cancel()
            self._queued_refresh = None

    # ---- signal handlers --------------------------------------------------

    async def _on_map_start(self, **kwargs) -> None:
        self._scores = {}
        self._queue_refresh()

    async def _on_player_change(self, **kwargs) -> None:
        self._queue_refresh()

    async def _on_scores(self, section=None, players=None, **kwargs) -> None:
        if section == "PreEndRound":
            return
        players = players or []
        try:
            mode = (await self.instance.mode_manager.get_current_script()).lower()
        except Exception:
            mode = ""
        self._is_rounds = any(
            t in mode for t in ("rounds", "teams", "cup", "laps")
        )
        new: dict[str, dict[str, Any]] = {}
        for entry in players:
            p = entry.get("player")
            login = str(getattr(p, "login", "") or "")
            if not login:
                continue
            nickname = str(getattr(p, "nickname", login) or login)
            zone = ""
            try:
                zone = getattr(getattr(p, "flow", None), "zone", "") or ""
            except Exception:
                zone = ""
            if self._is_rounds:
                pts = entry.get("map_points")
                if pts is None or int(pts) == -1:
                    continue
                new[login] = {
                    "login": login,
                    "nickname": nickname,
                    "country": _country_from_zone(zone),
                    "score": int(pts),
                    "finish": True,
                    "official_finish": True,
                    "giveup": False,
                }
            else:
                best = entry.get("best_race_time")
                if best is None or int(best) == -1:
                    continue
                prev = self._scores.get(login)
                if prev and bool(prev.get("official_finish")):
                    prev_score = _to_int(prev.get("score"), 0)
                    if prev_score > 0:
                        keep = dict(prev)
                        keep["nickname"] = nickname
                        keep["country"] = _country_from_zone(zone)
                        new[login] = keep
                        continue
                new[login] = {
                    "login": login,
                    "nickname": nickname,
                    "country": _country_from_zone(zone),
                    "score": int(best),
                    "finish": True,
                    "official_finish": False,
                    "giveup": False,
                }
        if new != self._scores:
            self._scores = new
            self._queue_refresh()

    async def _on_waypoint(self, player=None, race_time=None, raw=None, **kwargs) -> None:
        if self._is_rounds:
            return
        if player is None or not isinstance(raw, dict):
            return
        login = str(getattr(player, "login", "") or "")
        if not login:
            return
        nickname = str(getattr(player, "nickname", login) or login)
        zone = ""
        try:
            zone = getattr(getattr(player, "flow", None), "zone", "") or ""
        except Exception:
            zone = ""
        is_end = bool(raw.get("isendrace", False))
        # Match local_rankings behavior: on multilap maps, ignore end-waypoint
        # finish payloads and wait for trackmania:finish.
        if is_end and self._is_multi_lap_map():
            return
        # In multilap flows, waypoint(isendrace=True) may carry a lap-sized
        # time while the official total arrives via trackmania:finish.
        # Prefer race_time when present and keep a marker so finish can
        # overwrite unofficial end-waypoint values.
        rt = _elapsed_ms(raw, race_time, kwargs, end_race=bool(is_end))
        if rt <= 0:
            return
        official = False
        cur = self._scores.get(login)
        if cur is None or is_end:
            # On finish, only overwrite if better.
            if cur is None or not cur.get("finish") or rt < int(cur.get("score") or 0):
                self._scores[login] = {
                    "login": login,
                    "nickname": nickname,
                    "country": _country_from_zone(zone),
                    "score": rt,
                    "finish": is_end,
                    "official_finish": official,
                    "giveup": False,
                    "cps": int(raw.get("checkpointinrace", -1) or -1) + 1,
                }
                self._queue_refresh()
        else:
            # In-race update — only update if no finished score yet.
            if not cur.get("finish"):
                cur.update({
                    "nickname": nickname,
                    "score": rt,
                    "cps": int(raw.get("checkpointinrace", -1) or -1) + 1,
                    "giveup": False,
                })
                self._queue_refresh()

    async def _on_finish(self, player=None, lap_time=None, race_time=None, is_end_race=None, **kwargs) -> None:
        # Some titles only emit finish, not waypoint(isendrace=True).
        if player is None:
            return
        if is_end_race is False:
            return
        login = str(getattr(player, "login", "") or "")
        if not login:
            return
        rt = _to_int(race_time or lap_time, 0)
        if rt <= 0:
            rt = _elapsed_ms(None, race_time, kwargs, end_race=True)
        if rt <= 0:
            return
        cur = self._scores.get(login)
        if cur is None or (not cur.get("official_finish")) or rt < int(cur.get("score") or 0):
            nickname = str(getattr(player, "nickname", login) or login)
            zone = ""
            try:
                zone = getattr(getattr(player, "flow", None), "zone", "") or ""
            except Exception:
                zone = ""
            self._scores[login] = {
                "login": login,
                "nickname": nickname,
                "country": _country_from_zone(zone),
                "score": rt,
                "finish": True,
                "official_finish": True,
                "giveup": False,
            }
            self._queue_refresh()

    async def _on_giveup(self, player=None, **kwargs) -> None:
        login = str(getattr(player, "login", "") or "")
        cur = self._scores.get(login)
        if cur is None or cur.get("finish"):
            return
        if cur.get("giveup"):
            return
        cur["giveup"] = True
        self._queue_refresh()

    # ---- refresh ----------------------------------------------------------

    def _queue_refresh(self) -> None:
        if self._queued_refresh is not None and not self._queued_refresh.done():
            return

        async def _flush() -> None:
            try:
                await asyncio.sleep(_REFRESH_DEBOUNCE_S)
                host = self.instance.apps.apps.get("widget_engine")
                if host is not None:
                    await host.push_replacement(self.WIDGET_KEY)
            except Exception:
                logger.exception("tab_scoreboard: refresh push failed")
            finally:
                self._queued_refresh = None

        self._queued_refresh = asyncio.create_task(_flush())

    # ---- XML build --------------------------------------------------------

    def _sorted_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self._is_rounds:
            # Rounds: scored players first (desc points), then players with no
            # points yet, then spectators. Within groups sort by name.
            def _key_rounds(r: dict[str, Any]):
                name = str(r.get("nickname") or r.get("login") or "").lower()
                if r.get("spectator"):
                    return (3, 0, name)
                if r.get("score") is None:
                    return (2, 0, name)
                return (1, -_to_int(r.get("score"), 0), name)

            rows.sort(key=_key_rounds)
            return rows

        # TimeAttack-like: finished times first, then in-race entries,
        # then no-time players, then DNF, then spectators.
        def _key_ta(r: dict[str, Any]):
            name = str(r.get("nickname") or r.get("login") or "").lower()
            if r.get("spectator"):
                return (5, 0, 0, name)
            if r.get("giveup"):
                return (4, 0, 0, name)
            score = r.get("score")
            if score is None:
                return (3, 0, 0, name)
            if r.get("finish"):
                return (1, _to_int(score, 0), 0, name)
            return (2, -_to_int(r.get("cps"), 0), _to_int(score, 0), name)

        rows.sort(key=_key_ta)
        return rows

    def _format_score(self, r: dict[str, Any]) -> str:
        if r.get("spectator"):
            return "SPEC"
        if r.get("giveup"):
            return "DNF"
        raw = r.get("score")
        if raw is None:
            return "-"
        s = _to_int(raw, 0)
        if self._is_rounds:
            return str(s)
        return times.format_time(s)

    @staticmethod
    def _medal_substyle_for_time(
        r: dict[str, Any],
        author_ms: int,
        gold_ms: int,
        silver_ms: int,
        bronze_ms: int,
        points_mode: bool,
    ) -> str:
        """Return MedalsBig substyle token for a row time.

        Priority: author > gold > silver > bronze. Empty string means no
        medal (do not draw indicator).
        """
        if points_mode or r.get("spectator") or r.get("giveup"):
            return ""
        if not r.get("finish"):
            return ""
        score = r.get("score")
        if score is None:
            return ""
        t = _to_int(score, 0)
        if t <= 0:
            return ""
        if author_ms > 0 and t <= author_ms:
            return "MedalNadeo"
        if gold_ms > 0 and t <= gold_ms:
            return "MedalGold"
        if silver_ms > 0 and t <= silver_ms:
            return "MedalSilver"
        if bronze_ms > 0 and t <= bronze_ms:
            return "MedalBronze"
        return ""

    async def build_replacement_xml(self, login: str) -> str:
        try:
            return await self._build_replacement_xml_impl(login)
        except Exception:
            logger.exception("tab_scoreboard: build_replacement_xml failed")
            return (
                '<label pos="80 -8" size="156 8" halign="center" valign="center2" '
                'textsize="1.6" textcolor="ff8080ff" textfont="GameFontBlack" '
                'text="TAB Scoreboard render error"/>'
            )

    async def _build_replacement_xml_impl(self, login: str) -> str:
        # Map info.
        try:
            cm = self.instance.map_manager.current_map
            map_name = _xml_escape(getattr(cm, "name", "?") or "?")
            map_author = _xml_escape(
                getattr(cm, "author_nickname", "") or getattr(cm, "author_login", "?") or "?"
            )
            author_ms = _to_int(getattr(cm, "time_author", 0), 0)
            gold_ms = _to_int(getattr(cm, "time_gold", 0), 0)
            silver_ms = _to_int(getattr(cm, "time_silver", 0), 0)
            bronze_ms = _to_int(getattr(cm, "time_bronze", 0), 0)
        except Exception:
            map_name, map_author = "?", "?"
            author_ms = gold_ms = silver_ms = bronze_ms = 0
        # Player count.
        try:
            online = list(self.instance.player_manager.online)
        except Exception:
            online = []
        n_players = sum(
            1 for p in online
            if not getattr(getattr(p, "flow", None), "is_spectator", False)
        )
        n_specs = len(online) - n_players
        # Mode + time limit.
        try:
            mode = (await self.instance.mode_manager.get_current_script()) or "?"
        except Exception:
            mode = "?"
        mode_label = _xml_escape(mode.split("_")[-1] if "_" in mode else mode)

        # Merge online players with live score snapshots so everyone online
        # appears in the table even before setting a time.
        rows_src: list[dict[str, Any]] = []
        seen: set[str] = set()
        for p in online:
            plogin = str(getattr(p, "login", "") or "")
            if not plogin or plogin in seen:
                continue
            seen.add(plogin)
            pnick = str(getattr(p, "nickname", plogin) or plogin)
            pzone = ""
            try:
                pzone = getattr(getattr(p, "flow", None), "zone", "") or ""
            except Exception:
                pzone = ""
            pspec = bool(getattr(getattr(p, "flow", None), "is_spectator", False))
            base = dict(self._scores.get(plogin) or {})
            base["login"] = plogin
            base["nickname"] = pnick
            base["country"] = base.get("country") or _country_from_zone(pzone)
            base.setdefault("score", None)
            base.setdefault("finish", False)
            base.setdefault("giveup", False)
            base["spectator"] = pspec
            rows_src.append(base)

        # Keep any scored rows that might not be in `online` temporarily.
        for plogin, pdata in self._scores.items():
            if plogin in seen:
                continue
            extra = dict(pdata)
            extra.setdefault("login", plogin)
            extra.setdefault("nickname", plogin)
            extra.setdefault("country", "")
            extra.setdefault("spectator", False)
            rows_src.append(extra)

        rows = self._sorted_rows(rows_src)
        score_label = "PTS" if self._is_rounds else "TIME"

        # ---- Layout ----
        # Size is driven by replacement editor values.
        host = self.instance.apps.apps.get("widget_engine")
        resolved = None
        if host is not None:
            try:
                resolved = host.engine.resolve(self.WIDGET_KEY, login)
            except Exception:
                resolved = None
        widget_w = float(getattr(resolved, "w", 160.0) or 160.0)
        widget_h = float(getattr(resolved, "h", 80.0) or 80.0)

        pad_x = 2.0
        inner_w = max(40.0, widget_w - (pad_x * 2.0))

        # Header block.
        header_h = 17.0
        top_y = -1.0
        map_icon_size = 5.2
        players_badge_w = 30.0

        # Table block (always spans full inner width).
        table_top = top_y - header_h - 0.8
        table_h = max(18.0, widget_h - (header_h + 3.0))
        row_h = 4.0
        header_row_h = 4.0
        data_start_y = table_top - header_row_h
        max_rows_fit = max(1, int((table_h - header_row_h) // row_h))
        max_rows = min(_ROW_LIMIT, max_rows_fit)

        # Column sizing scales with widget width.
        rank_w = 10.0
        flag_w = 9.0
        time_w = max(24.0, inner_w * 0.24)
        player_w = max(12.0, inner_w - rank_w - flag_w - time_w)

        col_rank_x = pad_x + 2.0
        col_flag_x = pad_x + rank_w + 1.0
        col_player_x = pad_x + rank_w + flag_w + 1.5
        col_time_x = pad_x + inner_w - 1.5

        parts: list[str] = []

        # Header background with subtle split.
        parts.append(
            f'<quad pos="{pad_x} {top_y}" size="{inner_w} {header_h}" '
            f'halign="left" valign="top" bgcolor="00000050"/>'
        )
        parts.append(
            f'<quad pos="{pad_x} {top_y - (header_h / 2.0)}" size="{inner_w} {header_h / 2.0}" '
            f'halign="left" valign="top" bgcolor="ffffff08"/>'
        )

        # Map badge (left) with built-in icon.
        parts.append(
            f'<quad pos="{pad_x + 1.0} {top_y - 1.0}" size="{map_icon_size} {map_icon_size}" '
            f'halign="left" valign="top" bgcolor="ffae00aa"/>'
        )
        parts.append(
            f'<quad pos="{pad_x + 1.55} {top_y - 1.55}" size="{map_icon_size - 1.1} {map_icon_size - 1.1}" '
            f'halign="left" valign="top" style="Icons64x64_1" substyle="TrackInfo"/>'
        )

        title_x = pad_x + 1.0 + map_icon_size + 1.6
        title_w = max(20.0, inner_w - (title_x - pad_x) - players_badge_w - 2.0)
        parts.append(
            f'<label pos="{title_x} {top_y - 2.2}" size="{title_w} 6" '
            f'halign="left" valign="center2" textsize="2.4" '
            f'textcolor="ffffffff" textfont="GameFontBlack" text="{map_name}"/>'
        )
        parts.append(
            f'<label pos="{title_x} {top_y - 7.1}" size="{title_w} 4" '
            f'halign="left" valign="center2" textsize="1.2" '
            f'textcolor="b0b0b0ff" text="by {map_author}  $888|$aaa  {mode_label}"/>'
        )

        # Player count badge (right) with built-in player icon.
        badge_x = pad_x + inner_w - players_badge_w
        parts.append(
            f'<quad pos="{badge_x} {top_y - 1.0}" size="{players_badge_w - 1.0} 7.0" '
            f'halign="left" valign="top" bgcolor="00000066"/>'
        )
        parts.append(
            f'<quad pos="{badge_x + 1.1} {top_y - 1.9}" size="3.3 3.3" '
            f'halign="left" valign="top" style="Icons64x64_1" substyle="IconPlayers"/>'
        )
        parts.append(
            f'<label pos="{badge_x + 5.2} {top_y - 3.9}" size="{players_badge_w - 7.0} 4" '
            f'halign="left" valign="center2" textsize="1.3" textfont="GameFontBlack" '
            f'textcolor="ffffffff" text="{n_players}"/>'
        )
        if n_specs:
            parts.append(
                f'<label pos="{badge_x + 5.2} {top_y - 6.4}" size="{players_badge_w - 7.0} 3" '
                f'halign="left" valign="center2" textsize="0.9" textcolor="aaaaaaff" '
                f'text="{n_specs} spec"/>'
            )

        # Table header background spanning full width.
        parts.append(
            f'<quad pos="{pad_x} {table_top}" size="{inner_w} {header_row_h}" '
            f'halign="left" valign="top" bgcolor="00000075"/>'
        )
        parts.append(
            f'<label pos="{col_rank_x} {table_top - 2.0}" size="{rank_w} 4" '
            f'halign="left" valign="center2" textsize="1" textfont="GameFontBlack" '
            f'textcolor="ffae00ff" text="#"/>'
        )
        parts.append(
            f'<label pos="{col_flag_x} {table_top - 2.0}" size="{flag_w} 4" '
            f'halign="left" valign="center2" textsize="1" textfont="GameFontBlack" '
            f'textcolor="ffae00ff" text="FLAG"/>'
        )
        parts.append(
            f'<label pos="{col_player_x} {table_top - 2.0}" size="{player_w} 4" '
            f'halign="left" valign="center2" textsize="1" textfont="GameFontBlack" '
            f'textcolor="ffae00ff" text="PLAYER"/>'
        )
        parts.append(
            f'<label pos="{col_time_x} {table_top - 2.0}" size="{time_w} 4" '
            f'halign="right" valign="center2" textsize="1" textfont="GameFontBlack" '
            f'textcolor="ffae00ff" text="{score_label}"/>'
        )

        # Data rows.
        if not rows:
            parts.append(
                f'<label pos="{pad_x + (inner_w / 2.0)} {data_start_y - 9}" size="{inner_w - 2.0} 6" '
                f'halign="center" valign="center2" textsize="1.3" textcolor="888888ff" '
                f'text="No times recorded yet."/>'
            )
        else:
            for i, r in enumerate(rows[:max_rows]):
                rank = i + 1
                row_y = data_start_y - (i * row_h)
                is_me = r.get("login") == login
                bg = "ffae0033" if is_me else ("ffffff10" if i % 2 == 0 else "00000020")
                parts.append(
                    f'<quad pos="{pad_x} {row_y}" size="{inner_w} {row_h}" '
                    f'halign="left" valign="top" bgcolor="{bg}"/>'
                )
                rank_color = "ffae00ff" if rank <= 3 else "ffffffff"
                parts.append(
                    f'<label pos="{col_rank_x} {row_y - 2.0}" size="{rank_w} 4" '
                    f'halign="left" valign="center2" textsize="1.1" textfont="GameFontBlack" '
                    f'textcolor="{rank_color}" text="{rank}"/>'
                )
                country = r.get("country") or ""
                if country:
                    flag_url = f"file://Media/Flags/{_xml_escape(country)}.dds"
                else:
                    flag_url = "file://Media/Flags/World.dds"
                parts.append(
                    f'<quad pos="{col_flag_x} {row_y - 0.4}" size="6 3.6" '
                    f'halign="left" valign="top" image="{flag_url}"/>'
                )
                nick = _xml_escape(r.get("nickname") or r.get("login") or "?")
                target_login = _xml_escape(r.get("login") or "")
                eye_size = 3.2
                eye_x = col_player_x + player_w - 4.3
                parts.append(
                    f'<label pos="{col_player_x} {row_y - 2.0}" size="{player_w - 5.0} 4" '
                    f'halign="left" valign="center2" textsize="1.1" textcolor="ffffffff" '
                    f'text="{nick}"/>'
                )
                if target_login and not r.get("spectator"):
                    parts.append(
                        f'<frame pos="{eye_x} {row_y - 0.4}" data-login="{target_login}">'
                        f'<quad pos="0 0" size="{eye_size} {eye_size}" '
                        f'halign="left" valign="top" z-index="7" '
                        f'class="toggleSpec" scriptevents="1" '
                        f'style="Icons64x64_1" substyle="ShowRight2"/>'
                        f'</frame>'
                    )
                score_color = (
                    "aaaaaaff" if r.get("giveup") else
                    ("ffffffff" if r.get("finish") else "ffae00ff")
                )
                medal_substyle = self._medal_substyle_for_time(
                    r,
                    author_ms=author_ms,
                    gold_ms=gold_ms,
                    silver_ms=silver_ms,
                    bronze_ms=bronze_ms,
                    points_mode=self._is_rounds,
                )
                if medal_substyle:
                    # Dedicated medal slot inside the time column (left side),
                    # keeping clear separation from right-aligned time text.
                    medal_x = (col_time_x - time_w) + 1.2
                    medal_y = row_y - 0.2
                    parts.append(
                        f'<quad pos="{medal_x} {medal_y}" size="3.6 3.6" '
                        f'halign="left" valign="top" z-index="9" '
                        f'style="MedalsBig" substyle="{medal_substyle}"/>'
                    )
                parts.append(
                    f'<label pos="{col_time_x} {row_y - 2.0}" size="{time_w} 4" '
                    f'halign="right" valign="center2" z-index="10" textsize="1.1" textfont="GameFontBlack" '
                    f'textcolor="{score_color}" text="{self._format_score(r)}"/>'
                )

        return "".join(parts)
