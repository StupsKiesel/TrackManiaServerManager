"""tmsm.ui app — minimal AppConfig so the template prefix `tmsm_ui` registers.

This app does two jobs:
    1. Expose the shared Jinja templates under the `tmsm_ui` prefix so
       other addons can ``{% import 'tmsm_ui/widgets.xml' as ui %}``.
    2. Own the global UI theme: register PyPlanet settings for the
       shared window chrome colors (header/body/accent) and keep the
       module-level cache in ``ui.theme`` synced with their values, so
       every ``ui.window(...)`` call in every app reflects operator
       tweaks via ``!settings``.
"""
from __future__ import annotations

import logging

from pyplanet.apps.config import AppConfig
from pyplanet.contrib.setting import Setting

from .theme import (
    DEFAULT_WINDOW_ACCENT_COLOR,
    DEFAULT_WINDOW_BODY_COLOR,
    DEFAULT_WINDOW_HEADER_COLOR,
    update as _theme_update,
)

logger = logging.getLogger(__name__)


def _normalize_color(raw: str | None, fallback: str) -> str:
    if raw is None:
        return fallback
    s = str(raw).strip().lstrip("#").lower()
    if len(s) not in (3, 4, 6, 8):
        return fallback
    if any(c not in "0123456789abcdef" for c in s):
        return fallback
    return s


class UiApp(AppConfig):
    name = "pyplanet.apps.tmsm.ui"
    label = "tmsm_ui"
    app_dependencies = ["core.maniaplanet"]
    game_dependencies = ["trackmania", "trackmania_next"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setting_window_header_color = Setting(
            "window_header_color", "Window header color",
            Setting.CAT_DESIGN, type=str,
            default=DEFAULT_WINDOW_HEADER_COLOR,
            description=(
                "Manialink color (rgb / rrggbb / rrggbbaa, no '#') used as "
                "the background of every tmsm.ui window title bar."
            ),
        )
        self.setting_window_body_color = Setting(
            "window_body_color", "Window body color",
            Setting.CAT_DESIGN, type=str,
            default=DEFAULT_WINDOW_BODY_COLOR,
            description=(
                "Manialink color (rgb / rrggbb / rrggbbaa, no '#') used as "
                "the background of every tmsm.ui window body."
            ),
        )
        self.setting_window_accent_color = Setting(
            "window_accent_color", "Window accent line color",
            Setting.CAT_DESIGN, type=str,
            default=DEFAULT_WINDOW_ACCENT_COLOR,
            description=(
                "Manialink color (rgb / rrggbb / rrggbbaa, no '#') used as "
                "the thin highlight line under the title bar."
            ),
        )

    async def on_start(self) -> None:
        for s in (
            self.setting_window_header_color,
            self.setting_window_body_color,
            self.setting_window_accent_color,
        ):
            try:
                await self.context.setting.register(s)
            except Exception:
                logger.exception("tmsm.ui: setting register failed: %s", s.key)
            s.on_change = self._on_theme_setting_change
        await self._reload_theme()
        logger.info("tmsm.ui framework loaded (templates available as 'tmsm_ui/*.xml')")

    # ── theme cache plumbing ────────────────────────────────────────

    async def _reload_theme(self) -> None:
        try:
            header_raw = await self.setting_window_header_color.get_value()
        except Exception:
            header_raw = None
        try:
            body_raw = await self.setting_window_body_color.get_value()
        except Exception:
            body_raw = None
        try:
            accent_raw = await self.setting_window_accent_color.get_value()
        except Exception:
            accent_raw = None
        _theme_update(
            window_header_color=_normalize_color(
                header_raw, DEFAULT_WINDOW_HEADER_COLOR,
            ),
            window_body_color=_normalize_color(
                body_raw, DEFAULT_WINDOW_BODY_COLOR,
            ),
            window_accent_color=_normalize_color(
                accent_raw, DEFAULT_WINDOW_ACCENT_COLOR,
            ),
        )

    async def _on_theme_setting_change(self, *args, **kwargs) -> None:
        try:
            await self._reload_theme()
        except Exception:
            logger.exception("tmsm.ui: theme reload after setting change failed")

