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

from .views import ConsoleView

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
        self.view: ConsoleView | None = None
        # per-player command history (most-recent last) + last-status line
        self._cmd_log: dict[str, dict[str, list[str]]] = {}  # login -> {gbx: [...], pyp: [...]}
        self._status: dict[str, dict[str, tuple[str, str]]] = {}  # login -> {kind: (text, color)}
        self._input: dict[str, dict[str, str]] = {}  # login -> {kind: last_input}
        self._active: dict[str, str] = {}  # login -> active kind ('gbx'|'pyp')
        self._gbx_log: Path | None = _find_dedicated_log()
        self._pyp_log: Path | None = _find_pyplanet_log()

    async def on_start(self) -> None:
        logger.info("consoles: gbx_log=%s pyp_log=%s", self._gbx_log, self._pyp_log)

        self.view = ConsoleView(self)
        self.view.connect("send", self._on_send)
        self.view.connect("clear", self._on_clear)
        self.view.connect("refresh", self._on_refresh)
        self.view.connect("kind__tab__gbx", self._make_tab_handler("gbx"))
        self.view.connect("kind__tab__pyp", self._make_tab_handler("pyp"))

        await self._register_with_hub()

    async def on_stop(self) -> None:
        if self.view is not None:
            try:
                await self.view.destroy()
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
                key="console",
                name="Console",
                icon="terminal",
                role=Role.MASTER,
                description="XML-RPC + PyPlanet chat command consoles",
                open=self._open,
                order=10,
            ),
        }, raw=True)

    async def _open(self, player) -> None:
        if self.view is None:
            return
        self._cmd_log.setdefault(player.login, {}).setdefault("gbx", [])
        self._cmd_log.setdefault(player.login, {}).setdefault("pyp", [])
        try:
            await self.view.display(player_logins=[player.login])
        except Exception:
            logger.exception("consoles: open display failed")

    def _make_tab_handler(self, kind: str):
        async def _handler(player, **kwargs):
            self._active[player.login] = kind
            await self._refresh(player)
        return _handler

    # ---- per-player context ------------------------------------------

    def build_console_context(self, login: str) -> dict[str, Any]:
        kind = self._active.get(login, "gbx")
        log_path = self._gbx_log if kind == "gbx" else self._pyp_log
        lines = _tail(log_path, n=200) if log_path else []
        history = self._cmd_log.get(login, {}).get(kind, [])
        if history:
            lines = lines + [""] + history
        status_text, status_color = self._status.get(login, {}).get(kind, ("", "aaa"))
        prompt = "$" if kind == "gbx" else ">"
        hint = (
            'MethodName arg1 arg2  \u2014  args parsed as int/bool/str (use "quotes" for spaces)'
            if kind == "gbx"
            else "/help, //admin, /mapinfo, ...  \u2014  runs as you, output appears in chat + log"
        )
        return {
            "active_kind": kind,
            "tabs": [
                {"key": "gbx", "label": "Dedicated"},
                {"key": "pyp", "label": "PyPlanet"},
            ],
            "prompt": prompt,
            "hint": hint,
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

    def _kind_for_player(self, login: str) -> str:
        return self._active.get(login, "gbx")

    # ---- handlers ----------------------------------------------------

    async def _on_send(self, player, values=None, **kwargs) -> None:
        kind = self._kind_for_player(player.login)
        text = self._extract_input(values)
        text = (text or "").strip()
        self._input.setdefault(player.login, {})[kind] = text
        if not text:
            self._set_status(player.login, kind, "(empty)", "888")
            await self._refresh(player)
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
        await self._refresh(player)

    async def _on_clear(self, player, **kwargs) -> None:
        kind = self._kind_for_player(player.login)
        self._cmd_log.setdefault(player.login, {})[kind] = []
        self._set_status(player.login, kind, "cleared", "8f8")
        await self._refresh(player)

    async def _on_refresh(self, player, **kwargs) -> None:
        await self._refresh(player)

    async def _refresh(self, player) -> None:
        if self.view is None:
            return
        try:
            await self.view.display(player_logins=[player.login])
        except Exception:
            logger.exception("consoles: refresh display failed")

    # ---- input plumbing ----------------------------------------------

    def _extract_input(self, values: dict[str, Any] | None) -> str:
        if not values or self.view is None:
            return ""
        key = f"entry_{self.view.id}__cmd"
        return str(values.get(key, "") or "")

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
