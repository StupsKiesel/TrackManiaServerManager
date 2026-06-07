"""Map info widget — compact author time + graphical difficulty signal."""
from __future__ import annotations

import asyncio
import logging
import time

from pyplanet.apps.tmsm.widget_engine import AnimDir, DriveMode
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase


logger = logging.getLogger(__name__)


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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._queued_refresh: asyncio.Task | None = None
        self._tmx_lookup_lock = asyncio.Lock()
        self._tmx_last_lookup_by_uid: dict[str, float] = {}
        self._post_fetch_refresh_task: asyncio.Task | None = None

    async def on_start(self) -> None:
        await super().on_start()
        try:
            # Event-driven refresh: this widget is not periodic (refresh=0),
            # so explicit map lifecycle hooks keep it in sync.
            self.context.signals.listen("maniaplanet:loading_map_start", self._on_refresh_signal)
            self.context.signals.listen("maniaplanet:map_begin", self._on_refresh_signal)
            self.context.signals.listen("maniaplanet:map_start", self._on_refresh_signal)
            # PRE_RACE phase can be entered via multiple callbacks depending
            # on mode/warmup flow. Force-refresh on all known PRE_RACE
            # transitions so stale loading-map refresh jobs cannot suppress
            # fresh TMX metadata fetch for the new map.
            self.context.signals.listen("trackmania:warmup_end", self._on_pre_race_signal)
            self.context.signals.listen("trackmania:start_countdown", self._on_pre_race_signal)
            # IN_RACE phase transition.
            self.context.signals.listen("trackmania:start_line", self._on_in_race_signal)
            self.context.signals.listen("maniaplanet:player_connect", self._on_refresh_signal)
            self.context.signals.listen("maniaplanet:player_disconnect", self._on_refresh_signal)
        except Exception:
            pass

    async def on_stop(self) -> None:
        if self._queued_refresh is not None:
            self._queued_refresh.cancel()
            self._queued_refresh = None
        if self._post_fetch_refresh_task is not None:
            self._post_fetch_refresh_task.cancel()
            self._post_fetch_refresh_task = None
        await super().on_stop()

    def _queue_post_fetch_refresh_all(self) -> None:
        if self.view is None:
            return
        if self._post_fetch_refresh_task is not None and not self._post_fetch_refresh_task.done():
            return

        async def _run() -> None:
            try:
                # Defer by one loop tick so any in-flight DB write commits
                # before we force a full redraw.
                await asyncio.sleep(0)
                if self.view is None:
                    return
                try:
                    online_logins = [
                        p.login for p in self.instance.player_manager.online
                        if getattr(p, "login", None)
                    ]
                except Exception:
                    online_logins = []
                if online_logins:
                    await self.view.display(player_logins=online_logins)
                else:
                    await self.view.display()
            except Exception:
                logger.exception("map_info_widget: post-fetch redraw failed")
            finally:
                self._post_fetch_refresh_task = None

        self._post_fetch_refresh_task = asyncio.create_task(_run())

    def _queue_refresh(self, *, force: bool = False) -> None:
        if self.view is None:
            return
        if self._queued_refresh is not None and not self._queued_refresh.done():
            if not force:
                return
            self._queued_refresh.cancel()
            self._queued_refresh = None

        async def _flush() -> None:
            try:
                # Map-change callbacks can fire before `current_map` is fully
                # populated. Do a few delayed refresh passes so we repaint once
                # live map metadata becomes available.
                for delay_s in (0.20, 0.75, 1.50):
                    await asyncio.sleep(delay_s)
                    if self.view is None:
                        return
                    try:
                        online_logins = [
                            p.login for p in self.instance.player_manager.online
                            if getattr(p, "login", None)
                        ]
                    except Exception:
                        online_logins = []
                    if online_logins:
                        await self.view.display(player_logins=online_logins)
                    else:
                        await self.view.display()
                    try:
                        cm = self.instance.map_manager.current_map
                        uid = str(getattr(cm, "uid", "") or "").strip() if cm is not None else ""
                    except Exception:
                        uid = ""
                    if uid:
                        # We have a live map now; no need for later retries.
                        break
            except Exception:
                pass
            finally:
                self._queued_refresh = None

        self._queued_refresh = asyncio.create_task(_flush())

    async def _on_refresh_signal(self, **kwargs) -> None:
        self._queue_refresh()

    async def _on_pre_race_signal(self, **kwargs) -> None:
        self._queue_refresh(force=True)

    async def _on_in_race_signal(self, **kwargs) -> None:
        self._queue_refresh(force=True)

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
    def _current_map_id(current_map) -> int:
        """Return numeric server map id from possibly nested object forms."""
        if current_map is None:
            return 0
        raw = getattr(current_map, "id", 0)
        # Some runtime contexts expose `current_map.id` as a nested Map-like
        # object instead of a scalar primary key.
        if not isinstance(raw, (int, float, str, bytes, bytearray)):
            raw = getattr(raw, "id", 0)
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    async def _get_tmx_meta_for_current_map(current_map):
        """Resolve TMX metadata row for the currently running server map."""
        try:
            from pyplanet.apps.tmsm.tmx_browser.models import TmxMapMeta
        except Exception:
            return None

        map_id = MapInfoWidget._current_map_id(current_map)
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
    def _game_key(instance) -> str:
        try:
            return str(instance.game.game or "tmnext")
        except Exception:
            return "tmnext"

    async def _fetch_tmx_row_by_uid(self, map_uid: str) -> dict[str, object] | None:
        uid = str(map_uid or "").strip()
        if not uid:
            return None
        try:
            from pyplanet.apps.tmsm.tmx_browser.tmx import search as tmx_search
            game = self._game_key(self.instance)
            data = await tmx_search(game, map_uid=uid, limit=5)
            rows = list(data.get("results") or [])
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("uid") or "").strip() == uid:
                    return row
            return None
        except Exception:
            return None

    async def _persist_tmx_row(self, row: dict[str, object], current_map) -> None:
        map_id = self._current_map_id(current_map)
        tmx_app = getattr(self.instance.apps, "apps", {}).get("tmsm_tmx_browser")
        if tmx_app is not None and hasattr(tmx_app, "_persist_meta_row"):
            try:
                await tmx_app._persist_meta_row(
                    row,
                    server_map_id=(map_id if map_id > 0 else None),
                )
                return
            except Exception:
                pass

        # Fallback path when tmx_browser app instance is unavailable: do a
        # minimal upsert so this widget can still work standalone.
        try:
            import datetime
            from pyplanet.apps.tmsm.tmx_browser.models import TmxMapMeta

            if not TmxMapMeta.table_exists():
                TmxMapMeta.create_table(safe=True)

            track_id = int(row.get("track_id") or 0)
            if track_id <= 0:
                return

            try:
                rec = await TmxMapMeta.get(track_id=track_id)
                created = False
            except Exception:
                rec = TmxMapMeta(track_id=track_id)
                created = True

            tags = row.get("tags")
            if isinstance(tags, list):
                tags_csv = ",".join(str(t).strip() for t in tags if str(t).strip())
            else:
                tags_csv = str(tags or "").strip()

            rec.uid = str(row.get("uid") or "")[:64] or None
            rec.name = str(row.get("name") or "")[:255] or None
            rec.author = str(row.get("author") or "")[:150] or None
            rec.length = str(row.get("length") or "")[:32] or None
            rec.difficulty = str(row.get("difficulty") or "")[:64] or None
            rec.awards = int(row.get("awards") or 0)
            rec.style = str(row.get("style") or "")[:64] or None
            rec.uploaded = str(row.get("uploaded") or "")[:64] or None
            rec.filename = str(row.get("filename") or "")[:255] or None
            rec.map_type = str(row.get("map_type") or "")[:96] or None
            rec.title_pack = str(row.get("title_pack") or "")[:128] or None
            rec.environment = str(row.get("environment") or "")[:64] or None
            rec.vehicle = str(row.get("vehicle") or "")[:64] or None
            rec.mood = str(row.get("mood") or "")[:64] or None
            rec.route = str(row.get("route") or "")[:64] or None
            rec.tags_csv = tags_csv or None
            rec.comment_count = int(row.get("comment_count") or 0)
            rec.replay_count = int(row.get("replay_count") or 0)
            rec.track_value = int(row.get("track_value") or 0)
            rec.display_cost = int(row.get("display_cost") or 0)
            rec.laps = int(row.get("laps") or 0)
            rec.has_thumbnail = bool(row.get("has_thumbnail", False))
            rec.downloadable = bool(row.get("downloadable", True))
            rec.author_time = int(row.get("author_time") or 0)
            rec.comments = str(row.get("comments") or "") or None
            if map_id > 0:
                rec.server_map_id = map_id
            rec.updated_at = datetime.datetime.utcnow()
            await rec.save(force_insert=created)
        except Exception:
            return

    async def _ensure_tmx_meta_for_current_map(self, current_map):
        map_uid = str(getattr(current_map, "uid", "") or "").strip()
        if not map_uid:
            return None

        now = time.monotonic()
        last = float(self._tmx_last_lookup_by_uid.get(map_uid, 0.0) or 0.0)
        if (now - last) < 30.0:
            return await self._get_tmx_meta_for_current_map(current_map)

        async with self._tmx_lookup_lock:
            now = time.monotonic()
            last = float(self._tmx_last_lookup_by_uid.get(map_uid, 0.0) or 0.0)
            if (now - last) < 30.0:
                return await self._get_tmx_meta_for_current_map(current_map)
            self._tmx_last_lookup_by_uid[map_uid] = now

        row = await self._fetch_tmx_row_by_uid(map_uid)
        if row is None:
            return await self._get_tmx_meta_for_current_map(current_map)
        await self._persist_tmx_row(row, current_map)
        meta = await self._get_tmx_meta_for_current_map(current_map)
        if meta is not None:
            self._queue_post_fetch_refresh_all()
        return meta

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
            if meta is None and current_map is not None:
                meta = await self._ensure_tmx_meta_for_current_map(current_map)

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
            logger.exception("map_info_widget: get_widget_data failed")
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
