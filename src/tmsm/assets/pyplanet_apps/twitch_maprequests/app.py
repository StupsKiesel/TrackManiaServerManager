"""Twitch Map Requests — viewers add TMX maps via chat, auto-removed after play.

Flow:
  1. Streamer enables the app per pool and sets `twitch_channel`.
  2. A single anonymous IRC connection listens to that channel.
  3. On `!mr <tmx-id>` (configurable), we check permission + cooldowns,
     fetch TMX metadata, validate against safety rails, download +
     install the map, add it to the jukebox, and remember its UID.
  4. When that map ends (`mp_signals.map.map_end`), we remove it from
     the dedicated and its `.Gbx` file. Sidecar JSON survives restarts
     so leftover temp maps are cleaned on next boot.

All configuration lives in PyPlanet `Setting`s (DB-backed, per-pool),
so the streamer manages it via `/settings` in-game or the tmsm UI.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Optional

import aiohttp

from pyplanet.apps.config import AppConfig
from pyplanet.apps.core.maniaplanet import callbacks as mp_signals
from pyplanet.contrib.setting import Setting

from .irc import ChatMessage, TwitchIRC
from . import tmx_info as tmx
from .views import TwitchMrView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role, Status
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)

# `!mr 12345` / `!mr 12345  whatever after is ignored`
_TRIGGER_TAIL_RE = re.compile(r"\s*(\d+)\b")

# Safe filename pattern for files we write to UserData/Maps/tmsm-twitch/.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")

_STATE_FILENAME = "twitch_maprequests_state.json"
_MAP_SUBDIR = "tmsm-twitch"


def _safe_filename(name: str, track_id: int) -> str:
    leaf = _SAFE_NAME_RE.sub("_", name).strip("_") or f"map_{track_id}"
    return f"{_MAP_SUBDIR}/{leaf}_{track_id}.Map.Gbx"


class App_TwitchMapRequests(AppConfig):
    name = "pyplanet.apps.tmsm.twitch_maprequests"
    label = "twitch_maprequests"
    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_hub"]
    game_dependencies = ["trackmania_next"]

    # ── lifecycle ──────────────────────────────────────────────────────

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._irc: TwitchIRC | None = None
        self._irc_task: asyncio.Task | None = None
        self._irc_signature: tuple[str, str, str] = ("", "", "")

        # In-memory tracking — mirrored to sidecar JSON.
        # uid -> {track_id, filename, requested_by, added_at}
        self._tracked: dict[str, dict[str, Any]] = {}

        # Cooldowns.
        self._last_global_ts: float = 0.0
        self._last_user_ts: dict[str, float] = {}

        # Recent activity feed for the config window. Bounded ring buffer.
        # entries are dicts: {ts, level, text} (level in 'ok'/'warn'/'err'/'info').
        self._recent: list[dict[str, Any]] = []
        self._recent_cap = 30

        # UI view + per-player draft edits.
        self.view: TwitchMrView | None = None
        # login -> {setting_key: str}
        self._draft: dict[str, dict[str, str]] = {}
        # login -> active group key (config window pagination)
        self._active_tab: dict[str, str] = {}

        # All settings registered in on_start(); declared here so editors
        # have autocomplete and the setting list is one obvious block.
        s = Setting
        c = Setting.CAT_BEHAVIOUR
        self.s_enabled = s("twitch_mr_enabled",
            "Enable Twitch map requests", c, type=bool, default=False,
            description="Master on/off switch — turns the chat listener and the !mr command on or off.")
        self.s_channel = s("twitch_mr_channel",
            "Twitch channel", c, type=str, default="",
            description="The Twitch channel to listen in (lowercase, no leading #).")
        self.s_command = s("twitch_mr_command",
            "Chat command", c, type=str, default="!mr",
            description="Chat trigger. Usage in Twitch chat: `<command> <tmx-id>` (e.g. `!mr 12345`).")
        self.s_bot_user = s("twitch_mr_bot_username",
            "Bot Twitch username (optional)", c, type=str, default="",
            description="Only needed if you want the bot to reply in chat. Leave empty for anonymous read-only mode.")
        self.s_bot_oauth = s("twitch_mr_bot_oauth",
            "Bot OAuth token (optional)", c, type=str, default="",
            description="`oauth:...` token for the bot account. Required for chat replies; leave empty for read-only.")
        self.s_chat_replies = s("twitch_mr_chat_replies",
            "Post replies in Twitch chat", c, type=bool, default=False,
            description="Confirm/deny requests in chat. Requires the bot username + OAuth above.")
        # Permission gating (all settable from /settings).
        self.s_allow_everyone = s("twitch_mr_allow_everyone",
            "Allow everyone", c, type=bool, default=True,
            description="When on, any chatter may use the command (gated only by cooldowns).")
        self.s_allow_subs = s("twitch_mr_allow_subscribers",
            "Allow subscribers", c, type=bool, default=False,
            description="When 'allow everyone' is off, subscribers may use the command.")
        self.s_allow_vips = s("twitch_mr_allow_vips",
            "Allow VIPs", c, type=bool, default=False,
            description="When 'allow everyone' is off, VIPs may use the command.")
        self.s_allow_mods = s("twitch_mr_allow_moderators",
            "Allow moderators", c, type=bool, default=True,
            description="Moderators and the broadcaster can always use the command unless 'allow everyone' is on and you disable this.")
        # Cooldowns + queue cap.
        self.s_cd_user = s("twitch_mr_cooldown_user_sec",
            "Per-user cooldown (sec)", c, type=int, default=60,
            description="Seconds a Twitch user must wait between requests. 0 disables.")
        self.s_cd_global = s("twitch_mr_cooldown_global_sec",
            "Global cooldown (sec)", c, type=int, default=5,
            description="Seconds the listener pauses between successful requests. 0 disables.")
        self.s_max_pending = s("twitch_mr_max_pending",
            "Max pending temp maps", c, type=int, default=5,
            description="Cap on simultaneously-installed temp maps (those not yet played + removed). Extra requests are rejected.")
        # Safety rails.
        self.s_max_length = s("twitch_mr_max_length_sec",
            "Max map length (sec, 0 = off)", c, type=int, default=0,
            description="Reject requests for maps longer than this many seconds. 0 disables the check.")
        self.s_max_difficulty = s("twitch_mr_max_difficulty",
            "Max difficulty (Beginner|Intermediate|Advanced|Expert|Lunatic|Impossible|off)",
            c, type=str, default="",
            description="Reject maps harder than this TMX difficulty. Empty / 'off' disables the check.")
        self.s_refuse_in_jukebox = s("twitch_mr_refuse_if_jukeboxed",
            "Refuse if already in jukebox", c, type=bool, default=True,
            description="Reject the request if the same map is already queued.")
        self.s_refuse_in_playlist = s("twitch_mr_refuse_if_in_playlist",
            "Refuse if already in map list", c, type=bool, default=True,
            description="Reject the request if the map is already on the server's map rotation.")

        self._all_settings = [
            self.s_enabled, self.s_channel, self.s_command,
            self.s_bot_user, self.s_bot_oauth, self.s_chat_replies,
            self.s_allow_everyone, self.s_allow_subs,
            self.s_allow_vips, self.s_allow_mods,
            self.s_cd_user, self.s_cd_global, self.s_max_pending,
            self.s_max_length, self.s_max_difficulty,
            self.s_refuse_in_jukebox, self.s_refuse_in_playlist,
        ]

    async def on_start(self) -> None:
        for s in self._all_settings:
            await self.context.setting.register(s)

        self.context.signals.listen(mp_signals.map.map_end, self._on_map_end)

        # Build the config view (gracefully no-op if tmsm_ui isn't present).
        try:
            self.view = TwitchMrView(self)
            self.view.handle_catch_all = self._catch_all  # type: ignore[assignment]
        except Exception:
            logger.exception("twitch_maprequests: view init failed")
            self.view = None

        await self._load_state()
        await self._cleanup_orphans_on_boot()
        await self._restart_irc_if_needed()
        await self._register_with_hub()

    async def on_stop(self) -> None:
        await self._stop_irc()
        if self.view is not None:
            try:
                await self.view.destroy()
            except Exception:
                logger.exception("twitch_maprequests: destroy failed")
            self.view = None

    # ── hub tile (info / quick status) ─────────────────────────────────

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            return
        entry = HubAppEntry(
            key="twitch_maprequests",
            name="Twitch Requests",
            icon="twitch",
            color="93f",
            role=Role.ADMIN,
            status=Status.NEW,
            order=40,
            description="Let your Twitch chat add TMX maps via !mr.",
            open=self._hub_open,
            command="twitchmr",
            author="tmsm",
            version="0.1",
        )
        await sig.send_robust({"entry": entry}, raw=True)

    async def _hub_open(self, player) -> None:
        """Hub tile click — show the config view (admin-only)."""
        if self.view is None:
            return
        # Drop stale drafts whenever the window is re-opened.
        self._draft.pop(player.login, None)
        self._active_tab.pop(player.login, None)
        try:
            await self.view.display(player_logins=[player.login])
            # Track visibility so background refreshes (chat events) can
            # re-render only for players who actually have the window open.
            self.view._visible = True
            self.view._visible_logins.add(player.login)
        except Exception:
            logger.exception("twitch_maprequests: display failed for %s", player.login)

    # ── settings → IRC session lifecycle ───────────────────────────────

    async def _current_signature(self) -> tuple[bool, str, str, str]:
        return (
            bool(await self.s_enabled.get_value()),
            (await self.s_channel.get_value() or "").strip().lower().lstrip("#"),
            (await self.s_bot_user.get_value() or "").strip().lower(),
            (await self.s_bot_oauth.get_value() or "").strip(),
        )

    async def _restart_irc_if_needed(self) -> None:
        enabled, channel, bot, oauth = await self._current_signature()
        sig_now = (channel, bot, oauth)
        alive = self._irc_task is not None and not self._irc_task.done()
        if enabled and channel:
            if alive and sig_now == self._irc_signature:
                return
            await self._stop_irc()
            self._irc_signature = sig_now
            self._irc = TwitchIRC(channel, self._on_chat,
                                  nick=bot or None,
                                  oauth=oauth or None)
            self._irc_task = asyncio.ensure_future(self._irc.run())
            logger.info("twitch_maprequests: listening on #%s (auth=%s)",
                        channel, "oauth" if oauth else "anonymous")
        else:
            if alive:
                await self._stop_irc()

    async def _stop_irc(self) -> None:
        irc = self._irc
        task = self._irc_task
        self._irc = None
        self._irc_task = None
        if irc is not None:
            try:
                await irc.stop()
            except Exception:
                pass
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # ── chat handler ───────────────────────────────────────────────────

    async def _on_chat(self, msg: ChatMessage) -> None:
        try:
            if not await self.s_enabled.get_value():
                return
            cmd = (await self.s_command.get_value() or "!mr").strip()
            if not cmd:
                return
            text = msg.text.strip()
            if not text.lower().startswith(cmd.lower()):
                return
            tail = text[len(cmd):]
            m = _TRIGGER_TAIL_RE.match(tail)
            if not m:
                await self._reply(msg, f"@{msg.nick} usage: {cmd} <tmx-id>")
                return
            track_id = int(m.group(1))

            allowed, deny_reason = await self._permission_check(msg)
            if not allowed:
                await self._reply(msg, f"@{msg.nick} {deny_reason}")
                return

            now = time.monotonic()
            cd_user = int(await self.s_cd_user.get_value() or 0)
            cd_global = int(await self.s_cd_global.get_value() or 0)
            if cd_user > 0:
                last = self._last_user_ts.get(msg.nick.lower(), 0.0)
                if now - last < cd_user:
                    wait = int(cd_user - (now - last)) + 1
                    await self._reply(msg, f"@{msg.nick} cooldown ({wait}s left)")
                    return
            if cd_global > 0 and now - self._last_global_ts < cd_global:
                wait = int(cd_global - (now - self._last_global_ts)) + 1
                await self._reply(msg, f"@{msg.nick} bot is busy, try again in {wait}s")
                return

            cap = int(await self.s_max_pending.get_value() or 0)
            if cap > 0 and len(self._tracked) >= cap:
                await self._reply(msg,
                    f"@{msg.nick} request queue is full ({cap}); wait until a map gets played")
                return

            await self._handle_request(msg, track_id)
        except Exception:
            logger.exception("twitch_maprequests: chat handler crashed")

    async def _permission_check(self, msg: ChatMessage) -> tuple[bool, str]:
        # Broadcaster always allowed.
        if msg.is_broadcaster():
            return True, ""
        allow_everyone = await self.s_allow_everyone.get_value()
        allow_mods = await self.s_allow_mods.get_value()
        if allow_mods and msg.is_moderator():
            return True, ""
        if allow_everyone:
            return True, ""
        if await self.s_allow_subs.get_value() and msg.is_subscriber():
            return True, ""
        if await self.s_allow_vips.get_value() and msg.is_vip():
            return True, ""
        return False, "not allowed to request maps"

    # ── the actual request flow ────────────────────────────────────────

    async def _handle_request(self, msg: ChatMessage, track_id: int) -> None:
        try:
            info = await tmx.fetch_info(track_id)
        except aiohttp.ClientError as e:
            await self._reply(msg, f"@{msg.nick} TMX lookup failed ({e})")
            return
        if info is None:
            await self._reply(msg, f"@{msg.nick} TMX map #{track_id} not found")
            return
        if not info["downloadable"]:
            await self._reply(msg, f"@{msg.nick} that map is not publicly downloadable")
            return

        # Safety rails.
        max_len = int(await self.s_max_length.get_value() or 0)
        if max_len > 0 and info["length_ms"] and info["length_ms"] // 1000 > max_len:
            await self._reply(msg,
                f"@{msg.nick} that map is too long ({info['length_ms']//1000}s; limit {max_len}s)")
            return
        max_diff_raw = (await self.s_max_difficulty.get_value() or "").strip().lower()
        if max_diff_raw and max_diff_raw not in ("off", "none", "-"):
            cap_id = tmx.DIFFICULTY_BY_NAME.get(max_diff_raw)
            if cap_id is None:
                logger.warning("twitch_maprequests: unknown max_difficulty %r", max_diff_raw)
            elif info["difficulty_id"] is not None and info["difficulty_id"] > cap_id:
                await self._reply(msg,
                    f"@{msg.nick} too hard ({info['difficulty_name']}; max {tmx.DIFFICULTIES[cap_id]})")
                return

        uid = info["uid"]
        # Duplicate-detection (uid based — robust against filename collisions).
        if uid:
            if uid in self._tracked:
                await self._reply(msg, f"@{msg.nick} already in queue")
                return
            refuse_pl = await self.s_refuse_in_playlist.get_value()
            if refuse_pl and self._uid_in_playlist(uid):
                await self._reply(msg, f"@{msg.nick} already on the server")
                return
            refuse_jb = await self.s_refuse_in_jukebox.get_value()
            if refuse_jb and self._uid_in_jukebox(uid):
                await self._reply(msg, f"@{msg.nick} already in the jukebox")
                return

        # Download + install + jukebox-add (mirrors tmx_browser).
        ok, server_map, err = await self._install_map(track_id, info["name"])
        if not ok:
            await self._reply(msg, f"@{msg.nick} install failed: {err}")
            return

        jukebox = self.instance.apps.apps.get("jukebox")
        if jukebox is None:
            # No jukebox — we still added it to the playlist, but can't queue.
            await self._reply(msg, f"@{msg.nick} added (jukebox unavailable)")
        else:
            try:
                # add_to_jukebox(player, map) — pass a synthetic 'player' the
                # contrib code tolerates (it only uses .login and .nickname).
                await jukebox.add_to_jukebox(
                    _TwitchPlayer(msg.nick), server_map,
                )
            except Exception:
                logger.exception("twitch_maprequests: add_to_jukebox failed")

        # Track + persist.
        real_uid = getattr(server_map, "uid", "") or uid
        self._tracked[real_uid] = {
            "track_id":     track_id,
            "filename":     getattr(server_map, "file", ""),
            "requested_by": msg.nick,
            "added_at":     time.time(),
        }
        await self._save_state()

        now = time.monotonic()
        self._last_global_ts = now
        self._last_user_ts[msg.nick.lower()] = now

        await self._reply(msg,
            f"@{msg.nick} queued: {info['name']} by {info['uploader']}")
        self._push_recent(
            "ok",
            f"{msg.nick} → {info['name']} (#{track_id})",
        )
        await self._refresh_view()

    async def _install_map(self, track_id: int, display_name: str
                           ) -> tuple[bool, Any, str]:
        """Download from TMX, write to UserData/Maps/tmsm-twitch/, add to dedicated.

        Returns ``(ok, server_map_or_None, error_string)``. Refactored to
        be reusable; closely follows the working flow in
        ``tmx_browser/app.py::_on_add``.
        """
        try:
            from .tmx_info import _USER_AGENT  # reuse same UA
        except Exception:
            _USER_AGENT = "tmsm/1.0"

        url = f"https://trackmania.exchange/mapgbx/{int(track_id)}"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60),
                headers={"User-Agent": _USER_AGENT},
            ) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    if resp.status == 404:
                        return False, None, "map gone (404)"
                    resp.raise_for_status()
                    blob = await resp.read()
        except (aiohttp.ClientError, OSError) as e:
            return False, None, f"download error: {e}"
        if not blob:
            return False, None, "empty download"

        filename = _safe_filename(display_name, track_id)
        storage = self.instance.storage
        try:
            sub_dir = f"{storage.MAP_FOLDER}/{_MAP_SUBDIR}"
            if not await storage.driver.exists(sub_dir):
                await storage.driver.mkdir(sub_dir)
            async with storage.open_map(filename, "wb+") as fw:
                await fw.write(blob)
        except Exception as e:
            logger.exception("twitch_maprequests: write map file failed")
            return False, None, f"write failed: {e}"

        try:
            await self.instance.map_manager.add_map(
                filename, insert=True, save_matchsettings=False,
            )
        except Exception as e:
            if "already added" not in str(e).lower():
                logger.exception("twitch_maprequests: add_map failed")
                return False, None, f"add_map failed: {e}"

        # Resolve to a Map ORM object so the jukebox accepts it.
        try:
            from pyplanet.apps.core.maniaplanet.models import Map as _Map
            info = await self.instance.gbx("GetMapInfo", filename)
            if not info:
                return True, None, ""
            try:
                author_nick = await self.instance.map_manager.get_map_author_nickname(info)
            except Exception:
                author_nick = ""
            uploaded = await _Map.get_or_create_from_info(
                uid=info["UId"],
                name=info["Name"],
                author_login=info["Author"],
                author_nickname=author_nick,
                file=info["FileName"],
                environment=info.get("Environnement", ""),
                map_type=info.get("MapType", ""),
                map_style=info.get("MapStyle", ""),
                num_laps=info.get("NbLaps", 0),
                num_checkpoints=info.get("NbCheckpoints", 0),
                time_author=info.get("AuthorTime", 0),
                time_bronze=info.get("BronzeTime", 0),
                time_silver=info.get("SilverTime", 0),
                time_gold=info.get("GoldTime", 0),
                price=info.get("CopperPrice", 0),
            )
            return True, uploaded, ""
        except Exception as e:
            logger.exception("twitch_maprequests: post-add lookup failed")
            return True, None, str(e)

    # ── lifecycle: remove after the map ends ───────────────────────────

    async def _on_map_end(self, map, **kwargs) -> None:
        uid = getattr(map, "uid", "") or ""
        if not uid or uid not in self._tracked:
            return
        record = self._tracked.pop(uid, None) or {}
        await self._save_state()
        # Remove from dedicated + delete .Gbx so we don't leak files.
        try:
            await self.instance.map_manager.remove_map(map, delete_file=True)
            logger.info("twitch_maprequests: removed played temp map %s (%s)",
                        getattr(map, "name", uid), uid)
            self._push_recent(
                "info",
                f"removed after play: {getattr(map, 'name', uid)}",
            )
        except Exception:
            logger.exception("twitch_maprequests: remove_map failed for %s", uid)
            # Fallback: try removing by filename.
            fn = record.get("filename")
            if fn:
                try:
                    await self.instance.map_manager.remove_map(fn, delete_file=True)
                except Exception:
                    pass
        await self._refresh_view()

    async def _cleanup_orphans_on_boot(self) -> None:
        """At pool start, drop any tracked maps still on the server.

        Either the pool crashed mid-set, or removal failed last time. Either
        way the map has had its one play; clean it.
        """
        if not self._tracked:
            return
        # Snapshot — we mutate during iteration.
        for uid, record in list(self._tracked.items()):
            fn = record.get("filename") or ""
            try:
                if fn:
                    await self.instance.map_manager.remove_map(fn, delete_file=True)
            except Exception:
                logger.info("twitch_maprequests: boot cleanup skip %s (%s)",
                            uid, fn)
            self._tracked.pop(uid, None)
        await self._save_state()

    # ── duplicate checks ───────────────────────────────────────────────

    def _uid_in_playlist(self, uid: str) -> bool:
        try:
            for m in self.instance.map_manager.maps:
                if getattr(m, "uid", "") == uid:
                    return True
        except Exception:
            pass
        return False

    def _uid_in_jukebox(self, uid: str) -> bool:
        jukebox = self.instance.apps.apps.get("jukebox")
        if jukebox is None:
            return False
        try:
            for entry in getattr(jukebox, "jukebox", []) or []:
                m = entry.get("map") if isinstance(entry, dict) else None
                if m is not None and getattr(m, "uid", "") == uid:
                    return True
        except Exception:
            pass
        return False

    # ── sidecar state ──────────────────────────────────────────────────

    def _state_path(self) -> str:
        try:
            from pyplanet.conf import settings as pp_settings
            root = getattr(pp_settings, "POOL_ROOT", "") or os.getcwd()
        except Exception:
            root = os.getcwd()
        data_dir = os.path.join(root, "data")
        try:
            os.makedirs(data_dir, exist_ok=True)
        except OSError:
            data_dir = root
        return os.path.join(data_dir, _STATE_FILENAME)

    async def _load_state(self) -> None:
        path = self._state_path()
        if not os.path.isfile(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("tracked"), dict):
                self._tracked = {
                    str(k): dict(v)
                    for k, v in data["tracked"].items()
                    if isinstance(v, dict)
                }
        except (OSError, ValueError):
            logger.exception("twitch_maprequests: failed to load state")

    async def _save_state(self) -> None:
        path = self._state_path()
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"tracked": self._tracked}, f, indent=2)
            os.replace(tmp, path)
        except OSError:
            logger.exception("twitch_maprequests: failed to save state")

    # ── helpers ────────────────────────────────────────────────────────

    def _push_recent(self, level: str, text: str) -> None:
        """Append a line to the in-app activity feed (no in-game chat)."""
        self._recent.append({"ts": time.time(), "level": level, "text": text})
        if len(self._recent) > self._recent_cap:
            del self._recent[: len(self._recent) - self._recent_cap]

    async def _refresh_view(self) -> None:
        """Re-render the config view for whoever currently has it open."""
        if self.view is None:
            return
        try:
            await self.view.refresh()
        except Exception:
            pass

    async def _reply(self, msg: ChatMessage, text: str) -> None:
        """Optionally echo back to Twitch chat. Always logs."""
        logger.info("twitch_maprequests: %s", text)
        # Reject/cooldown/duplicate notices land in the activity feed too.
        lvl = "warn" if any(tok in text for tok in ("not allowed", "cooldown",
                            "already", "too long", "too hard", "failed",
                            "not found", "queue is full")) else "info"
        self._push_recent(lvl, text)
        # Re-render so cooldown/denial/duplicate notices actually appear in the
        # in-game activity feed (not just on a successful request).
        await self._refresh_view()
        if not await self.s_chat_replies.get_value():
            return
        irc = self._irc
        if irc is None:
            return
        try:
            await irc.say(text)
        except Exception:
            logger.exception("twitch_maprequests: chat reply failed")

    # ── config view: context + actions ─────────────────────────────────

    # Stable display order for the form. Each entry: (key, group, kind,
    # label, choices_or_None). Mirrors the Setting list above.
    _FORM = [
        ("twitch_mr_enabled",                "general",     "bool",   "Enabled",                 None),
        ("twitch_mr_channel",                "general",     "str",    "Twitch channel",          None),
        ("twitch_mr_command",                "general",     "str",    "Chat command",            None),
        ("twitch_mr_bot_username",           "bot",         "str",    "Bot username",            None),
        ("twitch_mr_bot_oauth",              "bot",         "secret", "Bot OAuth (oauth:…)",     None),
        ("twitch_mr_chat_replies",           "bot",         "bool",   "Reply in Twitch chat",    None),
        ("twitch_mr_allow_everyone",         "perms",       "bool",   "Everyone",                None),
        ("twitch_mr_allow_subscribers",      "perms",       "bool",   "Subscribers",             None),
        ("twitch_mr_allow_vips",             "perms",       "bool",   "VIPs",                    None),
        ("twitch_mr_allow_moderators",       "perms",       "bool",   "Moderators",              None),
        ("twitch_mr_cooldown_user_sec",      "limits",      "int",    "Per-user cooldown (s)",   None),
        ("twitch_mr_cooldown_global_sec",    "limits",      "int",    "Global cooldown (s)",     None),
        ("twitch_mr_max_pending",            "limits",      "int",    "Max pending temp maps",   None),
        ("twitch_mr_max_length_sec",         "rails",       "int",    "Max map length (s, 0=off)", None),
        ("twitch_mr_max_difficulty",         "rails",       "choice", "Max difficulty",
                                                ["", "Beginner", "Intermediate", "Advanced",
                                                 "Expert", "Lunatic", "Impossible"]),
        ("twitch_mr_refuse_if_jukeboxed",    "rails",       "bool",   "Refuse if jukeboxed",     None),
        ("twitch_mr_refuse_if_in_playlist",  "rails",       "bool",   "Refuse if in playlist",   None),
    ]

    _SETTING_BY_KEY = property(lambda self: {s.key: s for s in self._all_settings})

    _GROUPS = [
        ("general", "General"),
        ("bot",     "Bot (Twitch chat replies)"),
        ("perms",   "Who may request"),
        ("limits",  "Cooldowns & queue"),
        ("rails",   "Safety rails"),
    ]

    @staticmethod
    def _render_value(value, kind: str) -> str:
        if value is None:
            return ""
        if kind == "bool":
            return "1" if bool(value) else "0"
        return str(value)

    @staticmethod
    def _coerce(raw: str, kind: str):
        raw = (raw or "").strip()
        if kind == "bool":
            return raw in ("1", "true", "True", "yes", "on")
        if kind == "int":
            try:
                return int(raw) if raw else 0
            except ValueError:
                return 0
        return raw

    async def view_context(self, login: str) -> dict:
        """Build the per-player template context for the config view."""
        by_key = self._SETTING_BY_KEY
        baseline: dict[str, str] = {}
        for key, _g, kind, _lbl, _c in self._FORM:
            s = by_key.get(key)
            if s is None:
                continue
            try:
                cur = await s.get_value()
            except Exception:
                cur = None
            baseline[key] = self._render_value(cur, kind)

        draft = self._draft.get(login, {})
        dirty_count = sum(1 for k, v in draft.items() if v != baseline.get(k))
        dirty_by_group: dict[str, int] = {}
        for fkey, raw in draft.items():
            if raw == baseline.get(fkey):
                continue
            for fk, fg, _kd, _l, _c in self._FORM:
                if fk == fkey:
                    dirty_by_group[fg] = dirty_by_group.get(fg, 0) + 1
                    break

        active = self._active_tab.get(login) or self._GROUPS[0][0]
        if active not in {g[0] for g in self._GROUPS}:
            active = self._GROUPS[0][0]

        tabs_list = []
        for gkey, gname in self._GROUPS:
            n = dirty_by_group.get(gkey, 0)
            tabs_list.append({
                "key":   gkey,
                "label": (gname + ("  *" if n else "")),
            })

        # Only render the active group's fields — keeps the window uncluttered.
        fields: list[dict] = []
        for key, gk, kind, label, choices in self._FORM:
            if gk != active:
                continue
            s = by_key.get(key)
            base = baseline.get(key, "")
            val = draft.get(key, base)
            fields.append({
                "key":     key,
                "label":   label,
                "kind":    kind,
                "value":   val,
                "value_bool": (val == "1"),
                "dirty":   (val != base),
                "choices": choices or [],
                "desc":    (getattr(s, "description", "") or "") if s else "",
            })

        # Status summary.
        enabled = baseline.get("twitch_mr_enabled") == "1"
        channel = baseline.get("twitch_mr_channel") or ""
        irc_alive = self._irc_task is not None and not self._irc_task.done()
        if enabled and channel and irc_alive:
            status_text, status_color = "listening on #" + channel, "0f0"
        elif enabled and channel:
            status_text, status_color = "starting…", "fa0"
        elif enabled:
            status_text, status_color = "no channel set", "f60"
        else:
            status_text, status_color = "disabled", "888"

        cap_val = baseline.get("twitch_mr_max_pending") or "0"
        try:
            cap_int = int(cap_val)
        except ValueError:
            cap_int = 0

        recent = [
            {
                "level": e.get("level", "info"),
                "text":  str(e.get("text", ""))[:80],
            }
            for e in reversed(self._recent[-12:])
        ]

        return {
            "tabs_list":   tabs_list,
            "active_tab":  active,
            "fields":      fields,
            "dirty_count": dirty_count,
            "status":      status_text,
            "status_color": status_color,
            "pending":     len(self._tracked),
            "pending_cap": cap_int,
            "recent":      recent,
        }

    async def _absorb(self, login: str, values: dict | None) -> None:
        """Merge form values into the per-player draft, excluding equal-to-baseline."""
        if not values or self.view is None:
            return
        prefix = f"entry_{self.view.id}__field__"
        by_key = self._SETTING_BY_KEY
        # Build a quick (key -> kind) lookup.
        kind_by_key = {k: kd for k, _g, kd, _l, _c in self._FORM}
        draft = self._draft.setdefault(login, {})
        for k, v in values.items():
            if not k.startswith(prefix):
                continue
            field_key = k[len(prefix):]
            if field_key not in by_key:
                continue
            new = str(v if v is not None else "").strip()
            # Re-render baseline same way we compare in view_context.
            s = by_key[field_key]
            try:
                cur = await s.get_value()
            except Exception:
                cur = None
            base = self._render_value(cur, kind_by_key.get(field_key, "str"))
            if new == base:
                draft.pop(field_key, None)
            else:
                draft[field_key] = new
        if not draft:
            self._draft.pop(login, None)

    async def _catch_all(self, player, action: str, values) -> None:
        login = player.login

        if action == "back":
            await self._on_back(player)
            return
        if action == "save":
            await self._on_save(player, values)
            return
        if action == "reset_drafts":
            self._draft.pop(login, None)
            await self._toast(player, "drafts cleared", "info")
            await self._refresh_view()
            return
        if action == "refresh":
            await self._absorb(login, values)
            await self._refresh_view()
            return

        if action.startswith("groups__tab__"):
            new_tab = action[len("groups__tab__"):]
            # absorb anything the user typed on the current tab before switching
            await self._absorb(login, values)
            self._active_tab[login] = new_tab
            await self._refresh_view()
            return

        if action.startswith("toggle__"):
            key = action[len("toggle__"):]
            await self._toggle(login, key)
            await self._refresh_view()
            return
        if action.startswith("cycle__"):
            rest = action[len("cycle__"):]
            try:
                key, direction = rest.rsplit("__", 1)
            except ValueError:
                return
            await self._cycle(login, key, +1 if direction == "next" else -1)
            await self._refresh_view()
            return
        if action.startswith("reset_field__"):
            key = action[len("reset_field__"):]
            self._draft.get(login, {}).pop(key, None)
            if not self._draft.get(login):
                self._draft.pop(login, None)
            await self._refresh_view()
            return

    async def _toggle(self, login: str, key: str) -> None:
        by_key = self._SETTING_BY_KEY
        if key not in by_key:
            return
        s = by_key[key]
        try:
            cur = await s.get_value()
        except Exception:
            cur = False
        base = "1" if bool(cur) else "0"
        draft = self._draft.setdefault(login, {})
        new = "0" if draft.get(key, base) == "1" else "1"
        if new == base:
            draft.pop(key, None)
        else:
            draft[key] = new
        if not draft:
            self._draft.pop(login, None)

    async def _cycle(self, login: str, key: str, step: int) -> None:
        choices = None
        for fk, _g, _k, _l, ch in self._FORM:
            if fk == key:
                choices = ch
                break
        if not choices:
            return
        s = self._SETTING_BY_KEY.get(key)
        if s is None:
            return
        try:
            cur = await s.get_value()
        except Exception:
            cur = ""
        base = self._render_value(cur, "str")
        draft = self._draft.setdefault(login, {})
        val = draft.get(key, base)
        try:
            idx = choices.index(val)
        except ValueError:
            idx = -1
        nxt = (idx + step) % len(choices)
        new = choices[nxt]
        if new == base:
            draft.pop(key, None)
        else:
            draft[key] = new
        if not draft:
            self._draft.pop(login, None)

    async def _on_save(self, player, values) -> None:
        login = player.login
        await self._absorb(login, values or {})
        draft = self._draft.get(login, {})
        if not draft:
            await self._toast(player, "nothing to save", "warning")
            await self._refresh_view()
            return
        by_key = self._SETTING_BY_KEY
        kind_by_key = {k: kd for k, _g, kd, _l, _c in self._FORM}
        saved = 0
        failed: list[str] = []
        for key, raw in list(draft.items()):
            s = by_key.get(key)
            if s is None:
                failed.append(key)
                continue
            try:
                await s.set_value(self._coerce(raw, kind_by_key.get(key, "str")))
                saved += 1
                draft.pop(key, None)
            except Exception as e:
                logger.exception("twitch_maprequests: save %s failed", key)
                failed.append(f"{key} ({e})")
        if not draft:
            self._draft.pop(login, None)
        await self._restart_irc_if_needed()
        if failed:
            await self._toast(player,
                f"saved {saved}, failed: {', '.join(failed)}", "error")
        else:
            await self._toast(player, f"saved {saved} change(s)", "success")
        await self._refresh_view()

    async def _on_back(self, player) -> None:
        if self.view is None:
            return
        self.view._visible_logins.discard(player.login)
        if not self.view._visible_logins:
            self.view._visible = False
        try:
            from pyplanet.views.template import TemplateView
            await TemplateView.hide(self.view, player_logins=[player.login])
        except Exception:
            logger.exception("twitch_maprequests: hide failed")
        try:
            sig = self.context.signals.get_signal("tmsm_hub:show")
            await sig.send_robust({"player": player}, raw=True)
        except KeyError:
            pass

    _SEV_COLOR = {"info": "cce", "success": "0f0", "warning": "fa0", "error": "f44"}

    async def _toast(self, player, message: str, severity: str = "info") -> None:
        sig = None
        for code in ("notification_engine:notify", "tmsm_status:notify"):
            try:
                sig = self.context.signals.get_signal(code)
                break
            except KeyError:
                continue
        if sig is None:
            return
        try:
            await sig.send_robust({
                "message": message,
                "severity": severity,
                "login": player.login,
                "source": "twitch_maprequests",
            })
        except Exception:
            logger.exception("twitch_maprequests: toast emit failed")


class _TwitchPlayer:
    """Duck-typed minimal Player for jukebox.add_to_jukebox bookkeeping.

    The contrib jukebox uses .login (for cooldown / drop-mine) and
    .nickname (for chat messages). We don't want twitch:username to
    match any real login, so we prefix the login to keep it unique.
    """
    LEVEL_PLAYER = 0
    level = 0  # Player.LEVEL_PLAYER

    def __init__(self, twitch_nick: str) -> None:
        self.login = f"twitch:{twitch_nick.lower()}"
        self.nickname = f"$93ftwitch:$z$o{twitch_nick}$o"
        self.flow = type("F", (), {"is_spectator": True, "has_player_slot": False})()

    def get_level_string(self) -> str:
        return "Player"
