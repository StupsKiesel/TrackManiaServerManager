"""tmsm system — registers Status / Logs / Apps tiles in the hub."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import time
from pathlib import Path
from typing import Any

from pyplanet.apps.config import AppConfig

from .views import AppsView, LogsView, StatusView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)

TMSM_ROOT = Path.home() / ".tmsm"
PYPL_ROOT = TMSM_ROOT / "pyplanet"
PYPL_APPS_DIR = PYPL_ROOT / "src" / "pyplanet" / "apps" / "tmsm"
APPS_PY = PYPL_ROOT / "pools" / "pypl" / "settings" / "apps.py"
# Legacy explicit markers — still recognised for old apps.py files,
# but new writes use the header-based block managed by tmsm.assets.apps_py.
MARK_BEGIN = "# >>> tmsm-managed (uncomment a line to activate the addon) >>>"
MARK_END = "# <<< tmsm-managed <<<"

# Header-based block markers (current format). The block is the contiguous
# run of group headers + entries; nothing else surrounds it.
_HEADER_RE = re.compile(
    r"^[ \t]*#[ \t]*---[ \t]*(?:TrackManiaServerManager|Community made|other)[ \t]*---[ \t]*$"
)
_ENTRY_RE = re.compile(r"""^[ \t]*(\#[ \t]*)?["']([A-Za-z0-9_.]+)["'][ \t]*,?[ \t]*$""")


def _read_meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            rest = rest.strip()
            if rest.endswith(" kB"):
                try:
                    out[key] = int(rest[:-3]) * 1024
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def _read_loadavg() -> tuple[float, float, float]:
    try:
        parts = Path("/proc/loadavg").read_text().split()
        return float(parts[0]), float(parts[1]), float(parts[2])
    except (OSError, ValueError, IndexError):
        return (0.0, 0.0, 0.0)


def _read_uptime() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _human_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n = int(n / 1024 * 10) / 10
    return f"{n} PiB"


def _human_seconds(n: float) -> str:
    n = int(n)
    d, n = divmod(n, 86400)
    h, n = divmod(n, 3600)
    m, _ = divmod(n, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _port_listening(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _tail(path: Path, n: int = 200, needle: str = "") -> list[str]:
    if not path or not path.is_file():
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            back = min(size, 64 * 1024)
            f.seek(size - back, os.SEEK_SET)
            blob = f.read()
        lines = blob.decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if needle:
        lo = needle.lower()
        lines = [ln for ln in lines if lo in ln.lower()]
    return lines[-n:]


class SystemApp(AppConfig):
    name = "pyplanet.apps.tmsm.system"
    label = "tmsm_system"
    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_hub"]
    game_dependencies = ["trackmania", "trackmania_next"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.status_view: StatusView | None = None
        self.logs_view: LogsView | None = None
        self.apps_view: AppsView | None = None
        # logs viewer per-player state: {login: {file: str, filter: str}}
        self._logs_state: dict[str, dict[str, str]] = {}
        # apps manager pending toggles: {login: {addon_label: bool}}
        self._apps_pending: dict[str, dict[str, bool]] = {}
        self._apps_status: dict[str, tuple[str, str]] = {}  # login -> (msg, color)

    async def on_start(self) -> None:
        self.status_view = StatusView(self)
        self.logs_view = LogsView(self)
        self.apps_view = AppsView(self)

        self.status_view.connect("refresh", self._on_status_refresh)
        self.status_view.connect("back", self._on_back)

        self.logs_view.connect("refresh", self._on_logs_refresh)
        self.logs_view.connect("back", self._on_back)
        self.logs_view.handle_catch_all = self._logs_catch_all

        self.apps_view.connect("save", self._on_apps_save)
        self.apps_view.connect("refresh", self._on_apps_refresh)
        self.apps_view.connect("back", self._on_back)
        self.apps_view.handle_catch_all = self._apps_catch_all

        await self._register_with_hub()

    async def on_stop(self) -> None:
        for v in (self.status_view, self.logs_view, self.apps_view):
            if v is not None:
                try:
                    await v.destroy()
                except Exception:
                    logger.exception("system: destroy failed")

    # ---- hub registration -------------------------------------------

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("system: tmsm_hub:register signal not registered yet")
            return
        entries = [
            HubAppEntry(key="status", name="System Status", icon="info",
                        role=Role.MASTER, order=20,
                        description="Host CPU / memory / disk + service state",
                        open=self._open_status),
            HubAppEntry(key="logs", name="Logs Viewer", icon="file",
                        role=Role.MASTER, order=21,
                        description="Tail and filter any tmsm log file",
                        open=self._open_logs),
            HubAppEntry(key="apps", name="PyPlanet Apps", icon="cog",
                        role=Role.MASTER, order=22,
                        description="Enable / disable tmsm-managed addons",
                        open=self._open_apps),
        ]
        for e in entries:
            await sig.send_robust({"entry": e}, raw=True)

    async def _open_status(self, player) -> None:
        await self._open(self.status_view, player)

    async def _open_logs(self, player) -> None:
        # initialise state if missing
        files = self.list_log_files()
        st = self._logs_state.setdefault(player.login, {"file": "", "filter": ""})
        if not st.get("file") and files:
            st["file"] = files[0]["path"]
        await self._open(self.logs_view, player)

    async def _open_apps(self, player) -> None:
        self._apps_pending.pop(player.login, None)
        await self._open(self.apps_view, player)

    async def _open(self, view, player) -> None:
        if view is None:
            return
        try:
            await view.display(player_logins=[player.login])
        except Exception:
            logger.exception("system: open display failed")

    async def _on_back(self, player, **kwargs) -> None:
        for v in (self.status_view, self.logs_view, self.apps_view):
            if v is None:
                continue
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(v, player_logins=[player.login])
            except Exception:
                logger.exception("system: hide failed")
        try:
            sig = self.context.signals.get_signal("tmsm_hub:show")
            await sig.send_robust({"player": player}, raw=True)
        except KeyError:
            pass

    # ================================================================
    # Status
    # ================================================================

    def collect_status(self) -> dict[str, Any]:
        mi = _read_meminfo()
        total = mi.get("MemTotal", 0)
        avail = mi.get("MemAvailable", 0)
        used = max(0, total - avail)
        mem_pct = (used * 100 // total) if total else 0
        try:
            du = shutil.disk_usage(str(TMSM_ROOT) if TMSM_ROOT.is_dir() else "/")
        except OSError:
            du = (0, 0, 0)  # type: ignore[assignment]
        try:
            ncpu = os.cpu_count() or 1
        except Exception:
            ncpu = 1
        load = _read_loadavg()
        # pool log = we're running, by definition
        pyp_log = PYPL_ROOT / "pools" / "pypl" / "logs" / "tmsm.log"
        # detect dedicated server: check xmlrpc port from PyPlanet config
        ded_port = self._dedicated_xmlrpc_port()
        ded_up = _port_listening("127.0.0.1", ded_port) if ded_port else False
        try:
            hostname = socket.gethostname()
        except OSError:
            hostname = "?"
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        return {
            "hostname": hostname,
            "ts": ts,
            "uptime_s": _human_seconds(_read_uptime()),
            "cpu_count": ncpu,
            "load1": load[0], "load5": load[1], "load15": load[2],
            "load1_pct": min(100, int(load[0] / max(ncpu, 1) * 100)),
            "mem_total": _human_bytes(total),
            "mem_used": _human_bytes(used),
            "mem_avail": _human_bytes(avail),
            "mem_pct": mem_pct,
            "disk_total": _human_bytes(du[0]),
            "disk_used": _human_bytes(du[1]),
            "disk_free": _human_bytes(du[2]),
            "disk_pct": (du[1] * 100 // du[0]) if du[0] else 0,
            "pyp_up": True,
            "pyp_log_size": _human_bytes(pyp_log.stat().st_size) if pyp_log.is_file() else "?",
            "ded_port": ded_port or 0,
            "ded_up": ded_up,
        }

    def _dedicated_xmlrpc_port(self) -> int:
        try:
            ded = getattr(self.instance, "game", None)
            # cleanest: pull from settings the way the instance was configured
            from pyplanet.conf import settings
            return int(settings.DEDICATED["default"]["PORT"])
        except Exception:
            return 0

    async def _on_status_refresh(self, player, **kwargs) -> None:
        await self._open(self.status_view, player)

    # ================================================================
    # Logs Viewer
    # ================================================================

    def list_log_files(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        seen: set[Path] = set()
        candidates: list[Path] = []
        # pool logs
        for sub in (PYPL_ROOT / "pools").glob("*/logs/*.log"):
            candidates.append(sub)
        # server logs
        for sub in (TMSM_ROOT / "servers").glob("*/logs/*.log"):
            candidates.append(sub)
        for p in candidates:
            try:
                p = p.resolve()
            except OSError:
                continue
            if p in seen or not p.is_file():
                continue
            seen.add(p)
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            rel = p.relative_to(TMSM_ROOT) if TMSM_ROOT in p.parents else p
            out.append({
                "path": str(p),
                "label": str(rel),
                "size": _human_bytes(size),
            })
        out.sort(key=lambda r: r["label"])
        return out

    def logs_context(self, login: str) -> dict[str, Any]:
        files = self.list_log_files()
        st = self._logs_state.setdefault(login, {"file": "", "filter": ""})
        active = st.get("file") or (files[0]["path"] if files else "")
        flt = st.get("filter", "")
        lines = _tail(Path(active), n=200, needle=flt) if active else []
        return {
            "files": files,
            "active": active,
            "filter": flt,
            "lines": lines,
            "log_path": active,
        }

    async def _logs_catch_all(self, player, action, values):
        # actions: <view_id>__pick__<index>  or <view_id>__apply
        st = self._logs_state.setdefault(player.login, {"file": "", "filter": ""})
        if "__pick__" in action:
            try:
                idx = int(action.rsplit("__", 1)[-1])
            except ValueError:
                return
            files = self.list_log_files()
            if 0 <= idx < len(files):
                st["file"] = files[idx]["path"]
            await self._open(self.logs_view, player)
            return
        if action.endswith("__apply"):
            # find filter entry
            for k, v in (values or {}).items():
                if k.startswith("entry_") and k.endswith("__filter"):
                    st["filter"] = str(v or "")
                    break
            await self._open(self.logs_view, player)
            return

    async def _on_logs_refresh(self, player, **kwargs) -> None:
        await self._open(self.logs_view, player)

    # ================================================================
    # Apps manager
    # ================================================================

    def _discover_addons(self) -> list[dict[str, str]]:
        """Find every tmsm addon symlinked into the pool."""
        out: list[dict[str, str]] = []
        if not PYPL_APPS_DIR.is_dir():
            return out
        for sub in sorted(PYPL_APPS_DIR.iterdir()):
            if not sub.is_dir():
                continue
            manifest = sub / "tmsm-addon.json"
            desc = ""
            if manifest.is_file():
                try:
                    desc = json.loads(manifest.read_text(encoding="utf-8")).get("description", "")
                except Exception:
                    desc = ""
            out.append({
                "key": sub.name,
                "module": f"pyplanet.apps.tmsm.{sub.name}",
                "description": desc,
            })
        return out

    def _read_apps_block(self) -> tuple[list[str], list[str], list[str]]:
        """Return (head_lines, managed_module_names, tail_lines).

        managed_module_names contains every module currently active inside
        the tmsm-managed block (uncommented entries only). Supports both the
        current header-based block and the legacy >>> / <<< markers.
        """
        text = APPS_PY.read_text(encoding="utf-8") if APPS_PY.is_file() else ""
        lines = text.splitlines()

        # Legacy marker format takes priority — it's unambiguous.
        try:
            i_begin = next(i for i, l in enumerate(lines) if MARK_BEGIN in l)
            i_end = next(i for i, l in enumerate(lines) if MARK_END in l)
            active: list[str] = []
            for raw in lines[i_begin + 1:i_end]:
                s = raw.strip()
                if not s or s.startswith("#"):
                    continue
                m = re.match(r"['\"]([^'\"]+)['\"]\s*,?", s)
                if m:
                    active.append(m.group(1))
            return lines[:i_begin + 1], active, lines[i_end:]
        except StopIteration:
            pass

        # Header-based format: find the contiguous run of group headers /
        # entry lines / blank lines anchored at the first matching header.
        first_idx = next((i for i, l in enumerate(lines) if _HEADER_RE.match(l)), None)
        if first_idx is None:
            return lines, [], []
        # Include preceding blank lines so we replace them too on rewrite.
        start_idx = first_idx
        while start_idx > 0 and lines[start_idx - 1].strip() == "":
            start_idx -= 1
        last_content = first_idx
        i = first_idx
        while i < len(lines):
            stripped = lines[i].strip()
            if stripped == "":
                i += 1
                continue
            if _HEADER_RE.match(lines[i]) or _ENTRY_RE.match(lines[i]):
                last_content = i
                i += 1
                continue
            break
        active = []
        for raw in lines[start_idx:last_content + 1]:
            em = _ENTRY_RE.match(raw)
            if em and em.group(1) is None:   # uncommented = active
                active.append(em.group(2))
        return lines[:start_idx], active, lines[last_content + 1:]

    def apps_context(self, login: str | None) -> dict[str, Any]:
        head, active, tail = self._read_apps_block()
        active_set = set(active)
        addons = self._discover_addons()
        pending = self._apps_pending.get(login or "", {})
        rows: list[dict[str, Any]] = []
        for a in addons:
            now = pending.get(a["module"], a["module"] in active_set)
            rows.append({**a, "enabled": now,
                         "dirty": a["module"] in pending})
        for mod in active:
            if not any(r["module"] == mod for r in rows):
                rows.append({"key": mod.rsplit(".", 1)[-1], "module": mod,
                             "description": "(not found on disk)",
                             "enabled": pending.get(mod, True),
                             "dirty": mod in pending})
        status_text, status_color = self._apps_status.get(login or "", ("", "aaa"))
        return {"addons": rows, "apps_py": str(APPS_PY),
                "dirty_count": sum(1 for r in rows if r["dirty"]),
                "status_text": status_text, "status_color": status_color}

    def _write_apps_block(self, enabled_modules: list[str]) -> None:
        """Rewrite the tmsm-managed block. Uses the shared writer when the
        tmsm package is importable (the usual case in tmsm-managed pools),
        which preserves grouped headers and the on-disk format used by new
        pools. Falls back to a minimal in-place rewrite otherwise.
        """
        try:
            from tmsm.assets import apps_py as apps_py_mod  # type: ignore
        except Exception:
            apps_py_mod = None  # type: ignore

        if apps_py_mod is not None:
            # Collect every module we want to keep tracked (active + inactive
            # on disk + everything currently discoverable on the filesystem).
            _, active_on_disk, _ = self._read_apps_block()
            tracked: set[str] = set(active_on_disk)
            for a in self._discover_addons():
                tracked.add(a["module"])
            tracked.update(enabled_modules)

            # First pass: sync the full list (preserves per-module state).
            apps_py_mod.sync_apps_py(APPS_PY, sorted(tracked))

            # Second pass: force each entry to the user's chosen state.
            wanted_active = set(enabled_modules)
            out: list[str] = []
            for line in APPS_PY.read_text(encoding="utf-8").splitlines():
                em = _ENTRY_RE.match(line)
                if not em:
                    out.append(line)
                    continue
                module = em.group(2)
                indent = re.match(r"^([ \t]*)", line).group(1)  # type: ignore[union-attr]
                if module in wanted_active:
                    out.append(f"{indent}'{module}',")
                else:
                    out.append(f"{indent}# '{module}',")
            APPS_PY.write_text("\n".join(out) + "\n", encoding="utf-8")
            return

        # Fallback (tmsm not importable from this venv): minimal rewrite.
        head, _active, tail = self._read_apps_block()
        if not head and not tail:
            raise RuntimeError("could not locate tmsm-managed block in apps.py")
        body = ["        '" + m + "'," for m in enabled_modules]
        new_lines = head + body + tail
        APPS_PY.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    async def _apps_catch_all(self, player, action, values):
        if "__toggle__" in action:
            key = action.rsplit("__", 1)[-1]
            module = f"pyplanet.apps.tmsm.{key}"
            ctx = self.apps_context(player.login)
            current = {r["module"]: r["enabled"] for r in ctx["addons"]}
            pending = self._apps_pending.setdefault(player.login, {})
            pending[module] = not current.get(module, False)
            await self._open(self.apps_view, player)
            return

    async def _on_apps_save(self, player, **kwargs) -> None:
        ctx = self.apps_context(player.login)
        enabled = [r["module"] for r in ctx["addons"] if r["enabled"]]
        try:
            self._write_apps_block(enabled)
        except Exception as e:
            self._apps_status[player.login] = (f"save failed: {e}", "f44")
            await self._open(self.apps_view, player)
            return
        self._apps_pending.pop(player.login, None)
        self._apps_status[player.login] = ("saved — pool will restart", "0f0")
        await self._open(self.apps_view, player)
        # nudge dev_reload by touching apps.py once more (already done by write)
        try:
            APPS_PY.touch()
        except OSError:
            pass

    async def _on_apps_refresh(self, player, **kwargs) -> None:
        self._apps_pending.pop(player.login, None)
        await self._open(self.apps_view, player)
