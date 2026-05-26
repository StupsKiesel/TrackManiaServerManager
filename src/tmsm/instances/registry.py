from __future__ import annotations

from typing import List

from .. import paths
from ..config import Config
from .base import Instance
from .pool import PyPlanetPoolInstance
from .server import GameServerInstance
from .service import MariaDBInstance


def discover_all(cfg: Config) -> List[Instance]:
    """Scan ~/.tmsm/ and return every instance that exists on disk."""
    out: list[Instance] = []

    # Only show the MariaDB row once it's actually installed.
    if (paths.MARIADB_DIST / "bin" / "mysqld").is_file():
        out.append(MariaDBInstance(cfg))

    if paths.SERVERS_DIR.exists():
        for sub in sorted(paths.SERVERS_DIR.iterdir()):
            if (sub / "instance.toml").is_file():
                try:
                    out.append(GameServerInstance(sub))
                except Exception:
                    continue

    if paths.PYPLANET_POOLS.exists():
        for sub in sorted(paths.PYPLANET_POOLS.iterdir()):
            if (sub / "pool.toml").is_file():
                try:
                    out.append(PyPlanetPoolInstance(sub))
                except Exception:
                    continue

    return out
