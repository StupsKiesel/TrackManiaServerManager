"""Impersonate app - master-only UI-level override tester."""
from __future__ import annotations

import logging

from pyplanet.apps.config import AppConfig
from pyplanet.apps.tmsm.ui import perms
from pyplanet.contrib.command import Command

from .views import ImpersonatePickerView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)


_LEVEL_BY_NAME = {
    "player":   perms.LEVEL_PLAYER,
    "operator": perms.LEVEL_OPERATOR,
    "admin":    perms.LEVEL_ADMIN,
}
_NAME_BY_LEVEL = {v: k for k, v in _LEVEL_BY_NAME.items()}


class ImpersonateApp(AppConfig):
    name = "pyplanet.apps.tmsm.impersonate"
    label = "tmsm_impersonate"
    app_dependencies = ["core.maniaplanet", "tmsm_ui"]
    game_dependencies = ["trackmania", "trackmania_next"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.view: ImpersonatePickerView | None = None

    async def on_start(self) -> None:
        self.view = ImpersonatePickerView(self)
        self.view.connect("set_player",   self._on_set_player)
        self.view.connect("set_operator", self._on_set_operator)
        self.view.connect("set_admin",    self._on_set_admin)
        self.view.connect("clear",        self._on_clear)

        try:
            await self.instance.command_manager.register(
                Command(
                    command="impersonate",
                    target=self._cmd_impersonate,
                    description="Preview the tmsm UI as a lower permission level (master only).",
                ).add_param(name="level", required=False,
                            help="player | operator | admin | off"),
            )
        except Exception:
            logger.exception("impersonate: /impersonate command registration failed")

        self.context.signals.listen(
            "maniaplanet:player_disconnect", self._on_disconnect,
        )

        await self._register_with_hub()
        logger.info("impersonate: started")

    async def on_stop(self) -> None:
        # Wipe every override on shutdown so the next process boots clean.
        try:
            await perms.reset_all()
        except Exception:
            logger.exception("impersonate: reset_all on stop failed")
        if self.view is not None:
            try:
                await self.view.destroy()
            except Exception:
                logger.exception("impersonate: view destroy failed")
            self.view = None

    # ---- hub integration ----------------------------------------------

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("impersonate: tmsm_hub:register signal not registered yet")
            return
        entry = HubAppEntry(
            key="impersonate",
            name="Impersonate",
            icon="eye",
            color="b6f",
            role=Role.MASTER,
            order=50,
            description="Preview the tmsm UI at lower permission levels.",
            open=self._open,
            command="impersonate",
        )
        try:
            await sig.send_robust({"entry": entry}, raw=True)
        except Exception:
            logger.exception("impersonate: hub register failed")

    async def _open(self, player) -> None:
        # Real master required to open the picker - we read the real level,
        # NOT the effective one, otherwise an impersonating master would be
        # unable to switch back.
        if perms.get_real_level(player) < perms.LEVEL_MASTER:
            try:
                await self.instance.chat(
                    "$z$f00[impersonate]$z master admins only.", player,
                )
            except Exception:
                pass
            return
        if self.view is None:
            return
        try:
            await self.view.display(player_logins=[player.login])
            self.view._visible_logins.add(player.login)
            self.view._visible = True
        except Exception:
            logger.exception("impersonate: open display failed")

    # ---- chat command -------------------------------------------------

    async def _cmd_impersonate(self, player, data, **kwargs) -> None:
        if perms.get_real_level(player) < perms.LEVEL_MASTER:
            await self._chat(player, "$f00master admins only.")
            return
        raw = (getattr(data, "level", None) or "").strip().lower()
        if not raw:
            # No argument - open the picker UI.
            await self._open(player)
            return
        if raw in ("off", "none", "stop", "clear", "master"):
            await perms.clear_override(player.login)
            await self._chat(player, "$0afimpersonation cleared.")
            return
        level = _LEVEL_BY_NAME.get(raw)
        if level is None:
            await self._chat(player, "$f80usage: /impersonate player|operator|admin|off")
            return
        await perms.set_override(player.login, level)
        await self._chat(
            player, f"$0afimpersonating as $fff{_NAME_BY_LEVEL[level]}$0af."
        )

    async def _chat(self, player, msg: str) -> None:
        try:
            await self.instance.chat(f"$z$fff[impersonate]$z {msg}", player)
        except Exception:
            pass

    # ---- view actions -------------------------------------------------

    async def _on_set_player(self, player) -> None:
        await self._apply(player, perms.LEVEL_PLAYER)

    async def _on_set_operator(self, player) -> None:
        await self._apply(player, perms.LEVEL_OPERATOR)

    async def _on_set_admin(self, player) -> None:
        await self._apply(player, perms.LEVEL_ADMIN)

    async def _on_clear(self, player) -> None:
        if perms.get_real_level(player) < perms.LEVEL_MASTER:
            return
        await perms.clear_override(player.login)
        await self._refresh(player.login)

    async def _apply(self, player, level: int) -> None:
        if perms.get_real_level(player) < perms.LEVEL_MASTER:
            return
        await perms.set_override(player.login, level)
        await self._refresh(player.login)

    async def _refresh(self, login: str) -> None:
        if self.view is None:
            return
        if login not in self.view._visible_logins:
            return
        try:
            await self.view.display(player_logins=[login])
        except Exception:
            logger.exception("impersonate: refresh failed for %s", login)

    # ---- lifecycle hooks ----------------------------------------------

    async def _on_disconnect(self, player, **kwargs) -> None:
        login = getattr(player, "login", None)
        if not login:
            return
        if perms.get_override(login) is None:
            return
        try:
            await perms.clear_override(login)
        except Exception:
            logger.exception("impersonate: clear on disconnect failed for %s", login)

    # ---- view context -------------------------------------------------

    def view_context(self, login: str) -> dict:
        eff = perms.effective_level(login)
        real = perms.get_real_level(login)
        active = perms.get_override(login)
        return {
            "active":          active is not None,
            "effective":       eff,
            "effective_label": perms.level_label(login),
            "real":            real,
            "real_label":      {0: "player", 1: "operator", 2: "admin", 3: "master"}[real],
            "is_player":       eff == perms.LEVEL_PLAYER,
            "is_operator":     eff == perms.LEVEL_OPERATOR,
            "is_admin":        eff == perms.LEVEL_ADMIN,
            "is_master":       eff == perms.LEVEL_MASTER,
        }
