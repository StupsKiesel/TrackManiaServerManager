"""tmsm server — Server Settings + Mode Settings."""
from __future__ import annotations

import logging
from typing import Any

from pyplanet.apps.config import AppConfig

from .views import ModeSettingsView, ServerSettingsView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)


# (key, label, kind)  — kind in {str, int, bool, password}
SERVER_FIELDS: list[tuple[str, str, str]] = [
    ("Name", "Server name", "str"),
    ("Comment", "Comment", "str"),
    ("Password", "Player password", "password"),
    ("PasswordForSpectator", "Spectator password", "password"),
    ("HideServer", "Hide server (0/1/2)", "int"),
    ("CurrentMaxPlayers", "Max players", "int"),
    ("NextMaxPlayers", "Next max players", "int"),
    ("CurrentMaxSpectators", "Max spectators", "int"),
    ("NextMaxSpectators", "Next max spectators", "int"),
    ("KeepPlayerSlots", "Keep player slots", "bool"),
    ("IsP2PUpload", "P2P upload", "bool"),
    ("IsP2PDownload", "P2P download", "bool"),
    ("CurrentLadderMode", "Ladder mode", "int"),
    ("NextLadderMode", "Next ladder mode", "int"),
    ("CurrentVehicleNetQuality", "Vehicle net quality", "int"),
    ("NextVehicleNetQuality", "Next vehicle net quality", "int"),
    ("CurrentCallVoteTimeOut", "Vote timeout (ms)", "int"),
    ("NextCallVoteTimeOut", "Next vote timeout (ms)", "int"),
    ("CallVoteRatio", "Vote ratio", "str"),
    ("AllowMapDownload", "Allow map download", "bool"),
    ("AutoSaveReplays", "Auto-save replays", "bool"),
]
# the writable subset for SetServerOptions — keys must be there with their
# *current* values for non-edited fields. We keep them all and just send.

_WRITABLE_SUBSET = {k for k, _, _ in SERVER_FIELDS}


def _coerce(value: str, kind: str) -> Any:
    if kind == "int":
        try:
            return int(value)
        except ValueError:
            return 0
    if kind == "bool":
        v = (value or "").strip().lower()
        return v in ("1", "true", "yes", "on")
    return value or ""


def _render(value: Any, kind: str) -> str:
    if value is None:
        return ""
    if kind == "bool":
        return "1" if value else "0"
    return str(value)


class ServerApp(AppConfig):
    name = "pyplanet.apps.tmsm.server"
    label = "tmsm_server"
    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_hub"]
    game_dependencies = ["trackmania", "trackmania_next"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.settings_view: ServerSettingsView | None = None
        self.mode_view: ModeSettingsView | None = None
        # per-player draft edits: {login: {key: str_value}}
        self._draft: dict[str, dict[str, str]] = {}
        self._mode_draft: dict[str, dict[str, str]] = {}
        self._status: dict[str, tuple[str, str]] = {}
        self._mode_status: dict[str, tuple[str, str]] = {}

    async def on_start(self) -> None:
        self.settings_view = ServerSettingsView(self)
        self.mode_view = ModeSettingsView(self)

        self.settings_view.connect("save", self._on_settings_save)
        self.settings_view.connect("refresh", self._on_settings_refresh)
        self.settings_view.connect("back", self._on_back)
        self.settings_view.handle_catch_all = self._settings_catch_all

        self.mode_view.connect("save", self._on_mode_save)
        self.mode_view.connect("refresh", self._on_mode_refresh)
        self.mode_view.connect("back", self._on_back)
        self.mode_view.handle_catch_all = self._mode_catch_all

        await self._register_with_hub()

    async def on_stop(self) -> None:
        for v in (self.settings_view, self.mode_view):
            if v is None:
                continue
            try:
                await v.destroy()
            except Exception:
                logger.exception("server: destroy failed")

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("server: tmsm_hub:register signal not registered yet")
            return
        await sig.send_robust({"entry": HubAppEntry(
            key="server_settings", name="Server Settings", icon="cog",
            role=Role.MASTER, order=30,
            description="Edit dedicated server options (name, slots, passwords...)",
            open=self._open_settings,
        )}, raw=True)
        await sig.send_robust({"entry": HubAppEntry(
            key="mode_settings", name="Mode Settings", icon="flag",
            role=Role.MASTER, order=31,
            description="Edit the current mode script's settings live",
            open=self._open_mode,
        )}, raw=True)

    async def _open_settings(self, player) -> None:
        self._draft.pop(player.login, None)
        await self._open(self.settings_view, player)

    async def _open_mode(self, player) -> None:
        self._mode_draft.pop(player.login, None)
        await self._open(self.mode_view, player)

    async def _open(self, view, player) -> None:
        if view is None:
            return
        try:
            await view.display(player_logins=[player.login])
        except Exception:
            logger.exception("server: open display failed")

    async def _on_back(self, player, **kwargs) -> None:
        for v in (self.settings_view, self.mode_view):
            if v is None:
                continue
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(v, player_logins=[player.login])
            except Exception:
                logger.exception("server: hide failed")
        try:
            sig = self.context.signals.get_signal("tmsm_hub:show")
            await sig.send_robust({"player": player}, raw=True)
        except KeyError:
            pass

    # ================================================================
    # Server Settings
    # ================================================================

    async def server_settings_context(self, login: str) -> dict[str, Any]:
        try:
            current = await self.instance.gbx("GetServerOptions")
        except Exception as e:
            return {"fields": [], "loading": False,
                    "status": f"GetServerOptions failed: {e}", "status_color": "f44"}
        if not isinstance(current, dict):
            return {"fields": [], "loading": False,
                    "status": "unexpected response", "status_color": "f44"}
        draft = self._draft.get(login, {})
        fields = []
        for key, label, kind in SERVER_FIELDS:
            if key not in current:
                continue
            cur = current[key]
            val = draft.get(key, _render(cur, kind))
            fields.append({
                "key": key, "label": label, "kind": kind,
                "value": val, "dirty": key in draft,
            })
        status_text, status_color = self._status.get(login, ("", "aaa"))
        return {"fields": fields, "loading": False,
                "status": status_text, "status_color": status_color,
                "dirty_count": len([f for f in fields if f["dirty"]])}

    async def _settings_catch_all(self, player, action, values):
        # capture edits as the user types: any entry_<id>__field__<key> in values
        prefix = f"entry_{self.settings_view.id}__field__"
        draft = self._draft.setdefault(player.login, {})
        for k, v in (values or {}).items():
            if k.startswith(prefix):
                draft[k[len(prefix):]] = str(v or "")
        # toggle__<key> for booleans
        if "__toggle__" in action:
            key = action.rsplit("__", 1)[-1]
            kind = next((kn for kk, _l, kn in SERVER_FIELDS if kk == key), "str")
            if kind == "bool":
                cur_raw = draft.get(key)
                if cur_raw is None:
                    # no draft yet, fetch current
                    try:
                        current = await self.instance.gbx("GetServerOptions")
                        cur_raw = _render(current.get(key, False), "bool")
                    except Exception:
                        cur_raw = "0"
                draft[key] = "0" if _coerce(cur_raw, "bool") else "1"
            await self._open(self.settings_view, player)

    async def _on_settings_save(self, player, values=None, **kwargs) -> None:
        # absorb any pending entry values first
        await self._settings_catch_all(player, "save", values)
        draft = self._draft.get(player.login, {})
        if not draft:
            self._status[player.login] = ("no changes", "888")
            await self._open(self.settings_view, player)
            return
        try:
            current = await self.instance.gbx("GetServerOptions")
        except Exception as e:
            self._status[player.login] = (f"refetch failed: {e}", "f44")
            await self._open(self.settings_view, player)
            return
        payload = {}
        for key, _label, kind in SERVER_FIELDS:
            if key not in current:
                continue
            if key in draft:
                payload[key] = _coerce(draft[key], kind)
            else:
                payload[key] = current[key]
        try:
            await self.instance.gbx("SetServerOptions", payload)
        except Exception as e:
            self._status[player.login] = (f"SetServerOptions failed: {e}", "f44")
            await self._open(self.settings_view, player)
            return
        self._draft.pop(player.login, None)
        self._status[player.login] = (f"saved {len(draft)} fields", "0f0")
        await self._open(self.settings_view, player)

    async def _on_settings_refresh(self, player, **kwargs) -> None:
        self._draft.pop(player.login, None)
        self._status.pop(player.login, None)
        await self._open(self.settings_view, player)

    # ================================================================
    # Mode Settings
    # ================================================================

    async def mode_settings_context(self, login: str) -> dict[str, Any]:
        try:
            info = await self.instance.gbx("GetModeScriptInfo")
        except Exception as e:
            return {"mode_name": "", "fields": [], "loading": False,
                    "status": f"GetModeScriptInfo failed: {e}", "status_color": "f44"}
        try:
            values = await self.instance.gbx("GetModeScriptSettings")
        except Exception as e:
            return {"mode_name": info.get("Name", "?") if isinstance(info, dict) else "?",
                    "fields": [], "loading": False,
                    "status": f"GetModeScriptSettings failed: {e}", "status_color": "f44"}

        params = (info or {}).get("ParamDescs", []) or []
        if not isinstance(values, dict):
            values = {}
        draft = self._mode_draft.get(login, {})
        fields = []
        for p in params:
            key = p.get("Name") or ""
            if not key:
                continue
            kind = self._mode_kind(p.get("Type", "text"))
            cur = values.get(key, p.get("Default"))
            val = draft.get(key, _render(cur, kind))
            fields.append({
                "key": key,
                "label": p.get("Desc") or key,
                "kind": kind,
                "value": val,
                "default": _render(p.get("Default"), kind),
                "dirty": key in draft,
            })
        fields.sort(key=lambda f: f["label"].lower())
        status_text, status_color = self._mode_status.get(login, ("", "aaa"))
        return {"mode_name": (info or {}).get("Name", "?"),
                "fields": fields, "loading": False,
                "status": status_text, "status_color": status_color,
                "dirty_count": len([f for f in fields if f["dirty"]])}

    @staticmethod
    def _mode_kind(t: str) -> str:
        t = (t or "").lower()
        if "bool" in t:
            return "bool"
        if "int" in t:
            return "int"
        return "str"

    async def _mode_catch_all(self, player, action, values):
        prefix = f"entry_{self.mode_view.id}__field__"
        draft = self._mode_draft.setdefault(player.login, {})
        for k, v in (values or {}).items():
            if k.startswith(prefix):
                draft[k[len(prefix):]] = str(v or "")
        if "__toggle__" in action:
            key = action.rsplit("__", 1)[-1]
            cur = draft.get(key)
            if cur is None:
                try:
                    vs = await self.instance.gbx("GetModeScriptSettings")
                    cur = _render(vs.get(key, False), "bool") if isinstance(vs, dict) else "0"
                except Exception:
                    cur = "0"
            draft[key] = "0" if _coerce(cur, "bool") else "1"
            await self._open(self.mode_view, player)

    async def _on_mode_save(self, player, values=None, **kwargs) -> None:
        await self._mode_catch_all(player, "save", values)
        draft = self._mode_draft.get(player.login, {})
        if not draft:
            self._mode_status[player.login] = ("no changes", "888")
            await self._open(self.mode_view, player)
            return
        try:
            info = await self.instance.gbx("GetModeScriptInfo")
        except Exception as e:
            self._mode_status[player.login] = (f"info failed: {e}", "f44")
            await self._open(self.mode_view, player)
            return
        params = {p.get("Name"): self._mode_kind(p.get("Type", "")) for p in (info or {}).get("ParamDescs", [])}
        payload = {}
        for key, raw in draft.items():
            kind = params.get(key, "str")
            payload[key] = _coerce(raw, kind)
        try:
            await self.instance.gbx("SetModeScriptSettings", payload)
        except Exception as e:
            self._mode_status[player.login] = (f"SetModeScriptSettings failed: {e}", "f44")
            await self._open(self.mode_view, player)
            return
        self._mode_draft.pop(player.login, None)
        self._mode_status[player.login] = (f"saved {len(payload)} fields", "0f0")
        await self._open(self.mode_view, player)

    async def _on_mode_refresh(self, player, **kwargs) -> None:
        self._mode_draft.pop(player.login, None)
        self._mode_status.pop(player.login, None)
        await self._open(self.mode_view, player)
