from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

import tomli_w

from .. import paths
from .base import Instance, Kind


class GameType(str, Enum):
    TM2020 = "tm2020"
    MANIAPLANET = "maniaplanet"


def _dedicated_flags(meta: dict, srv_dir: Path) -> list[str]:
    """Assemble the dedicated boot flags (everything after the binary path)
    from a flat instance.toml mapping.

    KEEP IN SYNC WITH the identical copy in
    ``src/tmsm/assets/pyplanet_apps/restart/app.py`` — that PyPlanet app
    lives in a different runtime (WSL) and cannot import this module, so
    the body must be mirrored byte-for-byte. Defaults reproduce the
    historic hardcoded command line.
    """
    flags: list[str] = []
    # /nodaemon keeps the server in the foreground. Without it the
    # dedicated server forks and the parent exits, which tears down our
    # screen session and makes tmsm think the server crashed.
    if meta.get("nodaemon", True):
        flags.append("/nodaemon")
    flags.append(f"/title={meta.get('title', 'Trackmania')}")
    flags.append(f"/dedicated_cfg={meta.get('dedicated_cfg', 'dedicated_cfg.txt')}")
    ms = meta.get("matchsettings", "MatchSettings/example.txt")
    if ms and (srv_dir / "UserData" / "Maps" / ms).is_file():
        flags.append(f"/game_settings={ms}")
    extra = meta.get("extra_args")
    if isinstance(extra, (list, tuple)):
        for item in extra:
            flags.append(str(item))
    return flags


@dataclass
class ServerMeta:
    name: str
    game: GameType
    game_port: int = 2350
    xmlrpc_port: int = 5000
    title: str = "Trackmania"
    linked_pool: str | None = None      # pool name, if any
    binary: str = "TrackmaniaServer"    # relative to root/server/
    # Modular dedicated boot args. Defaults reproduce the historic
    # hardcoded command line, so pre-existing instances behave unchanged.
    nodaemon: bool = True
    dedicated_cfg: str = "dedicated_cfg.txt"
    matchsettings: str = "MatchSettings/example.txt"
    extra_args: list[str] = field(default_factory=list)

    @staticmethod
    def load(root: Path) -> "ServerMeta":
        with (root / "instance.toml").open("rb") as f:
            data = tomllib.load(f)
        data["game"] = GameType(data["game"])
        return ServerMeta(**data)

    def save(self, root: Path) -> None:
        data = self.__dict__.copy()
        data["game"] = self.game.value
        with (root / "instance.toml").open("wb") as f:
            tomli_w.dump({k: v for k, v in data.items() if v is not None}, f)


class GameServerInstance(Instance):
    kind = Kind.SERVER

    def __init__(self, root: Path):
        self.root = root
        self.meta = ServerMeta.load(root)
        self.name = self.meta.name

    # paths
    def server_dir(self) -> Path:
        return self.root / "server"

    def cwd(self) -> Path:
        return self.server_dir()

    def log_file(self) -> Path:
        return self.root / "logs" / "tmsm.log"

    def argv(self) -> list[str]:
        bin_path = self.server_dir() / self.meta.binary
        meta = {
            "title": self.meta.title,
            "nodaemon": self.meta.nodaemon,
            "dedicated_cfg": self.meta.dedicated_cfg,
            "matchsettings": self.meta.matchsettings,
            "extra_args": list(self.meta.extra_args or []),
        }
        return [str(bin_path), *_dedicated_flags(meta, self.server_dir())]

    def xmlrpc_port_str(self) -> str:
        return str(self.meta.xmlrpc_port)

    def account_name(self) -> str:
        import xml.etree.ElementTree as ET
        cfg = self.server_dir() / "UserData" / "Config" / "dedicated_cfg.txt"
        try:
            if cfg.is_file():
                el = ET.parse(cfg).getroot().find("masterserver_account/login")
                if el is not None and el.text and el.text.strip():
                    return el.text.strip()
        except Exception:
            pass
        return "—"

    def detail_rows(self) -> Iterable[tuple[str, str]]:
        st = self.status()
        yield ("Type", "Trackmania 2020" if self.meta.game is GameType.TM2020 else "ManiaPlanet")
        yield ("Path", str(self.root))
        yield ("Title", self.meta.title)
        yield ("Game port", str(self.meta.game_port))
        yield ("XMLRPC port", str(self.meta.xmlrpc_port))
        yield ("Linked pool", self.meta.linked_pool or "—")
        yield ("Status", st.status.value)
        if st.pid:
            yield ("PID", str(st.pid))
        if st.mem_mb is not None:
            yield ("Memory", f"{st.mem_mb:.1f} MB")

    def editable_files(self) -> list[tuple[str, Path]]:
        user_data = self.server_dir() / "UserData"
        cfg_dir = user_data / "Config"
        ms_dir = user_data / "Maps" / "MatchSettings"
        files: list[tuple[str, Path]] = []
        dedicated = cfg_dir / "dedicated_cfg.txt"
        if dedicated.is_file():
            files.append(("Dedicated config (dedicated_cfg.txt)", dedicated))
        # MatchSettings: list every .txt in the folder so users can pick a maplist.
        if ms_dir.is_dir():
            for p in sorted(ms_dir.glob("*.txt")):
                files.append((f"Match settings ({p.name})", p))
        return files

    def extra_log_files(self) -> list[tuple[str, Path]]:
        """Log files the dedicated server writes itself.

        TM2020/ManiaPlanet write into two places:
          * <server>/UserData/Logs/   (Console.*.log + auxiliary logs)
          * <server>/Logs/            (engine boot logs, separate dir)
        Both are scanned; newest first, capped so the picker stays small.
        """
        files: list[tuple[str, Path, float]] = []
        for label_prefix, logs_dir in (
            ("Console log (UserData)", self.server_dir() / "UserData" / "Logs"),
            ("Engine log",             self.server_dir() / "Logs"),
        ):
            if not logs_dir.is_dir():
                continue
            for p in logs_dir.iterdir():
                if not p.is_file():
                    continue
                if p.suffix.lower() not in (".log", ".txt"):
                    continue
                files.append((f"{label_prefix} — {p.name}", p, p.stat().st_mtime))
        files.sort(key=lambda t: t[2], reverse=True)
        return [(label, path) for label, path, _ in files[:20]]
