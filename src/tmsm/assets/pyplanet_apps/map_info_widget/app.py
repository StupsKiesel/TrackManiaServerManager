"""Map info widget — compact author time + graphical difficulty signal."""
from __future__ import annotations

from pyplanet.apps.tmsm.widget_engine import AnimDir, DriveMode
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase


class MapInfoWidget(WidgetAppBase):
    name = "pyplanet.apps.tmsm.map_info_widget"
    label = "map_info_widget"

    app_dependencies = ["core.maniaplanet", "widget_engine"]

    WIDGET_KEY = "map_info"
    WIDGET_NAME = "Map Info"
    WIDGET_DESCRIPTION = "TMX map metadata with icon-driven compact layout."
    WIDGET_ICON = "map"
    WIDGET_TEMPLATE = "map_info_widget/mapinfo.xml"

    WIDGET_DEFAULT_X = 130.0
    WIDGET_DEFAULT_Y = 90.0
    WIDGET_DEFAULT_W = 76.0
    WIDGET_DEFAULT_H = 20.0

    WIDGET_REFRESH_SECONDS = 0.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_DRIVE_MODE = DriveMode.FIXED
    WIDGET_ANIM_DIR = AnimDir.RIGHT
    WIDGET_ANIM_DURATION_MS = 250
    WIDGET_ANIM_IN_DELAY_MS = 0
    WIDGET_ANIM_OUT_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "33ff77ff"

    _ENV_IMAGE_BY_KEY = {
        "stadium": "https://trackmania.exchange/img/env/tm3_e1.png",
        "white shore": "https://trackmania.exchange/img/env/tm3_e5.png",
        "blue bay": "https://trackmania.exchange/img/env/tm3_e4.png",
        "red island": "https://trackmania.exchange/img/env/tm3_e2.png",
    }

    @staticmethod
    def _truncate(value: str, max_len: int) -> str:
        s = str(value or "").strip()
        if not s:
            return "-"
        if len(s) <= max_len:
            return s
        return s[: max(1, max_len - 1)] + "..."

    @staticmethod
    def _coalesce_str(*values, fallback: str = "-") -> str:
        for value in values:
            s = str(value or "").strip()
            if s:
                return s
        return fallback

    @staticmethod
    def _route_icon(value: str) -> str:
        s = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
        if not s or s == "-":
            return "&#xf128;"  # question-circle
        if "single" in s:
            return "&#xf061;"  # arrow-right
        if "multi" in s or "multible" in s or "multiple" in s:
            return "&#xf126;"  # code-fork
        if "sym" in s or "symmetric" in s or "symetrical" in s or "symmetrical" in s:
            return "&#xf074;"  # arrows-h
        return "&#xf128;"

    @classmethod
    def _environment_image_url(cls, value: str) -> str:
        s = str(value or "").strip().lower()
        if not s or s == "-":
            return ""

        # Prefer exact aliases, then permissive keyword matching.
        aliases = {
            "white_shore": "white shore",
            "white-shore": "white shore",
            "whiteshore": "white shore",
            "blue_bay": "blue bay",
            "blue-bay": "blue bay",
            "bluebay": "blue bay",
            "red_island": "red island",
            "red-island": "red island",
            "redisland": "red island",
        }
        normalized = aliases.get(s, s)
        if normalized in cls._ENV_IMAGE_BY_KEY:
            return cls._ENV_IMAGE_BY_KEY[normalized]

        if "stadium" in normalized:
            return cls._ENV_IMAGE_BY_KEY["stadium"]
        if "shore" in normalized:
            return cls._ENV_IMAGE_BY_KEY["white shore"]
        if "bay" in normalized:
            return cls._ENV_IMAGE_BY_KEY["blue bay"]
        if "island" in normalized:
            return cls._ENV_IMAGE_BY_KEY["red island"]
        return ""

    @staticmethod
    async def _get_tmx_meta_for_current_map(current_map):
        """Resolve TMX metadata row for the currently running server map."""
        try:
            from pyplanet.apps.tmsm.tmx_browser.models import TmxMapMeta
        except Exception:
            return None

        map_id = int(getattr(current_map, "id", 0) or 0)
        map_uid = str(getattr(current_map, "uid", "") or "").strip()

        if map_id > 0:
            try:
                return await TmxMapMeta.get(server_map_id=map_id)
            except Exception:
                pass

        if map_uid:
            try:
                return await TmxMapMeta.get(uid=map_uid)
            except Exception:
                pass

        return None

    @staticmethod
    def _format_time_ms(ms_value: int | None) -> str:
        """Format milliseconds to M:SS.mmm or em dash for invalid values."""
        try:
            ms = int(ms_value or 0)
        except (TypeError, ValueError):
            ms = 0
        if ms <= 0:
            return "—"
        total_secs = ms // 1000
        mins = total_secs // 60
        secs = total_secs % 60
        millis = ms % 1000
        return f"{mins}:{secs:02d}.{millis:03d}"

    @staticmethod
    def _difficulty_to_bars(current_map) -> int:
        """Map difficulty to signal bars: easier -> fewer bars.

        Returns 0 when difficulty metadata is unavailable/unknown so the
        template can render all bars as gray.
        """
        raw_diff = None
        for field in ("difficulty", "difficulty_name", "map_difficulty", "difficultylevel"):
            value = getattr(current_map, field, None)
            if value not in (None, ""):
                raw_diff = value
                break
        if raw_diff is None:
            return 0

        # Numeric forms seen across APIs:
        # - 0..5 (beginner..hardest) => +1
        # - 1..6 already bar-like => use directly
        # - larger ranges => scale to 1..6
        try:
            n = int(raw_diff)
            if 0 <= n <= 5:
                return n + 1
            if 1 <= n <= 6:
                return n
            if n > 6:
                scaled = int(round((min(n, 100) / 100.0) * 5.0)) + 1
                return max(1, min(6, scaled))
        except (TypeError, ValueError):
            pass

        # String / enum labels from map metadata.
        s = str(raw_diff).strip().lower().replace("_", " ").replace("-", " ")
        if "beginner" in s or "easy" in s:
            return 1
        if "intermediate" in s or "normal" in s:
            return 2
        if "advanced" in s:
            return 3
        if "expert" in s:
            return 4
        if "lunatic" in s or "impossible" in s or "insane" in s:
            return 5
        if "extreme" in s or "nightmare" in s:
            return 6

        # Unknown difficulty token.
        return 0

    async def get_widget_data(self, login):
        """Get compact map info for the widget."""
        try:
            map_mgr = self.instance.map_manager
            current_map = map_mgr.current_map
            meta = await self._get_tmx_meta_for_current_map(current_map)

            # Prefer TMX metadata values only when present; otherwise keep
            # reliable live map fallback so sparse rows never blank the widget.
            meta_author_time = int(getattr(meta, "author_time", 0) or 0) if meta is not None else 0
            live_author_time = int(getattr(current_map, "time_author", 0) or 0)
            author_time_ms = meta_author_time if meta_author_time > 0 else live_author_time
            author_time = self._format_time_ms(author_time_ms)

            bars = self._difficulty_to_bars(meta) if meta is not None else 0
            if bars <= 0:
                bars = self._difficulty_to_bars(current_map)

            if bars <= 2:
                sig_color = "2f6f"
            elif bars <= 4:
                sig_color = "fd3f"
            else:
                sig_color = "f44f"

            map_name = self._truncate(
                self._coalesce_str(
                    getattr(meta, "name", "") if meta is not None else "",
                    getattr(current_map, "name", ""),
                ),
                34,
            )
            map_author = self._truncate(
                self._coalesce_str(
                    getattr(meta, "author", "") if meta is not None else "",
                    getattr(current_map, "author_nickname", ""),
                    getattr(current_map, "author_login", ""),
                ),
                26,
            )
            map_length = self._coalesce_str(
                getattr(meta, "length", "") if meta is not None else "",
            )
            map_environment = self._coalesce_str(
                getattr(meta, "environment", "") if meta is not None else "",
                getattr(current_map, "environment", ""),
            )
            map_environment_image = self._environment_image_url(map_environment)
            map_route = self._coalesce_str(
                getattr(meta, "route", "") if meta is not None else "",
            )
            map_route_icon = self._route_icon(map_route)
            laps_meta = int(getattr(meta, "laps", 0) or 0) if meta is not None else 0
            laps_live = int(getattr(current_map, "num_laps", 0) or 0)
            map_laps = str(laps_meta if laps_meta > 0 else laps_live)
            if map_laps in ("0", ""):
                map_laps = "-"
            
            return {
                "map_name": map_name,
                "map_author": map_author,
                "map_length": self._truncate(map_length, 18),
                "map_environment": self._truncate(map_environment, 18),
                "map_environment_image": map_environment_image,
                "map_route": self._truncate(map_route, 18),
                "map_route_icon": map_route_icon,
                "map_laps": map_laps,
                "map_author_time": author_time,
                "map_checkpoints": int(getattr(current_map, "num_checkpoints", 0) or 0),
                "difficulty_bars": bars,
                "difficulty_color": sig_color,
            }
        except Exception:
            return {
                "map_name": "-",
                "map_author": "-",
                "map_length": "-",
                "map_environment": "-",
                "map_environment_image": "",
                "map_route": "-",
                "map_route_icon": "&#xf128;",
                "map_laps": "-",
                "map_author_time": "—",
                "map_checkpoints": 0,
                "difficulty_bars": 0,
                "difficulty_color": "6667",
            }
