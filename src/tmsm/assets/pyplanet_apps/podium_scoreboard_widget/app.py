"""Podium scoreboard widget.

A GBX manialink replacement that shows the final race results during
the podium phase. Modelled on ``tab_scoreboard`` but:

  * registers as a :class:`WidgetAppBase` widget (consistent with
    ``podium_countdown_widget``),
  * is restricted to :attr:`Phase.IN_PODIUM` via ``WIDGET_VISIBLE_PHASES``
    so the engine pushes it automatically on podium start and clears it
    when the next map loads, and
  * has no hotkey — the widget chrome stays visible for the whole podium
    without any client-side toggle.

Data lifecycle is identical to tab_scoreboard: the addon snapshots
``trackmania:scores`` and live waypoints/finishes/giveups into
``self._scores`` so that by the time ``Phase.IN_PODIUM`` is entered we
have a complete final table ready to render.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

from pyplanet.apps.tmsm.widget_engine import AnimDir, DriveMode
from pyplanet.apps.tmsm.widget_engine.registry import (
    GbxReplacement,
    Phase,
    WidgetEntry,
    WidgetKind,
)
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase
from pyplanet.utils import times

logger = logging.getLogger(__name__)


_MANIALINK_ID = "tmsm_podium_scoreboard"
# Title-pack scoreboard UI modules. Hiding them keeps the default
# in-race scoreboard from briefly flashing on top of our manialink if it
# is still mounted when podium starts.
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
    if not zone:
        return ""
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


class PodiumScoreboardWidgetApp(WidgetAppBase):
    name = "pyplanet.apps.tmsm.podium_scoreboard_widget"
    label = "podium_scoreboard_widget"

    WIDGET_KEY = "podium_scoreboard_widget"
    WIDGET_NAME = "Podium Scoreboard"
    WIDGET_DESCRIPTION = "Final scoreboard shown during the podium phase."
    WIDGET_ICON = "trophy"

    WIDGET_DEFAULT_X = -80.0
    WIDGET_DEFAULT_Y = 40.0
    WIDGET_DEFAULT_W = 160.0
    WIDGET_DEFAULT_H = 80.0

    # GBX replacement only: the engine should not render the regular
    # persistent frame manialink for this addon.
    WIDGET_KIND = WidgetKind.POPUP

    WIDGET_DRIVE_MODE = DriveMode.FIXED
    WIDGET_ANIM_DIR = AnimDir.UP
    WIDGET_ANIM_DURATION_MS = 200

    WIDGET_VISIBLE_PHASES = (Phase.IN_PODIUM,)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
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

    def build_entry(self) -> WidgetEntry:
        entry = super().build_entry()
        return replace(
            entry,
            gbx_replace=GbxReplacement(
                manialink_id=_MANIALINK_ID,
                hide_ui_modules=_HIDE_UI_MODULES,
                # No hold-to-show key: widget should be permanently
                # visible while the podium phase is active.
                hotkey=None,
            ),
        )

    # ---- lifecycle --------------------------------------------------------

    async def on_start(self) -> None:
        await super().on_start()
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
                logger.exception(
                    "podium_scoreboard_widget: listen '%s' failed", sig_name,
                )

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
        # Match local_rankings behavior: on multilap TA-like maps the
        # score snapshot can carry lap PBs, so rely on finish callbacks.
        if self._is_multi_lap_map() and not self._is_rounds:
            return
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
        if is_end and self._is_multi_lap_map():
            return
        rt = _elapsed_ms(raw, race_time, kwargs, end_race=bool(is_end))
        if rt <= 0:
            return
        official = False
        cur = self._scores.get(login)
        if cur is None or is_end:
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
            if not cur.get("finish"):
                cur.update({
                    "nickname": nickname,
                    "score": rt,
                    "cps": int(raw.get("checkpointinrace", -1) or -1) + 1,
                    "giveup": False,
                })
                self._queue_refresh()

    async def _on_finish(self, player=None, lap_time=None, race_time=None, is_end_race=None, **kwargs) -> None:
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
                if self.engine is None:
                    return
                # Only push while the engine is actually in the podium
                # phase — outside of it the resolver disables the entry
                # anyway, but skipping the call avoids needless XML work.
                if self.engine.current_phase != Phase.IN_PODIUM:
                    return
                await self.engine.push_replacement(self.WIDGET_KEY)
            except Exception:
                logger.exception(
                    "podium_scoreboard_widget: refresh push failed",
                )
            finally:
                self._queued_refresh = None

        self._queued_refresh = asyncio.create_task(_flush())

    # ---- XML build --------------------------------------------------------

    def _sorted_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self._is_rounds:
            def _key_rounds(r: dict[str, Any]):
                name = str(r.get("nickname") or r.get("login") or "").lower()
                if r.get("spectator"):
                    return (3, 0, name)
                if r.get("score") is None:
                    return (2, 0, name)
                return (1, -_to_int(r.get("score"), 0), name)

            rows.sort(key=_key_rounds)
            return rows

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
            logger.exception(
                "podium_scoreboard_widget: build_replacement_xml failed",
            )
            return (
                '<label pos="80 -8" size="156 8" halign="center" valign="center2" '
                'textsize="1.6" textcolor="ff8080ff" textfont="GameFontBlack" '
                'text="Podium Scoreboard render error"/>'
            )

    async def _build_replacement_xml_impl(self, login: str) -> str:
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
        try:
            online = list(self.instance.player_manager.online)
        except Exception:
            online = []
        n_players = sum(
            1 for p in online
            if not getattr(getattr(p, "flow", None), "is_spectator", False)
        )
        n_specs = len(online) - n_players
        try:
            mode = (await self.instance.mode_manager.get_current_script()) or "?"
        except Exception:
            mode = "?"
        mode_label = _xml_escape(mode.split("_")[-1] if "_" in mode else mode)

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

        host = self.instance.apps.apps.get("widget_engine")
        resolved = None
        if host is not None:
            try:
                resolved = host.engine.resolve(self.WIDGET_KEY, login)
            except Exception:
                resolved = None
        widget_w = float(getattr(resolved, "w", self.WIDGET_DEFAULT_W) or self.WIDGET_DEFAULT_W)
        widget_h = float(getattr(resolved, "h", self.WIDGET_DEFAULT_H) or self.WIDGET_DEFAULT_H)

        pad_x = 2.0
        inner_w = max(40.0, widget_w - (pad_x * 2.0))

        header_h = 17.0
        top_y = -1.0
        map_icon_size = 5.2
        players_badge_w = 30.0

        table_top = top_y - header_h - 0.8
        table_h = max(18.0, widget_h - (header_h + 3.0))
        row_h = 4.0
        header_row_h = 4.0
        data_start_y = table_top - header_row_h
        max_rows_fit = max(1, int((table_h - header_row_h) // row_h))
        max_rows = min(_ROW_LIMIT, max_rows_fit)

        rank_w = 10.0
        flag_w = 9.0
        time_w = max(24.0, inner_w * 0.24)
        player_w = max(12.0, inner_w - rank_w - flag_w - time_w)

        col_rank_x = pad_x + 2.0
        col_flag_x = pad_x + rank_w + 1.0
        col_player_x = pad_x + rank_w + flag_w + 1.5
        col_time_x = pad_x + inner_w - 1.5

        parts: list[str] = []

        parts.append(
            f'<quad pos="{pad_x} {top_y}" size="{inner_w} {header_h}" '
            f'halign="left" valign="top" bgcolor="00000050"/>'
        )
        parts.append(
            f'<quad pos="{pad_x} {top_y - (header_h / 2.0)}" size="{inner_w} {header_h / 2.0}" '
            f'halign="left" valign="top" bgcolor="ffffff08"/>'
        )

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

        # Podium badge instead of player count to make the widget visually
        # distinct from the in-race TAB scoreboard.
        badge_x = pad_x + inner_w - players_badge_w
        parts.append(
            f'<quad pos="{badge_x} {top_y - 1.0}" size="{players_badge_w - 1.0} 7.0" '
            f'halign="left" valign="top" bgcolor="ffae0088"/>'
        )
        parts.append(
            f'<quad pos="{badge_x + 1.1} {top_y - 1.9}" size="3.3 3.3" '
            f'halign="left" valign="top" style="BgRaceScore2" substyle="Podium"/>'
        )
        parts.append(
            f'<label pos="{badge_x + 5.2} {top_y - 3.9}" size="{players_badge_w - 7.0} 4" '
            f'halign="left" valign="center2" textsize="1.3" textfont="GameFontBlack" '
            f'textcolor="ffffffff" text="PODIUM"/>'
        )
        parts.append(
            f'<label pos="{badge_x + 5.2} {top_y - 6.4}" size="{players_badge_w - 7.0} 3" '
            f'halign="left" valign="center2" textsize="0.9" textcolor="222222ff" '
            f'text="{n_players}p  {n_specs}s"/>'
        )

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

        if not rows:
            parts.append(
                f'<label pos="{pad_x + (inner_w / 2.0)} {data_start_y - 9}" size="{inner_w - 2.0} 6" '
                f'halign="center" valign="center2" textsize="1.3" textcolor="888888ff" '
                f'text="No times recorded."/>'
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
