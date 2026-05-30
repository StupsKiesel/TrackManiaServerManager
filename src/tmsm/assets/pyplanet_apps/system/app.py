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
        # status: per-player active tab and metrics counters
        self._status_state: dict[str, dict[str, Any]] = {}
        # session-lifetime counters (since pyplanet start)
        self._metrics: dict[str, int] = {
            "chat": 0, "connects": 0, "disconnects": 0,
        }
        # cache for expensive storage walks: {key: (ts, value)}
        self._stat_cache: dict[str, tuple[float, Any]] = {}
        # logs viewer per-player state
        # {login: {file, filter, page, selected, confirm_delete, status, status_color}}
        self._logs_state: dict[str, dict[str, Any]] = {}
        # apps manager pending toggles: {login: {addon_label: bool}}
        self._apps_pending: dict[str, dict[str, bool]] = {}
        self._apps_status: dict[str, tuple[str, str]] = {}  # login -> (msg, color)

    async def on_start(self) -> None:
        self.status_view = StatusView(self)
        self.logs_view = LogsView(self)
        self.apps_view = AppsView(self)

        self.status_view.connect("refresh", self._on_status_refresh)
        self.status_view.handle_catch_all = self._status_catch_all

        # session counters
        try:
            self.context.signals.listen("maniaplanet:player_chat", self._on_chat)
            self.context.signals.listen("maniaplanet:player_connect", self._on_player_connect_metric)
            self.context.signals.listen("maniaplanet:player_disconnect", self._on_player_disconnect_metric)
        except Exception:
            logger.exception("system: failed to bind metric listeners")

        self.logs_view.connect("refresh", self._on_logs_refresh)
        self.logs_view.connect("apply", self._on_logs_apply)
        self.logs_view.connect("delete", self._on_logs_delete)
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
        files = self.list_log_files()
        st = self._logs_state.setdefault(player.login, self._fresh_logs_state())
        if not st.get("file") and files:
            st["file"] = files[0]["path"]
        st["confirm_delete"] = ""
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

    STATUS_TABS = [
        {"key": "host",     "label": "Host"},
        {"key": "storage",  "label": "Storage"},
        {"key": "server",   "label": "Server"},
        {"key": "pyplanet", "label": "PyPlanet"},
        {"key": "players",  "label": "Players"},
    ]
    STORAGE_CACHE_TTL = 30.0  # seconds

    def _status_st(self, login: str) -> dict[str, Any]:
        return self._status_state.setdefault(login, {"tab": "host"})

    async def _on_chat(self, **kwargs):
        self._metrics["chat"] += 1

    async def _on_player_connect_metric(self, **kwargs):
        self._metrics["connects"] += 1

    async def _on_player_disconnect_metric(self, **kwargs):
        self._metrics["disconnects"] += 1

    async def _status_catch_all(self, player, action, values):
        if action.startswith("tabs__tab__"):
            tab = action.rsplit("__", 1)[-1]
            if any(t["key"] == tab for t in self.STATUS_TABS):
                self._status_st(player.login)["tab"] = tab
                await self._open(self.status_view, player)

    # ----- shared host snapshot --------------------------------------

    def _host_block(self) -> dict[str, Any]:
        mi = _read_meminfo()
        total = mi.get("MemTotal", 0)
        avail = mi.get("MemAvailable", 0)
        used = max(0, total - avail)
        mem_pct = (used * 100 // total) if total else 0
        sw_total = mi.get("SwapTotal", 0)
        sw_free = mi.get("SwapFree", 0)
        sw_used = max(0, sw_total - sw_free)
        sw_pct = (sw_used * 100 // sw_total) if sw_total else 0
        try:
            du = shutil.disk_usage(str(TMSM_ROOT) if TMSM_ROOT.is_dir() else "/")
            d_total, d_used, d_free = du.total, du.used, du.free
        except OSError:
            d_total = d_used = d_free = 0
        try:
            ncpu = os.cpu_count() or 1
        except Exception:
            ncpu = 1
        load = _read_loadavg()
        try:
            hostname = socket.gethostname()
        except OSError:
            hostname = "?"
        # /etc/os-release pretty name
        os_name = "?"
        try:
            for line in Path("/etc/os-release").read_text().splitlines():
                if line.startswith("PRETTY_NAME="):
                    os_name = line.partition("=")[2].strip().strip('"')
                    break
        except OSError:
            pass
        kernel = ""
        try:
            kernel = os.uname().release
        except Exception:
            pass
        cpu_model = "?"
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("model name"):
                    cpu_model = line.partition(":")[2].strip()
                    break
        except OSError:
            pass
        return {
            "hostname": hostname,
            "os_name": os_name,
            "kernel": kernel,
            "uptime_s": _human_seconds(_read_uptime()),
            "cpu_count": ncpu,
            "cpu_model": cpu_model[:60],
            "load1": load[0], "load5": load[1], "load15": load[2],
            "load1_pct": min(100, int(load[0] / max(ncpu, 1) * 100)),
            "mem_total": _human_bytes(total),
            "mem_used": _human_bytes(used),
            "mem_avail": _human_bytes(avail),
            "mem_pct": mem_pct,
            "swap_total": _human_bytes(sw_total),
            "swap_used": _human_bytes(sw_used),
            "swap_pct": sw_pct,
            "disk_total": _human_bytes(d_total),
            "disk_used": _human_bytes(d_used),
            "disk_free": _human_bytes(d_free),
            "disk_pct": (d_used * 100 // d_total) if d_total else 0,
        }

    # ----- storage scan (cached) -------------------------------------

    def _walk_size(self, base: Path, patterns: tuple[str, ...]) -> tuple[int, int]:
        """Return (file_count, total_bytes) for files under base matching any suffix in patterns."""
        if not base.is_dir():
            return (0, 0)
        n = 0
        sz = 0
        try:
            for root, _, files in os.walk(base, followlinks=False):
                for fn in files:
                    if patterns and not fn.lower().endswith(patterns):
                        continue
                    fp = Path(root) / fn
                    try:
                        sz += fp.stat().st_size
                        n += 1
                    except OSError:
                        continue
        except OSError:
            pass
        return n, sz

    def _dir_size(self, base: Path) -> int:
        return self._walk_size(base, ())[1]

    def _storage_block(self) -> dict[str, Any]:
        now = time.time()
        cached = self._stat_cache.get("storage")
        if cached and now - cached[0] < self.STORAGE_CACHE_TTL:
            return cached[1]
        servers = TMSM_ROOT / "servers"
        backups = TMSM_ROOT / "backups"
        # logs across servers + pyplanet
        logs_n = logs_sz = 0
        for base in (servers, PYPL_ROOT / "pools"):
            n, s = self._walk_size(base, (".log",))
            logs_n += n
            logs_sz += s
        # maps under each server's UserData/Maps
        maps_n = maps_sz = 0
        if servers.is_dir():
            for srv in servers.iterdir():
                if not srv.is_dir():
                    continue
                n, s = self._walk_size(srv / "UserData" / "Maps", (".map.gbx",))
                maps_n += n
                maps_sz += s
        backups_sz = self._dir_size(backups)
        tmsm_total = self._dir_size(TMSM_ROOT)
        # DB size (sqlite file under pyplanet pool; mariadb datadir not reachable)
        db_sz = 0
        db_kind = "?"
        try:
            from pyplanet.conf import settings as _settings
            db_cfg = _settings.DATABASES.get("default", {})
            engine = db_cfg.get("ENGINE", "")
            db_kind = engine.rsplit(".", 1)[-1] or "?"
            if "sqlite" in engine.lower():
                f = Path(db_cfg.get("OPTIONS", {}).get("file", db_cfg.get("NAME", "")))
                if f.is_file():
                    db_sz = f.stat().st_size
        except Exception:
            logger.exception("system: db size lookup failed")
        out = {
            "tmsm_total":   _human_bytes(tmsm_total),
            "logs_count":   logs_n,
            "logs_size":    _human_bytes(logs_sz),
            "maps_count":   maps_n,
            "maps_size":    _human_bytes(maps_sz),
            "backups_size": _human_bytes(backups_sz),
            "db_kind":      db_kind,
            "db_size":      _human_bytes(db_sz) if db_sz else "—",
        }
        self._stat_cache["storage"] = (now, out)
        return out

    # ----- server (gbx) ----------------------------------------------

    async def _server_block(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "name": "?", "title": "?", "mode": "?",
            "players": 0, "max_players": 0,
            "specs": 0, "max_specs": 0,
            "current_map": "?", "map_author": "?",
            "playlist_count": 0,
            "session_chat": self._metrics.get("chat", 0),
            "session_connects": self._metrics.get("connects", 0),
            "session_disconnects": self._metrics.get("disconnects", 0),
        }
        try:
            opts = await self.instance.gbx("GetServerOptions")
            if isinstance(opts, dict):
                info["name"] = (opts.get("Name") or "?")[:60]
                info["max_players"] = int(opts.get("CurrentMaxPlayers", 0) or 0)
                info["max_specs"] = int(opts.get("CurrentMaxSpectators", 0) or 0)
        except Exception:
            logger.exception("system: GetServerOptions failed")
        try:
            online = list(self.instance.player_manager.online)
            specs = sum(1 for p in online if getattr(p, "flow", None) and getattr(p.flow, "is_spectator", False))
            info["players"] = len(online) - specs
            info["specs"] = specs
        except Exception:
            pass
        try:
            cm = self.instance.map_manager.current_map
            if cm is not None:
                info["current_map"] = str(getattr(cm, "name", "?"))[:60]
                info["map_author"] = str(getattr(cm, "author_login", "?"))
        except Exception:
            pass
        try:
            info["playlist_count"] = len(list(self.instance.map_manager.maps))
        except Exception:
            pass
        try:
            mode = await self.instance.mode_manager.get_current_full_script()
            info["mode"] = str(mode)[:60]
        except Exception:
            pass
        try:
            info["title"] = str(getattr(self.instance.game, "dedicated_title", "?"))
        except Exception:
            pass
        return info

    # ----- pyplanet --------------------------------------------------

    def _pyplanet_block(self) -> dict[str, Any]:
        ver = "?"
        try:
            import pyplanet as _pp
            ver = getattr(_pp, "__version__", "?")
        except Exception:
            pass
        pool = getattr(self.instance, "process_name", "?")
        try:
            apps_loaded = len(getattr(self.instance.apps, "apps", {}))
        except Exception:
            apps_loaded = 0
        pyp_log = PYPL_ROOT / "pools" / pool / "logs" / "tmsm.log"
        log_size = _human_bytes(pyp_log.stat().st_size) if pyp_log.is_file() else "?"
        return {
            "pyp_version": ver,
            "pyp_pool": pool,
            "pyp_apps_loaded": apps_loaded,
            "pyp_log_size": log_size,
        }

    # ----- players (db) ----------------------------------------------

    async def _players_block(self) -> dict[str, Any]:
        out = {
            "db_total": 0, "db_admins": 0, "db_operators": 0,
            "online_total": 0, "online_admins": 0,
            "recent_nick": "—",
        }
        try:
            online = list(self.instance.player_manager.online)
            out["online_total"] = len(online)
            out["online_admins"] = sum(1 for p in online if int(getattr(p, "level", 0)) >= 2)
        except Exception:
            pass
        try:
            from pyplanet.apps.core.maniaplanet.models import Player
            from peewee import fn
            total_q = Player.select(fn.COUNT(Player.id).alias("c"))
            admin_q = Player.select(fn.COUNT(Player.id).alias("c")).where(Player.level >= 2)
            oper_q = Player.select(fn.COUNT(Player.id).alias("c")).where(Player.level >= 1)
            recent_q = Player.select().order_by(Player.last_seen.desc()).limit(1)
            out["db_total"] = list(await self.instance.db.execute(total_q))[0].c
            out["db_admins"] = list(await self.instance.db.execute(admin_q))[0].c
            out["db_operators"] = list(await self.instance.db.execute(oper_q))[0].c
            rec = list(await self.instance.db.execute(recent_q))
            if rec:
                out["recent_nick"] = (getattr(rec[0], "nickname", "") or "—")[:40]
        except Exception:
            logger.exception("system: player db query failed")
        return out

    # ----- top-level context provider --------------------------------

    async def status_context(self, login: str) -> dict[str, Any]:
        st = self._status_st(login)
        tab = st.get("tab", "host")
        ctx: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "active_tab": tab,
            "tabs": list(self.STATUS_TABS),
        }
        # always include host for the header
        ctx.update(self._host_block())
        if tab == "storage":
            ctx.update(self._storage_block())
        elif tab == "server":
            ctx.update(await self._server_block())
        elif tab == "pyplanet":
            ctx.update(self._pyplanet_block())
            ctx.update(self._storage_block())  # for pyplanet log size context
        elif tab == "players":
            ctx.update(await self._players_block())
            ctx.update(self._server_block_safe_chat())
        return ctx

    def _server_block_safe_chat(self) -> dict[str, Any]:
        # Used by the Players tab to show chat-message session counter without
        # needing the full gbx round-trip.
        return {
            "session_chat": self._metrics.get("chat", 0),
            "session_connects": self._metrics.get("connects", 0),
            "session_disconnects": self._metrics.get("disconnects", 0),
        }

    async def _on_status_refresh(self, player, **kwargs) -> None:
        # bust the storage cache so refresh actually re-scans
        self._stat_cache.pop("storage", None)
        await self._open(self.status_view, player)

    # ================================================================
    # Logs Viewer
    # ================================================================

    LINES_PER_PAGE = 36
    MAX_LINES_LOADED = 50_000
    MAX_BYTES_READ = 8 * 1024 * 1024  # tail the last 8 MB of huge files
    VISIBLE_CHARS = 180   # approx chars that fit in the right panel
    HSCROLL_STEP = 40

    def _fresh_logs_state(self) -> dict[str, Any]:
        return {
            "file": "",
            "filter": "",
            "page": 1,
            "hscroll": 0,
            "selected": "",
            "confirm_delete": "",
            "status": "",
            "status_color": "aaa",
        }

    def list_log_files(self) -> list[dict[str, Any]]:
        """Discover log files across all tmsm-managed locations.

        Categories:
          pypl  : PyPlanet pool logs                ~/.tmsm/pyplanet/pools/*/logs/*
          tmsm  : tmsm wrapper logs around server   ~/.tmsm/servers/*/logs/*
          cons  : dedicated Console.*.log           ~/.tmsm/servers/*/UserData/Logs/*
          eng   : dedicated engine boot logs        ~/.tmsm/servers/*/Logs/*
        """
        roots: list[tuple[str, Path, str]] = [
            ("pypl", PYPL_ROOT / "pools", "*/logs/*"),
            ("tmsm", TMSM_ROOT / "servers", "*/logs/*"),
            ("cons", TMSM_ROOT / "servers", "*/UserData/Logs/*"),
            ("eng",  TMSM_ROOT / "servers", "*/Logs/*"),
        ]
        out: list[dict[str, Any]] = []
        seen: set[Path] = set()
        for cat, base, pattern in roots:
            if not base.is_dir():
                continue
            for p in base.glob(pattern):
                try:
                    p = p.resolve()
                except OSError:
                    continue
                if not p.is_file() or p in seen:
                    continue
                if p.suffix.lower() not in (".log", ".txt"):
                    continue
                seen.add(p)
                try:
                    stat = p.stat()
                except OSError:
                    continue
                rel = p.relative_to(TMSM_ROOT) if TMSM_ROOT in p.parents else p
                # short label = filename only (column is narrow)
                out.append({
                    "path": str(p),
                    "label": p.name,
                    "full": str(rel),
                    "category": cat,
                    "size": _human_bytes(stat.st_size),
                    "size_bytes": stat.st_size,
                    "mtime": stat.st_mtime,
                })
        # newest first per category
        out.sort(key=lambda r: (-r["mtime"],))
        return out

    def _load_log_lines(self, path: Path, needle: str) -> list[str]:
        if not path or not path.is_file():
            return []
        try:
            size = path.stat().st_size
            with path.open("rb") as f:
                if size > self.MAX_BYTES_READ:
                    f.seek(size - self.MAX_BYTES_READ)
                    f.readline()  # discard partial first line
                blob = f.read()
        except OSError:
            return []
        try:
            text = blob.decode("utf-8", errors="replace")
        except Exception:
            text = blob.decode("latin-1", errors="replace")
        lines = text.splitlines()
        if needle:
            lo = needle.lower()
            lines = [ln for ln in lines if lo in ln.lower()]
        if len(lines) > self.MAX_LINES_LOADED:
            lines = lines[-self.MAX_LINES_LOADED:]
        return lines

    @staticmethod
    def _escape_ml(s: str) -> str:
        """Escape ManiaPlanet $-codes for safe display in a label."""
        return (s.replace("$", "$$")
                 .replace('"', "'")
                 .replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;"))

    def logs_context(self, login: str) -> dict[str, Any]:
        files = self.list_log_files()
        st = self._logs_state.setdefault(login, self._fresh_logs_state())
        active = st.get("file") or (files[0]["path"] if files else "")
        if active and not any(f["path"] == active for f in files):
            # active file disappeared
            active = files[0]["path"] if files else ""
            st["file"] = active
            st["page"] = 1
            st["hscroll"] = 0
        flt = st.get("filter", "")
        all_lines = self._load_log_lines(Path(active), flt) if active else []
        # newest line first → reverse for paging
        rev = list(reversed(all_lines))
        total = max(1, (len(rev) + self.LINES_PER_PAGE - 1) // self.LINES_PER_PAGE)
        page = max(1, min(int(st.get("page", 1) or 1), total))
        st["page"] = page
        start = (page - 1) * self.LINES_PER_PAGE
        page_lines = rev[start:start + self.LINES_PER_PAGE]
        # horizontal scroll: slice each line
        max_len = max((len(ln) for ln in page_lines), default=0)
        hscroll = max(0, int(st.get("hscroll", 0) or 0))
        # don't allow scrolling so far that all lines become empty
        if hscroll > max(0, max_len - 20):
            hscroll = max(0, max_len - 20)
            st["hscroll"] = hscroll
        sliced = [ln[hscroll:hscroll + self.VISIBLE_CHARS] for ln in page_lines]
        return {
            "files": files,
            "active": active,
            "filter": flt,
            "lines": [self._escape_ml(ln) for ln in sliced],
            "_raw_lines": page_lines,  # full lines for click→copy lookup
            "log_path": active,
            "page": page,
            "total_pages": total,
            "line_count": len(rev),
            "hscroll": hscroll,
            "max_line_len": max_len,
            "selected": self._escape_ml(st.get("selected", "")),
            "status": st.get("status", ""),
            "status_color": st.get("status_color", "aaa"),
            "confirm_delete": st.get("confirm_delete", ""),
        }

    def _set_logs_status(self, login: str, msg: str, color: str = "aaa") -> None:
        st = self._logs_state.setdefault(login, self._fresh_logs_state())
        st["status"] = msg
        st["status_color"] = color

    async def _absorb_filter(self, login: str, values: dict | None) -> None:
        if not values or self.logs_view is None:
            return
        key = f"entry_{self.logs_view.id}__filter"
        if key in values:
            st = self._logs_state.setdefault(login, self._fresh_logs_state())
            new = str(values[key] or "")
            if new != st.get("filter", ""):
                st["filter"] = new
                st["page"] = 1

    async def _logs_catch_all(self, player, action, values):
        st = self._logs_state.setdefault(player.login, self._fresh_logs_state())
        login = player.login
        # any interaction other than delete clears the confirm flag
        clear_confirm = not action.startswith("delfile__") and action != "delete"
        if clear_confirm:
            st["confirm_delete"] = ""

        if action.startswith("pick__"):
            try:
                idx = int(action.split("__", 1)[1])
            except (ValueError, IndexError):
                return
            files = self.list_log_files()
            if 0 <= idx < len(files):
                if st.get("file") != files[idx]["path"]:
                    st["file"] = files[idx]["path"]
                    st["page"] = 1
                    self._set_logs_status(login, "opened: " + files[idx]["label"], "8af")
            await self._open(self.logs_view, player)
            return

        if action.startswith("delfile__"):
            try:
                idx = int(action.split("__", 1)[1])
            except (ValueError, IndexError):
                return
            files = self.list_log_files()
            if not (0 <= idx < len(files)):
                return
            path = files[idx]["path"]
            if st.get("confirm_delete") != path:
                st["confirm_delete"] = path
                self._set_logs_status(login, f"click X again to delete {files[idx]['label']}", "fa4")
            else:
                ok, msg = self._delete_log_file(path)
                st["confirm_delete"] = ""
                if ok:
                    if st.get("file") == path:
                        st["file"] = ""
                        st["page"] = 1
                    self._set_logs_status(login, msg, "8f8")
                else:
                    self._set_logs_status(login, msg, "f66")
            await self._open(self.logs_view, player)
            return

        if action.startswith("copyline__"):
            try:
                idx = int(action.split("__", 1)[1])
            except (ValueError, IndexError):
                return
            ctx = self.logs_context(login)
            raw = ctx.get("_raw_lines", [])
            if 0 <= idx < len(raw):
                st["selected"] = raw[idx]
                self._set_logs_status(login, "line copied to buffer (Ctrl+A, Ctrl+C)", "8af")
            await self._open(self.logs_view, player)
            return

        if action.startswith("pg__"):
            sub = action[len("pg__"):]
            ctx_for_total = self.logs_context(login)
            total = ctx_for_total["total_pages"]
            cur = st.get("page", 1)
            if sub == "first":
                st["page"] = 1
            elif sub == "prev":
                st["page"] = max(1, cur - 1)
            elif sub == "next":
                st["page"] = min(total, cur + 1)
            elif sub == "last":
                st["page"] = total
            elif sub.startswith("page__"):
                try:
                    st["page"] = max(1, min(total, int(sub.split("__", 1)[1])))
                except (ValueError, IndexError):
                    pass
            await self._absorb_filter(login, values)
            await self._open(self.logs_view, player)
            return

        if action in ("hs_home", "hs_end", "hs_left", "hs_right"):
            ctx_for_max = self.logs_context(login)
            mx = ctx_for_max["max_line_len"]
            cur = int(st.get("hscroll", 0) or 0)
            if action == "hs_home":
                st["hscroll"] = 0
            elif action == "hs_left":
                st["hscroll"] = max(0, cur - self.HSCROLL_STEP)
            elif action == "hs_right":
                st["hscroll"] = min(max(0, mx - 20), cur + self.HSCROLL_STEP)
            elif action == "hs_end":
                st["hscroll"] = max(0, mx - self.VISIBLE_CHARS // 2)
            await self._absorb_filter(login, values)
            await self._open(self.logs_view, player)
            return

    async def _on_logs_refresh(self, player, **kwargs) -> None:
        await self._absorb_filter(player.login, kwargs.get("values"))
        self._set_logs_status(player.login, "refreshed", "8af")
        await self._open(self.logs_view, player)

    async def _on_logs_apply(self, player, **kwargs) -> None:
        await self._absorb_filter(player.login, kwargs.get("values"))
        await self._open(self.logs_view, player)

    async def _on_logs_delete(self, player, **kwargs) -> None:
        """Bottom-of-window 'Delete file' button — deletes the currently open file."""
        st = self._logs_state.setdefault(player.login, self._fresh_logs_state())
        path = st.get("file", "")
        if not path:
            self._set_logs_status(player.login, "no file selected", "f66")
            await self._open(self.logs_view, player)
            return
        if st.get("confirm_delete") != path:
            st["confirm_delete"] = path
            self._set_logs_status(player.login, "click Delete again to confirm", "fa4")
        else:
            ok, msg = self._delete_log_file(path)
            st["confirm_delete"] = ""
            if ok:
                st["file"] = ""
                st["page"] = 1
                self._set_logs_status(player.login, msg, "8f8")
            else:
                self._set_logs_status(player.login, msg, "f66")
        await self._open(self.logs_view, player)

    def _delete_log_file(self, path: str) -> tuple[bool, str]:
        p = Path(path)
        # Safety: only allow deletion under TMSM_ROOT
        try:
            p = p.resolve()
            p.relative_to(TMSM_ROOT.resolve())
        except (OSError, ValueError):
            return False, "refused: path outside tmsm root"
        if not p.is_file():
            return False, "not a file"
        try:
            p.unlink()
            logger.info("system/logs: deleted %s", p)
            return True, f"deleted {p.name}"
        except OSError as e:
            logger.exception("system/logs: delete failed")
            return False, f"delete failed: {e}"

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
