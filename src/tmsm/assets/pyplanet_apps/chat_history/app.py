"""Chat history admin app.

Shows a global timeline and per-player chat timelines. Per-player view includes:
- incoming player chat messages
- server/app messages sent only to that player (for example command outputs)
"""
from __future__ import annotations

import logging
import time
from typing import Any

from pyplanet.apps.config import AppConfig

from .views import ChatHistoryView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)

_GLOBAL_KEY = "__global__"
_MAX_GLOBAL = 3000
_MAX_PER_PLAYER = 800
_MAX_RENDER_LINES = 34


class ChatHistoryApp(AppConfig):
    name = "pyplanet.apps.tmsm.chat_history"
    label = "chat_history"
    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_hub"]
    game_dependencies = ["trackmania", "trackmania_next"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.view: ChatHistoryView | None = None
        self._global_log: list[dict[str, Any]] = []
        self._per_player: dict[str, list[dict[str, Any]]] = {}
        self._selected: dict[str, str] = {}
        self._status: dict[str, tuple[str, str]] = {}
        self._opened: set[str] = set()

        self._chat_prev = None
        self._chat_wrapper = None

    async def on_start(self) -> None:
        self.view = ChatHistoryView(self)
        self.view.connect("refresh", self._on_refresh)
        self.view.connect("clear_selected", self._on_clear_selected)
        self.view.connect("clear_all", self._on_clear_all)
        self.view.connect("back", self._on_back)
        self.view.handle_catch_all = self._catch_all

        try:
            self.context.signals.listen("maniaplanet:player_chat", self._on_player_chat)
            self.context.signals.listen("maniaplanet:player_disconnect", self._on_player_disconnect)
        except Exception:
            logger.exception("chat_history: failed to register signal listeners")

        self._install_chat_wrapper()
        await self._register_with_hub()

    async def on_stop(self) -> None:
        if self.view is not None:
            try:
                await self.view.destroy()
            except Exception:
                logger.exception("chat_history: view destroy failed")
        self._remove_chat_wrapper()

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("chat_history: tmsm_hub:register signal not available")
            return
        await sig.send_robust({
            "entry": HubAppEntry(
                key="chat_history",
                name="Chat History",
                icon="comments",
                role=Role.MASTER,
                order=23,
                description="Global + per-player chat timelines",
                open=self._open,
            )
        }, raw=True)

    async def _open(self, player) -> None:
        if self.view is None:
            return
        self._opened.add(player.login)
        self._selected.setdefault(player.login, _GLOBAL_KEY)
        await self._display(player.login)

    async def _display(self, login: str) -> None:
        if self.view is None:
            return
        try:
            await self.view.display(player_logins=[login])
        except Exception:
            logger.exception("chat_history: display failed for %s", login)

    async def _on_back(self, player, **kwargs) -> None:
        self._opened.discard(player.login)
        if self.view is not None:
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.view, player_logins=[player.login])
            except Exception:
                logger.exception("chat_history: hide failed")
        try:
            sig = self.context.signals.get_signal("tmsm_hub:show")
            await sig.send_robust({"player": player}, raw=True)
        except KeyError:
            pass

    def _set_status(self, login: str, text: str, color: str = "8af") -> None:
        self._status[login] = (text, color)

    async def _on_refresh(self, player, **kwargs) -> None:
        self._set_status(player.login, "Refreshed", "8af")
        await self._display(player.login)

    async def _on_clear_selected(self, player, **kwargs) -> None:
        key = self._selected.get(player.login, _GLOBAL_KEY)
        if key == _GLOBAL_KEY:
            self._global_log.clear()
            self._set_status(player.login, "Cleared global history", "fa0")
        else:
            self._per_player.pop(key, None)
            self._set_status(player.login, f"Cleared history for {key}", "fa0")
        await self._display(player.login)

    async def _on_clear_all(self, player, **kwargs) -> None:
        self._global_log.clear()
        self._per_player.clear()
        self._set_status(player.login, "Cleared all history", "f66")
        await self._display(player.login)

    async def _catch_all(self, player, action=None, values=None, **kwargs) -> None:
        action = str(action or "")
        prefix = f"{self.view.id}__" if self.view and self.view.id else ""
        if prefix and action.startswith(prefix):
            action = action[len(prefix):]
        if action.startswith("sel__"):
            key = action.split("sel__", 1)[1]
            if key:
                self._selected[player.login] = key
                self._set_status(player.login, f"Selected {('Global' if key == _GLOBAL_KEY else key)}", "8af")
                await self._display(player.login)

    async def _on_player_disconnect(self, player=None, **kwargs) -> None:
        login = getattr(player, "login", None)
        if login:
            self._opened.discard(login)

    async def _on_player_chat(self, player=None, text: str = "", **kwargs) -> None:
        login = getattr(player, "login", "") or str(kwargs.get("login") or "")
        nickname = getattr(player, "nickname", "") or login or "?"
        msg = str(text or kwargs.get("text") or "").strip()
        if not login or not msg:
            return
        entry = {
            "ts": time.time(),
            "kind": "in",
            "player": login,
            "nick": nickname,
            "text": msg,
            "audience": "all",
        }
        self._append_global(entry)
        self._append_player(login, entry)
        await self._refresh_opened()

    def _extract_targets(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> list[str]:
        candidates: list[Any] = []
        if args:
            candidates.append(args[0])
        for k in ("player", "login", "player_login", "player_logins"):
            if k in kwargs:
                candidates.append(kwargs.get(k))

        out: list[str] = []

        def add_one(v: Any) -> None:
            if v is None:
                return
            if isinstance(v, str):
                s = v.strip()
                if s:
                    out.append(s)
                return
            if hasattr(v, "login"):
                s = str(getattr(v, "login") or "").strip()
                if s:
                    out.append(s)
                return
            if isinstance(v, (list, tuple, set)):
                for x in v:
                    add_one(x)

        for c in candidates:
            add_one(c)

        # de-dup while preserving order
        return list(dict.fromkeys(out))

    def _install_chat_wrapper(self) -> None:
        if self._chat_prev is not None:
            return
        prev = getattr(self.instance, "chat", None)
        if not callable(prev):
            return
        self._chat_prev = prev

        async def _wrapped(message, *args, **kwargs):
            try:
                await self._record_outgoing(message, args, kwargs)
            except Exception:
                logger.exception("chat_history: outgoing capture failed")
            return await prev(message, *args, **kwargs)

        self._chat_wrapper = _wrapped
        try:
            setattr(self.instance, "chat", _wrapped)
        except Exception:
            self._chat_prev = None
            self._chat_wrapper = None
            logger.exception("chat_history: failed to patch instance.chat")

    def _remove_chat_wrapper(self) -> None:
        if self._chat_prev is None:
            return
        try:
            current = getattr(self.instance, "chat", None)
            if current is self._chat_wrapper:
                setattr(self.instance, "chat", self._chat_prev)
        except Exception:
            logger.exception("chat_history: failed restoring instance.chat")
        finally:
            self._chat_prev = None
            self._chat_wrapper = None

    async def _record_outgoing(self, message: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        text = str(message or "").strip()
        if not text:
            return
        targets = self._extract_targets(args, kwargs)
        if not targets:
            entry = {
                "ts": time.time(),
                "kind": "out",
                "player": "",
                "nick": "server",
                "text": text,
                "audience": "all",
            }
            self._append_global(entry)
            await self._refresh_opened()
            return

        for login in targets:
            entry = {
                "ts": time.time(),
                "kind": "out",
                "player": login,
                "nick": login,
                "text": text,
                "audience": "private",
            }
            self._append_player(login, entry)
            self._append_global(entry)
        await self._refresh_opened()

    async def _refresh_opened(self) -> None:
        if not self._opened:
            return
        # Best-effort lightweight live refresh for viewers currently
        # watching the history window.
        for login in list(self._opened):
            await self._display(login)

    def _append_global(self, entry: dict[str, Any]) -> None:
        self._global_log.append(entry)
        if len(self._global_log) > _MAX_GLOBAL:
            del self._global_log[: len(self._global_log) - _MAX_GLOBAL]

    def _append_player(self, login: str, entry: dict[str, Any]) -> None:
        buf = self._per_player.setdefault(login, [])
        buf.append(entry)
        if len(buf) > _MAX_PER_PLAYER:
            del buf[: len(buf) - _MAX_PER_PLAYER]

    def _player_label(self, login: str) -> str:
        try:
            for p in self.instance.player_manager.online:
                if str(getattr(p, "login", "")) == login:
                    nick = str(getattr(p, "nickname", "") or "")
                    if nick:
                        return f"{nick}$z [$888{login}$z]"
        except Exception:
            pass
        return login

    def _players_for_sidebar(self, selected: str) -> list[dict[str, Any]]:
        keys: set[str] = set(self._per_player.keys())
        try:
            for p in self.instance.player_manager.online:
                lg = str(getattr(p, "login", "") or "")
                if lg:
                    keys.add(lg)
        except Exception:
            pass

        rows = [{
            "key": _GLOBAL_KEY,
            "label": "Global",
            "count": len(self._global_log),
            "active": selected == _GLOBAL_KEY,
        }]

        for login in sorted(keys):
            rows.append({
                "key": login,
                "label": self._player_label(login),
                "count": len(self._per_player.get(login, [])),
                "active": selected == login,
            })
        return rows

    def _format_entry(self, e: dict[str, Any]) -> dict[str, Any]:
        ts = time.strftime("%H:%M:%S", time.localtime(float(e.get("ts") or 0.0)))
        kind = str(e.get("kind") or "")
        audience = str(e.get("audience") or "")
        player = str(e.get("player") or "")
        nick = str(e.get("nick") or "")
        text = str(e.get("text") or "")

        if kind == "in":
            line = f"[{ts}] <{nick}> {text}"
            color = "8df"
        else:
            if audience == "private" and player:
                line = f"[{ts}] ->{player}: {text}"
            else:
                line = f"[{ts}] [broadcast] {text}"
            color = "fc8"

        return {"line": line, "color": color}

    def build_view_context(self, viewer_login: str) -> dict[str, Any]:
        selected = self._selected.get(viewer_login, _GLOBAL_KEY)
        if selected != _GLOBAL_KEY and selected not in self._per_player:
            selected = _GLOBAL_KEY
            self._selected[viewer_login] = selected

        players = self._players_for_sidebar(selected)
        source = self._global_log if selected == _GLOBAL_KEY else self._per_player.get(selected, [])
        messages = [self._format_entry(e) for e in source[-_MAX_RENDER_LINES:]]

        st_text, st_color = self._status.get(viewer_login, ("", "8af"))
        return {
            "players": players,
            "messages": messages,
            "selected_key": selected,
            "selected_label": "Global" if selected == _GLOBAL_KEY else selected,
            "status": st_text,
            "status_color": st_color,
        }
