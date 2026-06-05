"""tmsm settings — PyPlanet app settings master tool.

Lists every loaded PyPlanet app that registered any Setting via
``app.context.setting`` and lets a MASTER edit them in a single window.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from pyplanet.apps.config import AppConfig

from .views import SettingsView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────

def _render_value(value: Any, type_: type) -> str:
    if value is None:
        return ""
    if type_ is bool:
        return "1" if value else "0"
    if type_ in (list, set, dict):
        try:
            return json.dumps(list(value) if type_ is set else value,
                              ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _coerce(raw: str, type_: type) -> Any:
    """Convert a UI string into the Setting's Python type. Raises ValueError."""
    raw = raw if raw is not None else ""
    if type_ is bool:
        v = raw.strip().lower()
        if v in ("1", "true", "yes", "on", "y", "t"):
            return True
        if v in ("0", "false", "no", "off", "n", "f", ""):
            return False
        raise ValueError(f"not a bool: {raw!r}")
    if type_ is int:
        return int(raw.strip())
    if type_ is float:
        return float(raw.strip())
    if type_ in (list, set, dict):
        parsed = json.loads(raw) if raw.strip() else (
            {} if type_ is dict else []
        )
        if type_ is set:
            return set(parsed)
        if type_ is list and not isinstance(parsed, list):
            raise ValueError("expected a JSON array")
        if type_ is dict and not isinstance(parsed, dict):
            raise ValueError("expected a JSON object")
        return parsed
    return raw


def _kind(type_: type) -> str:
    if type_ is bool:
        return "bool"
    if type_ in (list, set, dict):
        return "json"
    return "text"


def _type_label(type_: type) -> str:
    return {
        str: "str", int: "int", float: "float", bool: "bool",
        list: "list", set: "set", dict: "dict",
    }.get(type_, getattr(type_, "__name__", str(type_)))


# ──────────────────────────────────────────────────────────────────────
# AppConfig
# ──────────────────────────────────────────────────────────────────────

class App_Settings(AppConfig):
    name = "pyplanet.apps.tmsm.settings"
    label = "tmsm_settings"
    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_hub"]

    PAGE_SIZE_APPS = 14
    PAGE_SIZE_SETTINGS = 10
    LEVEL_MASTER = 3

    _SEV_COLOR = {"success": "0f0", "error": "f44",
                  "warning": "fc4", "info": "888"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.view: SettingsView | None = None
        self._state: dict[str, dict[str, Any]] = {}
        # per-login draft edits keyed by (app_label, setting_key) -> str
        self._draft: dict[str, dict[tuple[str, str], str]] = {}
        # per-login last-rendered baseline (same keying) for dirty diffing
        self._baseline: dict[str, dict[tuple[str, str], str]] = {}

    # ---- lifecycle ---------------------------------------------------

    async def on_start(self) -> None:
        self.view = SettingsView(self)
        self.view.connect("back", self._on_back)
        self.view.connect("save", self._on_save)
        self.view.connect("refresh", self._on_reset)
        self.view.handle_catch_all = self._catch_all
        await self._register_with_hub()

    async def on_stop(self) -> None:
        if self.view is not None:
            try:
                await self.view.destroy()
            except Exception:
                logger.exception("settings: destroy failed")

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("settings: tmsm_hub:register not registered yet")
            return
        await sig.send_robust({"entry": HubAppEntry(
            key="settings", name="App Settings", icon="cogs",
            role=Role.MASTER, order=20,
            description="Edit PyPlanet app settings (every loaded plugin).",
            open=self._open_view,
        )}, raw=True)

    # ---- helpers -----------------------------------------------------

    def _gstate(self, login: str) -> dict[str, Any]:
        return self._state.setdefault(login, {
            "selected_app": "",
            "app_page": 0,
            "set_page": 0,
            "search": "",
            "status": "",
            "status_color": "aaa",
        })

    async def _toast(self, player, msg: str, severity: str = "info") -> None:
        st = self._gstate(player.login)
        st["status"] = msg
        st["status_color"] = self._SEV_COLOR.get(severity, "888")
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
                "message": msg, "severity": severity,
                "login": player.login, "source": "settings",
            })
        except Exception:
            logger.exception("settings: toast emit failed")

    async def _open_view(self, player) -> None:
        self._draft.pop(player.login, None)
        await self._open(player)

    async def _open(self, player) -> None:
        if self.view is None:
            return
        try:
            await self.view.display(player_logins=[player.login])
        except Exception:
            logger.exception("settings: display failed")

    async def _on_back(self, player, **_) -> None:
        try:
            from pyplanet.views.template import TemplateView
            await TemplateView.hide(self.view, player_logins=[player.login])
        except Exception:
            logger.exception("settings: hide failed")
        try:
            sig = self.context.signals.get_signal("tmsm_hub:show")
            await sig.send_robust({"player": player}, raw=True)
        except KeyError:
            pass

    # ---- enumeration -------------------------------------------------
    # NOTE: PyPlanet's `setting_manager.get_apps()` crashes with KeyError(None)
    # because the core global `performance_mode` setting has `app_label=None`
    # and the dict-builder does `instance.apps.apps[None]`. We enumerate the
    # `app_managers` dict directly to side-step that bug.

    def _app_label_to_name(self, label: str) -> str:
        try:
            cfg = self.instance.apps.apps.get(label)
        except Exception:
            cfg = None
        if cfg is None:
            return label
        return getattr(cfg, "name", None) or label

    async def _list_apps(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            app_managers = dict(self.instance.setting_manager.app_managers)
        except Exception:
            logger.exception("settings: cannot read app_managers")
            return []
        for label, mgr in app_managers.items():
            settings = list(getattr(mgr, "_settings", []) or [])
            if not settings:
                continue
            out.append({
                "label": label,
                "name": self._app_label_to_name(label),
                "count": len(settings),
            })
        out.sort(key=lambda a: a["name"].lower())
        return out

    async def _list_settings_for(self, app_label: str) -> list[dict[str, Any]]:
        try:
            mgr = self.instance.setting_manager.app_managers.get(app_label)
        except Exception:
            logger.exception("settings: app_managers lookup failed")
            return []
        if mgr is None:
            return []
        settings = list(getattr(mgr, "_settings", []) or [])
        out: list[dict[str, Any]] = []
        for s in settings:
            try:
                cur = await s.get_value()
            except Exception:
                cur = None
            type_ = getattr(s, "type", str) or str
            out.append({
                "key": getattr(s, "key", ""),
                "name": getattr(s, "name", "") or getattr(s, "key", ""),
                "category": getattr(s, "category", "") or "Other",
                "description": (getattr(s, "description", "") or ""),
                "type": _type_label(type_),
                "py_type": type_,
                "kind": _kind(type_),
                "choices": list(getattr(s, "choices", None) or []),
                "default": _render_value(getattr(s, "default", None), type_),
                "value_str": _render_value(cur, type_),
            })
        out.sort(key=lambda r: (r["category"], r["name"].lower()))
        return out

    # ---- context for view --------------------------------------------

    async def settings_context(self, player) -> dict[str, Any]:
        login = player.login
        level = int(getattr(player, "level", 0))
        st = self._gstate(login)
        is_master = level >= self.LEVEL_MASTER

        ctx: dict[str, Any] = {
            "is_master": is_master,
            "status": st.get("status", ""),
            "status_color": st.get("status_color", "aaa"),
        }

        apps = await self._list_apps()
        sel = st.get("selected_app") or ""
        labels = {a["label"] for a in apps}
        if sel not in labels:
            sel = apps[0]["label"] if apps else ""
            st["selected_app"] = sel
            st["set_page"] = 0

        search = (st.get("search") or "").strip().lower()
        if search:
            visible_apps = [
                a for a in apps if search in a["name"].lower()
                or search in a["label"].lower()
            ]
        else:
            visible_apps = apps

        ap = int(st.get("app_page", 0))
        ap_total = max(1, -(-len(visible_apps) // self.PAGE_SIZE_APPS))
        ap = max(0, min(ap, ap_total - 1))
        st["app_page"] = ap
        ctx["apps"] = [
            {**a, "selected": a["label"] == sel}
            for a in visible_apps[
                ap * self.PAGE_SIZE_APPS:(ap + 1) * self.PAGE_SIZE_APPS
            ]
        ]
        ctx["app_page"] = ap
        ctx["app_total_pages"] = ap_total
        ctx["apps_count"] = len(visible_apps)
        ctx["selected_app"] = sel
        ctx["search"] = st.get("search", "")
        sel_meta = next((a for a in apps if a["label"] == sel), None)
        ctx["selected_app_name"] = sel_meta["name"] if sel_meta else ""

        settings_rows = await self._list_settings_for(sel) if sel else []

        baseline = self._baseline.setdefault(login, {})
        for k in list(baseline):
            if k[0] != sel:
                baseline.pop(k, None)
        draft = self._draft.get(login, {})
        for row in settings_rows:
            bkey = (sel, row["key"])
            baseline[bkey] = row["value_str"]
            cur_draft = draft.get(bkey)
            row["value_edit"] = (
                cur_draft if cur_draft is not None else row["value_str"]
            )
            row["dirty"] = (
                cur_draft is not None and cur_draft != row["value_str"]
            )

        sp = int(st.get("set_page", 0))
        sp_total = max(1, -(-len(settings_rows) // self.PAGE_SIZE_SETTINGS))
        sp = max(0, min(sp, sp_total - 1))
        st["set_page"] = sp
        ctx["settings"] = settings_rows[
            sp * self.PAGE_SIZE_SETTINGS:(sp + 1) * self.PAGE_SIZE_SETTINGS
        ]
        ctx["set_page"] = sp
        ctx["set_total_pages"] = sp_total
        ctx["set_count"] = len(settings_rows)
        ctx["dirty_count"] = sum(
            1 for r in settings_rows if r.get("dirty")
        )
        return ctx

    # ---- input absorption --------------------------------------------

    def _absorb(self, login: str, values) -> None:
        if not values or self.view is None:
            return
        st = self._gstate(login)
        sel = st.get("selected_app") or ""
        prefix = f"entry_{self.view.id}__field__"
        baseline = self._baseline.get(login, {})
        draft = self._draft.setdefault(login, {})
        for k, v in values.items():
            if k.startswith(prefix):
                key = k[len(prefix):]
                bk = (sel, key)
                new = str(v or "")
                base = baseline.get(bk)
                if base is None:
                    if new != draft.get(bk, new):
                        draft[bk] = new
                    continue
                if new == base:
                    draft.pop(bk, None)
                else:
                    draft[bk] = new
        search_key = f"entry_{self.view.id}__search"
        if search_key in values:
            new = str(values[search_key] or "")
            if new != st.get("search", ""):
                st["search"] = new
                st["app_page"] = 0
        if not draft:
            self._draft.pop(login, None)

    # ---- catch-all ---------------------------------------------------

    async def _catch_all(self, player, action, values):
        login = player.login
        level = int(getattr(player, "level", 0))
        st = self._gstate(login)
        self._absorb(login, values)

        if action == "back":
            await self._on_back(player)
            return
        if action == "save":
            await self._on_save(player, values=values)
            return
        if action == "refresh":
            await self._on_reset(player)
            return

        if action in ("app_prev", "app_next"):
            st["app_page"] = max(0, st.get("app_page", 0)
                                 + (-1 if action == "app_prev" else 1))
            await self._open(player)
            return
        if action in ("set_prev", "set_next"):
            st["set_page"] = max(0, st.get("set_page", 0)
                                 + (-1 if action == "set_prev" else 1))
            await self._open(player)
            return

        if action.startswith("select_app__"):
            label = action[len("select_app__"):]
            if st.get("selected_app") != label:
                st["selected_app"] = label
                st["set_page"] = 0
            await self._open(player)
            return

        if action.startswith("toggle__"):
            if level < self.LEVEL_MASTER:
                await self._toast(player, "master required", "error")
                return
            key = action[len("toggle__"):]
            sel = st.get("selected_app") or ""
            bk = (sel, key)
            baseline = self._baseline.get(login, {})
            draft = self._draft.setdefault(login, {})
            cur = draft.get(bk, baseline.get(bk, "0"))
            new = "0" if cur == "1" else "1"
            if new == baseline.get(bk):
                draft.pop(bk, None)
            else:
                draft[bk] = new
            if not draft:
                self._draft.pop(login, None)
            await self._open(player)
            return

        if action.startswith("cycle__"):
            rest = action[len("cycle__"):]
            try:
                key, direction = rest.rsplit("__", 1)
            except ValueError:
                return
            if level < self.LEVEL_MASTER:
                await self._toast(player, "master required", "error")
                return
            sel = st.get("selected_app") or ""
            await self._cycle_choice(
                player, sel, key,
                step=1 if direction == "next" else -1,
            )
            return

        if action.startswith("reset_field__"):
            if level < self.LEVEL_MASTER:
                await self._toast(player, "master required", "error")
                return
            key = action[len("reset_field__"):]
            sel = st.get("selected_app") or ""
            await self._reset_to_default(player, sel, key)
            return

    # ---- choice cycling ---------------------------------------------

    async def _cycle_choice(self, player, app_label: str, key: str,
                            step: int) -> None:
        rows = await self._list_settings_for(app_label)
        row = next((r for r in rows if r["key"] == key), None)
        if row is None or not row["choices"]:
            return
        choices = row["choices"]
        bk = (app_label, key)
        baseline = self._baseline.setdefault(player.login, {})
        baseline[bk] = row["value_str"]
        draft = self._draft.setdefault(player.login, {})
        cur = draft.get(bk, row["value_str"])
        rendered = [_render_value(c, row["py_type"]) for c in choices]
        try:
            idx = rendered.index(cur)
        except ValueError:
            idx = -1
        nxt = (idx + step) % len(choices)
        new = rendered[nxt]
        if new == baseline.get(bk):
            draft.pop(bk, None)
        else:
            draft[bk] = new
        if not draft:
            self._draft.pop(player.login, None)
        await self._open(player)

    async def _reset_to_default(self, player, app_label: str, key: str) -> None:
        rows = await self._list_settings_for(app_label)
        row = next((r for r in rows if r["key"] == key), None)
        if row is None:
            return
        bk = (app_label, key)
        baseline = self._baseline.setdefault(player.login, {})
        baseline[bk] = row["value_str"]
        draft = self._draft.setdefault(player.login, {})
        new = row["default"]
        if new == baseline.get(bk):
            draft.pop(bk, None)
        else:
            draft[bk] = new
        if not draft:
            self._draft.pop(player.login, None)
        await self._open(player)

    # ---- save / reset ------------------------------------------------

    async def _on_reset(self, player, **_) -> None:
        self._draft.pop(player.login, None)
        await self._toast(player, "drafts cleared", "info")
        await self._open(player)

    async def _on_save(self, player, values=None, **_) -> None:
        level = int(getattr(player, "level", 0))
        if level < self.LEVEL_MASTER:
            await self._toast(player, "master required to save", "error")
            return
        self._absorb(player.login, values)
        draft = self._draft.get(player.login, {})
        if not draft:
            await self._toast(player, "no changes to save", "warning")
            await self._open(player)
            return

        per_app: dict[str, dict[str, str]] = {}
        for (app_label, key), raw in draft.items():
            per_app.setdefault(app_label, {})[key] = raw

        try:
            app_managers = dict(self.instance.setting_manager.app_managers)
        except Exception as e:
            await self._toast(player, f"app_managers failed: {e}", "error")
            await self._open(player)
            return

        ok = 0
        failed: list[str] = []
        for app_label, fields in per_app.items():
            mgr = app_managers.get(app_label)
            if mgr is None:
                failed.extend(f"{app_label}.{k}" for k in fields)
                continue
            by_key = {s.key: s for s in getattr(mgr, "_settings", []) or []}
            for key, raw in fields.items():
                s = by_key.get(key)
                if s is None:
                    failed.append(f"{app_label}.{key}")
                    continue
                type_ = getattr(s, "type", str) or str
                try:
                    parsed = _coerce(raw, type_)
                    await s.set_value(parsed)
                    ok += 1
                    self._draft.get(player.login, {}).pop(
                        (app_label, key), None
                    )
                except Exception as e:
                    failed.append(f"{app_label}.{key} ({e})")

        if not self._draft.get(player.login):
            self._draft.pop(player.login, None)

        if failed and ok:
            await self._toast(
                player,
                f"saved {ok}, {len(failed)} failed: {failed[0]}",
                "warning",
            )
        elif failed:
            await self._toast(
                player,
                f"all {len(failed)} failed: {failed[0]}",
                "error",
            )
        else:
            await self._toast(player, f"saved {ok} settings", "success")
        await self._open(player)