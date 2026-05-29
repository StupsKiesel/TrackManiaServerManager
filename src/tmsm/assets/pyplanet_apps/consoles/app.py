"""tmsm consoles — two master-only hub apps.

* GBX console: send any dedicated-server XML-RPC method, see the response.
  Log tail = the dedicated server's stdout (from `~/.tmsm/servers/*/logs/tmsm.log`).
* PyPlanet console: run any /chat command as the calling player, see the
  response in chat. Log tail = the pool's PyPlanet log (`logs/tmsm.log`).
"""
from __future__ import annotations

import logging
import os
import shlex
import time
from pathlib import Path
from typing import Any

from pyplanet.apps.config import AppConfig

from .views import GbxConsoleView, PypConsoleView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)


def _coerce(token: str) -> Any:
    """Best-effort parse of a CLI token into int/float/bool/str."""
    lo = token.lower()
    if lo in ("true", "yes"):
        return True
    if lo in ("false", "no"):
        return False
    try:
        if token.startswith(("-", "+")) or token[:1].isdigit():
            if "." in token:
                return float(token)
            return int(token)
    except (ValueError, IndexError):
        pass
    return token


def _tail(path: Path, n: int = 200) -> list[str]:
    if not path or not path.is_file():
        return []
    try:
        with path.open("rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                # 32 KiB is plenty for ~200 short log lines
                back = min(size, 32 * 1024)
                f.seek(size - back, os.SEEK_SET)
                blob = f.read()
            except OSError:
                blob = path.read_bytes()
        text = blob.decode("utf-8", errors="replace")
        return text.splitlines()[-n:]
    except Exception:
        logger.exception("consoles: tail failed for %s", path)
        return []


def _find_dedicated_log() -> Path | None:
    """Scan ~/.tmsm/servers/* for the first available logs/tmsm.log."""
    base = Path.home() / ".tmsm" / "servers"
    if not base.is_dir():
        return None
    for sub in sorted(base.iterdir()):
        candidate = sub / "logs" / "tmsm.log"
        if candidate.is_file():
            return candidate
    return None


def _find_pyplanet_log() -> Path | None:
    base = Path.home() / ".tmsm" / "pyplanet" / "pools"
    if not base.is_dir():
        return None
    for sub in sorted(base.iterdir()):
        candidate = sub / "logs" / "tmsm.log"
        if candidate.is_file():
            return candidate
    return None


class ConsolesApp(AppConfig):
    name = "pyplanet.apps.tmsm.consoles"
    label = "tmsm_consoles"
    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_hub"]
    game_dependencies = ["trackmania", "trackmania_next"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gbx_view: GbxConsoleView | None = None
        self.pyp_view: PypConsoleView | None = None
        # per-player command history (most-recent last) + last-status line
        self._cmd_log: dict[str, dict[str, list[str]]] = {}  # login -> {gbx: [...], pyp: [...]}
        self._status: dict[str, dict[str, tuple[str, str]]] = {}  # login -> {kind: (text, color)}
        self._input: dict[str, dict[str, str]] = {}  # login -> {kind: last_input}
        self._gbx_log: Path | None = _find_dedicated_log()
        self._pyp_log: Path | None = _find_pyplanet_log()

    async def on_start(self) -> None:
        logger.info("consoles: gbx_log=%s pyp_log=%s", self._gbx_log, self._pyp_log)

        self.gbx_view = GbxConsoleView(self)
        self.pyp_view = PypConsoleView(self)

        for view in (self.gbx_view, self.pyp_view):
            view.connect("send", self._on_send)
            view.connect("clear", self._on_clear)
            view.connect("refresh", self._on_refresh)
            view.connect("back", self._on_back)

        await self._register_with_hub()

    async def on_stop(self) -> None:
        for view in (self.gbx_view, self.pyp_view):
            if view is not None:
                try:
                    await view.destroy()
                except Exception:
                    logger.exception("consoles: destroy failed")

    # ---- hub registration ---------------------------------------------

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("consoles: tmsm_hub:register signal not registered yet")
            return
        await sig.send_robust({
            "entry": HubAppEntry(
                key="gbx_console",
                name="GBX Console",
                icon="terminal",
                role=Role.MASTER,
                description="Send dedicated-server XML-RPC methods",
                open=self._open_gbx,
                order=10,
            ),
        }, raw=True)
        await sig.send_robust({
            "entry": HubAppEntry(
                key="pyp_console",
                name="PyPlanet Console",
                icon="terminal",
                role=Role.MASTER,
                description="Run chat commands and tail the PyPlanet log",
                open=self._open_pyp,
                order=11,
            ),
        }, raw=True)

    async def _open_gbx(self, player) -> None:
        await self._open(self.gbx_view, "gbx", player)

    async def _open_pyp(self, player) -> None:
        await self._open(self.pyp_view, "pyp", player)

    async def _open(self, view, kind: str, player) -> None:
        if view is None:
            return
        self._cmd_log.setdefault(player.login, {}).setdefault(kind, [])
        try:
            await view.display(player_logins=[player.login])
        except Exception:
            logger.exception("consoles: open(%s) display failed", kind)

    # ---- per-player context ------------------------------------------

    def build_console_context(self, kind: str, login: str) -> dict[str, Any]:
        log_path = self._gbx_log if kind == "gbx" else self._pyp_log
        lines = _tail(log_path, n=200) if log_path else []
        # interleave: append per-player command history at the end so it shows
        # most-recently below the tailed file content.
        history = self._cmd_log.get(login, {}).get(kind, [])
        if history:
            lines = lines + [""] + history
        status_text, status_color = self._status.get(login, {}).get(kind, ("", "aaa"))
        return {
            "lines": lines,
            "input_value": self._input.get(login, {}).get(kind, ""),
            "last_status": status_text,
            "last_status_color": status_color,
            "log_path": str(log_path) if log_path else "",
        }

    def _set_status(self, login: str, kind: str, text: str, color: str = "aaa") -> None:
        self._status.setdefault(login, {})[kind] = (text, color)

    def _append_history(self, login: str, kind: str, line: str) -> None:
        buf = self._cmd_log.setdefault(login, {}).setdefault(kind, [])
        buf.append(line)
        # keep memory bounded
        if len(buf) > 200:
            del buf[: len(buf) - 200]

    def _kind_for(self, view) -> str:
        return "gbx" if view is self.gbx_view else "pyp"

    def _view_for(self, kind: str):
        return self.gbx_view if kind == "gbx" else self.pyp_view

    # ---- handlers ----------------------------------------------------

    async def _on_send(self, player, values=None, **kwargs) -> None:
        # we don't know which view fired without the action prefix; both
        # views share handlers, so we have to disambiguate from `values`
        # keys. The entry's name is `entry_<view_id>__cmd`.
        kind, text = self._extract_input(values)
        if kind is None:
            logger.warning("consoles: _on_send: no recognisable input in values")
            return
        text = (text or "").strip()
        self._input.setdefault(player.login, {})[kind] = text
        if not text:
            self._set_status(player.login, kind, "(empty)", "888")
            await self._refresh_view(kind, player)
            return
        ts = time.strftime("%H:%M:%S")
        self._append_history(player.login, kind, f"$ [{ts}] {'$' if kind == 'gbx' else '>'} {text}")
        try:
            if kind == "gbx":
                await self._run_gbx(player, text)
            else:
                await self._run_pyp(player, text)
        except Exception as e:
            self._append_history(player.login, kind, f"[err] {type(e).__name__}: {e}")
            self._set_status(player.login, kind, "error", "f44")
            logger.exception("consoles: send failed (%s) text=%r", kind, text)
        # clear input on success — keep last input on error
        if self._status.get(player.login, {}).get(kind, ("", ""))[1] != "f44":
            self._input.setdefault(player.login, {})[kind] = ""
        await self._refresh_view(kind, player)

    async def _on_clear(self, player, **kwargs) -> None:
        # Clear history for BOTH views; user can pick which is open anyway.
        for kind in ("gbx", "pyp"):
            self._cmd_log.setdefault(player.login, {})[kind] = []
            self._set_status(player.login, kind, "cleared", "8f8")
            await self._refresh_view(kind, player)

    async def _on_refresh(self, player, **kwargs) -> None:
        for kind in ("gbx", "pyp"):
            await self._refresh_view(kind, player)

    async def _on_back(self, player, **kwargs) -> None:
        for view in (self.gbx_view, self.pyp_view):
            if view is None:
                continue
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(view, player_logins=[player.login])
            except Exception:
                logger.exception("consoles: hide failed")
        # ask the hub to re-show
        try:
            sig = self.context.signals.get_signal("tmsm_hub:show")
            await sig.send_robust({"player": player}, raw=True)
        except KeyError:
            pass

    async def _refresh_view(self, kind: str, player) -> None:
        view = self._view_for(kind)
        if view is None:
            return
        try:
            await view.display(player_logins=[player.login])
        except Exception:
            logger.exception("consoles: refresh display failed (%s)", kind)

    # ---- input plumbing ----------------------------------------------

    def _extract_input(self, values: dict[str, Any] | None) -> tuple[str | None, str]:
        if not values:
            return None, ""
        # entry name = entry_<view_id>__cmd
        for key, val in values.items():
            if not key.startswith("entry_") or not key.endswith("__cmd"):
                continue
            if self.gbx_view is not None and self.gbx_view.id in key:
                return "gbx", str(val)
            if self.pyp_view is not None and self.pyp_view.id in key:
                return "pyp", str(val)
        return None, ""

    # ---- runners -----------------------------------------------------

    async def _run_gbx(self, player, text: str) -> None:
        try:
            parts = shlex.split(text)
        except ValueError as e:
            self._append_history(player.login, "gbx", f"[err] parse: {e}")
            self._set_status(player.login, "gbx", "parse error", "f44")
            return
        if not parts:
            return
        method, raw_args = parts[0], parts[1:]
        args = [_coerce(a) for a in raw_args]
        try:
            resp = await self.instance.gbx(method, *args)
        except Exception as e:
            self._append_history(player.login, "gbx", f"[err] {type(e).__name__}: {e}")
            self._set_status(player.login, "gbx", "rpc error", "f44")
            return
        out = repr(resp) if resp is not None else "OK"
        # keep long responses readable
        for chunk in self._chunk(out, 180):
            self._append_history(player.login, "gbx", chunk)
        self._set_status(player.login, "gbx", "ok", "0f0")

    async def _run_pyp(self, player, text: str) -> None:
        if not text.startswith("/"):
            text = "/" + text
        try:
            # Internal handler that PyPlanet uses when chat is observed.
            await self.instance.command_manager._on_chat(player, text, True)
        except Exception as e:
            self._append_history(player.login, "pyp", f"[err] {type(e).__name__}: {e}")
            self._set_status(player.login, "pyp", "command error", "f44")
            return
        self._set_status(player.login, "pyp", "dispatched", "0f0")

    @staticmethod
    def _chunk(s: str, n: int) -> list[str]:
        return [s[i : i + n] for i in range(0, len(s), n)] or [""]
