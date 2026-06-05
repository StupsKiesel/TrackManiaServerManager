"""Social media widget.

Renders six size-aware icon buttons in one row. All target URLs are editable in
app settings, including a custom icon/image + URL slot.
"""
from __future__ import annotations

from typing import Any

from pyplanet.contrib.setting import Setting

from pyplanet.apps.tmsm.widget_engine import AnimDir, DriveMode
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase


class SocialMediaWidgetApp(WidgetAppBase):
    name = "pyplanet.apps.tmsm.socialmedia_widget"
    label = "socialmedia_widget"

    WIDGET_KEY = "socialmedia_widget"
    WIDGET_NAME = "Social Media"
    WIDGET_DESCRIPTION = "Quick links to your social networks and websites."
    WIDGET_ICON = "share-alt"
    WIDGET_TEMPLATE = "socialmedia_widget/socialmedia.xml"

    WIDGET_DEFAULT_X = 90.0
    WIDGET_DEFAULT_Y = 84.0
    WIDGET_DEFAULT_W = 72.0
    WIDGET_DEFAULT_H = 9.0

    WIDGET_REFRESH_SECONDS = 0.0
    WIDGET_HIDE_NAMED = ["in_menu"]
    WIDGET_DRIVE_MODE = DriveMode.FIXED
    WIDGET_ANIM_DIR = AnimDir.RIGHT
    WIDGET_ANIM_DURATION_MS = 250
    WIDGET_ANIM_IN_DELAY_MS = 0
    WIDGET_ANIM_OUT_DELAY_MS = 0

    WIDGET_STRIP_COLOR = "4aa8ffff"

    DEFAULT_GITHUB_URL = ""
    DEFAULT_TWITCH_URL = ""
    DEFAULT_YOUTUBE_URL = ""
    DEFAULT_WEBSITE_URL = ""
    DEFAULT_PYPLANET_URL = "https://pypla.net/"
    DEFAULT_CUSTOM_IMAGE_URL = ""
    DEFAULT_CUSTOM_LINK_URL = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._settings_ready = False

        self.setting_github_url = Setting(
            "github_url",
            "GitHub URL",
            Setting.CAT_BEHAVIOUR,
            type=str,
            description="Link opened by the GitHub button.",
            default="",
        )
        self.setting_twitch_url = Setting(
            "twitch_url",
            "Twitch URL",
            Setting.CAT_BEHAVIOUR,
            type=str,
            description="Link opened by the Twitch button.",
            default="",
        )
        self.setting_youtube_url = Setting(
            "youtube_url",
            "YouTube URL",
            Setting.CAT_BEHAVIOUR,
            type=str,
            description="Link opened by the YouTube button.",
            default="",
        )
        self.setting_website_url = Setting(
            "website_url",
            "URL Button Link",
            Setting.CAT_BEHAVIOUR,
            type=str,
            description="Link opened by the generic URL button.",
            default="",
        )
        self.setting_pyplanet_url = Setting(
            "pyplanet_url",
            "PyPlanet URL",
            Setting.CAT_BEHAVIOUR,
            type=str,
            description="Link opened by the PyPlanet button.",
            default="https://pypla.net/",
        )
        self.setting_custom_image_url = Setting(
            "custom_image_url",
            "Custom Button Image URL",
            Setting.CAT_BEHAVIOUR,
            type=str,
            description="Image URL for the custom button icon.",
            default="",
        )
        self.setting_custom_link_url = Setting(
            "custom_link_url",
            "Custom Button Link URL",
            Setting.CAT_BEHAVIOUR,
            type=str,
            description="Link opened by the custom button.",
            default="",
        )

    async def on_start(self) -> None:
        await super().on_start()
        await self.context.setting.register(
            self.setting_github_url,
            self.setting_twitch_url,
            self.setting_youtube_url,
            self.setting_website_url,
            self.setting_pyplanet_url,
            self.setting_custom_image_url,
            self.setting_custom_link_url,
        )
        self._settings_ready = True

    async def _safe_setting_value(self, setting: Setting, default: str) -> str:
        try:
            return str(await setting.get_value() or "").strip()
        except Exception:
            return default

    async def get_widget_data(self, login: str) -> dict[str, Any]:
        if not self._settings_ready:
            github_url = self.DEFAULT_GITHUB_URL
            twitch_url = self.DEFAULT_TWITCH_URL
            youtube_url = self.DEFAULT_YOUTUBE_URL
            website_url = self.DEFAULT_WEBSITE_URL
            pyplanet_url = self.DEFAULT_PYPLANET_URL
            custom_image_url = self.DEFAULT_CUSTOM_IMAGE_URL
            custom_link_url = self.DEFAULT_CUSTOM_LINK_URL
        else:
            github_url = await self._safe_setting_value(
                self.setting_github_url,
                self.DEFAULT_GITHUB_URL,
            )
            twitch_url = await self._safe_setting_value(
                self.setting_twitch_url,
                self.DEFAULT_TWITCH_URL,
            )
            youtube_url = await self._safe_setting_value(
                self.setting_youtube_url,
                self.DEFAULT_YOUTUBE_URL,
            )
            website_url = await self._safe_setting_value(
                self.setting_website_url,
                self.DEFAULT_WEBSITE_URL,
            )
            pyplanet_url = await self._safe_setting_value(
                self.setting_pyplanet_url,
                self.DEFAULT_PYPLANET_URL,
            )
            custom_image_url = await self._safe_setting_value(
                self.setting_custom_image_url,
                self.DEFAULT_CUSTOM_IMAGE_URL,
            )
            custom_link_url = await self._safe_setting_value(
                self.setting_custom_link_url,
                self.DEFAULT_CUSTOM_LINK_URL,
            )

        buttons = [
            {
                "key": "github",
                "url": github_url,
                "icon": "&#xf09b;",
                "icon_color": "fff",
                "bg": "1f2937aa",
                "bg_disabled": "1f293744",
                "is_custom_image": True,
                "image_url": "https://img.icons8.com/ios-filled/100/ffffff/github.png",
            },
            {
                "key": "twitch",
                "url": twitch_url,
                "icon": "&#xf1e8;",
                "icon_color": "fff",
                "bg": "5b3aa9aa",
                "bg_disabled": "5b3aa944",
                "is_custom_image": False,
                "image_url": "",
            },
            {
                "key": "youtube",
                "url": youtube_url,
                "icon": "&#xf167;",
                "icon_color": "fff",
                "bg": "c4302baa",
                "bg_disabled": "c4302b44",
                "is_custom_image": True,
                "image_url": "https://img.icons8.com/ios-filled/100/ffffff/youtube-play.png",
            },
            {
                "key": "url",
                "url": website_url,
                "icon": "&#xf0c1;",
                "icon_color": "fff",
                "bg": "2563ebaa",
                "bg_disabled": "2563eb44",
                "is_custom_image": False,
                "image_url": "",
            },
            {
                "key": "pyplanet",
                "url": pyplanet_url,
                "icon": "&#xf0ac;",
                "icon_color": "fff",
                "bg": "059669aa",
                "bg_disabled": "05966944",
                "is_custom_image": False,
                "image_url": "",
            },
            {
                "key": "custom",
                "url": custom_link_url,
                "icon": "&#xf0c6;",
                "icon_color": "fff",
                "bg": "a855f7aa",
                "bg_disabled": "a855f744",
                "is_custom_image": bool(custom_image_url),
                "image_url": custom_image_url,
            },
        ]

        return {
            "buttons": buttons,
        }
