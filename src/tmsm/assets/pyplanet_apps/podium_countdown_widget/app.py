"""Podium countdown widget.

Shows the countdown from map end until podium starts. The timer is signal-
driven and refreshes once per second.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import time
from typing import Any

from pyplanet.contrib.setting import Setting

from pyplanet.apps.tmsm.widget_engine import AnimDir, DriveMode
from pyplanet.apps.tmsm.widget_engine.registry import (
    GbxReplacement,
    Phase,
    WidgetEntry,
    WidgetKind,
)
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase


class PodiumCountdownWidgetApp(WidgetAppBase):
    name = "pyplanet.apps.tmsm.podium_countdown_widget"
    label = "podium_countdown_widget"

    WIDGET_KEY = "podium_countdown_widget"
    WIDGET_NAME = "Podium Countdown"
    WIDGET_DESCRIPTION = "Countdown to podium after map end."
    WIDGET_ICON = "hourglass-half"
    WIDGET_TEMPLATE = "podium_countdown_widget/podium_countdown.xml"

    WIDGET_DEFAULT_X = 0.0
    WIDGET_DEFAULT_Y = 66.0
    WIDGET_DEFAULT_W = 36.0
    WIDGET_DEFAULT_H = 10.0

    WIDGET_REFRESH_SECONDS = 1.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_DRIVE_MODE = DriveMode.FIXED
    WIDGET_ANIM_DIR = AnimDir.RIGHT
    WIDGET_ANIM_DURATION_MS = 180
    WIDGET_ANIM_IN_DELAY_MS = 0
    WIDGET_ANIM_OUT_DELAY_MS = 0
    WIDGET_VISIBLE_PHASES = (
        Phase.WARMUP,
        Phase.PRE_RACE,
        Phase.IN_RACE,
        Phase.POST_RACE,
    )
    # GBX replacement-only widget: do not render the normal persistent frame.
    WIDGET_KIND = WidgetKind.POPUP

    WIDGET_STRIP_COLOR = "f59e0bff"
    WIDGET_BG_COLOR = "1b1f2a88"

    DEFAULT_DELAY_SECONDS = 8
    _MANIALINK_ID = "tmsm_podium_countdown"
    _HIDE_UI_MODULES = (
        "Race_Chrono",
        "Race_Chrono2",
        "Race_ChronoTable",
        "Race_Countdown",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._podium_eta_monotonic: float | None = None
        # Most recently observed total duration (race time limit or chat
        # time). Used by the reconcile loop to detect time-extend votes.
        self._baseline_total_seconds: int | None = None
        self._reconcile_task: asyncio.Task | None = None

        self.setting_delay_seconds = Setting(
            "podium_delay_seconds",
            "Podium Delay Seconds",
            Setting.CAT_BEHAVIOUR,
            type=int,
            default=self.DEFAULT_DELAY_SECONDS,
            description="Fallback seconds used when map_end does not expose countdown data.",
        )

    async def _timelimit_unit_hint(self) -> str:
        """Return one of: 'minutes', 'seconds', 'milliseconds', or ''."""
        try:
            info = await self.instance.gbx("GetModeScriptInfo")
        except Exception:
            return ""
        if not isinstance(info, dict):
            return ""
        for p in list(info.get("ParamDescs") or []):
            if not isinstance(p, dict):
                continue
            name = str(p.get("Name") or "")
            if "timelimit" not in name.lower():
                continue
            txt = f"{p.get('Type', '')} {p.get('Desc', '')}".lower()
            if "millisecond" in txt:
                return "milliseconds"
            if "second" in txt:
                return "seconds"
            if "minute" in txt:
                return "minutes"
        return ""

    @staticmethod
    def _normalize_timelimit_seconds(raw: Any, unit_hint: str = "") -> int | None:
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None
        if val <= 0:
            return None

        hint = str(unit_hint or "").lower()
        if hint == "minutes":
            secs = val * 60.0
        elif hint == "milliseconds":
            secs = val / 1000.0
        elif hint == "seconds":
            secs = val
        else:
            # TM2020 S_TimeLimit is in seconds. Only treat the value as
            # milliseconds when it is implausibly large for a race
            # countdown (>24h in seconds).
            secs = (val / 1000.0) if val > 86400 else val

        out = int(round(secs))
        if out <= 0:
            return None
        return max(1, min(86400, out))

    @staticmethod
    def _normalize_postrace_seconds(raw: Any) -> int | None:
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None
        if val <= 0:
            return None
        # Chat/finish timeout is usually seconds or milliseconds.
        secs = (val / 1000.0) if val >= 1000 else val
        out = int(round(secs))
        if out <= 0:
            return None
        return max(1, min(3600, out))

    async def _time_limit_from_mode_settings(self) -> int | None:
        unit_hint = await self._timelimit_unit_hint()
        try:
            settings = await self.instance.gbx("GetModeScriptSettings")
        except Exception:
            settings = None

        if isinstance(settings, dict):
            preferred_keys = (
                "S_TimeLimit",
                "TimeLimit",
                "S_TimeLimitSeconds",
            )
            for key in preferred_keys:
                if key in settings:
                    parsed = self._normalize_timelimit_seconds(settings.get(key), unit_hint)
                    if parsed is not None:
                        return parsed
            for key, value in settings.items():
                k = str(key or "")
                if "timelimit" in k.lower():
                    parsed = self._normalize_timelimit_seconds(value, unit_hint)
                    if parsed is not None:
                        return parsed

        try:
            gi = await self.instance.gbx("GetCurrentGameInfo")
        except Exception:
            gi = None
        if isinstance(gi, dict) and "TimeLimit" in gi:
            parsed = self._normalize_timelimit_seconds(gi.get("TimeLimit"), unit_hint)
            if parsed is not None:
                return parsed
        return None

    async def _post_race_from_mode_settings(self) -> int | None:
        try:
            settings = await self.instance.gbx("GetModeScriptSettings")
        except Exception:
            settings = None

        if isinstance(settings, dict):
            preferred_keys = (
                "S_ChatTime",
                "ChatTime",
                "S_FinishTimeout",
                "FinishTimeout",
            )
            for key in preferred_keys:
                if key in settings:
                    parsed = self._normalize_postrace_seconds(settings.get(key))
                    if parsed is not None:
                        return parsed
            for key, value in settings.items():
                k = str(key or "")
                kl = k.lower()
                if "chattime" in kl or "finishtimeout" in kl:
                    parsed = self._normalize_postrace_seconds(value)
                    if parsed is not None:
                        return parsed

        try:
            gi = await self.instance.gbx("GetCurrentGameInfo")
        except Exception:
            gi = None
        if isinstance(gi, dict):
            for key in ("ChatTime", "FinishTimeout"):
                if key in gi:
                    parsed = self._normalize_postrace_seconds(gi.get(key))
                    if parsed is not None:
                        return parsed
        return None

    async def _delay_from_mode_settings(self) -> int | None:
        # Legacy alias: prefer the race time limit for main countdown.
        return await self._time_limit_from_mode_settings()

    def build_entry(self) -> WidgetEntry:
        entry = super().build_entry()
        return replace(
            entry,
            gbx_replace=GbxReplacement(
                manialink_id=self._MANIALINK_ID,
                hide_ui_modules=self._HIDE_UI_MODULES,
                # Widget paints its own background and ships its own
                # ManiaScript for client-side ticking; the engine chrome
                # would nest our <script> inside frames and ManiaScript
                # would silently drop it.
                chrome=False,
            ),
        )

    async def on_start(self) -> None:
        await super().on_start()

        await self.context.setting.register(self.setting_delay_seconds)

        try:
            self.context.signals.listen("maniaplanet:map_end", self._on_map_end)
            self.context.signals.listen("maniaplanet:podium_start", self._on_podium_start)
            self.context.signals.listen("maniaplanet:map_start", self._on_map_start)
            self.context.signals.listen("maniaplanet:map_begin", self._on_map_start)
            self.context.signals.listen("maniaplanet:player_connect", self._on_player_connect)
        except Exception:
            pass

        self._reconcile_task = asyncio.create_task(self._reconcile_loop())

    async def on_stop(self) -> None:
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            self._reconcile_task = None
        await super().on_stop()

    async def _reconcile_loop(self) -> None:
        """Periodically re-read the active duration from mode settings so
        that a mid-race time-extend vote (which raises S_TimeLimit) is
        picked up and the on-screen countdown is shifted accordingly.
        Polling is cheap and avoids depending on a vote-specific signal."""
        try:
            while True:
                await asyncio.sleep(5.0)
                if self._podium_eta_monotonic is None or self._baseline_total_seconds is None:
                    continue
                phase = self.engine.current_phase if self.engine else None
                if phase == Phase.POST_RACE:
                    new_total = await self._post_race_from_mode_settings()
                else:
                    new_total = await self._time_limit_from_mode_settings()
                if new_total is None or new_total == self._baseline_total_seconds:
                    continue
                delta = new_total - self._baseline_total_seconds
                self._podium_eta_monotonic += float(delta)
                self._baseline_total_seconds = new_total
                await self._push_replacement()
        except asyncio.CancelledError:
            pass

    async def _push_replacement(self, logins: list[str] | None = None) -> None:
        if self.engine is None:
            return
        try:
            await self.engine.push_replacement(self.WIDGET_KEY, logins=logins)
        except Exception:
            pass

    async def _configured_delay(self) -> int:
        mode_delay = await self._time_limit_from_mode_settings()
        if mode_delay is not None:
            return mode_delay
        try:
            value = int(await self.setting_delay_seconds.get_value() or self.DEFAULT_DELAY_SECONDS)
        except Exception:
            value = self.DEFAULT_DELAY_SECONDS
        return max(1, min(86400, value))

    async def _on_map_end(self, **kwargs) -> None:
        total = await self._post_race_from_mode_settings()
        if total is None:
            total = await self._configured_delay()
        self._baseline_total_seconds = int(total)
        self._podium_eta_monotonic = time.monotonic() + float(total)
        await self._push_replacement()

    async def _on_podium_start(self, **kwargs) -> None:
        self._podium_eta_monotonic = None
        self._baseline_total_seconds = None
        await self._push_replacement()

    async def _on_map_start(self, **kwargs) -> None:
        total = await self._configured_delay()
        self._baseline_total_seconds = int(total)
        self._podium_eta_monotonic = time.monotonic() + float(total)
        await self._push_replacement()

    async def _on_player_connect(self, player=None, **kwargs) -> None:
        login = getattr(player, "login", None) if player is not None else None
        if not login or self._podium_eta_monotonic is None:
            return
        await self._push_replacement(logins=[login])

    def _remaining_ms(self) -> int | None:
        if self._podium_eta_monotonic is None:
            return None
        return int(max(0.0, self._podium_eta_monotonic - time.monotonic()) * 1000.0)

    async def build_replacement_xml(self, login: str) -> str:
        if self.engine is None:
            return ""

        remaining_ms = self._remaining_ms()
        if remaining_ms is None:
            # No active countdown (podium phase or just-cleared): return
            # an empty body so the engine sends an empty manialink and
            # clears the on-screen override.
            return ""

        resolved = self.engine.resolve(self.WIDGET_KEY, login)
        x = float(getattr(resolved, "x", self.WIDGET_DEFAULT_X) or 0.0)
        y = float(getattr(resolved, "y", self.WIDGET_DEFAULT_Y) or 0.0)
        w = float(getattr(resolved, "w", self.WIDGET_DEFAULT_W) or self.WIDGET_DEFAULT_W)
        h = float(getattr(resolved, "h", self.WIDGET_DEFAULT_H) or self.WIDGET_DEFAULT_H)

        time_size = (h * 0.46) if h > 7 else (h * 0.42)
        time_pos_x = w * 0.5
        time_pos_y = h * 0.62

        # Client-side ticking: bake the remaining-ms into the manialink and
        # let ManiaScript decrement once per second using CurrentTime as the
        # local monotonic clock. This means a single push covers the entire
        # countdown — we only re-push on real events (map start/end, podium
        # start, player connect, time-extend reconciliation).
        script = (
            '<script><!--\n'
            '#Include "TextLib" as TL\n'
            'main() {\n'
            '  declare CMlLabel Lbl <=> (Page.GetFirstChild("podium_countdown_value") as CMlLabel);\n'
            '  if (Lbl == Null) return;\n'
            f'  declare Integer RemainingMs = {remaining_ms};\n'
            '  declare Integer StartTick = CurrentTime;\n'
            '  declare Integer LastSec = -1;\n'
            '  while (True) {\n'
            '    yield;\n'
            '    declare Integer Left = RemainingMs - (CurrentTime - StartTick);\n'
            '    if (Left < 0) Left = 0;\n'
            '    declare Integer Sec = Left / 1000;\n'
            '    if (Sec != LastSec) {\n'
            '      LastSec = Sec;\n'
            '      declare Integer Mm = Sec / 60;\n'
            '      declare Integer Ss = Sec % 60;\n'
            '      Lbl.SetText("$fff" ^ TL::FormatInteger(Mm, 2) ^ ":" ^ TL::FormatInteger(Ss, 2));\n'
            '    }\n'
            '    if (Left == 0) break;\n'
            '  }\n'
            '}\n'
            '--></script>'
        )

        return (
            f'<frame pos="{x:.2f} {y:.2f}" z-index="40">'
            f'<quad pos="0 0" size="{w:.2f} {h:.2f}" bgcolor="1b1f2a88" />'
            f'<label pos="2 -2.1" z-index="40" '
            f'text="$dddPODIUM IN" textsize="1.35" textfont="GameFont" '
            f'halign="left" valign="top" />'
            f'<label id="podium_countdown_value" pos="{time_pos_x:.2f} -{time_pos_y:.2f}" z-index="42" '
            f'text="$fff--:--" textsize="{time_size:.2f}" '
            f'textfont="GameFontBlack" halign="center" valign="center2" />'
            f'</frame>'
            f'{script}'
        )

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        return {
            "countdown_text": await self._countdown_text(),
        }
