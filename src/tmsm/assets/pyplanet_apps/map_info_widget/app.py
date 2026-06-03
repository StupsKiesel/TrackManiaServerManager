"""Map info widget — compact author time + graphical difficulty signal."""
from __future__ import annotations

from pyplanet.apps.tmsm.widgets.widget_base import WidgetAppBase


class MapInfoWidget(WidgetAppBase):
    name = "pyplanet.apps.tmsm.map_info_widget"
    label = "map_info_widget"

    WIDGET_KEY = "map_info"
    WIDGET_NAME = "Map Info"
    WIDGET_DESCRIPTION = "Author time, checkpoints, and graphical map difficulty."
    WIDGET_ICON = "map"
    WIDGET_TEMPLATE = "map_info_widget/mapinfo.xml"

    WIDGET_DEFAULT_X = 130.0
    WIDGET_DEFAULT_Y = 90.0
    WIDGET_DEFAULT_W = 50.0
    WIDGET_DEFAULT_H = 12.0

    WIDGET_REFRESH_SECONDS = 0.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_HIDE_WHILE_DRIVING = False
    WIDGET_ANIM_DIR = "right"
    WIDGET_ANIM_DURATION_MS = 250
    WIDGET_ANIM_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "33ff77ff"

    async def get_widget_data(self, login):
        """Get compact map info for the widget."""
        try:
            map_mgr = self.instance.map_manager
            current_map = map_mgr.current_map
            
            # Format author time (milliseconds -> MM:SS.mmm)
            author_time = "—"
            if current_map.time_author and current_map.time_author > 0:
                ms = current_map.time_author
                total_secs = ms // 1000
                mins = total_secs // 60
                secs = total_secs % 60
                millis = ms % 1000
                author_time = f"{mins}:{secs:02d}.{millis:03d}"

            # Difficulty bars (0..6), preferring an explicit map field when
            # available. Fallback derives a rough challenge score from medal
            # spread so the widget still works across game versions.
            bars = 0
            raw_diff = getattr(current_map, "difficulty", None)
            if raw_diff is not None:
                try:
                    bars = int(raw_diff)
                except (TypeError, ValueError):
                    bars = 0
            if bars <= 0:
                ta = int(getattr(current_map, "time_author", 0) or 0)
                tg = int(getattr(current_map, "time_gold", 0) or 0)
                tb = int(getattr(current_map, "time_bronze", 0) or 0)
                if ta > 0 and tb > ta:
                    # Larger bronze-vs-author spread usually means easier map,
                    # so invert to a hardness score for the signal bars.
                    spread = (tb - ta) / max(1, ta)
                    if spread <= 0.22:
                        bars = 6
                    elif spread <= 0.30:
                        bars = 5
                    elif spread <= 0.45:
                        bars = 4
                    elif spread <= 0.65:
                        bars = 3
                    elif spread <= 0.90:
                        bars = 2
                    else:
                        bars = 1
                elif tg > ta > 0:
                    spread = (tg - ta) / max(1, ta)
                    if spread <= 0.12:
                        bars = 6
                    elif spread <= 0.20:
                        bars = 5
                    elif spread <= 0.35:
                        bars = 4
                    else:
                        bars = 3
                else:
                    bars = 3
            bars = max(1, min(6, bars))

            if bars <= 2:
                sig_color = "2f6f"
            elif bars <= 4:
                sig_color = "fd3f"
            else:
                sig_color = "f44f"
            
            return {
                "map_author_time": author_time,
                "map_checkpoints": int(getattr(current_map, "num_checkpoints", 0) or 0),
                "difficulty_bars": bars,
                "difficulty_color": sig_color,
            }
        except Exception:
            return {
                "map_author_time": "—",
                "map_checkpoints": 0,
                "difficulty_bars": 3,
                "difficulty_color": "fd3f",
            }
