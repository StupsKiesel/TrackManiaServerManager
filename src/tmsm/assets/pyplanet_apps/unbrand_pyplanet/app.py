"""Override PyPlanet's vanilla `pyplanet__controller` manialink to suppress
the logo image, the "Report issue / suggestion" label and/or the hide-chat
toggle button — without removing the core `core.pyplanet` app.

We re-send the manialink with the same id AFTER PyPlanet sends its own one,
so our XML replaces it client-side. Hidden F8/F9 hotkey labels are kept so
PyPlanet's visibility toggles keep working.

Also suppresses PyPlanet's startup chat banner and the "new version
available" chat notice, routing those to the notification_engine toast
widget for admin-level players (and master) only.
"""
from __future__ import annotations

import asyncio
import logging

from pyplanet.apps.config import AppConfig
from pyplanet.contrib.setting import Setting

logger = logging.getLogger(__name__)


_MANIALINK_ID = "pyplanet__controller"
_ADMIN_MIN_LEVEL = 2  # 2 = LEVEL_ADMIN, 3 = LEVEL_MASTER


class UnbrandPyPlanet(AppConfig):
    name = "pyplanet.apps.tmsm.unbrand_pyplanet"
    label = "unbrand_pyplanet"
    app_dependencies = ["core.pyplanet"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setting_hide_logo = Setting(
            "hide_pyplanet_logo", "Hide PyPlanet logo",
            Setting.CAT_BEHAVIOUR, type=bool, default=True,
            description="Hide the PyPlanet brand image at the bottom-right.",
        )
        self.setting_hide_report = Setting(
            "hide_report_button", "Hide report-issue link",
            Setting.CAT_BEHAVIOUR, type=bool, default=True,
            description="Hide the 'Report issue / suggestion' text link.",
        )
        self.setting_hide_chat_btn = Setting(
            "hide_chat_toggle_button", "Hide chat-toggle button",
            Setting.CAT_BEHAVIOUR, type=bool, default=True,
            description="Hide the small button that hides the in-game chat.",
        )
        self.setting_suppress_startup = Setting(
            "suppress_startup_notice", "Suppress PyPlanet startup chat",
            Setting.CAT_BEHAVIOUR, type=bool, default=True,
            description="Hide the 'PyPlanet vX.Y.Z' chat banner sent at server "
                        "start; instead show it as a status toast to admins only.",
        )
        self.setting_suppress_update = Setting(
            "suppress_update_notice", "Suppress update-available chat",
            Setting.CAT_BEHAVIOUR, type=bool, default=True,
            description="Hide the 'new version available' chat broadcast; "
                        "instead show it as a status toast to admins only.",
        )

        self._startup_notice: str | None = None
        self._update_notice: str | None = None
        self._orig_print_header = None
        self._orig_print_footer = None
        self._orig_update_check = None
        self._orig_update_connect = None

        # Cached setting values. Populated after on_start finishes registering
        # the settings (the patched ControllerView.display can fire BEFORE
        # then during core.pyplanet's on_start, when the SettingModel row
        # doesn't exist yet and get_value() raises DoesNotExist). Defaults
        # mirror the Setting defaults below (all True).
        self._cached_hide_logo: bool = True
        self._cached_hide_report: bool = True
        self._cached_hide_chat_btn: bool = True
        self._settings_ready: bool = False

    def _status_messages_available(self) -> bool:
        try:
            if self.instance.apps.apps.get("notification_engine") is None:
                return False
            self.context.signals.get_signal("notification_engine:notify")
            return True
        except Exception:
            return False

    async def on_init(self):
        # Patch instance.print_header BEFORE apps.start runs (and therefore
        # before print_header is invoked) so the "Loading..." chat banner is
        # suppressed. on_init fires during apps.init(), which precedes
        # print_header in the boot sequence.
        try:
            self._orig_print_header = self.instance.print_header

            async def _patched_print_header():
                return  # suppress vanilla "Loading..." chat broadcast

            self.instance.print_header = _patched_print_header
        except Exception:
            logger.exception("unbrand_pyplanet: failed to patch print_header")

        # Patch core.pyplanet's ControllerView.display so the vanilla logo /
        # report-link / chat-toggle XML is never sent in the first place
        # (avoids the brief flash on server restart). Apps.init runs before
        # apps.start, so the patch is in place before core.pyplanet calls
        # display() in its own on_start.
        try:
            from pyplanet.apps.core.pyplanet.views import controller as _ctrl_mod
            view_cls = _ctrl_mod.ControllerView
            app_ref = self

            async def _patched_display(self_view, **kwargs):
                xml = await app_ref._build_xml()
                if not xml:
                    # All three hide-toggles off → fall back to vanilla.
                    return await super(view_cls, self_view).display(**kwargs)
                logins = kwargs.get("player_logins")
                player = kwargs.get("player")
                if (not logins) and player is not None:
                    logins = [getattr(player, "login", None)]
                logins = [l for l in (logins or []) if l]
                try:
                    if logins:
                        await app_ref.instance.gbx(
                            "SendDisplayManialinkPageToLogin",
                            ",".join(logins), xml, 0, False,
                        )
                    else:
                        await app_ref.instance.gbx(
                            "SendDisplayManialinkPage", xml, 0, False,
                        )
                except Exception:
                    logger.exception(
                        "unbrand_pyplanet: patched ControllerView.display gbx send failed"
                    )

            view_cls.display = _patched_display
        except Exception:
            logger.exception("unbrand_pyplanet: failed to patch ControllerView.display")

    async def on_start(self):
        await self.context.setting.register(
            self.setting_hide_logo,
            self.setting_hide_report,
            self.setting_hide_chat_btn,
            self.setting_suppress_startup,
            self.setting_suppress_update,
        )
        self._settings_ready = True
        try:
            self._cached_hide_logo = bool(await self.setting_hide_logo.get_value())
            self._cached_hide_report = bool(await self.setting_hide_report.get_value())
            self._cached_hide_chat_btn = bool(await self.setting_hide_chat_btn.get_value())
        except Exception:
            logger.exception("unbrand_pyplanet: failed to prime setting cache")

        self.setting_hide_logo.on_change = self._on_setting_change
        self.setting_hide_report.on_change = self._on_setting_change
        self.setting_hide_chat_btn.on_change = self._on_setting_change

        self.context.signals.listen(
            "maniaplanet:player_connect", self._on_player_connect
        )

        await self._install_chat_overrides()

        # Push manialink override to everyone currently online (pool reload / app start).
        asyncio.ensure_future(self._send_override())

    async def _on_setting_change(self, *args, **kwargs):
        await self._send_override()

    async def _on_player_connect(self, player, **kwargs):
        # PyPlanet core's on_connect also sends the original manialink on this
        # same signal. Delay a beat so our override lands after it.
        await asyncio.sleep(0.5)
        try:
            await self._send_override(logins=[player.login])
        except Exception:
            logger.exception("unbrand_pyplanet: failed to send override on connect")

        # Replay cached notices to admin/master players on join.
        if int(getattr(player, "level", 0) or 0) >= _ADMIN_MIN_LEVEL:
            if self._startup_notice and await self.setting_suppress_startup.get_value():
                await self._toast(self._startup_notice, "info", [player.login])
            if self._update_notice and await self.setting_suppress_update.get_value():
                await self._toast(self._update_notice, "warning", [player.login])

    async def _build_xml(self) -> str:
        if self._settings_ready:
            try:
                hide_logo = await self.setting_hide_logo.get_value()
                hide_report = await self.setting_hide_report.get_value()
                hide_chat_btn = await self.setting_hide_chat_btn.get_value()
                self._cached_hide_logo = bool(hide_logo)
                self._cached_hide_report = bool(hide_report)
                self._cached_hide_chat_btn = bool(hide_chat_btn)
            except Exception:
                hide_logo = self._cached_hide_logo
                hide_report = self._cached_hide_report
                hide_chat_btn = self._cached_hide_chat_btn
        else:
            hide_logo = self._cached_hide_logo
            hide_report = self._cached_hide_report
            hide_chat_btn = self._cached_hide_chat_btn

        if not (hide_logo or hide_report or hide_chat_btn):
            return ""

        game = self.instance.game.game
        chat_pos = "-160.25 -63.75" if game == "tm" else "-160.25 -39.75"

        parts: list[str] = [f'<manialink id="{_MANIALINK_ID}" version="3">']

        if not hide_logo:
            parts.append(
                '<quad pos="150 -50" z-index="100" size="15 15" '
                'image="http://maniacdn.net//toffe/pyplanet/assets/logo/pyplanet-xs.png" '
                'autoscale="0" keepratio="Clip" halign="center" valign="center" '
                'opacity="0.4" url="http://pypla.net/"/>'
            )

        if not hide_report:
            report_pos = "0 -86.5" if game == "tm" else "140 -86.5"
            parts.append(
                f'<label pos="{report_pos}" z-index="100" size="52 5" '
                'text="Report issue / suggestion" style="TextButtonNav" scale="0.6" '
                'url="https://github.com/PyPlanet/PyPlanet/issues/new" halign="center"/>'
            )

        if not hide_chat_btn:
            parts.append(
                f'<frame pos="{chat_pos}" z-index="160" id="player_hide_chat_frame">'
                '<quad pos="0 -0.25" z-index="0" size="6.5 6" bgcolor="00000060"/>'
                '<label pos="3.25 -3.2" z-index="1" size="6.5 6" text="&#xf086;" '
                'halign="center" valign="center2" id="player_hide_chat_toggle" scriptevents="1"/>'
                '</frame>'
            )

        # Always preserve F8/F9 hotkey labels — actions still dispatch to
        # core.pyplanet's ControllerView server-side (same manialink id).
        parts.append(
            '<label pos="0 0" z-index="0" size="0 0" textsize="0.0" hide="1" text="" '
            f'action="{_MANIALINK_ID}__f8" actionkey="4" scriptevents="4" '
            'halign="center" valign="center2"/>'
        )
        parts.append(
            '<label pos="0 0" z-index="0" size="0 0" textsize="0.0" hide="1" text="" '
            f'action="{_MANIALINK_ID}__f9" actionkey="5" scriptevents="4" '
            'halign="center" valign="center2"/>'
        )
        parts.append('</manialink>')
        return "".join(parts)

    async def _send_override(self, logins: list[str] | None = None) -> None:
        xml = await self._build_xml()
        if not xml:
            return
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
            logger.exception("unbrand_pyplanet: gbx send failed")

    # ---- chat overrides (startup banner + update-available notice) -----

    async def _install_chat_overrides(self) -> None:
        try:
            from pyplanet import __version__ as pp_version
        except Exception:
            pp_version = "?"

        # 1) Startup banner — patch instance.print_footer (called once, AFTER
        # apps.start() and therefore AFTER this on_start runs).
        try:
            self._orig_print_footer = self.instance.print_footer

            async def _patched_print_footer():
                version_text = f"PyPlanet {pp_version}"
                self._startup_notice = version_text
                suppress = await self.setting_suppress_startup.get_value()
                if not suppress:
                    try:
                        await self._orig_print_footer()
                    except Exception:
                        logger.exception("unbrand_pyplanet: original print_footer raised")
                    return
                # print_footer normally kicks off the update checker; preserve
                # that side effect when we skip the original.
                try:
                    from pyplanet.utils import releases as _releases
                    asyncio.ensure_future(_releases.UpdateChecker.init_checker(self.instance))
                except Exception:
                    pass
                logins = self._online_admin_logins()
                if logins:
                    await self._toast(version_text, "info", logins)

            self.instance.print_footer = _patched_print_footer
        except Exception:
            logger.exception("unbrand_pyplanet: failed to patch print_footer")

        # 2) Update-available chat — patch UpdateChecker.check + connect.
        try:
            from pyplanet.utils import releases as _releases

            checker = _releases.UpdateChecker
            self._orig_update_check = checker.check
            self._orig_update_connect = checker.connect
            app_ref = self

            async def _patched_check(first_check: bool = False):
                from pyplanet import __version__ as current_version
                from pyplanet.utils import semver as _semver
                import aiohttp
                running_prerelease = _semver.is_prerelease(current_version)
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(checker.url) as resp:
                            data = await resp.json()
                            checker.releases = [r["tag_name"] for r in data]
                            for release in data:
                                if release.get("draft"):
                                    continue
                                if (not running_prerelease) and release.get("prerelease"):
                                    continue
                                checker.latest = release["tag_name"]
                                break
                            checker.current = current_version
                except Exception:
                    return
                if not (first_check and checker.update_available):
                    return
                msg = f"PyPlanet update available: v{checker.latest}"
                app_ref._update_notice = msg
                suppress = await app_ref.setting_suppress_update.get_value()
                if not suppress:
                    try:
                        await checker.instance.chat(
                            "\uf1e6 $FD4$oPy$369Planet$z$s$fff \uf0e7 "
                            f"new version available: v{checker.latest}. Consider updating!"
                        )
                    except Exception:
                        pass
                    return
                logins = app_ref._online_admin_logins()
                if logins:
                    await app_ref._toast(msg, "warning", logins)

            async def _patched_connect(player, **kwargs):
                if int(getattr(player, "level", 0) or 0) <= 0:
                    return
                if checker.update_available is False:
                    return
                msg = f"PyPlanet update available: v{checker.latest}"
                app_ref._update_notice = msg
                suppress = await app_ref.setting_suppress_update.get_value()
                if not suppress:
                    try:
                        await checker.instance.gbx.multicall(
                            checker.instance.chat(
                                "\uf1e6 $FD4$oPy$369Planet$z$s$fff \uf0e7 "
                                f"new version available: v{checker.latest}. "
                                "Consider updating!",
                                player,
                            ),
                            checker.instance.chat(
                                "$fffClick $l[http://pypla.net/en/stable/intro/upgrading.html]"
                                "here$l to open the upgrade instructions.",
                                player,
                            ),
                        )
                    except Exception:
                        pass
                    return
                if int(getattr(player, "level", 0) or 0) >= _ADMIN_MIN_LEVEL:
                    await app_ref._toast(msg, "warning", [player.login])

            checker.check = _patched_check
            checker.connect = _patched_connect
        except Exception:
            logger.exception("unbrand_pyplanet: failed to patch UpdateChecker")

    def _online_admin_logins(self) -> list[str]:
        out: list[str] = []
        try:
            for p in list(self.instance.player_manager.online):
                if int(getattr(p, "level", 0) or 0) >= _ADMIN_MIN_LEVEL:
                    out.append(p.login)
        except Exception:
            pass
        return out

    async def _toast(self, message: str, severity: str, logins: list[str]) -> None:
        if not logins:
            return
        if not self._status_messages_available():
            return
        try:
            sig = self.context.signals.get_signal("notification_engine:notify")
            await sig.send_robust({
                "message": message,
                "severity": severity,
                "login": logins,
            })
        except Exception:
            logger.exception("unbrand_pyplanet: failed to send notification_engine:notify")
