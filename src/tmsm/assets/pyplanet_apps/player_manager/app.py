"""Player manager app.

Master-admin hub app to list online players and run moderation actions.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from pyplanet.apps.config import AppConfig
from pyplanet.apps.tmsm.ui import perms
from pyplanet.contrib.command import Command

from .views import PlayerManagerView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)


_LEVEL_BY_NAME = {
    "player": perms.LEVEL_PLAYER,
    "operator": perms.LEVEL_OPERATOR,
    "admin": perms.LEVEL_ADMIN,
    "master": perms.LEVEL_MASTER,
}

_LEVEL_LABELS = {
    0: "player",
    1: "operator",
    2: "admin",
    3: "master",
}


class PlayerManagerApp(AppConfig):
    name = "pyplanet.apps.tmsm.player_manager"
    label = "player_manager"

    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_hub"]
    game_dependencies = ["trackmania", "trackmania_next"]

    PAGE_SIZE = 7

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.view: PlayerManagerView | None = None
        self._state: dict[str, dict[str, Any]] = {}

    async def on_start(self) -> None:
        self.view = PlayerManagerView(self)
        self.view.handle_catch_all = self._handle_action  # type: ignore[assignment]

        self.context.signals.listen("maniaplanet:player_connect", self._on_player_event)
        self.context.signals.listen("maniaplanet:player_disconnect", self._on_player_event)

        try:
            await self.instance.command_manager.register(
                Command(
                    command="player_manager",
                    target=self._cmd_open,
                    description="Open player manager (master admins only).",
                )
            )
        except Exception:
            logger.exception("player_manager: /player_manager command registration failed")

        await self._register_with_hub()

    async def on_stop(self) -> None:
        if self.view is not None:
            try:
                await self.view.destroy()
            except Exception:
                logger.exception("player_manager: view destroy failed")
            self.view = None

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("player_manager: tmsm_hub:register not available yet")
            return
        entry = HubAppEntry(
            key="player_manager",
            name="Player Manager",
            icon="users",
            color="e66",
            role=Role.MASTER,
            order=35,
            description="Manage online players (kick, ban, warn, level).",
            open=self._open,
            command="player_manager",
        )
        await sig.send_robust({"entry": entry}, raw=True)

    async def _cmd_open(self, player, data, **kwargs) -> None:
        await self._open(player)

    def _ensure_state(self, login: str) -> dict[str, Any]:
        state = self._state.get(login)
        if state is None:
            state = {
                "page": 1,
                "level_open_for": "",
                "search": "",
                "status": "",
                "status_color": "aaa",
                "confirm_open": False,
                "confirm_payload": {},
            }
            self._state[login] = state
        return state

    async def _open(self, player) -> None:
        if perms.get_real_level(player) < perms.LEVEL_MASTER:
            await self._chat(player, "$f00master admins only.")
            return
        if self.view is None:
            return
        login = str(getattr(player, "login", "") or "")
        self._ensure_state(login)
        self.view._visible = True
        self.view._visible_logins.add(login)
        await self.view.display(player_logins=[login])

    async def _chat(self, player, msg: str) -> None:
        try:
            await self.instance.chat(f"$z$fff[player_manager]$z {msg}", player)
        except Exception:
            pass

    async def _refresh_login(self, login: str) -> None:
        if self.view is None:
            return
        if login not in self.view._visible_logins:
            return
        try:
            await self.view.display(player_logins=[login])
        except Exception:
            logger.exception("player_manager: refresh failed for %s", login)

    async def _refresh_all_visible(self) -> None:
        if self.view is None:
            return
        if not self.view._visible_logins:
            return
        try:
            await self.view.display(player_logins=list(self.view._visible_logins))
        except Exception:
            logger.exception("player_manager: refresh all failed")

    def _online_players(self) -> list[Any]:
        try:
            players = list(self.instance.player_manager.online)
        except Exception:
            return []
        players.sort(key=lambda p: (str(getattr(p, "nickname", "") or "").lower(), str(getattr(p, "login", "") or "")))
        return players

    @staticmethod
    def _entry_value(values: Any, suffix: str) -> str:
        if not isinstance(values, dict):
            return ""
        tail = "__" + suffix
        for key, value in values.items():
            if str(key).endswith(tail):
                return str(value or "")
        return ""

    def _filter_players(self, players: list[Any], query: str) -> list[Any]:
        q = str(query or "").strip().lower()
        if not q:
            return players
        out: list[Any] = []
        for p in players:
            login = str(getattr(p, "login", "") or "")
            nickname = str(getattr(p, "nickname", login) or login)
            if q in login.lower() or q in nickname.lower():
                out.append(p)
        return out

    def _player_label(self, login: str) -> str:
        for p in self._online_players():
            if str(getattr(p, "login", "") or "") == login:
                return str(getattr(p, "nickname", login) or login)
        return login

    def _paginate(self, items: list[Any], page: int) -> tuple[list[Any], int, int]:
        total_pages = max(1, int(math.ceil(len(items) / float(self.PAGE_SIZE))))
        page = max(1, min(total_pages, int(page or 1)))
        start = (page - 1) * self.PAGE_SIZE
        end = start + self.PAGE_SIZE
        return items[start:end], page, total_pages

    def view_context(self, login: str) -> dict[str, Any]:
        st = self._ensure_state(login)
        players_all = self._filter_players(self._online_players(), st.get("search", ""))
        page_items, page, total_pages = self._paginate(players_all, st.get("page", 1))
        st["page"] = page

        rows: list[dict[str, Any]] = []
        open_for = str(st.get("level_open_for", "") or "")
        for p in page_items:
            plogin = str(getattr(p, "login", "") or "")
            if not plogin:
                continue
            rows.append({
                "login": plogin,
                "nickname": str(getattr(p, "nickname", plogin) or plogin),
                "effective_label": perms.level_label(p),
                "real_label": _LEVEL_LABELS.get(perms.get_real_level(p), "player"),
                "level_selected": _LEVEL_LABELS.get(perms.get_real_level(p), "player"),
                "level_open": open_for == plogin,
            })

        payload = st.get("confirm_payload", {}) if isinstance(st.get("confirm_payload"), dict) else {}
        confirm_verb = str(payload.get("verb", "") or "")
        confirm_target = str(payload.get("target", "") or "")
        confirm_value = str(payload.get("value", "") or "")
        confirm_open = bool(st.get("confirm_open", False)) and bool(confirm_verb and confirm_target)
        if confirm_verb == "level":
            confirm_title = "Change Player Level"
            confirm_message = (
                f"Set PyPlanet level for {self._player_label(confirm_target)} ({confirm_target}) "
                f"to {confirm_value}?"
            )
            confirm_variant = "warning"
            confirm_ok = "Apply"
            confirm_icon = "cog"
        elif confirm_verb == "warn":
            confirm_title = "Warn Player"
            confirm_message = (
                f"Send warning message to {self._player_label(confirm_target)} "
                f"({confirm_target})?"
            )
            confirm_variant = "warning"
            confirm_ok = "Warn"
            confirm_icon = "warning"
        elif confirm_verb == "kick":
            confirm_title = "Kick Player"
            confirm_message = (
                f"Kick {self._player_label(confirm_target)} ({confirm_target}) from the server?"
            )
            confirm_variant = "danger"
            confirm_ok = "Kick"
            confirm_icon = "user-times"
        else:
            confirm_title = "Ban Player"
            confirm_message = (
                f"Ban {self._player_label(confirm_target)} ({confirm_target}) from the server?"
            )
            confirm_variant = "danger"
            confirm_ok = "Ban"
            confirm_icon = "ban"

        return {
            "players": rows,
            "page": page,
            "total_pages": total_pages,
            "search": str(st.get("search", "") or ""),
            "status": str(st.get("status", "") or ""),
            "status_color": str(st.get("status_color", "aaa") or "aaa"),
            "level_options": [
                {"value": "player", "label": "Player"},
                {"value": "operator", "label": "Operator"},
                {"value": "admin", "label": "Admin"},
                {"value": "master", "label": "Master"},
            ],
            "confirm_open": confirm_open,
            "confirm_title": confirm_title,
            "confirm_message": confirm_message,
            "confirm_variant": confirm_variant,
            "confirm_ok": confirm_ok,
            "confirm_icon": confirm_icon,
        }

    async def _on_player_event(self, player=None, **kwargs) -> None:
        await self._refresh_all_visible()

    async def _handle_action(self, player, action, values=None, **kwargs):
        login = str(getattr(player, "login", "") or "")
        if not login:
            return
        if perms.get_real_level(player) < perms.LEVEL_MASTER:
            await self._chat(player, "$f00master admins only.")
            return

        st = self._ensure_state(login)

        try:
            if action == "search":
                st["search"] = self._entry_value(values, "search").strip()
                st["page"] = 1
                await self._refresh_login(login)
                return

            if action == "search__clear":
                st["search"] = ""
                st["page"] = 1
                await self._refresh_login(login)
                return

            if action == "refresh":
                st["status"] = ""
                await self._refresh_login(login)
                return

            if action == "confirm_action__cancel" or action == "confirm_action__backdrop":
                st["confirm_open"] = False
                st["confirm_payload"] = {}
                await self._refresh_login(login)
                return

            if action == "confirm_action__ok":
                payload = st.get("confirm_payload", {}) if isinstance(st.get("confirm_payload"), dict) else {}
                verb = str(payload.get("verb", "") or "")
                target = str(payload.get("target", "") or "")
                value = str(payload.get("value", "") or "")
                st["confirm_open"] = False
                st["confirm_payload"] = {}
                if verb == "level":
                    ok, msg = await self._set_pyplanet_level(target, value)
                else:
                    ok, msg = await self._perform_action(verb, player, target)
                st["status"] = msg
                st["status_color"] = "0af" if ok else "f44"
                await self._refresh_login(login)
                return

            if action.startswith("pager__"):
                verb = action[len("pager__"):]
                cur = int(st.get("page", 1) or 1)
                total_items = len(self._filter_players(self._online_players(), st.get("search", "")))
                total_pages = max(1, int(math.ceil(total_items / float(self.PAGE_SIZE))))
                if verb == "first":
                    cur = 1
                elif verb == "prev":
                    cur = max(1, cur - 1)
                elif verb == "next":
                    cur = min(total_pages, cur + 1)
                elif verb == "last":
                    cur = total_pages
                elif verb.startswith("page__"):
                    try:
                        cur = int(verb[len("page__"):])
                    except (TypeError, ValueError):
                        pass
                st["page"] = max(1, min(total_pages, cur))
                await self._refresh_login(login)
                return

            if action.startswith("lvl__"):
                parts = action.split("__")
                # lvl__<target_login>__toggle
                # lvl__<target_login>__pick__<value>
                if len(parts) >= 3 and parts[2] == "toggle":
                    target = parts[1]
                    cur = str(st.get("level_open_for", "") or "")
                    st["level_open_for"] = "" if cur == target else target
                    await self._refresh_login(login)
                    return
                if len(parts) >= 4 and parts[2] == "pick":
                    target = parts[1]
                    value = parts[3]
                    st["level_open_for"] = ""
                    st["confirm_open"] = True
                    st["confirm_payload"] = {
                        "verb": "level",
                        "target": target,
                        "value": value,
                    }
                    await self._refresh_login(login)
                    return

            for verb in ("warn", "kick", "ban"):
                prefix = verb + "__"
                if action.startswith(prefix):
                    target = action[len(prefix):]
                    st["confirm_open"] = True
                    st["confirm_payload"] = {
                        "verb": verb,
                        "target": target,
                    }
                    await self._refresh_login(login)
                    return
        except Exception:
            logger.exception("player_manager: action failed: %s", action)
            st["status"] = "action failed (see logs)"
            st["status_color"] = "f44"
            await self._refresh_login(login)

    async def _set_pyplanet_level(self, target_login: str, value: str) -> tuple[bool, str]:
        lvl = _LEVEL_BY_NAME.get(value)
        if lvl is None:
            return False, f"invalid level: {value}"

        target = await self._get_online_player(target_login)
        if target is None:
            return False, f"player not online: {target_login}"

        pm = getattr(self.instance, "permission_manager", None)
        if pm is not None:
            methods = (
                "set_level",
                "set_player_level",
                "set_player_permission_level",
                "set_user_level",
                "update_level",
            )
            for method in methods:
                fn = getattr(pm, method, None)
                if fn is None:
                    continue
                try:
                    res = fn(target, lvl)
                    if hasattr(res, "__await__"):
                        await res
                    return True, f"level for {target_login} set to {value}"
                except Exception:
                    continue

        try:
            setattr(target, "level", int(lvl))
            save = getattr(target, "save", None)
            if save is not None:
                res = save()
                if hasattr(res, "__await__"):
                    await res
            return True, f"level for {target_login} set to {value}"
        except Exception as exc:
            logger.exception("player_manager: set level failed for %s", target_login)
            return False, f"set level failed: {exc}"

    async def _get_online_player(self, login: str):
        try:
            return await self.instance.player_manager.get_player(login=login, lock=False)
        except Exception:
            return None

    async def _perform_action(self, verb: str, actor, target_login: str) -> tuple[bool, str]:
        target = await self._get_online_player(target_login)
        if target is None:
            return False, f"player not online: {target_login}"

        actor_login = str(getattr(actor, "login", "") or "")
        if actor_login and actor_login == target_login and verb in ("kick", "ban"):
            return False, "cannot kick/ban yourself"

        if verb == "warn":
            try:
                await self.instance.chat(
                    "$z$f80[player_manager]$z Warning from server staff: please follow server rules.",
                    target,
                )
                return True, f"warned {target_login}"
            except Exception as exc:
                return False, f"warn failed: {exc}"

        if verb == "kick":
            ok = await self._try_gbx_calls([
                ("Kick", [target_login, "kicked by server staff"]),
                ("Kick", [target_login]),
            ])
            return (True, f"kicked {target_login}") if ok else (False, f"kick failed for {target_login}")

        if verb == "ban":
            ok = await self._try_gbx_calls([
                ("BanAndBlackList", [target_login, "banned by server staff"]),
                ("Ban", [target_login, "banned by server staff"]),
                ("Ban", [target_login]),
                ("BlackList", [target_login]),
            ])
            return (True, f"banned {target_login}") if ok else (False, f"ban failed for {target_login}")

        return False, "unknown action"

    async def _try_gbx_calls(self, calls: list[tuple[str, list[Any]]]) -> bool:
        for method, args in calls:
            try:
                await self.instance.gbx(method, *args)
                return True
            except Exception:
                continue
        return False
