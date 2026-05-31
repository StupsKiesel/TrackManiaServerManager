"""tmsm Jukebox - server-wide map queue, ported from PyPlanet's contrib
jukebox onto the tmsm.ui / tmsm.hub framework.

Keeps the public API of the contrib app (``.jukebox`` list,
``add_to_jukebox``, ``drop_from_jukebox``, ``move_map``, ``insert_map``,
``append_map``, ``clear_jukebox``, ``podium_start``) so other apps (e.g.
the tmsm maplist) can interoperate without code changes.

Out of scope (kept compatible to add later):

* folder system (database-backed playlists)
* karma / local-records integration in views
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from xmlrpc.client import Fault

from aiohttp import web

from pyplanet.apps.config import AppConfig
from pyplanet.apps.core.maniaplanet import callbacks as mp_signals
from pyplanet.contrib.command import Command
from pyplanet.contrib.setting import Setting

from .views import JukeboxView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)


class App_Jukebox(AppConfig):
    name = "pyplanet.apps.tmsm.jukebox"
    label = "jukebox"  # shadow the contrib app under the same key
    app_dependencies = ["core.maniaplanet"]
    game_dependencies = ["trackmania", "trackmania_next", "shootmania"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Public API surface (compatible with pyplanet.apps.contrib.jukebox).
        self.lock = asyncio.Lock()
        self.jukebox: list[dict[str, Any]] = []

        # UI / state.
        self.view: JukeboxView | None = None
        self._state: dict[str, dict[str, Any]] = {}
        self._visible_logins: set[str] = set()

        # Asset HTTP server (serves the CD icon for the hub tile).
        # Host defaults to 127.0.0.1; override with TMSM_ASSET_HOST for
        # remote players (must be reachable from their TM client).
        self._asset_host = os.environ.get("TMSM_ASSET_HOST", "127.0.0.1")
        self._asset_port = int(os.environ.get("TMSM_JUKEBOX_ASSET_PORT", "8181"))
        self._asset_runner: web.AppRunner | None = None
        self._assets_dir = os.path.join(os.path.dirname(__file__), "assets")

        # Settings.
        self.setting_allow_juking = Setting(
            "allow_juking", "Allow juking of maps by players",
            Setting.CAT_BEHAVIOUR, type=bool,
            description="Allow juking maps by non-admin players.",
            default=True,
        )

    # ---- lifecycle -----------------------------------------------------

    async def on_start(self) -> None:
        # Permissions (mirrors contrib).
        await self.instance.permission_manager.register(
            "clear", "Clear the jukebox", app=self, min_level=1,
        )
        await self.instance.permission_manager.register(
            "move", "Move entries in the jukebox", app=self, min_level=1,
        )

        # Chat commands. We intentionally keep the `/jukebox` namespace
        # and `/clearjukebox` only; the `/list` umbrella from contrib is
        # left to the maplist app (which owns that hub command).
        await self.instance.command_manager.register(
            Command(
                command="clearjukebox", aliases=["cjb"],
                target=self.chat_clear, perms="jukebox:clear", admin=True,
                description="Clears the current maps from the jukebox.",
            ),
            Command(
                command="jukebox", target=self.chat_command,
                description="Provides access to the jukebox commands.",
            ).add_param(name="option", required=False),
        )

        # Settings.
        await self.context.setting.register(self.setting_allow_juking)

        # Map flow callback.
        self.context.signals.listen(
            mp_signals.flow.podium_start, self.podium_start,
        )

        # UI view.
        try:
            self.view = JukeboxView(self)
            self.view.connect("clear", self._on_clear)
            self.view.connect("drop_mine", self._on_drop_mine)
            self.view.connect("refresh", self._on_refresh)
            self.view.connect("open_maplist", self._on_open_maplist)
            self.view.handle_catch_all = self._catch_all  # type: ignore[assignment]
        except Exception:
            logger.exception("jukebox: view init failed")
            self.view = None

        await self._start_asset_server()
        await self._register_with_hub()

    async def on_stop(self) -> None:
        if self.view is not None:
            try:
                await self.view.destroy()
            except Exception:
                logger.exception("jukebox: destroy failed")
            self.view = None
        await self._stop_asset_server()

    # ---- asset server -------------------------------------------------

    async def _start_asset_server(self) -> None:
        app = web.Application()
        app.router.add_get("/cd.png", self._handle_cd)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self._asset_port)
        try:
            await site.start()
            self._asset_runner = runner
            logger.info("jukebox: asset server on :%d", self._asset_port)
        except OSError as e:
            logger.warning("jukebox: asset server failed to bind :%d (%s)",
                           self._asset_port, e)
            await runner.cleanup()

    async def _stop_asset_server(self) -> None:
        if self._asset_runner is not None:
            await self._asset_runner.cleanup()
            self._asset_runner = None

    async def _handle_cd(self, request: web.Request) -> web.Response:
        path = os.path.join(self._assets_dir, "cd.png")
        try:
            with open(path, "rb") as f:
                blob = f.read()
        except OSError:
            return web.Response(status=404, text="cd.png missing")
        return web.Response(
            body=blob, content_type="image/png",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    # ---- hub integration ----------------------------------------------

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("jukebox: tmsm_hub:register not available yet")
            return
        entry = HubAppEntry(
            key="jukebox",
            name="Jukebox",
            icon="save",
            icon_image=f"http://{self._asset_host}:{self._asset_port}/cd.png",
            color="b6f",
            role=Role.PLAYER,
            order=11,
            description="See and manage the map queue.",
            open=self._open,
            command="jukebox",
        )
        await sig.send_robust({"entry": entry}, raw=True)

    async def _open(self, player) -> None:
        if self.view is None:
            return
        self._state.setdefault(player.login, self._default_state())
        try:
            await self.view.display(player_logins=[player.login])
            self._visible_logins.add(player.login)
            self.view._visible = bool(self._visible_logins)
        except Exception:
            logger.exception("jukebox: open failed")

    # ---- per-player state ---------------------------------------------

    def _default_state(self) -> dict[str, Any]:
        return {"status": "", "status_color": "aaa"}

    def _set_status(self, login: str, text: str, color: str = "aaa") -> None:
        st = self._state.setdefault(login, self._default_state())
        st["status"] = text
        st["status_color"] = color

    async def _refresh_view(self) -> None:
        if self.view is None:
            return
        try:
            if self._visible_logins and getattr(self.view, "_visible", False):
                await self.view.refresh()
        except Exception:
            logger.exception("jukebox: refresh failed")

    # ---- context for view ---------------------------------------------

    async def view_context(self, login: str) -> dict[str, Any]:
        st = self._state.setdefault(login, self._default_state())
        try:
            cm = self.instance.map_manager.current_map
            cur_name = str(getattr(cm, "name", "") or "")
            cur_uid = str(getattr(cm, "uid", "") or "")
        except Exception:
            cur_name = ""
            cur_uid = ""

        is_admin = False
        try:
            from pyplanet.apps.tmsm.ui import perms as _perms
            is_admin = _perms.is_operator(login)
        except Exception:
            is_admin = False

        try:
            allow_juking = bool(await self.setting_allow_juking.get_value())
        except Exception:
            allow_juking = True

        has_maplist = self._maplist_app() is not None

        entries = []
        for i, e in enumerate(self.jukebox, start=1):
            if not isinstance(e, dict):
                continue
            m = e.get("map")
            p = e.get("player")
            if m is None:
                continue
            entries.append({
                "index":      i,
                "uid":        str(getattr(m, "uid", "") or ""),
                "name":       str(getattr(m, "name", "") or "(unnamed)"),
                "environment": str(getattr(m, "environment", "") or ""),
                "requester":  str(getattr(p, "nickname", "")
                                  or getattr(p, "login", "") or ""),
                "requester_login": str(getattr(p, "login", "") or ""),
                "is_mine":    bool(p is not None
                                   and getattr(p, "login", None) == login),
            })

        return {
            "entries":      entries,
            "current_map":  cur_name,
            "current_uid":  cur_uid,
            "total":        len(entries),
            "is_admin":     is_admin,
            "allow_juking": allow_juking,
            "has_maplist":  has_maplist,
            "status":       st["status"],
            "status_color": st["status_color"],
        }

    # ---- view handlers ------------------------------------------------

    async def _catch_all(self, player, action, values, **kwargs) -> None:
        import re

        m = re.match(r"^(top|up|down|bottom|drop)__(\d+)$", action)
        if m:
            await self._on_row_action(player, m.group(1), int(m.group(2)))
            return

        if action == "_close" or action.startswith("_crumb__"):
            login = player.login
            self._visible_logins.discard(login)
            if self.view is not None:
                self.view._visible = bool(self._visible_logins)
                try:
                    from pyplanet.views.template import TemplateView
                    await TemplateView.hide(self.view, player_logins=[login])
                except Exception:
                    logger.exception("jukebox: hide on close failed")
            return

    async def _on_row_action(self, player, op: str, index: int) -> None:
        login = player.login
        # 1-based index from the template -> 0-based list slot.
        pos = index - 1
        if pos < 0 or pos >= len(self.jukebox):
            self._set_status(login, "queue position out of range", "fa0")
            await self._refresh_view()
            return
        entry = self.jukebox[pos]
        target_map = entry.get("map") if isinstance(entry, dict) else None

        if op == "drop":
            owner = entry.get("player")
            is_owner = bool(owner is not None
                            and getattr(owner, "login", None) == login)
            from pyplanet.apps.tmsm.ui import perms as _perms
            is_admin = _perms.is_operator(player)
            if not (is_owner or is_admin):
                self._set_status(
                    login, "only the requester or an admin can drop", "fa0",
                )
                await self._refresh_view()
                return
            await self.drop_from_jukebox_entry(player, entry)
            await self._refresh_view()
            return

        # All move ops require move permission.
        if op in ("top", "up", "down", "bottom"):
            try:
                has = await self.instance.permission_manager.has_permission(
                    player, "jukebox:move",
                )
            except Exception:
                has = False
            if not has:
                self._set_status(
                    login, "you need jukebox:move permission", "fa0",
                )
                await self._refresh_view()
                return
            if target_map is None:
                return
            new_pos = {
                "top":    0,
                "up":     "+1",
                "down":   "-1",
                "bottom": len(self.jukebox) - 1,
            }[op]
            res = await self.move_map(player, target_map, new_pos)
            if res is False:
                self._set_status(login, "move failed", "fa0")
            await self._refresh_view()

    async def _on_clear(self, player) -> None:
        # Mirrors /clearjukebox - admin only.
        from pyplanet.apps.tmsm.ui import perms as _perms
        if not _perms.is_operator(player):
            self._set_status(player.login,
                             "admins only", "f44")
            await self._refresh_view()
            return
        await self.clear_jukebox(player, None)
        await self._refresh_view()

    async def _on_drop_mine(self, player) -> None:
        # Equivalent of `/jukebox drop`.
        await self._chat_drop_own(player)
        await self._refresh_view()

    async def _on_refresh(self, player) -> None:
        await self._refresh_view()

    def _maplist_app(self):
        try:
            candidates = list(self.instance.apps.apps.values())
        except Exception:
            return None
        for app in candidates:
            if getattr(app, "name", "") == "pyplanet.apps.tmsm.maplist":
                return app
        return None

    async def _on_open_maplist(self, player) -> None:
        """Hide our window and hand off to the maplist app."""
        ml = self._maplist_app()
        opener = getattr(ml, "_open", None) if ml is not None else None
        if opener is None:
            self._set_status(player.login, "maplist app is not loaded", "f44")
            await self._refresh_view()
            return
        login = player.login
        if self.view is not None:
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.view, player_logins=[login])
                self._visible_logins.discard(login)
                self.view._visible = bool(self._visible_logins)
            except Exception:
                logger.exception("jukebox: hide view failed")
        try:
            await opener(player)
        except Exception:
            logger.exception("jukebox: failed to open maplist")

    # ---- public API (compatible with contrib jukebox) -----------------

    def insert_map(self, player, map, index: int = 0) -> None:
        self.jukebox.insert(index, {"player": player, "map": map})

    def append_map(self, player, map) -> None:
        self.jukebox.append({"player": player, "map": map})

    def empty_jukebox(self) -> None:
        self.jukebox.clear()
        try:
            self.lock.release()
        except RuntimeError:
            pass

    async def move_map(self, player, map, move_action):
        """Move a map within the queue.

        ``move_action`` is either ``"+1"`` / ``"-1"`` for relative moves or
        an int for an absolute new position. Returns the new index, or
        ``False`` on permission failure / no-op / out-of-bounds.
        """
        if player is not None and not await \
                self.instance.permission_manager.has_permission(
                    player, "jukebox:move"):
            return False

        try:
            current_index = next(
                i for i, e in enumerate(self.jukebox)
                if isinstance(e, dict) and e.get("map") == map
            )
        except StopIteration:
            logger.warning(
                "jukebox: map vanished from queue before move could apply",
            )
            return False

        if isinstance(move_action, str):
            new_position = (current_index - 1 if move_action == "+1"
                            else current_index + 1)
        else:
            new_position = int(move_action)

        if new_position < 0 or new_position > (len(self.jukebox) - 1):
            return False
        if new_position == current_index:
            return False

        entry = self.jukebox.pop(current_index)
        self.jukebox.insert(new_position, entry)
        return new_position

    async def add_to_jukebox(self, player, map) -> None:
        """Add ``map`` to the queue on behalf of ``player`` (chats outcome)."""
        from pyplanet.apps.tmsm.ui import perms as _perms
        is_admin = _perms.is_operator(player)
        async with self.lock:
            if not is_admin \
                    and not await self.setting_allow_juking.get_value():
                await self.instance.chat(
                    "$i$f00Juking maps has been disabled for players!",
                    player,
                )
                return

            if not is_admin and any(
                    isinstance(item, dict)
                    and item.get("player") is not None
                    and item["player"].login == player.login
                    for item in self.jukebox):
                await self.instance.chat(
                    "$i$f00You already have a map in the jukebox! Wait till "
                    "it's been played before adding another.",
                    player,
                )
                return

            try:
                cur = self.instance.map_manager.current_map
                same_as_current = (cur is not None
                                   and map.get_id() == cur.get_id())
            except Exception:
                same_as_current = False
            if same_as_current and not is_admin:
                await self.instance.chat(
                    "$i$f00You can't add the current map to the jukebox!",
                    player,
                )
                return

            if not any(isinstance(item, dict) and item.get("map") == map
                       for item in self.jukebox):
                self.jukebox.append({"player": player, "map": map})
                await self.instance.chat(
                    "$fff{}$z$s$fa0 was added to the jukebox by "
                    "$fff{}$z$s$fa0.".format(map.name, player.nickname),
                )
            else:
                await self.instance.chat(
                    "$i$f00This map has already been added to the jukebox, "
                    "pick another one.",
                    player,
                )

    async def drop_from_jukebox(self, player, instance) -> None:
        """Drop a map identified by ``instance`` (a dict with ``map_name``
        and ``player_login``), matching the contrib signature."""
        from pyplanet.apps.tmsm.ui import perms as _perms
        async with self.lock:
            if not _perms.is_operator(player) \
                    and instance.get("player_login") != player.login:
                await self.instance.chat(
                    "$i$f00You can only drop your own jukeboxed maps!",
                    player,
                )
                return
            drop_map = next(
                (item for item in self.jukebox
                 if isinstance(item, dict)
                 and item.get("map") is not None
                 and item["map"].name == instance.get("map_name")),
                None,
            )
            if drop_map is not None:
                self.jukebox.remove(drop_map)
                await self.instance.chat(
                    "$fff{}$z$s$fa0 dropped $fff{}$z$s$fa0 from the "
                    "jukebox.".format(player.nickname,
                                      instance.get("map_name", "")),
                )
        await self._refresh_view()

    async def drop_from_jukebox_entry(self, player, entry) -> None:
        """Variant taking the queue entry directly (used by the tmsm UI)."""
        async with self.lock:
            if entry in self.jukebox:
                self.jukebox.remove(entry)
                try:
                    m = entry.get("map")
                    await self.instance.chat(
                        "$fff{}$z$s$fa0 dropped $fff{}$z$s$fa0 from the "
                        "jukebox.".format(player.nickname,
                                          getattr(m, "name", "")),
                    )
                except Exception:
                    pass

    # ---- chat commands ------------------------------------------------

    async def chat_clear(self, player, data, **kwargs) -> None:
        await self.clear_jukebox(player, data)

    async def clear_jukebox(self, player, data, **kwargs) -> None:
        async with self.lock:
            if self.jukebox:
                self.jukebox.clear()
                await self.instance.chat(
                    "$ff0Admin $fff{}$z$s$ff0 has cleared the "
                    "jukebox.".format(player.nickname),
                )
            else:
                await self.instance.chat(
                    "$i$f00There are currently no maps in the jukebox.",
                    player,
                )

    async def chat_command(self, player, data, **kwargs) -> None:
        option = getattr(data, "option", None) if data is not None else None
        if option is None:
            await self._chat_help(player)
            return

        if option in ("list", "display"):
            await self._open(player)
            return
        if option == "drop":
            await self._chat_drop_own(player)
            return
        if option == "clear":
            from pyplanet.apps.tmsm.ui import perms as _perms
            if not _perms.is_operator(player):
                await self.instance.chat(
                    "$i$f00You're not allowed to do this!", player,
                )
                return
            await self.clear_jukebox(player, data)
            return
        await self._chat_help(player)

    async def _chat_drop_own(self, player) -> None:
        async with self.lock:
            mine = next(
                (item for item in reversed(self.jukebox)
                 if isinstance(item, dict)
                 and item.get("player") is not None
                 and item["player"].login == player.login),
                None,
            )
            if mine is not None:
                self.jukebox.remove(mine)
                await self.instance.chat(
                    "$fff{}$z$s$fa0 dropped $fff{}$z$s$fa0 from the "
                    "jukebox.".format(mine["player"].nickname,
                                      mine["map"].name),
                )
            else:
                await self.instance.chat(
                    "$i$f00You currently don't have a map in the jukebox.",
                    player,
                )

    async def _chat_help(self, player) -> None:
        msg = ("$ff0Available jukebox commands: $ffflist$ff0 | "
               "$fffdisplay$ff0 | $fffdrop$ff0")
        from pyplanet.apps.tmsm.ui import perms as _perms
        if _perms.is_operator(player):
            msg += " | $fffclear$ff0"
        msg += "."
        await self.instance.chat(msg, player)

    # ---- map-flow callbacks -------------------------------------------

    async def podium_start(self, **kwargs) -> None:
        if not self.jukebox:
            return
        nxt = self.jukebox.pop(0)
        if not isinstance(nxt, dict) or "map" not in nxt:
            return
        msg = ("$fa0The next map will be $fff{}$z$s$fa0 as requested by "
               "$fff{}$z$s$fa0.".format(
                   nxt["map"].name,
                   getattr(nxt.get("player"), "nickname", "?")))
        try:
            await asyncio.gather(
                self.instance.chat(msg),
                self.instance.map_manager.set_next_map(nxt["map"]),
            )
        except Fault as e:
            if "Map not in the selection" in e.faultString \
                    or "Map unknown" in e.faultString:
                await self.instance.chat(
                    "$fa0Setting the next map has been canceled because "
                    "the map is not on the server anymore!",
                )
                await self.podium_start()
            else:
                raise
        await self._refresh_view()