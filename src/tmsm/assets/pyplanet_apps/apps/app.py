"""Master Apps store — datatable for enabling/disabling installed pyplanet addons.

Uses the tmsm.ui framework (BaseView + tmsm_ui/widgets.xml macros).
"""
from __future__ import annotations

import ast
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from pyplanet.apps.config import AppConfig

from .views import AppsStoreView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)

TMSM_ROOT = Path.home() / ".tmsm"
PYPL_ROOT = TMSM_ROOT / "pyplanet"
APPS_ROOT = PYPL_ROOT / "src" / "pyplanet" / "apps"


def _apps_py_path() -> Path:
    """Resolve the active pool's ``settings/apps.py``.

    PyPlanet always runs with the pool directory as cwd, so cwd/settings/apps.py
    is the active file regardless of the pool's name.
    """
    return Path(os.getcwd()) / "settings" / "apps.py"


APPS_PY = _apps_py_path()

_ENTRY_RE = re.compile(
    r"""^[ \t]*(\#[ \t]*)?["']([A-Za-z0-9_.]+)["'][ \t]*,?[ \t]*$"""
)

PAGE_SIZE = 21

_DISCOVER_CACHE: list[dict[str, str]] | None = None
_ACTIVE_CACHE: set[str] | None = None


def _invalidate_caches() -> None:
    global _DISCOVER_CACHE, _ACTIVE_CACHE
    _DISCOVER_CACHE = None
    _ACTIVE_CACHE = None


def _discover_installed() -> list[dict[str, str]]:
    global _DISCOVER_CACHE
    if _DISCOVER_CACHE is not None:
        return _DISCOVER_CACHE
    rows: list[dict[str, str]] = []
    for namespace in ("tmsm", "contrib"):
        ns_dir = APPS_ROOT / namespace
        if not ns_dir.is_dir():
            continue
        for sub in sorted(ns_dir.iterdir()):
            if not sub.is_dir() or sub.name.startswith("_"):
                continue
            rows.append({
                "key": sub.name,
                "namespace": namespace,
                "module": f"pyplanet.apps.{namespace}.{sub.name}",
                "description": _describe(sub),
            })
    _DISCOVER_CACHE = rows
    return rows


def _describe(app_dir: Path) -> str:
    manifest = app_dir / "tmsm-addon.json"
    if manifest.is_file():
        try:
            desc = json.loads(manifest.read_text(encoding="utf-8")).get("description", "")
            if desc:
                return str(desc).splitlines()[0][:75]
        except Exception:
            pass
    init_py = app_dir / "__init__.py"
    if init_py.is_file():
        try:
            doc = ast.get_docstring(ast.parse(init_py.read_text(encoding="utf-8")))
            if doc:
                return doc.splitlines()[0][:75]
        except Exception:
            pass
    return ""


def _read_active_modules() -> set[str]:
    global _ACTIVE_CACHE
    if _ACTIVE_CACHE is not None:
        return _ACTIVE_CACHE
    if not APPS_PY.is_file():
        _ACTIVE_CACHE = set()
        return _ACTIVE_CACHE
    out: set[str] = set()
    for line in APPS_PY.read_text(encoding="utf-8").splitlines():
        m = _ENTRY_RE.match(line)
        if m and m.group(1) is None and m.group(2).startswith("pyplanet.apps."):
            out.add(m.group(2))
    _ACTIVE_CACHE = out
    return out


def _write_apps_state(enabled: set[str]) -> None:
    try:
        from tmsm.assets import apps_py as apps_py_mod  # type: ignore
    except Exception:
        apps_py_mod = None  # type: ignore
    installed = {r["module"] for r in _discover_installed()}
    tracked = installed | enabled
    if apps_py_mod is not None:
        apps_py_mod.sync_apps_py(APPS_PY, sorted(tracked))

    out_lines: list[str] = []
    for line in APPS_PY.read_text(encoding="utf-8").splitlines():
        m = _ENTRY_RE.match(line)
        if not m or not m.group(2).startswith("pyplanet.apps."):
            out_lines.append(line)
            continue
        module = m.group(2)
        indent = re.match(r"^([ \t]*)", line).group(1)  # type: ignore[union-attr]
        if module in enabled:
            out_lines.append(f"{indent}'{module}',")
        else:
            out_lines.append(f"{indent}# '{module}',")
    APPS_PY.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    _invalidate_caches()


class App_Apps(AppConfig):
    name = "pyplanet.apps.tmsm.apps"
    label = "tmsm_apps_store"
    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_hub"]
    game_dependencies = ["trackmania", "trackmania_next"]

    HUB_KEY = "apps"
    HUB_NAME = "Apps Store"
    HUB_ICON = "th-large"
    HUB_DESCRIPTION = "Enable / disable installed pyplanet addons."
    HUB_ORDER = 10

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.view: AppsStoreView | None = None
        # per-player ui state
        self._pending: dict[str, dict[str, bool]] = {}
        self._page: dict[str, int] = {}
        self._query: dict[str, str] = {}
        self._status: dict[str, tuple[str, str]] = {}  # login -> (msg, color)

    async def on_start(self) -> None:
        self.view = AppsStoreView(self)
        self.view.connect("apply", self._on_apply)
        self.view.connect("reset", self._on_reset)
        self.view.handle_catch_all = self._catch_all

        if not _HAS_HUB:
            logger.warning("apps: tmsm_hub not available")
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("apps: tmsm_hub:register signal not registered yet")
            return
        entry = HubAppEntry(
            key=self.HUB_KEY, name=self.HUB_NAME, icon=self.HUB_ICON,
            role=Role.MASTER, order=self.HUB_ORDER,
            description=self.HUB_DESCRIPTION, open=self._open,
        )
        await sig.send_robust({"entry": entry}, raw=True)

    async def on_stop(self) -> None:
        if self.view is not None:
            try:
                await self.view.destroy()
            except Exception:
                pass
            self.view = None

    async def _open(self, player) -> None:
        if self.view is None:
            return
        try:
            await self.view.display(player_logins=[player.login])
        except Exception:
            logger.exception("apps: open display failed")

    async def _on_back(self, player, **kwargs) -> None:
        if self.view is not None:
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.view, player_logins=[player.login])
            except Exception:
                logger.exception("apps: hide failed")
        try:
            sig = self.context.signals.get_signal("tmsm_hub:show")
            await sig.send_robust({"player": player}, raw=True)
        except KeyError:
            pass

    async def _on_reset(self, player, **kwargs) -> None:
        self._pending.pop(player.login, None)
        self._status.pop(player.login, None)
        await self._open(player)

    async def _on_apply(self, player, **kwargs) -> None:
        pending = self._pending.get(player.login) or {}
        if not pending:
            self._status[player.login] = ("nothing to apply", "aaa")
            await self._open(player)
            return
        active = _read_active_modules()
        enabled = set(active)
        for module, want_on in pending.items():
            if want_on:
                enabled.add(module)
            else:
                enabled.discard(module)
        try:
            _write_apps_state(enabled)
        except Exception as e:
            logger.exception("apps: write failed")
            self._status[player.login] = (f"save failed: {e}", "f44")
            await self._open(player)
            return
        n = len(pending)
        self._pending.pop(player.login, None)
        self._status[player.login] = (
            f"saved {n} change(s) — pool will reload", "0f0")
        await self._open(player)
        try:
            APPS_PY.touch()
        except OSError:
            pass
        try:
            sig = self.context.signals.get_signal("tmsm_status:notify")
            await sig.send_robust({
                "message": f"Apps updated: {n} change(s)",
                "severity": "success",
                "login": player.login,
                "source": "apps",
                "duration_ms": 3500,
            })
        except KeyError:
            pass

    async def _catch_all(self, player, action, values, **kwargs):
        # The view-id prefix is stripped by TemplateView before dispatch, so
        # actions arrive here as e.g. "toggle__<module>", "pg__first",
        # "pg__page__3", or "q__clear".
        self._absorb_query(player, values)

        if action.startswith("toggle__"):
            module = action[len("toggle__"):]
            active = _read_active_modules()
            pending = self._pending.setdefault(player.login, {})
            cur = pending.get(module, module in active)
            new = not cur
            if new == (module in active):
                pending.pop(module, None)
            else:
                pending[module] = new
            await self._open(player)
            return

        if action.startswith("pg__"):
            tail = action[len("pg__"):]
            total = self._filtered_total(player.login)
            total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            cur = self._page.get(player.login, 1)
            if tail == "first":
                cur = 1
            elif tail == "prev":
                cur = max(1, cur - 1)
            elif tail == "next":
                cur = min(total_pages, cur + 1)
            elif tail == "last":
                cur = total_pages
            elif tail.startswith("page__"):
                try:
                    cur = max(1, min(total_pages, int(tail.split("__", 1)[1])))
                except ValueError:
                    pass
            self._page[player.login] = cur
            await self._open(player)
            return

        if action == "q__clear":
            self._query.pop(player.login, None)
            self._page[player.login] = 1
            await self._open(player)
            return

    def _absorb_query(self, player, values):
        if not values or self.view is None:
            return
        key = f"entry_{self.view.id}__q"
        if key in values:
            q = str(values[key] or "").strip()
            old = self._query.get(player.login, "")
            if q != old:
                self._query[player.login] = q
                self._page[player.login] = 1

    def _filtered_rows(self, login: str | None) -> list[dict[str, Any]]:
        q = (self._query.get(login or "", "") or "").lower()
        rows = _discover_installed()
        if q:
            rows = [r for r in rows
                    if q in r["key"].lower()
                    or q in r["description"].lower()
                    or q in r["namespace"].lower()]
        return rows

    def _filtered_total(self, login: str | None) -> int:
        return len(self._filtered_rows(login))

    def apps_context(self, login: str | None) -> dict[str, Any]:
        active = _read_active_modules()
        pending = self._pending.get(login or "", {})
        all_rows = _discover_installed()
        filtered = self._filtered_rows(login)
        total = len(filtered)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(1, min(total_pages, self._page.get(login or "", 1)))
        start = (page - 1) * PAGE_SIZE
        sliced = filtered[start:start + PAGE_SIZE]
        rows = []
        for r in sliced:
            module = r["module"]
            now_active = module in active
            enabled = pending.get(module, now_active)
            rows.append({
                **r,
                "enabled": enabled,
                "dirty": module in pending,
            })
        status_text, status_color = self._status.get(login or "", ("", "aaa"))
        return {
            "addons": rows,
            "page": page,
            "total_pages": total_pages,
            "total_count": len(all_rows),
            "query": self._query.get(login or "", ""),
            "dirty_count": len(pending),
            "status_text": status_text,
            "status_color": status_color,
            "apps_py": str(APPS_PY),
        }
