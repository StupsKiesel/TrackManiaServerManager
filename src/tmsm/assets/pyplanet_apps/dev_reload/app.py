"""dev_reload bundled addon: master-admin button that restarts the pool process.

The button is shown only to players whose `level >= LEVEL_MASTER`. On click we
broadcast a notice, spawn a detached respawner that will re-create the
`tmsm-<pool>` screen session after we exit, and then exit the current pool
process. We don't rely on PyPlanet's god supervisor because `pyplanet start`
runs with `--max-restarts=0`, so god won't bring the pool back on its own.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from pyplanet.apps.config import AppConfig
from pyplanet.apps.core.maniaplanet.models import Player

from .view import ReloadButtonView

logger = logging.getLogger(__name__)

# Where bundled tmsm addons live (symlinked into PyPlanet's apps tree).
TMSM_APPS_DIR = Path.home() / ".tmsm" / "pyplanet" / "src" / "pyplanet" / "apps" / "tmsm"
# How often the watcher polls for file changes (seconds).
WATCH_POLL_INTERVAL = 2.0
# Files matching these suffixes trigger a reload when modified.
WATCH_SUFFIXES = (".py", ".xml", ".json")


class DevReloadApp(AppConfig):
    name = "pyplanet.apps.tmsm.dev_reload"
    app_dependencies = ["core.maniaplanet"]
    game_dependencies = ["trackmania", "trackmania_next"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.button: ReloadButtonView | None = None
        self.watch_active: bool = False
        self._watch_task: asyncio.Task | None = None
        self._watch_triggered: bool = False  # debounce so we don't reload twice

    # ---- lifecycle -----------------------------------------------------

    async def on_start(self) -> None:
        self._load_state()
        try:
            self.button = ReloadButtonView(self)
        except Exception:
            logger.exception("dev_reload: view init failed")
            return

        self.context.signals.listen("maniaplanet:player_connect", self._on_player_connect)

        await self._refresh_display()

        if self.watch_active:
            self._start_watcher()

    async def on_stop(self) -> None:
        self._stop_watcher()
        if self.button is not None:
            try:
                await self.button.destroy()
            except Exception:
                logger.exception("dev_reload: destroy failed")
            self.button = None

    # ---- signals -------------------------------------------------------

    async def _on_player_connect(self, player, **kwargs):
        if self.button is None or not self._is_master(player):
            return
        try:
            await self.button.display(player_logins=[player.login])
        except Exception:
            logger.exception("dev_reload: per-player display failed")

    # ---- click handlers ------------------------------------------------

    async def handle_reload_click(self, player) -> None:
        if not self._is_master(player):
            await self._say(player, "$f00not allowed (master admin only)")
            return
        await self._trigger_reload(reason=f"by ${player.nickname}")

    async def handle_toggle_watch(self, player) -> None:
        if not self._is_master(player):
            await self._say(player, "$f00not allowed (master admin only)")
            return
        self.watch_active = not self.watch_active
        self._save_state()
        if self.watch_active:
            self._start_watcher()
            await self._broadcast(
                f"$0f4$o[dev_reload]$z auto-reload $0f4ON$z — watching {TMSM_APPS_DIR}"
            )
        else:
            self._stop_watcher()
            await self._broadcast("$f80$o[dev_reload]$z auto-reload $f80OFF")
        await self._refresh_display()

    # ---- reload core ---------------------------------------------------

    async def _trigger_reload(self, reason: str) -> None:
        await self._broadcast(f"$f80$o[dev_reload]$z $fffrestart triggered {reason}")
        logger.warning("dev_reload: restart triggered %s", reason)

        try:
            self._spawn_respawner()
        except Exception as e:
            logger.exception("dev_reload: failed to spawn respawner")
            await self._broadcast(
                f"$f00[dev_reload] respawner failed: {type(e).__name__}: {e} — pool will NOT restart"
            )
            return

        async def _exit():
            await asyncio.sleep(0.5)
            logger.warning("dev_reload: exiting pool process now")
            os._exit(1)

        asyncio.ensure_future(_exit())

    # ---- watcher -------------------------------------------------------

    def _start_watcher(self) -> None:
        if self._watch_task and not self._watch_task.done():
            return
        self._watch_triggered = False
        self._watch_task = asyncio.ensure_future(self._watch_loop())
        logger.warning("dev_reload: watcher started on %s", TMSM_APPS_DIR)

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
                    logger.warning("dev_reload: change detected -> %s", changed)
                    await self._trigger_reload(reason=f"by file change ({changed})")
                    return
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("dev_reload: watcher crashed")

    def _snapshot_mtimes(self) -> dict[str, float]:
        """Walk TMSM_APPS_DIR (following symlinks) and return {path: mtime}."""
        out: dict[str, float] = {}
        if not TMSM_APPS_DIR.exists():
            return out
        for root, _dirs, files in os.walk(TMSM_APPS_DIR, followlinks=True):
            for fn in files:
                if not fn.endswith(WATCH_SUFFIXES):
                    continue
                p = os.path.join(root, fn)
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

    # ---- state persistence --------------------------------------------

    def _state_path(self) -> Path:
        return Path(os.getcwd()).resolve() / "dev_reload.state.json"

    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_path().read_text())
            self.watch_active = bool(data.get("watch_active", False))
        except (OSError, ValueError):
            self.watch_active = False

    def _save_state(self) -> None:
        try:
            self._state_path().write_text(json.dumps({"watch_active": self.watch_active}))
        except OSError:
            logger.exception("dev_reload: failed to save state")

    # ---- display helper ------------------------------------------------

    async def _refresh_display(self) -> None:
        if self.button is None:
            return
        try:
            online = list(self.instance.player_manager.online)
        except Exception:
            online = []
        masters = [p.login for p in online if self._is_master(p)]
        if not masters:
            return
        try:
            await self.button.display(player_logins=masters)
        except Exception:
            logger.exception("dev_reload: refresh display failed")

    # ---- respawn helper ------------------------------------------------

    def _spawn_respawner(self) -> None:
        """Detached bash that recreates the screen session after we exit."""
        pool_root = Path(os.getcwd()).resolve()
        pool_name = pool_root.name
        sess = f"tmsm-{pool_name}"
        log_file = pool_root / "logs" / "tmsm.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        # tmsm pools are launched as: `<venv>/bin/pyplanet start --settings=settings`
        # with PYTHONPATH=<pool_root>. Try the known tmsm layout first, then
        # fall back to whatever sits next to the current python interpreter.
        candidates = [
            Path.home() / ".tmsm" / "pyplanet" / "venv" / "bin" / "pyplanet",
            Path(sys.executable).resolve().parent / "pyplanet",
        ]
        pyplanet_bin = next((p for p in candidates if p.exists()), None)
        if pyplanet_bin is None:
            raise RuntimeError(
                f"pyplanet binary not found. sys.executable={sys.executable!r}; "
                "tried: " + ", ".join(str(p) for p in candidates)
            )

        screendir = os.environ.get("SCREENDIR") or str(Path.home() / ".tmsm" / "screen")

        # `start_new_session=True` already detaches; no need for nohup.
        script = (
            f'export SCREENDIR={_q(screendir)}; '
            f'export PYTHONPATH={_q(str(pool_root))}; '
            f'mkdir -p "$SCREENDIR" && chmod 700 "$SCREENDIR" 2>/dev/null || true; '
            f'sleep 2; '
            f'screen -S {_q(sess)} -X quit >/dev/null 2>&1 || true; '
            f'sleep 1; '
            f'cd {_q(str(pool_root))} && '
            f'exec screen -dmS {_q(sess)} -L -Logfile {_q(str(log_file))} '
            f'{_q(str(pyplanet_bin))} start --settings=settings'
        )

        env = os.environ.copy()
        env["SCREENDIR"] = screendir
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{pool_root}{os.pathsep}{existing_pp}" if existing_pp else str(pool_root)
        )

        trace = pool_root / "logs" / "dev_reload.log"
        try:
            trace.write_text(script + "\n")
        except OSError:
            pass

        logger.warning("dev_reload: spawning respawner -> %s", trace)

        bash = _which("bash") or "/bin/bash"
        subprocess.Popen(
            [bash, "-c", script],
            cwd=str(pool_root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=open(trace, "ab", buffering=0),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )

    # ---- helpers -------------------------------------------------------

    @staticmethod
    def _is_master(player) -> bool:
        return getattr(player, "level", 0) >= Player.LEVEL_MASTER

    async def _say(self, player, msg: str) -> None:
        try:
            await self.instance.chat(msg, player)
        except Exception:
            logger.exception("dev_reload: chat send failed")

    async def _broadcast(self, msg: str) -> None:
        try:
            await self.instance.chat(msg)
        except Exception:
            logger.exception("dev_reload: broadcast failed")


def _q(s: str) -> str:
    """Single-quote a string for safe inclusion in a bash -c script."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _which(name: str) -> str | None:
    return shutil.which(name)
