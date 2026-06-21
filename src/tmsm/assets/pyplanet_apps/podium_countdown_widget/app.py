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
    _LEGACY_HIDE_UI_MODULES = (
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
        # Phase anchors for more accurate periodic re-alignment.
        self._race_start_monotonic: float | None = None
        self._postrace_start_monotonic: float | None = None
        self._reconcile_task: asyncio.Task | None = None
        self._resync_task: asyncio.Task | None = None
        self._extend_vote_triggers_by_min: dict[int, int] = {5: 0, 10: 0, 15: 0}
        self._extend_vote_lock = asyncio.Lock()

        self.setting_delay_seconds = Setting(
            "podium_delay_seconds",
            "Podium Delay Seconds",
            Setting.CAT_BEHAVIOUR,
            type=int,
            default=self.DEFAULT_DELAY_SECONDS,
            description="Fallback seconds used when map_end does not expose countdown data.",
        )
        self.setting_sync_enabled = Setting(
            "sync_enabled",
            "Sync Countdown To Server Time",
            Setting.CAT_BEHAVIOUR,
            type=bool,
            default=True,
            description="When enabled, periodically re-sync countdown display with server time.",
        )
        self.setting_sync_interval_seconds = Setting(
            "sync_interval_seconds",
            "Sync Interval Seconds",
            Setting.CAT_BEHAVIOUR,
            type=int,
            default=10,
            description="How often to re-sync the countdown anchor (seconds).",
        )
        self.setting_button_plus5_enabled = Setting(
            "button_plus5_enabled",
            "Enable +5 Button",
            Setting.CAT_BEHAVIOUR,
            type=bool,
            default=True,
            description="Show the +5 time extension vote button.",
        )
        self.setting_button_plus10_enabled = Setting(
            "button_plus10_enabled",
            "Enable +10 Button",
            Setting.CAT_BEHAVIOUR,
            type=bool,
            default=True,
            description="Show the +10 time extension vote button.",
        )
        self.setting_button_plus15_enabled = Setting(
            "button_plus15_enabled",
            "Enable +15 Button",
            Setting.CAT_BEHAVIOUR,
            type=bool,
            default=True,
            description="Show the +15 time extension vote button.",
        )
        self.setting_button_plus5_max_per_map = Setting(
            "button_plus5_max_per_map",
            "+5 Button Max Triggers Per Map",
            Setting.CAT_BEHAVIOUR,
            type=int,
            default=1,
            description="How often the +5 vote button can be triggered per map (0 hides it).",
        )
        self.setting_button_plus10_max_per_map = Setting(
            "button_plus10_max_per_map",
            "+10 Button Max Triggers Per Map",
            Setting.CAT_BEHAVIOUR,
            type=int,
            default=1,
            description="How often the +10 vote button can be triggered per map (0 hides it).",
        )
        self.setting_button_plus15_max_per_map = Setting(
            "button_plus15_max_per_map",
            "+15 Button Max Triggers Per Map",
            Setting.CAT_BEHAVIOUR,
            type=int,
            default=1,
            description="How often the +15 vote button can be triggered per map (0 hides it).",
        )

    @staticmethod
    def _signal_payload(kwargs: dict[str, Any]) -> dict[str, Any]:
        src = kwargs.get("source")
        if isinstance(src, dict):
            return src
        out = dict(kwargs)
        out.pop("signal", None)
        out.pop("source", None)
        return out

    @staticmethod
    def _extend_minutes_from_result(result: dict[str, Any]) -> int | None:
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        action = str(metadata.get("action") or "")
        if action != "extend_time":
            return None
        raw = metadata.get("extend_minutes")
        try:
            mins = int(raw)
        except (TypeError, ValueError):
            mins = 0
        if mins in (5, 10, 15):
            return mins
        winner = str(result.get("winner") or "")
        if winner == "extend_5":
            return 5
        if winner == "extend_10":
            return 10
        if winner == "extend_15":
            return 15
        return None

    async def _setting_bool(self, setting: Setting, default: bool) -> bool:
        try:
            raw = await setting.get_value()
        except Exception:
            return default
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        txt = str(raw).strip().lower()
        if txt in ("1", "true", "yes", "on"):
            return True
        if txt in ("0", "false", "no", "off"):
            return False
        return default

    async def _enabled_extend_minutes(self) -> list[int]:
        enabled: list[int] = []
        if await self._setting_bool(self.setting_button_plus5_enabled, True):
            enabled.append(5)
        if await self._setting_bool(self.setting_button_plus10_enabled, True):
            enabled.append(10)
        if await self._setting_bool(self.setting_button_plus15_enabled, True):
            enabled.append(15)
        return enabled

    async def _setting_int(self, setting: Setting, default: int, *, minimum: int = 0, maximum: int = 999) -> int:
        try:
            raw = await setting.get_value()
        except Exception:
            raw = default
        try:
            val = int(raw)
        except (TypeError, ValueError):
            val = int(default)
        if val < minimum:
            return minimum
        if val > maximum:
            return maximum
        return val

    async def _extend_trigger_limits_per_map(self) -> dict[int, int]:
        return {
            5: await self._setting_int(self.setting_button_plus5_max_per_map, 1),
            10: await self._setting_int(self.setting_button_plus10_max_per_map, 1),
            15: await self._setting_int(self.setting_button_plus15_max_per_map, 1),
        }

    async def _visible_extend_minutes(self) -> list[int]:
        enabled = await self._enabled_extend_minutes()
        limits = await self._extend_trigger_limits_per_map()
        out: list[int] = []
        for mins in enabled:
            limit = int(limits.get(mins, 0) or 0)
            used = int(self._extend_vote_triggers_by_min.get(mins, 0) or 0)
            if limit > 0 and used < limit:
                out.append(mins)
        return out

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

    @staticmethod
    def _normalize_remaining_seconds(raw: Any) -> int | None:
        """Normalize a remaining-time value (seconds or milliseconds)."""
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None
        if val <= 0:
            return None
        secs = (val / 1000.0) if val >= 1000.0 else val
        out = int(round(secs))
        if out <= 0:
            return None
        return max(1, min(86400, out))

    async def _remaining_from_game_info(self, phase: Phase | None) -> int | None:
        """Best-effort probe for authoritative *remaining* seconds.

        Some modes expose explicit remaining-time fields in
        GetCurrentGameInfo; when present they are more accurate than a long-
        lived local anchor.
        """
        try:
            gi = await self.instance.gbx("GetCurrentGameInfo")
        except Exception:
            gi = None
        if not isinstance(gi, dict):
            return None

        if phase == Phase.POST_RACE:
            preferred = (
                "ChatTimeLeft",
                "FinishTimeoutLeft",
                "PostRaceTimeLeft",
                "PostRaceRemaining",
            )
        else:
            preferred = (
                "TimeLimitLeft",
                "TimeLeft",
                "RemainingTime",
                "RemainingTimeLimit",
                "RaceTimeLeft",
            )

        for key in preferred:
            if key in gi:
                parsed = self._normalize_remaining_seconds(gi.get(key))
                if parsed is not None:
                    return parsed

        # Generic fallback for titles that use different key names.
        for key, value in gi.items():
            kl = str(key or "").lower()
            if "left" not in kl and "remain" not in kl:
                continue
            if "time" not in kl and "limit" not in kl and "timeout" not in kl:
                continue
            parsed = self._normalize_remaining_seconds(value)
            if parsed is not None:
                return parsed
        return None

    async def _resync_anchor_from_server(self) -> bool:
        """Re-anchor ETA from authoritative server remaining-time.

        Returns True only when the local countdown drift exceeds 1 second and
        we actually adjusted the anchor. This avoids periodic redraw flicker.
        """
        if self.engine is None:
            return False
        phase = self.engine.current_phase
        remaining = await self._remaining_from_game_info(phase)
        if remaining is None:
            return False

        local_remaining_ms = self._remaining_ms()
        remote_remaining_ms = int(remaining * 1000)
        if local_remaining_ms is not None:
            if abs(local_remaining_ms - remote_remaining_ms) <= 1000:
                return False

        self._podium_eta_monotonic = time.monotonic() + float(remaining)
        # Keep baseline aligned for reconcile logic and vote delta-shifts.
        if self._baseline_total_seconds is None or remaining > self._baseline_total_seconds:
            self._baseline_total_seconds = int(remaining)
        return True

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

    async def _timelimit_disabled_in_matchsettings(self) -> bool:
        """Return True when timelimit is explicitly configured to 0.

        We intentionally only treat explicit non-positive values as disabled;
        missing/unknown values are handled by normal fallback logic.
        """
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
                    try:
                        return float(settings.get(key) or 0.0) <= 0.0
                    except (TypeError, ValueError):
                        return False
            for key, value in settings.items():
                if "timelimit" not in str(key or "").lower():
                    continue
                try:
                    return float(value or 0.0) <= 0.0
                except (TypeError, ValueError):
                    return False

        try:
            gi = await self.instance.gbx("GetCurrentGameInfo")
        except Exception:
            gi = None
        if isinstance(gi, dict) and "TimeLimit" in gi:
            try:
                return float(gi.get("TimeLimit") or 0.0) <= 0.0
            except (TypeError, ValueError):
                return False
        return False

    def build_entry(self) -> WidgetEntry:
        entry = super().build_entry()
        return replace(
            entry,
            gbx_replace=GbxReplacement(
                manialink_id=self._MANIALINK_ID,
                # Widget paints its own background and ships its own
                # ManiaScript for client-side ticking; the engine chrome
                # would nest our <script> inside frames and ManiaScript
                # would silently drop it.
                chrome=False,
            ),
        )

    async def on_start(self) -> None:
        await super().on_start()

        # One-time compatibility migration: older builds hid Race_Chrono*
        # modules server-wide, which suppresses checkpoint split popups.
        await self._migrate_legacy_ui_hide_override()

        await self.context.setting.register(self.setting_delay_seconds)
        await self.context.setting.register(self.setting_sync_enabled)
        await self.context.setting.register(self.setting_sync_interval_seconds)
        await self.context.setting.register(self.setting_button_plus5_enabled)
        await self.context.setting.register(self.setting_button_plus10_enabled)
        await self.context.setting.register(self.setting_button_plus15_enabled)
        await self.context.setting.register(self.setting_button_plus5_max_per_map)
        await self.context.setting.register(self.setting_button_plus10_max_per_map)
        await self.context.setting.register(self.setting_button_plus15_max_per_map)

        self.setting_delay_seconds.on_change = self._on_setting_change
        self.setting_sync_enabled.on_change = self._on_setting_change
        self.setting_sync_interval_seconds.on_change = self._on_setting_change
        self.setting_button_plus5_enabled.on_change = self._on_setting_change
        self.setting_button_plus10_enabled.on_change = self._on_setting_change
        self.setting_button_plus15_enabled.on_change = self._on_setting_change
        self.setting_button_plus5_max_per_map.on_change = self._on_setting_change
        self.setting_button_plus10_max_per_map.on_change = self._on_setting_change
        self.setting_button_plus15_max_per_map.on_change = self._on_setting_change

        self.context.signals.listen("maniaplanet:map_end", self._on_map_end)
        self.context.signals.listen("maniaplanet:podium_start", self._on_podium_start)
        self.context.signals.listen("maniaplanet:map_start", self._on_map_start)
        self.context.signals.listen("maniaplanet:map_begin", self._on_map_start)
        self._listen_if_exists("trackmania:start_line", self._on_start_line)
        self.context.signals.listen("maniaplanet:player_connect", self._on_player_connect)
        self.context.signals.listen("maniaplanet:manialink_answer", self._on_manialink_action)
        self._listen_if_exists("tmsm_voting_engine:ended", self._on_vote_engine_ended)
        self._listen_if_exists("maniaplanet:manialink_page_answer", self._on_manialink_action)

        self._reconcile_task = asyncio.create_task(self._reconcile_loop())
        self._resync_task = asyncio.create_task(self._resync_loop())

    async def _migrate_legacy_ui_hide_override(self) -> None:
        host = getattr(self.instance.apps, "apps", {}).get("widget_engine")
        if host is None:
            return
        getter = getattr(host, "get_effective_hide_ui_modules", None)
        setter = getattr(host, "set_ui_modules_override", None)
        if not callable(getter) or not callable(setter):
            return
        try:
            effective = tuple(getter(self.WIDGET_KEY) or ())
        except Exception:
            return
        if effective != self._LEGACY_HIDE_UI_MODULES:
            return
        try:
            await setter(self.WIDGET_KEY, None)
        except Exception:
            return

    async def on_stop(self) -> None:
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            self._reconcile_task = None
        if self._resync_task is not None:
            self._resync_task.cancel()
            self._resync_task = None
        await super().on_stop()

    async def _reconcile_loop(self) -> None:
        """Reconcile countdown with mode settings.

        Handles dynamic timelimit changes (for example vote-based extensions)
        and recovers state if lifecycle events were missed.
        """
        try:
            while True:
                await asyncio.sleep(5.0)
                if await self._timelimit_disabled_in_matchsettings():
                    if self._podium_eta_monotonic is not None or self._baseline_total_seconds is not None:
                        self._podium_eta_monotonic = None
                        self._baseline_total_seconds = None
                        await self._push_replacement()
                    continue

                # Timelimit may have switched from 0 -> positive value.
                if self._podium_eta_monotonic is None or self._baseline_total_seconds is None:
                    if await self._ensure_countdown_state():
                        await self._push_replacement()
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

    async def _resync_loop(self) -> None:
        """Periodic server-time resync for long countdowns.

        The client-side script decrements locally; on long durations this can
        drift. Re-anchor from server state whenever possible and redraw.
        """
        try:
            while True:
                interval = await self._setting_int(
                    self.setting_sync_interval_seconds,
                    10,
                    minimum=2,
                    maximum=120,
                )
                await asyncio.sleep(float(interval))
                if self._podium_eta_monotonic is None:
                    continue
                if not await self._setting_bool(self.setting_sync_enabled, True):
                    continue
                if await self._resync_anchor_from_server():
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

    def _listen_if_exists(self, signal_name: str, callback) -> None:
        try:
            self.context.signals.get_signal(signal_name)
        except Exception:
            return
        self.context.signals.listen(signal_name, callback)

    async def _on_setting_change(self, *args, **kwargs) -> None:
        # AppConfig setting updates should immediately reflect in-game for all
        # players (button visibility/sync behavior/countdown source).
        await self._push_replacement()

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
        if await self._timelimit_disabled_in_matchsettings():
            self._baseline_total_seconds = None
            self._podium_eta_monotonic = None
            self._postrace_start_monotonic = None
            await self._push_replacement()
            return
        total = await self._post_race_from_mode_settings()
        if total is None:
            total = await self._configured_delay()
        self._postrace_start_monotonic = time.monotonic()
        self._race_start_monotonic = None
        self._baseline_total_seconds = int(total)
        self._podium_eta_monotonic = self._postrace_start_monotonic + float(total)
        await self._push_replacement()

    async def _on_podium_start(self, **kwargs) -> None:
        self._podium_eta_monotonic = None
        self._baseline_total_seconds = None
        self._race_start_monotonic = None
        self._postrace_start_monotonic = None
        await self._push_replacement()

    async def _on_map_start(self, **kwargs) -> None:
        self._extend_vote_triggers_by_min = {5: 0, 10: 0, 15: 0}
        self._race_start_monotonic = None
        self._postrace_start_monotonic = None
        if await self._timelimit_disabled_in_matchsettings():
            self._baseline_total_seconds = None
            self._podium_eta_monotonic = None
            await self._push_replacement()
            return
        total = await self._configured_delay()
        self._baseline_total_seconds = int(total)
        self._podium_eta_monotonic = time.monotonic() + float(total)
        await self._push_replacement()

    async def _on_start_line(self, **kwargs) -> None:
        # The actual race clock starts here. Re-anchor so pre-race countdown
        # time does not make the displayed timelimit run early.
        if await self._timelimit_disabled_in_matchsettings():
            return
        total = await self._configured_delay()
        self._race_start_monotonic = time.monotonic()
        self._postrace_start_monotonic = None
        self._baseline_total_seconds = int(total)
        self._podium_eta_monotonic = self._race_start_monotonic + float(total)
        await self._push_replacement()

    async def _on_vote_engine_ended(self, **kwargs) -> None:
        payload = self._signal_payload(kwargs)
        result = payload.get("result") if isinstance(payload.get("result"), dict) else None
        if result is None:
            return
        if bool(result.get("cancelled", False)):
            return

        mins = self._extend_minutes_from_result(result)
        if mins not in (5, 10, 15):
            return

        winner = str(result.get("winner") or "")
        if winner not in ("yes", f"extend_{mins}"):
            return

        async with self._extend_vote_lock:
            self._extend_vote_triggers_by_min[mins] = int(
                self._extend_vote_triggers_by_min.get(mins, 0) or 0
            ) + 1
        await self._push_replacement()

    async def _on_player_connect(self, player=None, **kwargs) -> None:
        login = getattr(player, "login", None) if player is not None else None
        if not login:
            return
        if self._podium_eta_monotonic is None:
            await self._ensure_countdown_state()
            if self._podium_eta_monotonic is None:
                return
        await self._push_replacement(logins=[login])

    async def _on_manialink_action(self, player=None, action=None, **kwargs) -> None:
        action_raw = str(action or kwargs.get("action") or "")
        prefix = f"{self._MANIALINK_ID}__extend__"
        if not action_raw.startswith(prefix):
            return
        try:
            minutes = int(action_raw.split("__")[-1])
        except (TypeError, ValueError):
            return
        if minutes not in (5, 10, 15):
            return

        enabled_minutes = await self._enabled_extend_minutes()
        if minutes not in enabled_minutes:
            return

        limits = await self._extend_trigger_limits_per_map()
        limit = int(limits.get(minutes, 0) or 0)
        if limit <= 0:
            await self._push_replacement()
            return

        p = player
        if p is None:
            login = str(kwargs.get("login") or "")
            if login:
                try:
                    p = await self.instance.player_manager.get_player(login=login)
                except Exception:
                    p = None
        if p is None:
            return

        voting = getattr(self.instance.apps, "apps", {}).get("voting")
        starter = getattr(voting, "_start_extend_vote", None) if voting is not None else None
        if not callable(starter):
            try:
                await self.instance.chat("$f80Voting app unavailable.", p.login)
            except Exception:
                pass
            return

        used = int(self._extend_vote_triggers_by_min.get(minutes, 0) or 0)
        if used >= limit:
            try:
                await self.instance.chat(
                    f"$fa0+{minutes} vote reached its per-map limit ({limit}).",
                    p.login,
                )
            except Exception:
                pass
            await self._push_replacement(logins=[p.login])
            return

        await starter(p, minutes)
        await self._push_replacement()

    def _remaining_ms(self) -> int | None:
        if self._podium_eta_monotonic is None:
            return None
        return int(max(0.0, self._podium_eta_monotonic - time.monotonic()) * 1000.0)

    async def _ensure_countdown_state(self) -> bool:
        """Recover countdown state when lifecycle events were missed."""
        if self._podium_eta_monotonic is not None:
            return True
        if await self._timelimit_disabled_in_matchsettings():
            self._baseline_total_seconds = None
            self._podium_eta_monotonic = None
            return False
        phase = self.engine.current_phase if self.engine is not None else None
        if phase is None:
            return False
        if phase == Phase.POST_RACE:
            total = await self._post_race_from_mode_settings()
            if total is None:
                total = await self._configured_delay()
            self._postrace_start_monotonic = time.monotonic()
            self._race_start_monotonic = None
        elif phase in (Phase.WARMUP, Phase.PRE_RACE, Phase.IN_RACE):
            total = await self._configured_delay()
            self._race_start_monotonic = time.monotonic()
            self._postrace_start_monotonic = None
        else:
            return False
        self._baseline_total_seconds = int(total)
        self._podium_eta_monotonic = time.monotonic() + float(total)
        return True

    async def build_replacement_xml(self, login: str) -> str:
        if self.engine is None:
            return ""

        if self._podium_eta_monotonic is None:
            await self._ensure_countdown_state()

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

        enabled_minutes = await self._visible_extend_minutes()

        # Responsive single-row layout: [time] [ +N ] [ +N ] ...
        # Derive all sizes from resolved widget width/height so controls
        # stay non-overlapping on custom dimensions.
        pad = max(0.8, h * 0.08)
        gap = max(0.6, h * 0.08)
        row_y = h * 0.50
        inside_w = max(1.0, w - (2.0 * pad))

        btn_count = len(enabled_minutes)
        min_time_w = 8.0
        max_btn_w_fit = max(
            3.2,
            (inside_w - (btn_count * gap) - min_time_w) / max(1.0, float(btn_count)),
        )
        btn_w_pref = max(4.8, h * 1.05)
        btn_w = min(btn_w_pref, max_btn_w_fit) if btn_count > 0 else 0.0
        btn_h = max(1.0, h - (2.0 * pad))

        buttons_total_w = (btn_count * btn_w) + (max(0, btn_count - 1) * gap)
        time_w = max(6.0, inside_w - buttons_total_w - (gap if btn_count > 0 else 0.0))
        time_pos_x = pad + (time_w * 0.5)
        time_pos_y = row_y

        button_start_x = pad + time_w + (gap if btn_count > 0 else 0.0)

        time_size = max(1.2, min(h * 0.42, time_w * 0.18))
        btn_text_size = 1.2

        # Client-side ticking: bake the remaining-ms into the manialink and
        # let ManiaScript decrement once per second using CurrentTime as the
        # local monotonic clock. This means a single push covers the entire
        # countdown — we only re-push on real events (map start/end, podium
        # start, player connect, time-extend reconciliation).
        #
        # The label reference is re-acquired every loop iteration: when the
        # widget_engine re-broadcasts this manialink (gbx replacements get
        # pushed by phase/runtime-layout/global-color changes), the previous
        # XML's controls are destroyed but the old `main()` can still run
        # one more tick during the handover. A stale `Lbl` held from before
        # the yield then points to a freed `Page.ControlsCache` slot and
        # `Lbl.SetText` raises "Invalid access to parameter". Re-acquiring
        # inside the loop sidesteps the race — we either bind to the fresh
        # control, or skip the tick if the manialink is mid-teardown.
        script = (
            '<script><!--\n'
            '#Include "TextLib" as TL\n'
            'main() {\n'
            f'  declare Integer RemainingMs = {remaining_ms};\n'
            '  declare Integer StartTick = CurrentTime;\n'
            '  declare Integer LastSec = -1;\n'
            '  while (True) {\n'
            '    yield;\n'
            '    declare CMlLabel Lbl <=> (Page.GetFirstChild("podium_countdown_value") as CMlLabel);\n'
            '    if (Lbl == Null) continue;\n'
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
            f'<label id="podium_countdown_value" pos="{time_pos_x:.2f} -{time_pos_y:.2f}" z-index="42" '
            f'text="$fff--:--" textsize="{time_size:.2f}" '
            f'textfont="GameFontBlack" halign="center" valign="center2" />'
            + "".join(
                (
                    f'<quad pos="{(button_start_x + (idx * (btn_w + gap))):.2f} -{row_y:.2f}" '
                    f'z-index="42" size="{btn_w:.2f} {btn_h:.2f}" '
                    f'halign="left" valign="center2" bgcolor="1b1f2a88" bgcolorfocus="3d6a94ff" '
                    f'action="{self._MANIALINK_ID}__extend__{mins}" scriptevents="1" />'
                    f'<label pos="{(button_start_x + (idx * (btn_w + gap)) + (btn_w / 2.0)):.2f} -{row_y:.2f}" '
                    f'z-index="43" text="$fff+{mins}" textsize="{btn_text_size:.2f}" '
                    f'textfont="GameFontBlack" halign="center" valign="center2" />'
                )
                for idx, mins in enumerate(enabled_minutes)
            )
            + f'</frame>'
            + f'{script}'
        )

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        return {
            "countdown_text": await self._countdown_text(),
        }
