"""All filesystem paths tmsm uses, rooted at ~/.tmsm/ (override with $TMSM_HOME)."""
from __future__ import annotations

import os
from pathlib import Path


def _home() -> Path:
    override = os.environ.get("TMSM_HOME")
    return Path(override) if override else Path.home() / ".tmsm"


HOME = _home()
CONFIG_FILE = HOME / "config.toml"
RUN_DIR = HOME / "run"
LOGS_DIR = HOME / "logs"
BACKUPS_DIR = HOME / "backups"

SERVERS_DIR = HOME / "servers"
PYPLANET_DIR = HOME / "pyplanet"
PYPLANET_SRC = PYPLANET_DIR / "src"
PYPLANET_VENV = PYPLANET_DIR / "venv"
PYPLANET_POOLS = PYPLANET_DIR / "pools"

MARIADB_DIR = HOME / "mariadb"
MARIADB_DIST = MARIADB_DIR / "dist"
MARIADB_DATA = MARIADB_DIR / "data"

PYENV_DIR = HOME / "pyenv"


def ensure_home() -> None:
    for p in (
        HOME, RUN_DIR, LOGS_DIR, BACKUPS_DIR,
        SERVERS_DIR, PYPLANET_DIR, PYPLANET_POOLS, MARIADB_DIR,
    ):
        p.mkdir(parents=True, exist_ok=True)


def pid_file(name: str) -> Path:
    return RUN_DIR / f"{name}.pid"
