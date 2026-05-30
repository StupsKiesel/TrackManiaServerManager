"""tmsm server — Server Settings + Game Settings (mode + match settings)."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from pyplanet.apps.config import AppConfig

from .views import GameSettingsView, ServerSettingsView

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


def _clean_label(text: str) -> str:
    """Strip leading ManiaPlanet `$x` style codes and non-printable / non-ASCII
    leading chars from a mode-script Desc (Nadeo sometimes prefixes glyphs
    that have no representation in `GameFont` and render as squares)."""
    if not text:
        return ""
    s = text.lstrip()
    # drop leading $-escapes ($i, $o, $w, $z, $<rgb>...)
    while s.startswith("$") and len(s) >= 2:
        c = s[1].lower()
        if c in "oibwznmpgls":
            s = s[2:]
        elif c in "0123456789abcdef" and len(s) >= 4:
            s = s[4:]
        else:
            break
        s = s.lstrip()
    # drop leading non-printable / non-ASCII glyphs (square boxes in GameFont)
    while s and (ord(s[0]) < 0x20 or ord(s[0]) > 0x7e):
        s = s[1:]
    return s.strip()


class ServerApp(AppConfig):
    name = "pyplanet.apps.tmsm.server"
    label = "tmsm_server"
    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_hub"]
    game_dependencies = ["trackmania", "trackmania_next"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.settings_view: ServerSettingsView | None = None
        self.game_view: GameSettingsView | None = None
        # per-player draft edits: {login: {key: str_value}}
        self._draft: dict[str, dict[str, str]] = {}
        self._mode_draft: dict[str, dict[str, str]] = {}
        self._status: dict[str, tuple[str, str]] = {}
        self._mode_status: dict[str, tuple[str, str]] = {}
        # last-rendered baseline of mode script setting values per login
        # (so we can tell a typed value from an unchanged echo)
        self._mode_baseline: dict[str, dict[str, str]] = {}
        # game settings per-player ui state
        # tab: 'mode'|'match', switcher_open: bool, picker_page/match_page/mode_page: int,
        # save_as: str, confirm_delete: str (filename pending confirm)
        self._game_state: dict[str, dict[str, Any]] = {}
        # cached game-data-dir lookup
        self._game_data_dir: Path | None = None
        # last successfully loaded match settings file path (relative)
        self._loaded_profile: str | None = None

    async def on_start(self) -> None:
        self.settings_view = ServerSettingsView(self)
        self.game_view = GameSettingsView(self)

        self.settings_view.connect("save", self._on_settings_save)
        self.settings_view.connect("refresh", self._on_settings_refresh)
        self.settings_view.handle_catch_all = self._settings_catch_all

        self.game_view.connect("save", self._on_mode_save)
        self.game_view.connect("refresh", self._on_game_refresh)
        self.game_view.handle_catch_all = self._game_catch_all

        await self._register_with_hub()

    async def on_stop(self) -> None:
        for v in (self.settings_view, self.game_view):
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
            key="game_settings", name="Game Settings", icon="flag",
            role=Role.ADMIN, order=31,
            description="Edit current mode script settings; Master: switch mode / load match-settings profiles",
            open=self._open_game,
        )}, raw=True)

    async def _open_settings(self, player) -> None:
        self._draft.pop(player.login, None)
        await self._open(self.settings_view, player)

    async def _open_game(self, player) -> None:
        self._mode_draft.pop(player.login, None)
        await self._open(self.game_view, player)

    async def _open(self, view, player) -> None:
        if view is None:
            return
        try:
            await view.display(player_logins=[player.login])
        except Exception:
            logger.exception("server: open display failed")

    async def _on_back(self, player, **kwargs) -> None:
        for v in (self.settings_view, self.game_view):
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
    # Game Settings (Mode + Match Settings)
    # ================================================================

    # --- categorization of mode-script params ------------------------
    MODE_CATEGORIES: list[tuple[str, str, tuple[str, ...]]] = [
        # (key, label, substrings to match against lowercase param name)
        ("warmup",  "Warmup",        ("warmup",)),
        ("time",    "Time",          ("timelimit", "finishtimeout", "chattime",
                                       "graceperiod", "respawntime", "loading_time")),
        ("score",   "Score / Points", ("pointslimit", "pointsrepartition", "nbofwinners",
                                        "mappointslimit", "matchpointslimit", "scoreslimit")),
        ("rounds",  "Rounds / Match", ("roundsperma", "useties", "forcelaps",
                                        "disablegoto", "cup", "matchlimit",
                                        "alternaterules")),
        ("ui",      "UI / Chat",     ("displaynet", "hidenotice", "noticelifetime",
                                        "hudvis", "chat", "scorestable")),
    ]

    GAME_TABS = [
        {"key": "mode",  "label": "Mode"},
        {"key": "match", "label": "Match Settings"},
    ]

    LEVEL_ADMIN = 2
    LEVEL_MASTER = 3
    PAGE_SIZE = 12

    @staticmethod
    def _mode_kind(t: str) -> str:
        t = (t or "").lower()
        if "bool" in t:
            return "bool"
        if "int" in t:
            return "int"
        if "real" in t or "float" in t or "double" in t:
            return "real"
        return "str"

    def _gstate(self, login: str) -> dict[str, Any]:
        return self._game_state.setdefault(login, {
            "tab": "mode", "switcher_open": False,
            "mode_page": 0, "picker_page": 0,
            "match_page": 0, "save_as": "",
            "confirm_delete": "",
            "cat_pages": {},  # {cat_key: page_idx}
        })

    async def _game_data_dir_path(self) -> Path | None:
        if self._game_data_dir is not None:
            return self._game_data_dir
        try:
            raw = await self.instance.gbx("GameDataDirectory")
        except Exception:
            logger.exception("server: GameDataDirectory failed")
            return None
        if not raw:
            return None
        p = Path(str(raw))
        if p.is_dir():
            self._game_data_dir = p
        return self._game_data_dir

    async def _list_scripts(self) -> list[dict[str, str]]:
        gd = await self._game_data_dir_path()
        if gd is None:
            return []
        modes_root = gd / "Scripts" / "Modes"
        if not modes_root.is_dir():
            return []
        out: list[dict[str, str]] = []
        for p in sorted(modes_root.rglob("*.Script.txt")):
            rel = p.relative_to(modes_root).as_posix()
            out.append({"path": rel, "name": p.stem.replace(".Script", ""),
                        "group": rel.split("/", 1)[0] if "/" in rel else ""})
        return out

    async def _list_match_profiles(self) -> list[dict[str, Any]]:
        gd = await self._game_data_dir_path()
        if gd is None:
            return []
        ms_dir = gd / "Maps" / "MatchSettings"
        if not ms_dir.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for p in sorted(ms_dir.glob("*.txt")):
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({
                "rel": f"MatchSettings/{p.name}",
                "name": p.stem,
                "size": st.st_size,
            })
        return out

    def _categorize(self, name: str) -> str:
        lname = (name or "").lower()
        for key, _label, needles in self.MODE_CATEGORIES:
            for n in needles:
                if n in lname:
                    return key
        return "misc"

    async def game_context(self, player) -> dict[str, Any]:
        login = player.login
        level = int(getattr(player, "level", 0))
        st = self._gstate(login)
        ctx: dict[str, Any] = {
            "tabs": list(self.GAME_TABS),
            "active_tab": st["tab"],
            "is_master": level >= self.LEVEL_MASTER,
            "is_admin": level >= self.LEVEL_ADMIN,
            "loaded_profile": self._loaded_profile or "",
            "mode_name": "?",
            "categories": [],
            "dirty_count": 0,
            "status": "", "status_color": "aaa",
            "switcher_open": bool(st.get("switcher_open")),
            "picker_page": int(st.get("picker_page", 0)),
            "scripts": [],
            "active_script": "",
            "match_page": int(st.get("match_page", 0)),
            "profiles": [],
            "save_as": st.get("save_as", ""),
            "confirm_delete": st.get("confirm_delete", ""),
            "page_size": self.PAGE_SIZE,
        }
        status_text, status_color = self._mode_status.get(login, ("", "aaa"))
        ctx["status"] = status_text
        ctx["status_color"] = status_color

        if st["tab"] == "mode":
            await self._fill_mode_tab(ctx, login, level, st)
        else:
            await self._fill_match_tab(ctx, login, level, st)
        return ctx

    async def _fill_mode_tab(self, ctx, login, level, st):
        try:
            info = await self.instance.gbx("GetModeScriptInfo")
        except Exception as e:
            ctx["status"] = f"GetModeScriptInfo failed: {e}"
            ctx["status_color"] = "f44"
            return
        try:
            values = await self.instance.gbx("GetModeScriptSettings")
        except Exception as e:
            ctx["mode_name"] = (info or {}).get("Name", "?") if isinstance(info, dict) else "?"
            ctx["status"] = f"GetModeScriptSettings failed: {e}"
            ctx["status_color"] = "f44"
            return
        if not isinstance(values, dict):
            values = {}
        ctx["mode_name"] = (info or {}).get("Name", "?") if isinstance(info, dict) else "?"
        try:
            cur_script = await self.instance.gbx("GetScriptName")
            if isinstance(cur_script, dict):
                ctx["active_script"] = str(cur_script.get("NextValue") or cur_script.get("CurrentValue") or "")
        except Exception:
            pass

        params = (info or {}).get("ParamDescs", []) or []
        draft = self._mode_draft.get(login, {})
        baseline: dict[str, str] = {}
        # bucket by category, dropping params Nadeo marks as hidden
        buckets: dict[str, list[dict[str, Any]]] = {k: [] for k, _l, _n in self.MODE_CATEGORIES}
        buckets["misc"] = []
        for p in params:
            key = p.get("Name") or ""
            if not key:
                continue
            desc_raw = (p.get("Desc") or "").strip()
            if desc_raw.lower().startswith("<hidden>"):
                continue
            kind = self._mode_kind(p.get("Type", "text"))
            cur = values.get(key, p.get("Default"))
            cur_rendered = _render(cur, kind)
            baseline[key] = cur_rendered
            val = draft.get(key, cur_rendered)
            cat = self._categorize(key)
            label = _clean_label(desc_raw) or key
            buckets[cat].append({
                "key": key,
                "label": label[:60],
                "kind": kind,
                "value": val,
                "default": _render(p.get("Default"), kind),
                "dirty": key in draft and draft.get(key) != cur_rendered,
            })
        self._mode_baseline[login] = baseline
        for arr in buckets.values():
            arr.sort(key=lambda f: f["label"].lower())
        cats_meta = list(self.MODE_CATEGORIES) + [("misc", "Misc", ())]
        # paginate each category independently
        cat_pages = st.setdefault("cat_pages", {})
        ITEMS_PER_CAT_PAGE = 6
        cats_out = []
        for k, label, _n in cats_meta:
            arr = buckets.get(k, [])
            if not arr:
                continue
            total_pages = max(1, -(-len(arr) // ITEMS_PER_CAT_PAGE))
            page = max(0, min(int(cat_pages.get(k, 0)), total_pages - 1))
            cat_pages[k] = page
            visible = arr[page * ITEMS_PER_CAT_PAGE:(page + 1) * ITEMS_PER_CAT_PAGE]
            cats_out.append({
                "key": k, "label": label,
                "fields": visible,
                "total": len(arr),
                "page": page,
                "total_pages": total_pages,
                "has_pages": total_pages > 1,
            })
        ctx["categories"] = cats_out
        ctx["dirty_count"] = sum(1 for arr in buckets.values() for f in arr if f["dirty"])

        if level >= self.LEVEL_MASTER and st.get("switcher_open"):
            scripts = await self._list_scripts()
            page = int(st.get("picker_page", 0))
            total = max(1, -(-len(scripts) // self.PAGE_SIZE))
            page = max(0, min(page, total - 1))
            st["picker_page"] = page
            ctx["scripts"] = scripts[page * self.PAGE_SIZE:(page + 1) * self.PAGE_SIZE]
            ctx["picker_total_pages"] = total
            ctx["scripts_count"] = len(scripts)

    async def _fill_match_tab(self, ctx, login, level, st):
        profiles = await self._list_match_profiles()
        page = int(st.get("match_page", 0))
        total = max(1, -(-len(profiles) // self.PAGE_SIZE))
        page = max(0, min(page, total - 1))
        st["match_page"] = page
        ctx["profiles"] = profiles[page * self.PAGE_SIZE:(page + 1) * self.PAGE_SIZE]
        ctx["match_total_pages"] = total
        ctx["profiles_count"] = len(profiles)

    # ---- input absorption -------------------------------------------

    def _absorb_field_inputs(self, login, values) -> None:
        if not values:
            return
        prefix = f"entry_{self.game_view.id}__field__"
        baseline = self._mode_baseline.get(login, {})
        draft = self._mode_draft.setdefault(login, {})
        for k, v in values.items():
            if not k.startswith(prefix):
                continue
            key = k[len(prefix):]
            new = str(v or "")
            base = baseline.get(key)
            if base is None:
                # we have no baseline yet; only treat as draft if it differs
                # from whatever the draft already holds (i.e. user typed)
                if new != draft.get(key, new):
                    draft[key] = new
                continue
            if new == base:
                draft.pop(key, None)
            else:
                draft[key] = new
        if not draft:
            self._mode_draft.pop(login, None)
        # save-as text field
        save_as_key = f"entry_{self.game_view.id}__saveas"
        if save_as_key in values:
            self._gstate(login)["save_as"] = str(values[save_as_key] or "")

    async def _game_catch_all(self, player, action, values):
        login = player.login
        level = int(getattr(player, "level", 0))
        st = self._gstate(login)
        self._absorb_field_inputs(login, values)

        # tab switch
        if action.startswith("tabs__tab__"):
            tab = action.rsplit("__", 1)[-1]
            if any(t["key"] == tab for t in self.GAME_TABS):
                st["tab"] = tab
                await self._open(self.game_view, player)
            return

        # bool toggle for a mode script setting (admin+ allowed)
        if action.startswith("toggle__"):
            if level < self.LEVEL_ADMIN:
                return
            key = action.split("__", 1)[1]
            draft = self._mode_draft.setdefault(login, {})
            cur = draft.get(key)
            if cur is None:
                try:
                    vs = await self.instance.gbx("GetModeScriptSettings")
                    cur = _render(vs.get(key, False), "bool") if isinstance(vs, dict) else "0"
                except Exception:
                    cur = "0"
            draft[key] = "0" if _coerce(cur, "bool") else "1"
            await self._open(self.game_view, player)
            return

        # picker controls (master-only)
        if action == "switcher_toggle":
            if level >= self.LEVEL_MASTER:
                st["switcher_open"] = not st.get("switcher_open")
                await self._open(self.game_view, player)
            return
        if action in ("picker_prev", "picker_next"):
            if level >= self.LEVEL_MASTER:
                st["picker_page"] = max(0, st.get("picker_page", 0) + (-1 if action == "picker_prev" else 1))
                await self._open(self.game_view, player)
            return
        if action.startswith("pick_script__"):
            if level < self.LEVEL_MASTER:
                return
            rel = action[len("pick_script__"):]
            await self._switch_script(player, rel)
            return

        # per-category pagination (cat_<key>_prev/next)
        if action.startswith("cat_") and (action.endswith("_prev") or action.endswith("_next")):
            suffix = "_prev" if action.endswith("_prev") else "_next"
            cat_key = action[4:-len(suffix)]
            pages = st.setdefault("cat_pages", {})
            pages[cat_key] = max(0, pages.get(cat_key, 0) + (-1 if suffix == "_prev" else 1))
            await self._open(self.game_view, player)
            return

        # match settings pagination + actions
        if action in ("match_prev", "match_next"):
            st["match_page"] = max(0, st.get("match_page", 0) + (-1 if action == "match_prev" else 1))
            await self._open(self.game_view, player)
            return
        if action.startswith("load_profile__"):
            if level < self.LEVEL_MASTER:
                return
            rel = action[len("load_profile__"):]
            await self._load_profile(player, rel)
            return
        if action == "save_current":
            if level < self.LEVEL_MASTER:
                return
            await self._save_current(player)
            return
        if action.startswith("dup_profile__"):
            if level < self.LEVEL_MASTER:
                return
            rel = action[len("dup_profile__"):]
            await self._dup_profile(player, rel)
            return
        if action.startswith("del_profile__"):
            if level < self.LEVEL_MASTER:
                return
            rel = action[len("del_profile__"):]
            if st.get("confirm_delete") == rel:
                await self._del_profile(player, rel)
                st["confirm_delete"] = ""
            else:
                st["confirm_delete"] = rel
                await self._open(self.game_view, player)
            return

    # ---- mode actions -----------------------------------------------

    _SEV_COLOR = {"success": "0f0", "error": "f44", "warning": "fc4", "info": "888"}

    async def _toast(self, player, msg: str, severity: str = "info") -> None:
        """Set inline UI status AND emit a toast through tmsm_status:notify."""
        color = self._SEV_COLOR.get(severity, "888")
        self._mode_status[player.login] = (msg, color)
        try:
            sig = self.context.signals.get_signal("tmsm_status:notify")
            await sig.send_robust({
                "message": msg, "severity": severity,
                "login": player.login, "source": "server",
            })
        except Exception:
            logger.exception("server: toast emit failed")

    async def _on_mode_save(self, player, values=None, **kwargs) -> None:
        # Save button is wired to game_view 'save'; gate by admin.
        level = int(getattr(player, "level", 0))
        if level < self.LEVEL_ADMIN:
            await self._toast(player, "admin required to save", "error")
            await self._open(self.game_view, player)
            return
        self._absorb_field_inputs(player.login, values)
        draft = self._mode_draft.get(player.login, {})
        if not draft:
            await self._toast(player, "no changes to save", "warning")
            await self._open(self.game_view, player)
            return
        try:
            info = await self.instance.gbx("GetModeScriptInfo")
        except Exception as e:
            await self._toast(player, f"GetModeScriptInfo failed: {e}", "error")
            await self._open(self.game_view, player)
            return
        params = {p.get("Name"): self._mode_kind(p.get("Type", ""))
                  for p in (info or {}).get("ParamDescs", [])}
        payload: dict[str, Any] = {}
        rejected: list[str] = []
        for key, raw in draft.items():
            kind = params.get(key)
            if kind is None:
                rejected.append(key)
                continue
            try:
                payload[key] = _coerce(raw, kind)
            except (TypeError, ValueError):
                rejected.append(key)
        if not payload:
            await self._toast(
                player,
                f"no valid values ({len(rejected)} rejected)",
                "error",
            )
            await self._open(self.game_view, player)
            return
        try:
            await self.instance.gbx("SetModeScriptSettings", payload)
        except Exception as e:
            await self._toast(player, f"SetModeScriptSettings failed: {e}", "error")
            await self._open(self.game_view, player)
            return
        # only clear successfully saved keys from the draft
        for key in payload:
            self._mode_draft.get(player.login, {}).pop(key, None)
        if not self._mode_draft.get(player.login):
            self._mode_draft.pop(player.login, None)
        if rejected:
            await self._toast(
                player,
                f"saved {len(payload)}, rejected {len(rejected)}",
                "warning",
            )
        else:
            await self._toast(player, f"saved {len(payload)} fields", "success")
        await self._open(self.game_view, player)

    async def _on_game_refresh(self, player, **kwargs) -> None:
        self._mode_draft.pop(player.login, None)
        self._mode_status.pop(player.login, None)
        st = self._gstate(player.login)
        st["confirm_delete"] = ""
        await self._open(self.game_view, player)

    async def _switch_script(self, player, rel: str) -> None:
        # rel is path under Scripts/Modes/, forward-slash separated.
        try:
            await self.instance.gbx("SetScriptName", rel)
            await self.instance.gbx("RestartMap")
        except Exception as e:
            self._mode_status[player.login] = (f"SetScriptName failed: {e}", "f44")
            await self._open(self.game_view, player)
            return
        # reset drafts; the new script has different params
        self._mode_draft.pop(player.login, None)
        st = self._gstate(player.login)
        st["switcher_open"] = False
        self._mode_status[player.login] = (f"switched to {Path(rel).stem}", "0f0")
        await self._open(self.game_view, player)

    # ---- match settings actions -------------------------------------

    async def _load_profile(self, player, rel: str) -> None:
        try:
            await self.instance.gbx("LoadMatchSettings", rel)
        except Exception as e:
            self._mode_status[player.login] = (f"LoadMatchSettings failed: {e}", "f44")
            await self._open(self.game_view, player)
            return
        self._loaded_profile = rel
        self._mode_draft.pop(player.login, None)
        self._mode_status[player.login] = (f"loaded {Path(rel).stem}", "0f0")
        await self._open(self.game_view, player)

    async def _save_current(self, player) -> None:
        st = self._gstate(player.login)
        name = (st.get("save_as") or "").strip()
        if not name:
            self._mode_status[player.login] = ("enter a filename first", "f44")
            await self._open(self.game_view, player)
            return
        if not name.lower().endswith(".txt"):
            name = name + ".txt"
        # sanitize: only basename, no slashes
        if "/" in name or "\\" in name or ".." in name:
            self._mode_status[player.login] = ("invalid filename", "f44")
            await self._open(self.game_view, player)
            return
        rel = f"MatchSettings/{name}"
        try:
            await self.instance.gbx("SaveMatchSettings", rel)
        except Exception as e:
            self._mode_status[player.login] = (f"SaveMatchSettings failed: {e}", "f44")
            await self._open(self.game_view, player)
            return
        self._loaded_profile = rel
        self._mode_status[player.login] = (f"saved {name}", "0f0")
        await self._open(self.game_view, player)

    async def _dup_profile(self, player, rel: str) -> None:
        gd = await self._game_data_dir_path()
        if gd is None:
            self._mode_status[player.login] = ("game data dir unknown", "f44")
            await self._open(self.game_view, player)
            return
        src = gd / rel
        if not src.is_file():
            self._mode_status[player.login] = ("source missing", "f44")
            await self._open(self.game_view, player)
            return
        dst = src.with_name(src.stem + "_copy.txt")
        i = 1
        while dst.exists():
            i += 1
            dst = src.with_name(f"{src.stem}_copy{i}.txt")
        try:
            shutil.copy2(src, dst)
        except OSError as e:
            self._mode_status[player.login] = (f"copy failed: {e}", "f44")
            await self._open(self.game_view, player)
            return
        self._mode_status[player.login] = (f"copied to {dst.name}", "0f0")
        await self._open(self.game_view, player)

    async def _del_profile(self, player, rel: str) -> None:
        gd = await self._game_data_dir_path()
        if gd is None:
            self._mode_status[player.login] = ("game data dir unknown", "f44")
            await self._open(self.game_view, player)
            return
        target = (gd / rel).resolve()
        ms_root = (gd / "Maps" / "MatchSettings").resolve()
        if ms_root not in target.parents:
            self._mode_status[player.login] = ("refused: outside MatchSettings", "f44")
            await self._open(self.game_view, player)
            return
        try:
            target.unlink()
        except OSError as e:
            self._mode_status[player.login] = (f"delete failed: {e}", "f44")
            await self._open(self.game_view, player)
            return
        if self._loaded_profile == rel:
            self._loaded_profile = None
        self._mode_status[player.login] = (f"deleted {target.name}", "0f0")
        await self._open(self.game_view, player)
