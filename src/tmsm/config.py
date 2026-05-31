"""Global tmsm config (~/.tmsm/config.toml)."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

from . import paths


DEFAULT_TM2020_URL = "https://nadeo-download.cdn.ubi.com/trackmania/TrackmaniaServer_Latest.zip"
DEFAULT_MANIAPLANET_URL = "http://files.v04.maniaplanet.com/server/ManiaplanetServer_Latest.zip"
# Pinned MariaDB tarball — adjust to a real, currently-hosted version when wiring downloads.
DEFAULT_MARIADB_URL = "https://archive.mariadb.org/mariadb-10.11.8/bintar-linux-systemd-x86_64/mariadb-10.11.8-linux-systemd-x86_64.tar.gz"
DEFAULT_PYPLANET_GIT = "https://github.com/PyPlanet/PyPlanet.git"
DEFAULT_PYPLANET_REF = "latest-release"   # resolved to newest tag at install
DEFAULT_PYTHON_38 = "3.8.20"


@dataclass
class DownloadsCfg:
    tm2020_url: str = DEFAULT_TM2020_URL
    maniaplanet_url: str = DEFAULT_MANIAPLANET_URL
    mariadb_url: str = DEFAULT_MARIADB_URL
    pyplanet_git: str = DEFAULT_PYPLANET_GIT
    pyplanet_ref: str = DEFAULT_PYPLANET_REF
    python38_version: str = DEFAULT_PYTHON_38


@dataclass
class MariaDBCfg:
    host: str = "127.0.0.1"
    port: int = 3306
    root_password: str = ""   # filled in by first-run wizard


@dataclass
class DBToolCfg:
    command: str = "harlequin"
    # tmsm supports Harlequin and lazysql and builds the connection args per-launch.


@dataclass
class Config:
    downloads: DownloadsCfg = field(default_factory=DownloadsCfg)
    mariadb: MariaDBCfg = field(default_factory=MariaDBCfg)
    db_tool: DBToolCfg = field(default_factory=DBToolCfg)


_LEGACY_DB_TOOLS = {"gobang", "dblab", "lazysql"}
_LEGACY_TM2020_URLS = {
    "http://files.v04.maniaplanet.com/server/TrackmaniaServer_Latest.zip",
    "https://files.v04.maniaplanet.com/server/TrackmaniaServer_Latest.zip",
}


def load() -> Config:
    if not paths.CONFIG_FILE.exists():
        return Config()
    with paths.CONFIG_FILE.open("rb") as f:
        data = tomllib.load(f)
    db_tool_data = dict(data.get("db_tool", {}))
    if db_tool_data.get("command") in _LEGACY_DB_TOOLS:
        # Migrate old defaults/tool names to Harlequin.
        # Users who explicitly want lazysql can still set a full executable path.
        db_tool_data["command"] = "harlequin"
    downloads_data = dict(data.get("downloads", {}))
    if downloads_data.get("tm2020_url") in _LEGACY_TM2020_URLS:
        downloads_data["tm2020_url"] = DEFAULT_TM2020_URL
    return Config(
        downloads=DownloadsCfg(**downloads_data),
        mariadb=MariaDBCfg(**data.get("mariadb", {})),
        db_tool=DBToolCfg(**db_tool_data),
    )


def save(cfg: Config) -> None:
    data = {
        "downloads": cfg.downloads.__dict__,
        "mariadb": cfg.mariadb.__dict__,
        "db_tool": cfg.db_tool.__dict__,
    }
    paths.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = paths.CONFIG_FILE.with_suffix(".toml.tmp")
    with tmp.open("wb") as f:
        tomli_w.dump(data, f)
    tmp.replace(paths.CONFIG_FILE)
    try:
        paths.CONFIG_FILE.chmod(0o600)
    except OSError:
        pass
