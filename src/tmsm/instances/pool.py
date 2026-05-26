from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import tomli_w

from .. import paths
from .base import Instance, Kind


@dataclass
class PoolMeta:
    name: str
    target_server: str | None = None    # server name this pool drives
    db_name: str = ""                   # auto: pyplanet_<name>
    db_user: str = ""
    db_password: str = ""
    settings_module: str = "settings"   # python module path inside pool dir

    @staticmethod
    def load(root: Path) -> "PoolMeta":
        with (root / "pool.toml").open("rb") as f:
            data = tomllib.load(f)
        return PoolMeta(**data)

    def save(self, root: Path) -> None:
        with (root / "pool.toml").open("wb") as f:
            tomli_w.dump({k: v for k, v in self.__dict__.items() if v is not None}, f)


class PyPlanetPoolInstance(Instance):
    kind = Kind.POOL

    def __init__(self, root: Path):
        self.root = root
        self.meta = PoolMeta.load(root)
        self.name = self.meta.name

    def cwd(self) -> Path:
        return self.root

    def log_file(self) -> Path:
        return self.root / "logs" / "tmsm.log"

    def argv(self) -> list[str]:
        # Run PyPlanet via the CLI installed in the shared venv, against this pool's settings.
        pyplanet = paths.PYPLANET_VENV / "bin" / "pyplanet"
        return [str(pyplanet), "start", f"--settings={self.meta.settings_module}"]

    def env(self) -> dict[str, str]:
        # Put the pool dir on PYTHONPATH so `--settings=settings` resolves to its own settings/.
        return {"PYTHONPATH": str(self.root)}

    def detail_rows(self) -> Iterable[tuple[str, str]]:
        st = self.status()
        yield ("Type", "PyPlanet pool")
        yield ("Path", str(self.root))
        yield ("Target server", self.meta.target_server or "—")
        yield ("Database", self.meta.db_name or "—")
        yield ("Status", st.status.value)
        if st.pid:
            yield ("PID", str(st.pid))
        if st.mem_mb is not None:
            yield ("Memory", f"{st.mem_mb:.1f} MB")

    def editable_files(self) -> list[tuple[str, Path]]:
        files: list[tuple[str, Path]] = []
        settings = self.root / "settings"
        for name, label in [
            ("base.py", "Connection & logging (settings/base.py)"),
            ("apps.py", "Apps list (settings/apps.py)"),
            ("local.py", "Local overrides (settings/local.py)"),
        ]:
            p = settings / name
            if p.is_file():
                files.append((label, p))
        return files
