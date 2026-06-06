"""Discord live status app.

Maintains exactly two Discord webhook messages:
  1) current map info (updated on map changes)
  2) current player list (updated on player connect/disconnect)

The app stores both Discord message IDs in settings so it can edit the
same two messages across restarts.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
from pyplanet.apps.config import AppConfig
from pyplanet.contrib.setting import Setting
from pyplanet.views.template import TemplateView

from .views import DiscordLiveStatusSettingsView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)


def _with_query(url: str, **params: str) -> str:
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q.update({k: v for k, v in params.items() if v is not None})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def _webhook_message_url(webhook_url: str, message_id: str) -> str:
    base = _with_query(webhook_url, wait=None)
    parts = urlsplit(base)
    path = parts.path.rstrip("/") + f"/messages/{message_id}"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _map_name_safe(raw: str) -> str:
    text = (raw or "").replace("$", "")
    return text.strip() or "(unknown)"


async def _get_tmx_track_id_for_current_map(current_map) -> int | None:
    """Resolve TMX track id for the currently running server map.

    Returns None when no TMX metadata row is available.
    """
    try:
        from pyplanet.apps.tmsm.tmx_browser.models import TmxMapMeta
    except Exception:
        return None

    map_id = int(getattr(current_map, "id", 0) or 0)
    map_uid = str(getattr(current_map, "uid", "") or "").strip()

    if map_id > 0:
        try:
            meta = await TmxMapMeta.get(server_map_id=map_id)
            return int(getattr(meta, "track_id", 0) or 0) or None
        except Exception:
            pass

    if map_uid:
        try:
            meta = await TmxMapMeta.get(uid=map_uid)
            return int(getattr(meta, "track_id", 0) or 0) or None
        except Exception:
            pass

    return None


class DiscordLiveStatusApp(AppConfig):
    name = "pyplanet.apps.tmsm.discord_live_status"
    label = "discord_live_status"
    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_hub"]
    game_dependencies = ["trackmania", "trackmania_next"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setting_enabled = Setting(
            "enabled", "Enabled",
            Setting.CAT_BEHAVIOUR, type=bool, default=False,
            description="Enable Discord live status webhook updates.",
        )
        self.setting_webhook_url = Setting(
            "webhook_url", "Discord webhook URL",
            Setting.CAT_BEHAVIOUR, type=str, default="",
            description="Incoming webhook URL to post/edit the two status messages.",
        )
        self.setting_map_message_id = Setting(
            "map_message_id", "Map message ID",
            Setting.CAT_BEHAVIOUR, type=str, default="",
            description="Internal: Discord message id for the map status message.",
        )
        self.setting_players_message_id = Setting(
            "players_message_id", "Players message ID",
            Setting.CAT_BEHAVIOUR, type=str, default="",
            description="Internal: Discord message id for the players status message.",
        )

        self._lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self._pending_player_refresh: asyncio.Task | None = None
        self._players_tick_task: asyncio.Task | None = None
        self.view: DiscordLiveStatusSettingsView | None = None
        self._settings_state: dict[str, dict[str, str | None]] = {}

    async def on_start(self) -> None:
        for s in (
            self.setting_enabled,
            self.setting_webhook_url,
            self.setting_map_message_id,
            self.setting_players_message_id,
        ):
            try:
                await self.context.setting.register(s)
            except Exception:
                logger.exception("discord_live_status: setting register failed: %s", s.key)

        timeout = aiohttp.ClientTimeout(total=10.0)
        self._session = aiohttp.ClientSession(timeout=timeout)

        self.view = DiscordLiveStatusSettingsView(self)
        self.view.connect("save", self._on_save)
        self.view.connect("sync", self._on_sync_now)
        self.view.connect("reset_ids", self._on_reset_ids)
        self.view.connect("_crumb__hub", self._on_back)
        self.view.handle_catch_all = self._catch_all  # type: ignore[assignment]

        for sig_name, handler in (
            ("maniaplanet:map_start", self._on_map_start),
            ("maniaplanet:player_connect", self._on_player_event),
            ("maniaplanet:player_disconnect", self._on_player_event),
        ):
            try:
                self.context.signals.listen(sig_name, handler)
            except Exception:
                logger.exception("discord_live_status: listen '%s' failed", sig_name)

        await self._register_with_hub()

        self._players_tick_task = asyncio.create_task(self._players_refresh_loop())

        # Bootstrap both messages once on startup.
        await self._sync_map_message()
        await self._sync_players_message()

    async def on_stop(self) -> None:
        if self._pending_player_refresh is not None:
            self._pending_player_refresh.cancel()
            self._pending_player_refresh = None
        if self._players_tick_task is not None:
            self._players_tick_task.cancel()
            self._players_tick_task = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        if self.view is not None:
            try:
                await self.view.destroy()
            except Exception:
                logger.exception("discord_live_status: view destroy failed")
            self.view = None

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("discord_live_status: tmsm_hub:register signal not registered yet")
            return
        await sig.send_robust({
            "entry": HubAppEntry(
                key="discord_live_status",
                name="Discord Live",
                icon="discord",
                icon_image="https://img.icons8.com/color/96/discord-logo.png",
                role=Role.ADMIN,
                description="Sync map + player status messages to Discord webhook",
                open=self._open_from_hub,
                order=92,
            ),
        }, raw=True)

    async def _open_from_hub(self, player) -> None:
        if self.view is None:
            return
        await self._refresh_settings(player)

    def _default_settings_state(self) -> dict[str, str | None]:
        return {
            "webhook_draft": None,
            "status": "",
            "status_color": "888",
        }

    def _absorb_entries(self, login: str, values: Any) -> None:
        if not values or self.view is None:
            return
        st = self._settings_state.setdefault(login, self._default_settings_state())
        key = f"entry_{self.view.id}__webhook"
        if key in values:
            st["webhook_draft"] = str(values.get(key) or "")

    async def build_settings_context(self, login: str) -> dict[str, Any]:
        st = self._settings_state.setdefault(login, self._default_settings_state())
        enabled = bool(await self.setting_enabled.get_value())
        stored_url = str(await self.setting_webhook_url.get_value() or "")
        webhook = st["webhook_draft"] if st.get("webhook_draft") is not None else stored_url
        map_id = str(await self.setting_map_message_id.get_value() or "")
        players_id = str(await self.setting_players_message_id.get_value() or "")
        return {
            "enabled": enabled,
            "webhook": webhook,
            "map_message_id": map_id,
            "players_message_id": players_id,
            "status": st.get("status") or "",
            "status_color": st.get("status_color") or "888",
        }

    async def _refresh_settings(self, player) -> None:
        if self.view is None:
            return
        try:
            await self.view.display(player_logins=[player.login])
        except Exception:
            logger.exception("discord_live_status: settings display failed")

    async def _on_back(self, player, **kwargs) -> None:  # noqa: ARG002
        if self.view is not None:
            try:
                await TemplateView.hide(self.view, player_logins=[player.login])
            except Exception:
                logger.exception("discord_live_status: hide failed")
        try:
            sig = self.context.signals.get_signal("tmsm_hub:show")
            await sig.send_robust({"player": player}, raw=True)
        except KeyError:
            pass

    async def _catch_all(self, player, action, values, **kwargs) -> None:
        self._absorb_entries(player.login, values)
        if not action:
            return
        if action == "toggle_enabled":
            try:
                cur = bool(await self.setting_enabled.get_value())
                await self.setting_enabled.set_value(not cur)
                st = self._settings_state.setdefault(player.login, self._default_settings_state())
                st["status"] = "Enabled." if not cur else "Disabled."
                st["status_color"] = "8f8" if not cur else "aaa"
            except Exception:
                logger.exception("discord_live_status: toggle enabled failed")
            await self._refresh_settings(player)

    async def _on_save(self, player, values=None, **kwargs) -> None:
        self._absorb_entries(player.login, values)
        st = self._settings_state.setdefault(player.login, self._default_settings_state())
        draft = (st.get("webhook_draft") or "").strip()
        if draft and not draft.startswith(("http://", "https://")):
            st["status"] = "Webhook URL must start with http:// or https://"
            st["status_color"] = "f44"
            await self._refresh_settings(player)
            return
        try:
            await self.setting_webhook_url.set_value(draft)
        except Exception:
            logger.exception("discord_live_status: save webhook failed")
            st["status"] = "Save failed (see server log)."
            st["status_color"] = "f44"
            await self._refresh_settings(player)
            return
        st["webhook_draft"] = None
        st["status"] = "Saved."
        st["status_color"] = "8f8"
        await self._refresh_settings(player)

    async def _on_sync_now(self, player, values=None, **kwargs) -> None:
        self._absorb_entries(player.login, values)
        st = self._settings_state.setdefault(player.login, self._default_settings_state())
        try:
            await self._sync_map_message()
            await self._sync_players_message()
        except Exception:
            logger.exception("discord_live_status: sync now failed")
            st["status"] = "Sync failed (see server log)."
            st["status_color"] = "f44"
            await self._refresh_settings(player)
            return
        st["status"] = "Synced map + players messages."
        st["status_color"] = "8f8"
        await self._refresh_settings(player)

    async def _on_reset_ids(self, player, values=None, **kwargs) -> None:
        self._absorb_entries(player.login, values)
        st = self._settings_state.setdefault(player.login, self._default_settings_state())
        try:
            await self.setting_map_message_id.set_value("")
            await self.setting_players_message_id.set_value("")
        except Exception:
            logger.exception("discord_live_status: reset message ids failed")
            st["status"] = "Reset failed (see server log)."
            st["status_color"] = "f44"
            await self._refresh_settings(player)
            return
        st["status"] = "Message ids cleared. Next sync will create fresh messages."
        st["status_color"] = "f80"
        await self._refresh_settings(player)

    async def _is_enabled(self) -> bool:
        try:
            return bool(await self.setting_enabled.get_value())
        except Exception:
            return False

    async def _webhook_url(self) -> str:
        try:
            return str(await self.setting_webhook_url.get_value() or "").strip()
        except Exception:
            return ""

    async def _request_json(self, method: str, url: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if self._session is None:
            return None
        try:
            async with self._session.request(method, url, json=payload) as resp:
                body = await resp.text()
                if resp.status not in (200, 204):
                    logger.warning(
                        "discord_live_status: webhook %s %s failed: %s %s",
                        method, urlsplit(url).path, resp.status, body[:200],
                    )
                    return None
                if not body.strip():
                    return None
                try:
                    return await resp.json()
                except Exception:
                    return None
        except aiohttp.ClientError:
            logger.exception("discord_live_status: network error on %s %s", method, url)
            return None

    async def _upsert_message(
        self,
        *,
        webhook_url: str,
        message_id: str,
        content: str,
    ) -> str:
        payload = {
            "content": content[:2000],
            "allowed_mentions": {"parse": []},
        }
        if message_id:
            msg_url = _webhook_message_url(webhook_url, message_id)
            result = await self._request_json("PATCH", msg_url, payload)
            if result is not None:
                return message_id
            # If edit fails (deleted message, stale id, etc.), fall through
            # to create a fresh message and store its new id.

        exec_url = _with_query(webhook_url, wait="true")
        created = await self._request_json("POST", exec_url, payload)
        if not isinstance(created, dict):
            return message_id
        new_id = str(created.get("id") or "").strip()
        return new_id or message_id

    async def _build_map_message(self) -> str:
        now = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        try:
            cm = self.instance.map_manager.current_map
        except Exception:
            cm = None
        if cm is None:
            return (
                "## Current Map\n"
                "No active map detected.\n\n"
                f"_Last update: {now}_"
            )

        # If TMX metadata exists, emit only the canonical TMX map URL.
        tmx_track_id = await _get_tmx_track_id_for_current_map(cm)
        if tmx_track_id:
            return f"https://trackmania.exchange/mapshow/{tmx_track_id}"

        name = _map_name_safe(str(getattr(cm, "name", "") or ""))
        uid = str(getattr(cm, "uid", "") or "")
        author = str(
            getattr(cm, "author_nickname", "")
            or getattr(cm, "author_login", "")
            or "?"
        )
        # mode script retrieval may fail depending on game state; best effort.
        try:
            mode_script = str(await self.instance.mode_manager.get_current_script() or "")
        except Exception:
            mode_script = ""

        lines = [
            "## Current Map",
            f"**Name:** {name}",
            f"**Author:** {author}",
        ]
        if uid:
            lines.append(f"**UID:** `{uid}`")
        if mode_script:
            lines.append(f"**Mode:** `{mode_script}`")
        lines.append("")
        lines.append(f"_Last update: {now}_")
        return "\n".join(lines)

    def _build_players_message(self) -> str:
        now = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        try:
            online = list(self.instance.player_manager.online)
        except Exception:
            online = []

        rows: list[tuple[str, str]] = []
        for p in online:
            login = str(getattr(p, "login", "") or "")
            nick = str(getattr(p, "nickname", "") or login)
            if not login:
                continue
            rows.append((nick, f"- {nick}"))

        rows.sort(key=lambda it: it[0].lower())
        lines = [line for _, line in rows]
        header = f"## Online Players ({len(rows)})"
        if not lines:
            return f"{header}\n_No players online._\n\n_Last update: {now}_"

        content = "\n".join([header, *lines, "", f"_Last update: {now}_"])
        if len(content) <= 2000:
            return content

        # Keep within Discord message limit.
        out = [header]
        used = len(header) + 1
        max_rows = 0
        for row in lines:
            if used + len(row) + 1 > 1860:
                break
            out.append(row)
            used += len(row) + 1
            max_rows += 1
        remaining = len(lines) - max_rows
        if remaining > 0:
            out.append(f"- ... and {remaining} more")
        out.extend(["", f"_Last update: {now}_"])
        return "\n".join(out)

    async def _sync_map_message(self) -> None:
        if not await self._is_enabled():
            return
        webhook_url = await self._webhook_url()
        if not webhook_url:
            return
        async with self._lock:
            content = await self._build_map_message()
            old_id = str(await self.setting_map_message_id.get_value() or "").strip()
            new_id = await self._upsert_message(
                webhook_url=webhook_url,
                message_id=old_id,
                content=content,
            )
            if new_id and new_id != old_id:
                await self.setting_map_message_id.set_value(new_id)

    async def _sync_players_message(self) -> None:
        if not await self._is_enabled():
            return
        webhook_url = await self._webhook_url()
        if not webhook_url:
            return
        async with self._lock:
            content = self._build_players_message()
            old_id = str(await self.setting_players_message_id.get_value() or "").strip()
            new_id = await self._upsert_message(
                webhook_url=webhook_url,
                message_id=old_id,
                content=content,
            )
            if new_id and new_id != old_id:
                await self.setting_players_message_id.set_value(new_id)

    async def _on_map_start(self, **kwargs) -> None:  # noqa: ARG002
        # Slight delay: map metadata is sometimes not fully updated at
        # the exact signal edge.
        await asyncio.sleep(0.25)
        await self._sync_map_message()

    async def _on_player_event(self, **kwargs) -> None:  # noqa: ARG002
        # Debounce connect/disconnect bursts into one edit call.
        if self._pending_player_refresh is not None and not self._pending_player_refresh.done():
            return

        async def _run() -> None:
            try:
                await asyncio.sleep(0.2)
                await self._sync_players_message()
            finally:
                self._pending_player_refresh = None

        self._pending_player_refresh = asyncio.create_task(_run())

    async def _players_refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60.0)
                await self._sync_players_message()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("discord_live_status: periodic players sync failed")
