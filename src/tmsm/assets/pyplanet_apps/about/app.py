"""about — a player-facing window with version + project info for PyPlanet and tmsm.

Shows two group boxes (PyPlanet on top, TrackMania Server Manager below), each
with a logo/avatar image and a short blurb plus the live version string. Opened
from the hub tile (all players) or via the ``/about`` chat command.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from pyplanet.apps.config import AppConfig
from pyplanet.contrib.command import Command

logger = logging.getLogger(__name__)

try:  # hub tile is optional — the app still works (via /about) without the hub.
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

try:
    from .views import AboutView
    _HAS_VIEW = True
except Exception:
    AboutView = None  # type: ignore[assignment]
    _HAS_VIEW = False


def _pyplanet_version() -> str:
    try:
        import pyplanet
        return str(getattr(pyplanet, "__version__", "?") or "?")
    except Exception:
        return "?"


def _tmsm_version() -> str:
    # Prefer a real import (works when the tmsm package is on the path)...
    try:
        import tmsm  # type: ignore
        v = getattr(tmsm, "__version__", None)
        if v:
            return str(v)
    except Exception:
        pass
    # ...otherwise parse tmsm/__init__.py via this file's *resolved* path. The
    # app dir is symlinked into the pool from
    #   <tmsm>/src/tmsm/assets/pyplanet_apps/about/  ->  parents[3] == <tmsm>/src/tmsm
    try:
        init = Path(__file__).resolve().parents[3] / "__init__.py"
        if init.is_file():
            m = re.search(
                r'__version__\s*=\s*["\']([^"\']+)["\']',
                init.read_text(encoding="utf-8"),
            )
            if m:
                return m.group(1)
    except Exception:
        pass
    return "?"


class AboutApp(AppConfig):
    name = "pyplanet.apps.tmsm.about"
    label = "about"
    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_hub"]
    game_dependencies = ["trackmania", "trackmania_next"]

    HUB_KEY = "about"
    HUB_NAME = "About"
    HUB_ICON = "info"
    HUB_DESCRIPTION = "PyPlanet & tmsm versions and project info."
    HUB_ORDER = 999  # keep it last in the hub grid

    # Hidden manialink that pre-fetches the panel images into each client's
    # media cache so the About window renders them instantly when opened.
    PRELOAD_ID = "tmsm_about_preload"

    # PyPlanet ----------------------------------------------------------
    PYPLANET_NAME = "PyPlanet"
    PYPLANET_DESC = (
        "Out-of-the-box server controller for ManiaPlanet & TrackMania, "
        "providing the app framework every tmsm addon runs on."
    )
    PYPLANET_URL = "https://pypla.net"
    PYPLANET_LOGO = "http://maniacdn.net/toffe/pyplanet/assets/logo/pyplanet-sm.png"

    # tmsm --------------------------------------------------------------
    TMSM_NAME = "TrackMania Server Manager"
    TMSM_DESC = (
        "Installer & manager for TrackMania dedicated servers: PyPlanet pools, "
        "bundled addons and a Textual TUI — all from one tool."
    )
    TMSM_AUTHOR = "StupsKiesel"
    TMSM_URL = "https://github.com/StupsKiesel/TrackManiaServerManager"
    TMSM_AVATAR = "http://maniacdn.net/stupskiesel/SK_white_256.png"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.view = None

    # ---- lifecycle -----------------------------------------------------

    async def on_start(self) -> None:
        if _HAS_VIEW:
            try:
                self.view = AboutView(self)
            except Exception:
                logger.exception("about: view init failed")
                self.view = None

        try:
            await self.instance.command_manager.register(
                Command(
                    command="about", target=self._cmd_open, admin=False,
                    description="Show info about PyPlanet and tmsm.",
                ),
            )
        except Exception:
            logger.exception("about: /about command registration failed")

        if _HAS_HUB and self.view is not None:
            try:
                sig = self.context.signals.get_signal("tmsm_hub:register")
                entry = HubAppEntry(
                    key=self.HUB_KEY, name=self.HUB_NAME, icon=self.HUB_ICON,
                    role=Role.PLAYER, order=self.HUB_ORDER,
                    description=self.HUB_DESCRIPTION, open=self._open,
                    author=self.TMSM_AUTHOR, version=_tmsm_version(),
                )
                await sig.send_robust({"entry": entry}, raw=True)
            except KeyError:
                logger.info("about: tmsm_hub:register not ready yet")
            except Exception:
                logger.exception("about: hub tile registration failed")

        # Warm the image cache: pre-fetch the panel images on every online
        # client now, and on each player as they connect.
        try:
            self.context.signals.listen(
                "maniaplanet:player_connect", self._on_player_connect)
        except Exception:
            logger.exception("about: player_connect listen failed")
        try:
            await self._preload_images()
        except Exception:
            logger.exception("about: initial image preload failed")

    async def on_stop(self) -> None:
        # Remove the hidden preload manialink from all clients.
        try:
            await self.instance.gbx(
                "SendDisplayManialinkPage",
                '<manialink id="' + self.PRELOAD_ID + '" version="3"></manialink>',
                0, False,
            )
        except Exception:
            pass
        if self.view is not None:
            try:
                await self.view.destroy()
            except Exception:
                pass

    # ---- image preload -------------------------------------------------

    def _preload_xml(self) -> str:
        # Sub-pixel, near-transparent quads tucked into the bottom-right corner.
        # They must stay on-screen with size > 0 and opacity > 0, otherwise the
        # client skips the image download (which is exactly what made the first
        # open slow). At 0.4u / 3% opacity they are effectively invisible.
        quads = []
        for i, url in enumerate((self.PYPLANET_LOGO, self.TMSM_AVATAR)):
            quads.append(
                '<quad pos="0 ' + str(-0.5 * i) + '" size="0.4 0.4" '
                'image="' + url + '" opacity="0.03" keepratio="Fit"/>'
            )
        return (
            '<manialink id="' + self.PRELOAD_ID + '" version="3">'
            '<frame pos="79 -64" z-index="-100">' + "".join(quads) + '</frame>'
            '</manialink>'
        )

    async def _preload_images(self, logins=None) -> None:
        xml = self._preload_xml()
        try:
            if logins:
                await self.instance.gbx(
                    "SendDisplayManialinkPageToLogin",
                    ",".join(logins), xml, 0, False,
                )
            else:
                await self.instance.gbx(
                    "SendDisplayManialinkPage", xml, 0, False,
                )
        except Exception:
            logger.exception("about: preload gbx send failed")

    async def _on_player_connect(self, player=None, **kwargs) -> None:
        login = getattr(player, "login", None)
        if login:
            await self._preload_images([login])

    # ---- data ----------------------------------------------------------

    def panel_context(self) -> dict:
        return {
            "pyplanet_name": self.PYPLANET_NAME,
            "pyplanet_version": _pyplanet_version(),
            "pyplanet_desc": self.PYPLANET_DESC,
            "pyplanet_url": self.PYPLANET_URL,
            "pyplanet_logo": self.PYPLANET_LOGO,
            "tmsm_name": self.TMSM_NAME,
            "tmsm_version": _tmsm_version(),
            "tmsm_desc": self.TMSM_DESC,
            "tmsm_author": self.TMSM_AUTHOR,
            "tmsm_url": self.TMSM_URL,
            "tmsm_avatar": self.TMSM_AVATAR,
        }

    # ---- open ----------------------------------------------------------

    async def _open(self, player) -> None:
        if self.view is None:
            return
        try:
            await self.view.display(player_logins=[player.login])
        except Exception:
            logger.exception("about: open display failed")

    async def _cmd_open(self, player, data, **kwargs) -> None:
        await self._open(player)
