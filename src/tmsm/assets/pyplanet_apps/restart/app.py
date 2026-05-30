"""tmsm restart - manual + scheduled restarts of PyPlanet and the dedicated server.

Hub tile opens a window with:
  * Three buttons: Restart PyPlanet, Restart Dedicated, Restart Both.
  * A list of scheduled restarts (time/days/target) with enable/delete.
  * A small form to add a new schedule.

Master admins only. State is persisted to ``restart.state.json`` in the pool's
working directory.

PyPlanet restart re-uses the proven dev_reload pattern: spawn a detached bash
respawner that re-creates the ``tmsm-<pool>`` screen session, then call
``os._exit(1)`` to drop the current process. Dedicated restart spawns a
similar respawner that quits ``tmsm-<server>`` and re-launches the dedicated
binary with arguments reconstructed from ``~/.tmsm/servers/<server>/instance.toml``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from pyplanet.apps.config import AppConfig
from pyplanet.apps.core.maniaplanet.models import Player

from .views import RestartScheduleFormView, RestartView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)


TMSM_HOME = Path(os.environ.get("TMSM_HOME") or (Path.home() / ".tmsm"))
SCREENDIR = os.environ.get("SCREENDIR") or str(TMSM_HOME / "screen")
SERVERS_DIR = TMSM_HOME / "servers"
PYPLANET_BIN_CANDIDATES = (
    TMSM_HOME / "pyplanet" / "venv" / "bin" / "pyplanet",
    Path(sys.executable).resolve().parent / "pyplanet",
)

TARGETS = ("pyplanet", "dedicated")
FREQS = ("weekly", "monthly")
DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
ALL_DAYS_MASK = 0b1111111

SCHEDULER_TICK_SECONDS = 20.0
# How long pyplanet waits (in the detached respawner) for the new dedicated
# server's XML-RPC port to accept connections before launching itself. Keeps
# pyplanet from connecting to a still-shutting-down ghost.
DEDI_PORT_WAIT_SECONDS = 60

# File watcher (dev_reload-style auto-reload of pyplanet) settings.
WATCH_POLL_INTERVAL = 2.0
WATCH_SUFFIXES = (".py", ".xml", ".json")
DEFAULT_WATCH_DIR = str(
    TMSM_HOME / "pyplanet" / "src" / "pyplanet" / "apps" / "tmsm"
)


def _default_draft() -> dict:
    return {
        "time": "04:00",
        "target": "pyplanet",
        "freq": "weekly",
        "days": ALL_DAYS_MASK,
        "dom": 1,
        # List of {"min": int, "text": str}. Empty text -> default broadcast.
        "notifs": [
            {"min": 15, "text": "Pyplanet restart"},
        ],
        "watch_dir": DEFAULT_WATCH_DIR,
    }


def _normalize_notifs(raw) -> list[dict]:
    """Normalize a notifications list to ``[{min:int, text:str}, ...]``.

    Accepts the legacy ``[int, ...]`` form too, so existing state files keep
    working. Drops entries with non-positive or non-integer minutes; clamps
    lead time to (0, 24h]. Result is sorted descending by minutes and free of
    duplicates (same minute kept only once — first occurrence wins).
    """
    out: list[dict] = []
    seen: set[int] = set()
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, dict):
            try:
                m = int(item.get("min"))
            except (TypeError, ValueError):
                continue
            txt = str(item.get("text") or "").strip()
        else:
            try:
                m = int(item)
            except (TypeError, ValueError):
                continue
            txt = ""
        if not (0 < m <= 24 * 60):
            continue
        if m in seen:
            continue
        seen.add(m)
        out.append({"min": m, "text": txt})
    out.sort(key=lambda d: d["min"], reverse=True)
    return out


class RestartApp(AppConfig):
    name = "pyplanet.apps.tmsm.restart"
    label = "tmsm_restart"
    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_hub"]
    game_dependencies = ["trackmania", "trackmania_next"]

    LEVEL_MASTER = Player.LEVEL_MASTER

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.view: RestartView | None = None
        self.form_view: RestartScheduleFormView | None = None
        # Per-player draft state for the add-schedule form
        # {login: {"time": "HH:MM", "target": "pyplanet|dedicated|both",
        #          "days": int (bitmask)}}
        self._draft: dict[str, dict] = {}
        # Status line per player: (text, color)
        self._status: dict[str, tuple[str, str]] = {}
        # Persistent schedule list, loaded from disk
        self._schedules: list[dict] = []
        self._scheduler_task: asyncio.Task | None = None
        self._fired_minute_keys: set[str] = set()
        # Dedup keys for pre-fire "Restart in Xmin" warnings.
        self._warned_keys: set[str] = set()
        # File watcher state (auto-restart pyplanet on file change).
        self.watch_active: bool = False
        self.watch_dir: str = DEFAULT_WATCH_DIR
        self._watch_task: asyncio.Task | None = None
        self._watch_triggered: bool = False

    # ---- lifecycle -----------------------------------------------------

    async def on_start(self) -> None:
        self._load_state()
        try:
            self.view = RestartView(self)
            self.view.connect("restart_pp", self._on_restart_pp)
            self.view.connect("restart_dedi", self._on_restart_dedi)
            self.view.connect("open_form", self._on_open_form)
            self.view.connect("toggle_watch", self._on_toggle_watch)
            self.view.connect("save_watch_dir", self._on_save_watch_dir)
            self.view.handle_catch_all = self._catch_all  # type: ignore[assignment]
            self.form_view = RestartScheduleFormView(self)
            self.form_view.connect("add_schedule", self._on_add_schedule)
            self.form_view.connect("cancel_form", self._on_cancel_form)
            self.form_view.connect("_crumb__restart", self._on_crumb_restart)
            self.form_view.handle_catch_all = self._catch_all  # type: ignore[assignment]
        except Exception:
            logger.exception("restart: view init failed")
            return
        await self._register_with_hub()
        self._scheduler_task = asyncio.ensure_future(self._scheduler_loop())
        if self.watch_active:
            self._start_watcher()

    async def on_stop(self) -> None:
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
        self._scheduler_task = None
        self._stop_watcher()
        for v in (self.view, self.form_view):
            if v is None:
                continue
            try:
                await v.destroy()
            except Exception:
                logger.exception("restart: destroy failed")
        self.view = None
        self.form_view = None

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("restart: tmsm_hub:register signal not registered yet")
            return
        entry = HubAppEntry(
            key="restart", name="Restart", icon="power-off", color="f44",
            role=Role.MASTER, order=40,
            description="Restart PyPlanet or the dedicated server, now or on a schedule.",
            open=self._open,
        )
        await sig.send_robust({"entry": entry}, raw=True)

    async def _open(self, player) -> None:
        if self.view is None:
            return
        try:
            await self.view.display(player_logins=[player.login])
            # Mark visible so subsequent refresh() (which routes through show())
            # actually re-renders for this player.
            self.view._visible = True
        except Exception:
            logger.exception("restart: open display failed")

    # ---- view context --------------------------------------------------

    async def view_context(self, login: str) -> dict:
        draft = self._draft.setdefault(login, _default_draft())
        # backfill any new keys on previously-cached drafts
        for k, v in _default_draft().items():
            draft.setdefault(k, v)
        # keep the watch_dir draft in sync with persisted value when the
        # user hasn't typed into it yet this session
        draft.setdefault("watch_dir", self.watch_dir)
        status_text, status_color = self._status.get(login, ("", "aaa"))
        rows = []
        for sch in self._schedules:
            notifs = sch.get("notifications") or []
            mins = [n.get("min") if isinstance(n, dict) else n for n in notifs]
            rows.append({
                "id": sch["id"],
                "enabled": bool(sch.get("enabled", True)),
                "time": sch.get("time", "??:??"),
                "when_label": self._when_label(sch),
                "target": sch.get("target", "both"),
                "last_run": sch.get("last_run", ""),
                "notifs_label": ",".join(str(int(m)) for m in mins if m),
            })
        return {
            "schedules": rows,
            "draft_watch_dir": draft["watch_dir"],
            "status": status_text,
            "status_color": status_color,
            "watch_active": bool(self.watch_active),
            "watch_dir": self.watch_dir,
            "watch_dir_exists": Path(self.watch_dir).exists(),
        }

    async def form_context(self, login: str) -> dict:
        draft = self._draft.setdefault(login, _default_draft())
        for k, v in _default_draft().items():
            draft.setdefault(k, v)
        # Normalize legacy CSV string -> list form if needed.
        if not isinstance(draft.get("notifs"), list):
            draft["notifs"] = _default_draft()["notifs"]
        status_text, status_color = self._status.get(login, ("", "aaa"))
        return {
            "draft_time": draft["time"],
            "draft_target": draft["target"],
            "draft_freq": draft["freq"],
            "draft_days": draft["days"],
            "draft_dom": draft["dom"],
            "draft_notifs": list(draft["notifs"]),
            "day_labels": list(DAY_LABELS),
            "freq_options": list(FREQS),
            "target_options": list(TARGETS),
            "status": status_text,
            "status_color": status_color,
        }

    # ---- click handlers ------------------------------------------------

    async def _on_restart_pp(self, player) -> None:
        if not self._is_master(player):
            await self._toast(player, "master required", "f44")
            return
        await self._set_status(player, "restarting PyPlanet...", "fc4")
        await self._broadcast(f"$f80$o[restart]$z PyPlanet restart by ${player.nickname}")
        try:
            self._spawn_pp_respawner()
        except Exception as e:
            logger.exception("restart: pp respawner failed")
            await self._set_status(player, f"failed: {e}", "f44")
            return
        asyncio.ensure_future(self._exit_soon())

    async def _on_restart_dedi(self, player) -> None:
        """Restart the dedicated server. PyPlanet is also restarted (waits for
        the new dedicated's XML-RPC port) so we never reconnect to a ghost."""
        if not self._is_master(player):
            await self._toast(player, "master required", "f44")
            return
        try:
            port = await self._restart_dedicated(reason=f"by ${player.nickname}")
        except Exception as e:
            logger.exception("restart: dedicated restart failed")
            await self._set_status(player, f"dedicated failed: {e}", "f44")
            return
        await self._broadcast(
            f"$f80$o[restart]$z dedicated restarted, PyPlanet restart by ${player.nickname}"
        )
        try:
            self._spawn_pp_respawner(wait_port=port)
        except Exception as e:
            logger.exception("restart: pp respawner failed (with dedi)")
            await self._set_status(player, f"pp failed: {e}", "f44")
            return
        asyncio.ensure_future(self._exit_soon(delay=2.0))

    async def _on_add_schedule(self, player, values=None) -> None:
        if not self._is_master(player):
            await self._toast(player, "master required", "f44")
            return
        self._absorb_draft(player.login, values)
        draft = self._draft.get(player.login, _default_draft())
        t = self._normalize_time(draft.get("time", ""))
        if t is None:
            await self._set_status(player, "invalid time, use HH:MM", "f44")
            return
        target = draft.get("target", "dedicated")
        if target not in TARGETS:
            target = "dedicated"
        freq = draft.get("freq", "weekly")
        if freq not in FREQS:
            freq = "weekly"
        sch = {
            "id": uuid.uuid4().hex[:8],
            "time": t,
            "target": target,
            "freq": freq,
            "enabled": True,
            "last_run": "",
            "notifications": _normalize_notifs(draft.get("notifs", [])),
        }
        if freq == "weekly":
            days = int(draft.get("days", 0)) & ALL_DAYS_MASK
            if days == 0:
                await self._set_status(player, "pick at least one day", "f44")
                return
            sch["days"] = days
        else:  # monthly
            try:
                dom = int(draft.get("dom", 1))
            except (TypeError, ValueError):
                dom = 0
            if not (1 <= dom <= 31):
                await self._set_status(player, "day of month must be 1-31", "f44")
                return
            sch["dom"] = dom
        self._schedules.append(sch)
        self._save_state()
        await self._set_status(player, f"added: {t} ({target}, {freq})", "0f8")
        # close the form, re-open main list
        if self.form_view is not None:
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.form_view, player_logins=[player.login])
            except Exception:
                logger.exception("restart: hide form_view failed")
        await self._open(player)

    async def _on_open_form(self, player) -> None:
        if not self._is_master(player):
            await self._toast(player, "master required", "f44")
            return
        if self.form_view is None:
            return
        # Hide the main restart window first so the two never overlap.
        if self.view is not None:
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.view, player_logins=[player.login])
                self.view._visible = False
            except Exception:
                logger.exception("restart: hide main view failed")
        try:
            await self.form_view.display(player_logins=[player.login])
            self.form_view._visible = True
        except Exception:
            logger.exception("restart: open form failed")

    async def _on_cancel_form(self, player) -> None:
        if self.form_view is not None:
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.form_view, player_logins=[player.login])
            except Exception:
                logger.exception("restart: hide form_view failed")
        # Restore the main restart window.
        await self._open(player)

    async def _on_crumb_restart(self, player, **_) -> None:
        """Breadcrumb back-nav from the form to the main restart view."""
        if self.form_view is not None:
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.form_view, player_logins=[player.login])
            except Exception:
                logger.exception("restart: hide form_view on crumb failed")
        await self._open(player)

    async def _catch_all(self, player, action, values, **kwargs) -> None:
        login = player.login
        # absorb whatever the form pushed
        self._absorb_draft(login, values)

        # delete: del__<id>
        m = re.match(r"^del__(.+)$", action)
        if m:
            if not self._is_master(player):
                await self._toast(player, "master required", "f44")
                return
            sid = m.group(1)
            self._schedules = [s for s in self._schedules if s.get("id") != sid]
            self._save_state()
            await self._set_status(player, "schedule deleted", "0f8")
            if self.view is not None:
                await self.view.refresh()
            return

        # toggle enabled: tog__<id>
        m = re.match(r"^tog__(.+)$", action)
        if m:
            if not self._is_master(player):
                await self._toast(player, "master required", "f44")
                return
            sid = m.group(1)
            for s in self._schedules:
                if s.get("id") == sid:
                    s["enabled"] = not bool(s.get("enabled", True))
                    break
            self._save_state()
            if self.view is not None:
                await self.view.refresh()
            return

        # day checkbox toggle: day__<0..6>
        m = re.match(r"^day__([0-6])$", action)
        if m:
            bit = 1 << int(m.group(1))
            draft = self._draft.setdefault(login, _default_draft())
            draft["days"] = int(draft.get("days", 0)) ^ bit
            if self.form_view is not None:
                await self.form_view.refresh()
            return

        # target select: target__<value>
        m = re.match(r"^target__(pyplanet|dedicated)$", action)
        if m:
            draft = self._draft.setdefault(login, _default_draft())
            draft["target"] = m.group(1)
            if self.form_view is not None:
                await self.form_view.refresh()
            return

        # frequency select: freq__<value>
        m = re.match(r"^freq__(weekly|monthly)$", action)
        if m:
            draft = self._draft.setdefault(login, _default_draft())
            draft["freq"] = m.group(1)
            if self.form_view is not None:
                await self.form_view.refresh()
            return

        # notifications: add/delete rows
        if action == "add_notif":
            draft = self._draft.setdefault(login, _default_draft())
            if not isinstance(draft.get("notifs"), list):
                draft["notifs"] = []
            # Pick a default minute that is not already in the list.
            used = {int(r.get("min", 0)) for r in draft["notifs"]}
            for cand in (5, 10, 15, 30, 60, 1):
                if cand not in used:
                    new_min = cand
                    break
            else:
                new_min = max(used) + 1 if used else 5
            draft["notifs"].append({"min": new_min, "text": ""})
            draft["notifs"].sort(key=lambda d: int(d.get("min", 0)), reverse=True)
            if self.form_view is not None:
                await self.form_view.refresh()
            return

        m = re.match(r"^notif_del__(\d+)$", action)
        if m:
            idx = int(m.group(1))
            draft = self._draft.setdefault(login, _default_draft())
            notifs = draft.get("notifs")
            if isinstance(notifs, list) and 0 <= idx < len(notifs):
                notifs.pop(idx)
            if self.form_view is not None:
                await self.form_view.refresh()
            return

        # close / crumb handled by BaseView defaults; ignore other unknowns
        if action in ("_close",) or action.startswith("_crumb__"):
            return
        logger.debug("restart: unmatched action %s", action)

    # ---- draft absorption ---------------------------------------------

    def _absorb_draft(self, login: str, values) -> None:
        if not values:
            return
        draft = self._draft.setdefault(login, _default_draft())
        if not isinstance(draft.get("notifs"), list):
            draft["notifs"] = []
        view_ids = []
        if self.view is not None:
            view_ids.append(self.view.id)
        if self.form_view is not None:
            view_ids.append(self.form_view.id)
        for vid in view_ids:
            time_key = f"entry_{vid}__time"
            if time_key in values:
                draft["time"] = str(values[time_key] or "").strip()
            dom_key = f"entry_{vid}__dom"
            if dom_key in values:
                raw = str(values[dom_key] or "").strip()
                try:
                    draft["dom"] = max(1, min(31, int(raw)))
                except ValueError:
                    pass
            wd_key = f"entry_{vid}__watch_dir"
            if wd_key in values:
                draft["watch_dir"] = str(values[wd_key] or "").strip()
            # per-row notification inputs: notif_min__<i>, notif_text__<i>
            notifs = draft["notifs"]
            for i, row in enumerate(notifs):
                mkey = f"entry_{vid}__notif_min__{i}"
                tkey = f"entry_{vid}__notif_text__{i}"
                if mkey in values:
                    raw = str(values[mkey] or "").strip()
                    try:
                        row["min"] = max(1, min(24 * 60, int(raw)))
                    except ValueError:
                        pass
                if tkey in values:
                    row["text"] = str(values[tkey] or "").strip()

    # ---- pyplanet restart (dev_reload pattern) ------------------------

    def _spawn_pp_respawner(self, wait_port: int | None = None) -> None:
        """Spawn a detached bash that re-creates the pool's screen session.

        If `wait_port` is given, the respawner first polls 127.0.0.1:<port>
        until it accepts a connection (or DEDI_PORT_WAIT_SECONDS elapses) so
        pyplanet doesn't connect to a still-shutting-down dedicated."""
        pool_root = Path(os.getcwd()).resolve()
        pool_name = pool_root.name
        sess = f"tmsm-{pool_name}"
        log_file = pool_root / "logs" / "tmsm.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        pyplanet_bin = next(
            (p for p in PYPLANET_BIN_CANDIDATES if p.exists()), None
        )
        if pyplanet_bin is None:
            raise RuntimeError(
                "pyplanet binary not found; tried: "
                + ", ".join(str(p) for p in PYPLANET_BIN_CANDIDATES)
            )

        if wait_port:
            # Short head-start (dedi respawner already burned ~2s killing the
            # old screen), then poll the XML-RPC port at 0.25s intervals so we
            # launch pyplanet the moment the dedicated accepts connections.
            wait_block = (
                f'echo "[restart] waiting for dedicated xmlrpc on :{wait_port}"; '
                f'sleep 2; '
                f'for i in $(seq 1 {DEDI_PORT_WAIT_SECONDS * 4}); do '
                f'  (echo > /dev/tcp/127.0.0.1/{wait_port}) >/dev/null 2>&1 && '
                f'    {{ echo "[restart] dedicated ready"; break; }}; '
                f'  sleep 0.25; '
                f'done; '
            )
        else:
            wait_block = ''

        script = (
            f'export SCREENDIR={_q(SCREENDIR)}; '
            f'export PYTHONPATH={_q(str(pool_root))}; '
            f'mkdir -p "$SCREENDIR" && chmod 700 "$SCREENDIR" 2>/dev/null || true; '
            f'screen -S {_q(sess)} -X quit >/dev/null 2>&1 || true; '
            + wait_block +
            f'cd {_q(str(pool_root))} && '
            f'exec screen -dmS {_q(sess)} -L -Logfile {_q(str(log_file))} '
            f'{_q(str(pyplanet_bin))} start --settings=settings'
        )
        self._spawn_detached(script, pool_root, "restart_pp.log")

    async def _exit_soon(self, delay: float = 0.5) -> None:
        await asyncio.sleep(delay)
        logger.warning("restart: exiting pool process now")
        os._exit(1)

    # ---- dedicated restart --------------------------------------------

    async def _restart_dedicated(self, reason: str) -> int:
        """Spawn a detached bash that quits and restarts the dedicated screen
        session. Returns the XML-RPC port the new dedicated will listen on,
        so callers can have pyplanet wait for it before reconnecting."""
        target_server = self._target_server_name()
        if not target_server:
            raise RuntimeError("pool has no target_server in pool.toml")
        srv_root = SERVERS_DIR / target_server
        if not (srv_root / "instance.toml").is_file():
            raise RuntimeError(f"server not found at {srv_root}")
        meta = _parse_toml_simple(srv_root / "instance.toml")
        binary = meta.get("binary", "TrackmaniaServer")
        title = meta.get("title", "Trackmania")
        try:
            xmlrpc_port = int(meta.get("xmlrpc_port", 5000))
        except (TypeError, ValueError):
            xmlrpc_port = 5000
        sess = f"tmsm-{target_server}"
        srv_dir = srv_root / "server"
        log_file = srv_root / "logs" / "tmsm.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        bin_path = srv_dir / binary
        if not bin_path.is_file():
            raise RuntimeError(f"dedicated binary missing: {bin_path}")

        # Reconstruct argv the same way GameServerInstance.argv() does.
        argv = [
            str(bin_path),
            "/nodaemon",
            f"/title={title}",
            "/dedicated_cfg=dedicated_cfg.txt",
        ]
        maplist = srv_dir / "UserData" / "Maps" / "MatchSettings" / "example.txt"
        if maplist.is_file():
            argv.append("/game_settings=MatchSettings/example.txt")
        argv_quoted = " ".join(_q(a) for a in argv)

        script = (
            f'export SCREENDIR={_q(SCREENDIR)}; '
            f'mkdir -p "$SCREENDIR" && chmod 700 "$SCREENDIR" 2>/dev/null || true; '
            f'screen -S {_q(sess)} -X quit >/dev/null 2>&1 || true; '
            f'sleep 1; '
            f'cd {_q(str(srv_dir))} && '
            f'exec screen -dmS {_q(sess)} -L -Logfile {_q(str(log_file))} '
            f'{argv_quoted}'
        )
        await self._broadcast(
            f"$f80$o[restart]$z dedicated restart {reason}"
        )
        self._spawn_detached(script, srv_root, "restart_dedi.log")
        return xmlrpc_port

    def _target_server_name(self) -> str | None:
        pool_toml = Path(os.getcwd()).resolve() / "pool.toml"
        if not pool_toml.is_file():
            return None
        return _parse_toml_simple(pool_toml).get("target_server")

    # ---- detached spawn -----------------------------------------------

    def _spawn_detached(self, script: str, cwd: Path, trace_name: str) -> None:
        env = os.environ.copy()
        env["SCREENDIR"] = SCREENDIR
        trace = cwd / "logs" / trace_name
        trace.parent.mkdir(parents=True, exist_ok=True)
        try:
            trace.write_text(script + "\n")
        except OSError:
            pass
        bash = shutil.which("bash") or "/bin/bash"
        subprocess.Popen(
            [bash, "-c", script],
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=open(trace, "ab", buffering=0),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )

    # ---- scheduler ----------------------------------------------------

    async def _scheduler_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(SCHEDULER_TICK_SECONDS)
                await self._scheduler_tick()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("restart: scheduler crashed")

    async def _scheduler_tick(self) -> None:
        now = datetime.now()
        now_hm = now.strftime("%H:%M")
        weekday = now.weekday()  # mon=0
        wbit = 1 << weekday
        dom = now.day
        minute_key_base = now.strftime("%Y-%m-%d %H:%M")
        if len(self._fired_minute_keys) > 200:
            self._fired_minute_keys.clear()
        if len(self._warned_keys) > 500:
            self._warned_keys.clear()

        for sch in list(self._schedules):
            if not sch.get("enabled", True):
                continue
            # ---- pre-fire warnings -----------------------------------
            await self._maybe_warn(sch, now)
            # ---- actual fire ----------------------------------------
            if sch.get("time") != now_hm:
                continue
            freq = sch.get("freq", "weekly")
            if freq == "monthly":
                if int(sch.get("dom", 0)) != dom:
                    continue
            else:  # weekly (default)
                if not (int(sch.get("days", 0)) & wbit):
                    continue
            fkey = f"{minute_key_base}:{sch.get('id')}"
            if fkey in self._fired_minute_keys:
                continue
            self._fired_minute_keys.add(fkey)
            sch["last_run"] = now.strftime("%Y-%m-%d %H:%M")
            self._save_state()
            await self._fire_scheduled(sch)

    async def _maybe_warn(self, sch: dict, now: datetime) -> None:
        notifs = sch.get("notifications") or []
        if not notifs:
            return
        sched_hm = sch.get("time") or ""
        freq = sch.get("freq", "weekly")
        for entry in notifs:
            if isinstance(entry, dict):
                try:
                    lead_i = int(entry.get("min"))
                except (TypeError, ValueError):
                    continue
                custom_text = str(entry.get("text") or "").strip()
            else:
                try:
                    lead_i = int(entry)
                except (TypeError, ValueError):
                    continue
                custom_text = ""
            if lead_i <= 0:
                continue
            future = now + timedelta(minutes=lead_i)
            if future.strftime("%H:%M") != sched_hm:
                continue
            if freq == "monthly":
                if future.day != int(sch.get("dom", 0)):
                    continue
            else:
                wbit = 1 << future.weekday()
                if not (int(sch.get("days", 0)) & wbit):
                    continue
            wkey = (
                f"{now.strftime('%Y-%m-%d %H:%M')}:{sch.get('id')}:{lead_i}"
            )
            if wkey in self._warned_keys:
                continue
            self._warned_keys.add(wkey)
            target = sch.get("target", "dedicated")
            label = (
                f"{lead_i // 60}h" if lead_i >= 60 and lead_i % 60 == 0
                else (
                    f"{lead_i // 60}h{lead_i % 60}m" if lead_i >= 60
                    else f"{lead_i}min"
                )
            )
            if custom_text:
                msg = f"$f80$o[restart]$z {custom_text}"
            else:
                msg = (
                    f"$f80$o[restart]$z incoming in $fff{label}$z "
                    f"({target} restart)"
                )
            await self._broadcast(msg)

    async def _fire_scheduled(self, sch: dict) -> None:
        target = sch.get("target", "dedicated")
        logger.warning("restart: scheduled fire id=%s target=%s", sch.get("id"), target)
        await self._broadcast(
            f"$f44$o[restart]$z $fff$oRestarting Now$z ($fff{target}$z)"
        )
        wait_port: int | None = None
        if target == "dedicated":
            try:
                wait_port = await self._restart_dedicated(reason="(scheduled)")
            except Exception:
                logger.exception("restart: scheduled dedicated failed")
        # Always restart pyplanet too (whether target was pp or dedicated):
        # if dedicated was restarted, pp waits for the new xmlrpc port.
        try:
            self._spawn_pp_respawner(wait_port=wait_port)
        except Exception:
            logger.exception("restart: scheduled pp respawner failed")
            return
        asyncio.ensure_future(
            self._exit_soon(delay=3.0 if wait_port else 0.5)
        )

    # ---- state ---------------------------------------------------------

    def _state_path(self) -> Path:
        return Path(os.getcwd()).resolve() / "restart.state.json"

    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_path().read_text())
            scheds = data.get("schedules", [])
            if isinstance(scheds, list):
                self._schedules = [s for s in scheds if isinstance(s, dict)]
            self.watch_active = bool(data.get("watch_active", False))
            wd = data.get("watch_dir")
            if isinstance(wd, str) and wd.strip():
                self.watch_dir = wd.strip()
        except (OSError, ValueError):
            self._schedules = []
            self.watch_active = False

    def _save_state(self) -> None:
        try:
            self._state_path().write_text(
                json.dumps({
                    "schedules": self._schedules,
                    "watch_active": self.watch_active,
                    "watch_dir": self.watch_dir,
                }, indent=2)
            )
        except OSError:
            logger.exception("restart: state save failed")

    # ---- file watcher (dev_reload-style) ------------------------------

    async def _on_toggle_watch(self, player, values=None) -> None:
        if not self._is_master(player):
            await self._toast(player, "master required", "f44")
            return
        # absorb any pending watch_dir edit before flipping
        self._absorb_draft(player.login, values)
        draft = self._draft.get(player.login, {})
        new_dir = (draft.get("watch_dir") or self.watch_dir).strip()
        if new_dir:
            self.watch_dir = new_dir
        self.watch_active = not self.watch_active
        self._save_state()
        if self.watch_active:
            self._start_watcher()
            await self._notify(
                f"auto-reload ON - watching {self.watch_dir}",
                severity="success", audience="ops",
            )
        else:
            self._stop_watcher()
            await self._notify(
                "auto-reload OFF", severity="warning", audience="ops",
            )
        # The status text refresh above goes through view.refresh(); also
        # explicitly re-display so the checkbox / line_edit visual updates
        # even if _visible was somehow not set.
        if self.view is not None:
            try:
                await self.view.display(player_logins=[player.login])
            except Exception:
                logger.exception("restart: re-display after toggle failed")

    async def _on_save_watch_dir(self, player, values=None) -> None:
        if not self._is_master(player):
            await self._toast(player, "master required", "f44")
            return
        self._absorb_draft(player.login, values)
        draft = self._draft.get(player.login, {})
        new_dir = (draft.get("watch_dir") or "").strip()
        if not new_dir:
            await self._set_status(player, "watch dir cannot be empty", "f44")
            return
        was_active = self.watch_active
        if was_active:
            self._stop_watcher()
        self.watch_dir = new_dir
        self._save_state()
        if was_active:
            self._start_watcher()
        exists = Path(new_dir).exists()
        await self._notify(
            f"watch dir saved{' (path missing!)' if not exists else ''}: {new_dir}",
            severity=("success" if exists else "warning"), audience="ops",
        )

    def _start_watcher(self) -> None:
        if self._watch_task and not self._watch_task.done():
            return
        self._watch_triggered = False
        self._watch_task = asyncio.ensure_future(self._watch_loop())
        logger.warning("restart: watcher started on %s", self.watch_dir)

    def _stop_watcher(self) -> None:
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
        self._watch_task = None

    async def _watch_loop(self) -> None:
        try:
            snapshot = self._snapshot_mtimes()
            while True:
                await asyncio.sleep(WATCH_POLL_INTERVAL)
                if self._watch_triggered:
                    return
                current = self._snapshot_mtimes()
                if current != snapshot:
                    changed = self._first_diff(snapshot, current)
                    snapshot = current
                    self._watch_triggered = True
                    logger.warning("restart: change detected -> %s", changed)
                    await self._notify(
                        f"dev-reload: {changed} changed - reloading PyPlanet",
                        severity="warning", audience="ops",
                    )
                    try:
                        self._spawn_pp_respawner()
                    except Exception:
                        logger.exception("restart: watcher respawner failed")
                        return
                    asyncio.ensure_future(self._exit_soon())
                    return
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("restart: watcher crashed")

    def _snapshot_mtimes(self) -> dict[str, float]:
        out: dict[str, float] = {}
        root = Path(self.watch_dir)
        if not root.exists():
            return out
        for dirpath, _dirs, files in os.walk(root, followlinks=True):
            for fn in files:
                if not fn.endswith(WATCH_SUFFIXES):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    out[p] = os.stat(p).st_mtime
                except OSError:
                    continue
        return out

    @staticmethod
    def _first_diff(old: dict[str, float], new: dict[str, float]) -> str:
        for p, m in new.items():
            if old.get(p) != m:
                return os.path.basename(p)
        for p in old:
            if p not in new:
                return f"{os.path.basename(p)} (deleted)"
        return "?"

    # ---- helpers -------------------------------------------------------

    @staticmethod
    def _is_master(player) -> bool:
        return int(getattr(player, "level", 0)) >= Player.LEVEL_MASTER

    @staticmethod
    def _normalize_time(s: str) -> "str | None":
        s = (s or "").strip()
        m = re.match(r"^(\d{1,2}):(\d{2})$", s)
        if not m:
            return None
        h, mi = int(m.group(1)), int(m.group(2))
        if not (0 <= h < 24 and 0 <= mi < 60):
            return None
        return f"{h:02d}:{mi:02d}"

    @staticmethod
    def _days_label(mask: int) -> str:
        mask &= ALL_DAYS_MASK
        if mask == ALL_DAYS_MASK:
            return "every day"
        if mask == 0b0011111:
            return "Mon-Fri"
        if mask == 0b1100000:
            return "Sat+Sun"
        return ",".join(DAY_LABELS[i] for i in range(7) if mask & (1 << i)) or "-"

    @classmethod
    def _when_label(cls, sch: dict) -> str:
        freq = sch.get("freq", "weekly")
        if freq == "monthly":
            dom = int(sch.get("dom", 0))
            suffix = "th"
            if dom % 10 == 1 and dom != 11:
                suffix = "st"
            elif dom % 10 == 2 and dom != 12:
                suffix = "nd"
            elif dom % 10 == 3 and dom != 13:
                suffix = "rd"
            return f"{dom}{suffix} of month"
        return cls._days_label(int(sch.get("days", 0)))

    async def _set_status(self, player, text: str, color: str) -> None:
        self._status[player.login] = (text, color)
        if self.view is not None:
            await self.view.refresh()

    async def _broadcast(self, msg: str) -> None:
        try:
            await self.instance.chat(msg)
        except Exception:
            logger.exception("restart: broadcast failed")

    async def _notify(self, message: str, severity: str = "info",
                      audience: str = "global") -> None:
        """Send a toast via the tmsm status-messages widget."""
        try:
            sig = self.context.signals.get_signal("tmsm_status:notify")
            await sig.send_robust({
                "message": message,
                "severity": severity,
                "audience": audience,
                "source": "restart",
            })
        except Exception:
            logger.exception("restart: notify failed")

    async def _toast(self, player, msg: str, color: str = "aaa") -> None:
        self._status[player.login] = (msg, color)
        if self.view is not None:
            try:
                await self.view.refresh()
            except Exception:
                pass


def _q(s: str) -> str:
    """Single-quote a string for safe inclusion in a bash -c script."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


_TOML_KV = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$')


def _parse_toml_simple(path: Path) -> dict:
    """Tiny key=value reader for the flat top-level toml files tmsm writes
    (pool.toml, instance.toml). Strings are unquoted, integers parsed as int.
    Ignores comments and section headers."""
    out: dict = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("["):
            continue
        m = _TOML_KV.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val.startswith('"') and val.endswith('"'):
            out[key] = val[1:-1]
        elif val.startswith("'") and val.endswith("'"):
            out[key] = val[1:-1]
        elif val.lower() in ("true", "false"):
            out[key] = val.lower() == "true"
        else:
            try:
                out[key] = int(val)
            except ValueError:
                out[key] = val
    return out