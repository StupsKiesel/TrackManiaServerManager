"""Bug reports app — player /report form + master /reports triage list.

Players submit short bug reports (subject + details). Each row also
captures their PyPlanet auth level, the active game phase, dedicated
and pyplanet uptimes, the dedicated server version, the player's
self-declared input device, and what part of the server the report is
about (widgets / UI windows). Master admins can mark reports as
fixed/wontfix, delete them, and export the whole table to a markdown
file.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import re
import time
from pathlib import Path
from typing import Any

from pyplanet.apps.config import AppConfig
from pyplanet.contrib.setting import Setting

from .discord import DiscordDeliveryError, send_ping, send_reports
from .storage import (
    BugReportStorage,
    STATUS_FIXED,
    STATUS_OPEN,
    STATUS_WONTFIX,
    VALID_STATUSES,
    _fmt_seconds,
    rows_to_markdown,
)
from .views import ReportFormView, ReportListView, SettingsView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)


_FILTER_LABELS = (
    ("all",     "All"),
    ("open",    "Open"),
    ("fixed",   "Fixed"),
    ("wontfix", "Wontfix"),
)

_AUTH_LEVEL_LABELS = {
    0: "player",
    1: "operator",
    2: "admin",
    3: "masteradmin",
}

_INPUT_DEVICES = (
    ("keyboard",   "Keyboard"),
    ("controller", "Controller"),
    ("other",      "Other"),
)

_DELIVERY_MODES = (
    ("off",       "Off (do not send to Discord)"),
    ("immediate", "Immediate (send each report as it arrives)"),
    ("daily",     "Daily batch (send once every 24 hours)"),
    ("weekly",    "Weekly batch (send once every 7 days)"),
)
_VALID_DELIVERY_MODES = frozenset(v for v, _ in _DELIVERY_MODES)

_BATCH_INTERVAL_S = {
    "daily":  24 * 60 * 60,
    "weekly": 7 * 24 * 60 * 60,
}

_FLUSH_TICK_S = 5 * 60  # poll the scheduler every 5 minutes


def _ago(ts: _dt.datetime | None) -> str:
    if ts is None:
        return ""
    try:
        delta = _dt.datetime.utcnow() - ts
    except TypeError:
        return ""
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _truncate(text: str, n: int) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[: max(0, n - 1)].rstrip() + "\u2026"


def _slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", (text or "").strip()) or "report"


class BugReportsApp(AppConfig):
    name = "pyplanet.apps.tmsm.bug_reports"
    label = "tmsm_bug_reports"
    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_hub"]
    game_dependencies = ["trackmania", "trackmania_next"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.storage = BugReportStorage(self.instance)
        self.form_view: ReportFormView | None = None
        self.list_view: ReportListView | None = None
        self.settings_view: SettingsView | None = None
        # Set once at on_start; used to compute pyplanet uptime at submit time.
        # Best-effort proxy: tracks time since this app loaded (= controller boot
        # in practice unless the addon was hot-reloaded).
        self._pyplanet_start: float = time.monotonic()
        # Cached dedicated-server start epoch in s (psutil best-effort).
        self._dedicated_start_epoch: float | None = None

        # Per-login transient UI state.
        # Form draft: subject/details + classification + status line.
        self._form_state: dict[str, dict[str, Any]] = {}
        # List: { login: {"filter": str, "status": str, "status_color": str,
        #                  "selected_id": int | None} }
        self._list_state: dict[str, dict[str, Any]] = {}
        # Settings draft: webhook field is held here while user edits it,
        # plus per-master status line.
        self._settings_state: dict[str, dict[str, Any]] = {}

        # Settings (registered in on_start).
        self.setting_discord_enabled = Setting(
            "discord_enabled", "Discord delivery enabled",
            Setting.CAT_BEHAVIOUR, type=bool, default=False,
            description="When True, submitted bug reports are forwarded to the configured Discord webhook.",
        )
        self.setting_discord_webhook_url = Setting(
            "discord_webhook_url", "Discord webhook URL",
            Setting.CAT_BEHAVIOUR, type=str, default="",
            description="Full Discord webhook URL (https://discord.com/api/webhooks/...).",
        )
        self.setting_delivery_mode = Setting(
            "delivery_mode", "Delivery schedule",
            Setting.CAT_BEHAVIOUR, type=str, default="immediate",
            description="When reports are sent to Discord: off, immediate, daily, or weekly.",
        )
        self.setting_store_locally = Setting(
            "store_locally", "Store local DB copy",
            Setting.CAT_BEHAVIOUR, type=bool, default=True,
            description="Keep reports in the local DB. When False, reports are deleted right after successful Discord delivery.",
        )
        self.setting_last_delivery_at = Setting(
            "last_delivery_at", "Last Discord delivery (UTC)",
            Setting.CAT_BEHAVIOUR, type=str, default="",
            description="Internal: ISO-8601 timestamp of the most recent successful batch send.",
        )

        self._flush_task: asyncio.Task | None = None

    # ── lifecycle ────────────────────────────────────────────────────

    async def on_start(self) -> None:
        try:
            await self.storage.ensure_schema()
        except Exception:
            logger.exception("bug_reports: schema bootstrap failed")

        for s in (
            self.setting_discord_enabled,
            self.setting_discord_webhook_url,
            self.setting_delivery_mode,
            self.setting_store_locally,
            self.setting_last_delivery_at,
        ):
            try:
                await self.context.setting.register(s)
            except Exception:
                logger.exception("bug_reports: setting register failed: %s", s.key)

        try:
            self.form_view = ReportFormView(self)
            self.form_view.connect("submit", self._on_submit)
            self.form_view.connect("clear",  self._on_form_clear)
            self.form_view.handle_catch_all = self._form_catch_all  # type: ignore[assignment]
        except Exception:
            logger.exception("bug_reports: form view init failed")
        try:
            self.list_view = ReportListView(self)
            self.list_view.connect("refresh",   self._on_list_refresh)
            self.list_view.connect("export",    self._on_export)
            self.list_view.connect("settings",  self._on_open_settings)
            self.list_view.handle_catch_all = self._list_catch_all  # type: ignore[assignment]
        except Exception:
            logger.exception("bug_reports: list view init failed")
        try:
            self.settings_view = SettingsView(self)
            self.settings_view.connect("save",  self._on_settings_save)
            self.settings_view.connect("test",  self._on_settings_test)
            self.settings_view.connect("flush", self._on_settings_flush)
            self.settings_view.connect("_crumb__reports", self._on_settings_back)
            self.settings_view.handle_catch_all = self._settings_catch_all  # type: ignore[assignment]
        except Exception:
            logger.exception("bug_reports: settings view init failed")

        await self._register_with_hub()

        # Periodic delivery scheduler.
        try:
            self._flush_task = asyncio.create_task(self._scheduler_loop())
        except Exception:
            logger.exception("bug_reports: scheduler task spawn failed")

    async def on_stop(self) -> None:
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except Exception:
                pass
            self._flush_task = None
        for v in (self.form_view, self.list_view, self.settings_view):
            if v is not None:
                try:
                    await v.destroy()
                except Exception:
                    logger.exception("bug_reports: view destroy failed")

    # ── hub registration ────────────────────────────────────────────

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("bug_reports: tmsm_hub:register not registered yet")
            return
        try:
            await sig.send_robust({"entry": HubAppEntry(
                key="bug_report",
                name="Report bug",
                icon="bug",
                color="f55",
                role=Role.PLAYER,
                description="Found a bug? Send a short report to the admins.",
                open=self._open_form,
                command="report",
                order=80,
            )}, raw=True)
        except Exception:
            logger.exception("bug_reports: hub register (player) failed")
        try:
            await sig.send_robust({"entry": HubAppEntry(
                key="bug_reports",
                name="Bug reports",
                icon="bug",
                color="f93",
                role=Role.MASTER,
                description="Triage submitted bug reports.",
                open=self._open_list,
                command="reports",
                order=15,
            )}, raw=True)
        except Exception:
            logger.exception("bug_reports: hub register (master) failed")

    # ── open helpers ─────────────────────────────────────────────────

    async def _open_form(self, player) -> None:
        if self.form_view is None:
            return
        self._form_state.setdefault(player.login, self._default_form_state())
        try:
            await self.form_view.display(player_logins=[player.login])
            self.form_view._visible = True
            self.form_view._visible_logins.add(player.login)
        except Exception:
            logger.exception("bug_reports: open form failed")

    async def _open_list(self, player) -> None:
        if self.list_view is None:
            return
        self._list_state.setdefault(player.login, self._default_list_state())
        try:
            await self.list_view.display(player_logins=[player.login])
            self.list_view._visible = True
            self.list_view._visible_logins.add(player.login)
        except Exception:
            logger.exception("bug_reports: open list failed")

    def _default_form_state(self) -> dict[str, Any]:
        return {
            "subject": "",
            "details": "",
            "about_widgets": False,
            "about_ui": False,
            "input_device": "keyboard",
            "uses_openplanet": False,
            "status": "",
            "status_color": "888",
        }

    def _default_list_state(self) -> dict[str, Any]:
        return {
            "filter": "all",
            "status": "",
            "status_color": "888",
            "selected_id": None,
        }

    # ── context builders (called by views.get_per_player_data) ───────

    async def build_form_context(self, login: str) -> dict[str, Any]:
        st = self._form_state.setdefault(login, self._default_form_state())
        map_name = ""
        map_uid = ""
        mode_script = ""
        try:
            cm = self.instance.map_manager.current_map
            if cm is not None:
                map_name = str(getattr(cm, "name", "") or "")
                map_uid = str(getattr(cm, "uid", "") or "")
        except Exception:
            pass
        try:
            mode_script = str(await self.instance.mode_manager.get_current_script() or "")
        except Exception:
            mode_script = ""
        # Resolve auth level from the player record (avoid extra DB lookups).
        auth_level = "player"
        try:
            p = await self.instance.player_manager.get_player(login=login, lock=False)
            auth_level = _AUTH_LEVEL_LABELS.get(int(getattr(p, "level", 0) or 0), "player")
        except Exception:
            pass
        # Client (reporter) game version via XMLRPC GetDetailedPlayerInfo.
        client_version = await self._client_game_version(login)
        return {
            "subject_value": st.get("subject", ""),
            "details_value": st.get("details", ""),
            "about_widgets": bool(st.get("about_widgets", False)),
            "about_ui":      bool(st.get("about_ui", False)),
            "input_device":  st.get("input_device", "keyboard"),
            "input_devices": [{"value": v, "label": l} for v, l in _INPUT_DEVICES],
            "uses_openplanet": bool(st.get("uses_openplanet", False)),
            "map_name": map_name,
            "map_uid": map_uid,
            "mode_script": mode_script,
            "game_phase": self._current_game_phase(),
            "auth_level": auth_level,
            "game_version": await self._game_version(),
            "client_version": client_version,
            "pyplanet_uptime_s": self._pyplanet_uptime_s(),
            "dedicated_uptime_s": self._dedicated_uptime_s(),
            "status": st.get("status", ""),
            "status_color": st.get("status_color", "888"),
        }

    async def build_list_context(self, login: str) -> dict[str, Any]:
        st = self._list_state.setdefault(login, self._default_list_state())
        flt = st.get("filter") or "all"
        rows = await self.storage.list_all(
            status=None if flt == "all" else flt,
        )
        counts = await self.storage.counts_by_status()
        filters = [
            {"key": k, "label": label,
             "count": counts.get(k, counts.get("total", 0)) if k != "all" else counts.get("total", 0),
             "active": k == flt}
            for k, label in _FILTER_LABELS
        ]
        sel_id = st.get("selected_id")
        selected = None
        if sel_id is not None:
            for r in rows:
                if int(r.get("id", 0)) == int(sel_id):
                    selected = r
                    break
        view_rows: list[dict[str, Any]] = []
        for r in rows[:60]:
            view_rows.append({
                "id":           int(r.get("id", 0)),
                "subject":      _truncate(r.get("subject") or "", 70),
                "subject_full": r.get("subject") or "",
                "login":        r.get("login") or "",
                "nickname":     r.get("nickname") or r.get("login") or "",
                "map_name":     _truncate(r.get("map_name") or "", 40),
                "mode_script":  _truncate(r.get("mode_script") or "", 40),
                "status":       r.get("status") or "open",
                "ago":          _ago(r.get("created_at")),
                "is_selected":  int(r.get("id", 0)) == int(sel_id) if sel_id is not None else False,
            })
        selected_view: dict[str, Any] | None = None
        if selected is not None:
            created = selected.get("created_at")
            created_s = (created.strftime("%Y-%m-%d %H:%M:%S UTC")
                         if hasattr(created, "strftime") else "")
            tags = []
            if selected.get("about_widgets"):
                tags.append("widgets")
            if selected.get("about_ui"):
                tags.append("UI windows")
            selected_view = {
                "id":          int(selected.get("id", 0)),
                "subject":     selected.get("subject") or "",
                "details":     selected.get("details") or "",
                "login":       selected.get("login") or "",
                "nickname":    selected.get("nickname") or "",
                "map_name":    selected.get("map_name") or "",
                "map_uid":     selected.get("map_uid") or "",
                "mode_script": selected.get("mode_script") or "",
                "status":      selected.get("status") or "open",
                "created_s":   created_s,
                "auth_level":      selected.get("auth_level") or "",
                "game_phase":      selected.get("game_phase") or "",
                "about_tags":      ", ".join(tags),
                "input_device":    selected.get("input_device") or "",
                "game_version":    selected.get("game_version") or "",
                "client_version":  selected.get("client_version") or "",
                "uses_openplanet": bool(selected.get("uses_openplanet")),
                "pyplanet_uptime": _fmt_seconds(int(selected.get("pyplanet_uptime_s") or 0)) if selected.get("pyplanet_uptime_s") else "",
                "dedicated_uptime": _fmt_seconds(int(selected.get("dedicated_uptime_s") or 0)) if selected.get("dedicated_uptime_s") else "",
            }
        return {
            "filters":     filters,
            "rows":        view_rows,
            "counts":      counts,
            "selected":    selected_view,
            "status":      st.get("status", ""),
            "status_color": st.get("status_color", "888"),
        }

    # ── form handlers ────────────────────────────────────────────────

    def _extract_entry(self, values: Any, name: str, view) -> str:
        if not values or view is None:
            return ""
        key = f"entry_{view.id}__{name}"
        return str(values.get(key, "") or "")

    async def _on_submit(self, player, values=None, **kwargs) -> None:
        if self.form_view is None:
            return
        st = self._form_state.setdefault(player.login, self._default_form_state())
        # Always pull the latest text-entry values (they are pushed in `values`
        # on every action, not only on Submit).
        self._absorb_entries(player.login, values)
        subject = st.get("subject", "").strip()
        details = st.get("details", "").strip()
        if not subject:
            st["status"] = "Subject required."
            st["status_color"] = "f80"
            await self._refresh_form(player)
            return
        ctx = await self.build_form_context(player.login)
        try:
            nickname = str(getattr(player, "nickname", "") or player.login)
        except Exception:
            nickname = player.login
        rid = await self.storage.create(
            login=player.login,
            nickname=nickname,
            map_uid=ctx.get("map_uid") or None,
            map_name=ctx.get("map_name") or None,
            mode_script=ctx.get("mode_script") or None,
            subject=subject,
            details=details,
            auth_level=ctx.get("auth_level") or None,
            game_phase=ctx.get("game_phase") or None,
            about_widgets=bool(st.get("about_widgets", False)),
            about_ui=bool(st.get("about_ui", False)),
            input_device=st.get("input_device") or None,
            game_version=ctx.get("game_version") or None,
            client_version=ctx.get("client_version") or None,
            uses_openplanet=bool(st.get("uses_openplanet", False)),
            pyplanet_uptime_s=int(ctx.get("pyplanet_uptime_s") or 0),
            dedicated_uptime_s=int(ctx.get("dedicated_uptime_s") or 0),
        )
        if rid is None:
            st["status"] = "Submit failed (server error)."
            st["status_color"] = "f44"
            await self._toast(player, "Bug report failed", "error")
            await self._refresh_form(player)
            return
        st["subject"] = ""
        st["details"] = ""
        st["about_widgets"] = False
        st["about_ui"] = False
        st["uses_openplanet"] = False
        # keep input_device sticky between submissions
        st["status"] = f"Report #{rid} submitted. Thanks!"
        st["status_color"] = "8f8"
        await self._toast(player, f"Bug report #{rid} submitted", "success")
        # Best-effort immediate Discord delivery (mode=="immediate").
        asyncio.create_task(self._maybe_deliver_immediate(rid))
        await self._refresh_form(player)

    async def _on_form_clear(self, player, values=None, **kwargs) -> None:
        st = self._form_state.setdefault(player.login, self._default_form_state())
        st["subject"] = ""
        st["details"] = ""
        st["about_widgets"] = False
        st["about_ui"] = False
        st["uses_openplanet"] = False
        st["status"] = "Cleared."
        st["status_color"] = "888"
        await self._refresh_form(player)

    def _absorb_entries(self, login: str, values: Any) -> None:
        """Keep `subject`/`details` draft in sync with whatever is currently
        typed when any action (checkbox toggle, radio pick, Submit) fires."""
        if not values or self.form_view is None:
            return
        st = self._form_state.setdefault(login, self._default_form_state())
        for name in ("subject", "details"):
            key = f"entry_{self.form_view.id}__{name}"
            if key in values:
                st[name] = str(values.get(key) or "")

    async def _form_catch_all(self, player, action, values, **kwargs) -> None:
        """Handle checkbox toggles + input-device radio selection."""
        self._absorb_entries(player.login, values)
        if not action:
            return
        st = self._form_state.setdefault(player.login, self._default_form_state())
        if action == "toggle_widgets":
            st["about_widgets"] = not bool(st.get("about_widgets", False))
            await self._refresh_form(player)
            return
        if action == "toggle_ui":
            st["about_ui"] = not bool(st.get("about_ui", False))
            await self._refresh_form(player)
            return
        if action == "toggle_openplanet":
            st["uses_openplanet"] = not bool(st.get("uses_openplanet", False))
            await self._refresh_form(player)
            return
        # device__set__<value> (from ui.radio_box / radio_group)
        if action.startswith("device__set__"):
            value = action[len("device__set__"):]
            if value in {v for v, _ in _INPUT_DEVICES}:
                st["input_device"] = value
                await self._refresh_form(player)
            return

    async def _refresh_form(self, player) -> None:
        if self.form_view is None:
            return
        try:
            await self.form_view.display(player_logins=[player.login])
        except Exception:
            logger.exception("bug_reports: form refresh failed")

    # ── list handlers ────────────────────────────────────────────────

    async def _list_catch_all(self, player, action, values, **kwargs) -> None:
        """Dispatch row-level actions encoded as `<verb>__<id>` or `filter__<key>`."""
        try:
            verb, _, arg = (action or "").partition("__")
        except Exception:
            return
        if verb == "filter" and arg:
            st = self._list_state.setdefault(player.login, self._default_list_state())
            st["filter"] = arg
            st["selected_id"] = None
            await self._refresh_list(player)
            return
        if verb == "select" and arg:
            st = self._list_state.setdefault(player.login, self._default_list_state())
            try:
                st["selected_id"] = int(arg)
            except (TypeError, ValueError):
                st["selected_id"] = None
            await self._refresh_list(player)
            return
        if verb == "status" and arg:
            rest, _, status = arg.partition(":")
            try:
                rid = int(rest)
            except (TypeError, ValueError):
                rid = 0
            if rid <= 0 or status not in VALID_STATUSES:
                return
            ok = await self.storage.set_status(rid, status)
            st = self._list_state.setdefault(player.login, self._default_list_state())
            if ok:
                st["status"] = f"#{rid} -> {status}"
                st["status_color"] = "8f8"
            else:
                st["status"] = f"#{rid} update failed"
                st["status_color"] = "f44"
            await self._refresh_list(player)
            return
        if verb == "delete" and arg:
            try:
                rid = int(arg)
            except (TypeError, ValueError):
                rid = 0
            if rid <= 0:
                return
            ok = await self.storage.delete(rid)
            st = self._list_state.setdefault(player.login, self._default_list_state())
            if ok:
                if st.get("selected_id") == rid:
                    st["selected_id"] = None
                st["status"] = f"#{rid} deleted"
                st["status_color"] = "8f8"
            else:
                st["status"] = f"#{rid} delete failed"
                st["status_color"] = "f44"
            await self._refresh_list(player)
            return

    async def _on_list_refresh(self, player, **kwargs) -> None:
        await self._refresh_list(player)

    async def _on_export(self, player, **kwargs) -> None:
        st = self._list_state.setdefault(player.login, self._default_list_state())
        try:
            rows = await self.storage.list_all()
        except Exception:
            logger.exception("bug_reports: export list_all failed")
            st["status"] = "Export failed (db error)."
            st["status_color"] = "f44"
            await self._refresh_list(player)
            return
        try:
            text = rows_to_markdown(rows)
        except Exception:
            logger.exception("bug_reports: export render failed")
            st["status"] = "Export failed (render error)."
            st["status_color"] = "f44"
            await self._refresh_list(player)
            return
        ts = _dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        path = Path.home() / ".tmsm" / "bug_reports" / f"export-{ts}.md"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except Exception:
            logger.exception("bug_reports: write export file failed (%s)", path)
            st["status"] = f"Export failed (write error: {path})."
            st["status_color"] = "f44"
            await self._refresh_list(player)
            return
        st["status"] = f"Exported {len(rows)} report(s) -> {path}"
        st["status_color"] = "8f8"
        await self._toast(player, f"Bug reports exported -> {path}", "success")
        await self._refresh_list(player)

    async def _refresh_list(self, player) -> None:
        if self.list_view is None:
            return
        try:
            await self.list_view.display(player_logins=[player.login])
        except Exception:
            logger.exception("bug_reports: list refresh failed")

    # ── settings sub-window ──────────────────────────────────────────

    def _default_settings_state(self) -> dict[str, Any]:
        return {
            "webhook_draft": None,  # None = use stored Setting value
            "status": "",
            "status_color": "888",
        }

    async def _open_settings(self, player) -> None:
        if self.settings_view is None:
            return
        self._settings_state.setdefault(player.login, self._default_settings_state())
        try:
            await self.settings_view.display(player_logins=[player.login])
            self.settings_view._visible = True
            self.settings_view._visible_logins.add(player.login)
        except Exception:
            logger.exception("bug_reports: open settings failed")

    async def _refresh_settings(self, player) -> None:
        if self.settings_view is None:
            return
        try:
            await self.settings_view.display(player_logins=[player.login])
        except Exception:
            logger.exception("bug_reports: settings refresh failed")

    async def _on_open_settings(self, player, **kwargs) -> None:
        """Header button on the list view: hide list, open settings."""
        if self.list_view is not None:
            try:
                self.list_view._visible_logins.discard(player.login)
                # Use the underlying TemplateView.hide for per-player removal.
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.list_view, player_logins=[player.login])
            except Exception:
                logger.exception("bug_reports: hide list before settings failed")
        await self._open_settings(player)

    async def _on_settings_back(self, player, **kwargs) -> None:
        """`reports` breadcrumb on the settings view → back to the list."""
        if self.settings_view is not None:
            try:
                self.settings_view._visible_logins.discard(player.login)
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.settings_view, player_logins=[player.login])
            except Exception:
                logger.exception("bug_reports: hide settings on back failed")
        await self._open_list(player)

    async def build_settings_context(self, login: str) -> dict[str, Any]:
        st = self._settings_state.setdefault(login, self._default_settings_state())
        try:
            enabled = bool(await self.setting_discord_enabled.get_value())
        except Exception:
            enabled = False
        if st.get("webhook_draft") is None:
            try:
                url = str(await self.setting_discord_webhook_url.get_value() or "")
            except Exception:
                url = ""
        else:
            url = st["webhook_draft"]
        try:
            mode = str(await self.setting_delivery_mode.get_value() or "immediate")
        except Exception:
            mode = "immediate"
        if mode not in _VALID_DELIVERY_MODES:
            mode = "immediate"
        try:
            store_locally = bool(await self.setting_store_locally.get_value())
        except Exception:
            store_locally = True
        try:
            last_at = str(await self.setting_last_delivery_at.get_value() or "")
        except Exception:
            last_at = ""
        try:
            pending = await self.storage.list_pending_delivery()
            pending_count = len(pending)
        except Exception:
            pending_count = 0
        try:
            counts = await self.storage.counts_by_status()
            total_count = int(counts.get("total", 0))
        except Exception:
            total_count = 0
        return {
            "discord_enabled":     enabled,
            "discord_webhook_url": url,
            "delivery_mode":       mode,
            "delivery_modes": [{"value": v, "label": l} for v, l in _DELIVERY_MODES],
            "store_locally":       store_locally,
            "last_delivery_s":     last_at,
            "pending_count":       pending_count,
            "total_count":         total_count,
            "status":              st.get("status", ""),
            "status_color":        st.get("status_color", "888"),
        }

    def _absorb_settings_entries(self, login: str, values: Any) -> None:
        if not values or self.settings_view is None:
            return
        st = self._settings_state.setdefault(login, self._default_settings_state())
        key = f"entry_{self.settings_view.id}__webhook"
        if key in values:
            st["webhook_draft"] = str(values.get(key) or "")

    async def _settings_catch_all(self, player, action, values, **kwargs) -> None:
        self._absorb_settings_entries(player.login, values)
        if not action:
            return
        if action == "toggle_discord":
            try:
                cur = bool(await self.setting_discord_enabled.get_value())
                await self.setting_discord_enabled.set_value(not cur)
            except Exception:
                logger.exception("bug_reports: toggle discord_enabled failed")
            await self._refresh_settings(player)
            return
        if action == "toggle_store":
            try:
                cur = bool(await self.setting_store_locally.get_value())
                await self.setting_store_locally.set_value(not cur)
            except Exception:
                logger.exception("bug_reports: toggle store_locally failed")
            await self._refresh_settings(player)
            return
        if action.startswith("mode__set__"):
            value = action[len("mode__set__"):]
            if value in _VALID_DELIVERY_MODES:
                try:
                    await self.setting_delivery_mode.set_value(value)
                except Exception:
                    logger.exception("bug_reports: set delivery_mode failed")
                await self._refresh_settings(player)
            return

    async def _on_settings_save(self, player, values=None, **kwargs) -> None:
        self._absorb_settings_entries(player.login, values)
        st = self._settings_state.setdefault(player.login, self._default_settings_state())
        draft = st.get("webhook_draft")
        if draft is not None:
            url = draft.strip()
            if url and not url.startswith(("http://", "https://")):
                st["status"] = "Webhook URL must start with http:// or https://"
                st["status_color"] = "f44"
                await self._refresh_settings(player)
                return
            try:
                await self.setting_discord_webhook_url.set_value(url)
            except Exception:
                logger.exception("bug_reports: save webhook URL failed")
                st["status"] = "Save failed (see server log)."
                st["status_color"] = "f44"
                await self._refresh_settings(player)
                return
            st["webhook_draft"] = None  # re-read from Setting next refresh
        st["status"] = "Saved."
        st["status_color"] = "8f8"
        await self._refresh_settings(player)

    async def _on_settings_test(self, player, values=None, **kwargs) -> None:
        self._absorb_settings_entries(player.login, values)
        st = self._settings_state.setdefault(player.login, self._default_settings_state())
        draft = st.get("webhook_draft")
        if draft is not None:
            url = draft.strip()
        else:
            try:
                url = str(await self.setting_discord_webhook_url.get_value() or "").strip()
            except Exception:
                url = ""
        if not url:
            st["status"] = "Set a webhook URL first."
            st["status_color"] = "f80"
            await self._refresh_settings(player)
            return
        try:
            await send_ping(url)
        except DiscordDeliveryError as e:
            st["status"] = f"Test failed: {e}"
            st["status_color"] = "f44"
            await self._toast(player, f"Discord test failed: {e}", "error")
            await self._refresh_settings(player)
            return
        except Exception as e:
            logger.exception("bug_reports: discord test ping crashed")
            st["status"] = f"Test crashed: {e}"
            st["status_color"] = "f44"
            await self._refresh_settings(player)
            return
        st["status"] = "Test ping sent OK."
        st["status_color"] = "8f8"
        await self._toast(player, "Discord test ping sent", "success")
        await self._refresh_settings(player)

    async def _on_settings_flush(self, player, values=None, **kwargs) -> None:
        self._absorb_settings_entries(player.login, values)
        st = self._settings_state.setdefault(player.login, self._default_settings_state())
        try:
            sent, msg = await self._flush_pending(reason="manual")
        except Exception as e:
            logger.exception("bug_reports: manual flush crashed")
            st["status"] = f"Flush crashed: {e}"
            st["status_color"] = "f44"
            await self._refresh_settings(player)
            return
        if sent > 0:
            st["status"] = f"Sent {sent} report(s) to Discord."
            st["status_color"] = "8f8"
            await self._toast(player, f"Sent {sent} bug report(s) to Discord", "success")
        else:
            st["status"] = msg or "Nothing to send."
            st["status_color"] = "888" if "nothing" in (msg or "").lower() else "f80"
        await self._refresh_settings(player)

    # ── delivery scheduler ──────────────────────────────────────────

    async def _maybe_deliver_immediate(self, rid: int) -> None:
        """Send a single freshly-created report if mode == immediate."""
        try:
            if not bool(await self.setting_discord_enabled.get_value()):
                return
            if str(await self.setting_delivery_mode.get_value() or "") != "immediate":
                return
            url = str(await self.setting_discord_webhook_url.get_value() or "").strip()
            if not url:
                return
            row = await self.storage.get(int(rid))
            if not row:
                return
            await send_reports(url, [row])
            await self.storage.mark_delivered([int(rid)])
            await self.setting_last_delivery_at.set_value(
                _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            )
            if not bool(await self.setting_store_locally.get_value()):
                await self.storage.delete_delivered()
        except DiscordDeliveryError as e:
            logger.warning("bug_reports: immediate delivery failed: %s", e)
        except Exception:
            logger.exception("bug_reports: immediate delivery crashed")

    async def _flush_pending(self, *, reason: str = "scheduled") -> tuple[int, str]:
        """Send every pending report in batches of ≤10. Returns
        `(sent_count, status_message)`. Marks rows delivered batch-by-batch
        so a partial failure still records the successful chunks; purges
        delivered rows if store_locally is False."""
        try:
            if not bool(await self.setting_discord_enabled.get_value()):
                return (0, "Discord delivery disabled.")
            url = str(await self.setting_discord_webhook_url.get_value() or "").strip()
            if not url:
                return (0, "Webhook URL not configured.")
            pending = await self.storage.list_pending_delivery()
        except Exception:
            logger.exception("bug_reports: flush precheck failed")
            return (0, "Flush precheck failed.")
        if not pending:
            return (0, "Nothing to send.")
        total = len(pending)
        sent_total = 0
        last_error: str | None = None
        for i in range(0, total, 10):
            batch = pending[i:i + 10]
            header = (f"tmsm bug_reports — {reason} flush · {total} report(s)"
                      if i == 0 else None)
            try:
                await send_reports(url, batch, header=header)
            except DiscordDeliveryError as e:
                last_error = str(e)
                logger.warning("bug_reports: flush batch failed at offset %d: %s", i, e)
                break
            except Exception as e:
                last_error = str(e)
                logger.exception("bug_reports: flush batch crashed at offset %d", i)
                break
            try:
                await self.storage.mark_delivered([int(r["id"]) for r in batch if r.get("id")])
                sent_total += len(batch)
            except Exception:
                logger.exception("bug_reports: mark_delivered failed at offset %d", i)
        if sent_total > 0:
            try:
                await self.setting_last_delivery_at.set_value(
                    _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
                )
                if not bool(await self.setting_store_locally.get_value()):
                    await self.storage.delete_delivered()
            except Exception:
                logger.exception("bug_reports: post-delivery bookkeeping failed")
        if last_error is not None:
            return (sent_total,
                    f"Sent {sent_total}/{total}; Discord error: {last_error}")
        return (sent_total, f"Sent {sent_total} report(s).")

    async def _scheduler_loop(self) -> None:
        """Background task: every 5 minutes, decide whether a daily/weekly
        batch is due and run `_flush_pending` if so. Runs until cancelled."""
        # Small jitter at startup to avoid a thundering herd if multiple
        # controllers boot together.
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            return
        while True:
            try:
                mode = str(await self.setting_delivery_mode.get_value() or "")
                interval = _BATCH_INTERVAL_S.get(mode)
                if interval is not None and bool(await self.setting_discord_enabled.get_value()):
                    last_iso = str(await self.setting_last_delivery_at.get_value() or "")
                    due = True
                    if last_iso:
                        try:
                            last = _dt.datetime.strptime(
                                last_iso.replace("Z", ""), "%Y-%m-%dT%H:%M:%S"
                            )
                            due = (_dt.datetime.utcnow() - last).total_seconds() >= interval
                        except Exception:
                            due = True
                    if due:
                        sent, msg = await self._flush_pending(reason=mode)
                        if sent > 0:
                            logger.info("bug_reports: %s flush sent %d (%s)",
                                        mode, sent, msg)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("bug_reports: scheduler tick failed")
            try:
                await asyncio.sleep(_FLUSH_TICK_S)
            except asyncio.CancelledError:
                return

    # ── toasts (notification_engine + tmsm_status fallback) ──────────

    async def _toast(self, player, msg: str, severity: str = "info") -> None:
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
                "login": player.login, "source": "bug_reports",
            })
        except Exception:
            logger.exception("bug_reports: toast emit failed")

    # ── metadata helpers ─────────────────────────────────────────────

    def _current_game_phase(self) -> str:
        """Look up the current race phase from widget_engine if loaded."""
        try:
            we = self.instance.apps.apps.get("widget_engine")
            engine = getattr(we, "engine", None) if we is not None else None
            phase = getattr(engine, "current_phase", None) if engine is not None else None
            if phase is None:
                return "unknown"
            return str(getattr(phase, "value", phase))
        except Exception:
            return "unknown"

    def _pyplanet_uptime_s(self) -> int:
        try:
            return max(0, int(time.monotonic() - self._pyplanet_start))
        except Exception:
            return 0

    def _dedicated_uptime_s(self) -> int:
        """Best-effort dedicated server uptime via psutil. Caches the start
        epoch so we only enumerate processes once."""
        try:
            if self._dedicated_start_epoch is None:
                self._dedicated_start_epoch = _detect_dedicated_start_epoch()
            if self._dedicated_start_epoch is None:
                return 0
            return max(0, int(time.time() - self._dedicated_start_epoch))
        except Exception:
            return 0

    async def _game_version(self) -> str:
        """Read dedicated server version + build via the XMLRPC `GetVersion`
        call. Returns a short human-friendly string."""
        try:
            v = await self.instance.gbx("GetVersion")
        except Exception:
            return ""
        if not isinstance(v, dict):
            return ""
        name = str(v.get("Name") or "").strip()
        version = str(v.get("Version") or "").strip()
        build = str(v.get("Build") or "").strip()
        parts = [p for p in (name, version, build) if p]
        return " ".join(parts)

    async def _client_game_version(self, login: str) -> str:
        """Resolve the reporter's client game version via `GetDetailedPlayerInfo`.

        Returns ``ClientVersion`` (and ``ClientTitleVersion`` when present)
        formatted as ``"<title> <version>"`` — silently empty if the call
        fails or the player has already disconnected.
        """
        try:
            info = await self.instance.gbx("GetDetailedPlayerInfo", login)
        except Exception:
            return ""
        if not isinstance(info, dict):
            return ""
        cv = str(info.get("ClientVersion") or "").strip()
        ctv = str(info.get("ClientTitleVersion") or "").strip()
        if cv and ctv and ctv not in cv:
            return f"{cv} ({ctv})"
        return cv or ctv


_DEDICATED_PROC_NAMES = ("TrackmaniaServer", "ManiaPlanetServer")


def _detect_dedicated_start_epoch() -> float | None:
    """Return the start-time epoch of the dedicated server process, or None.

    Uses psutil if available; silently falls back to None on any failure
    (psutil missing, no permission, no matching process).
    """
    try:
        import psutil  # type: ignore
    except Exception:
        return None
    try:
        for proc in psutil.process_iter(attrs=("name", "create_time")):
            try:
                name = (proc.info.get("name") or "").strip()
                if not name:
                    continue
                if any(name == n or name.startswith(n) for n in _DEDICATED_PROC_NAMES):
                    return float(proc.info.get("create_time") or 0) or None
            except Exception:
                continue
    except Exception:
        return None
    return None
