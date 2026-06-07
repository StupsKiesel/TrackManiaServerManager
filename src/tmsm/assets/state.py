"""On-disk record of which PyPlanet addons are installed via tmsm."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import paths


@dataclass
class InstalledAddon:
    name: str                   # catalog name
    source: str                 # "bundled" | "community"
    install_dir: str            # directory name under apps/contrib or apps/tmsm
    namespace: str              # "contrib" | "tmsm"
    repo: str = ""              # community only
    ref: str = ""               # community only
    cache_path: str = ""        # community only — where the git clone lives

    @property
    def module_name(self) -> str:
        leaf = str(self.install_dir or "")
        if self.namespace == "tmsm":
            leaf = leaf.lower()
        return f"pyplanet.apps.{self.namespace}.{leaf}"


@dataclass
class State:
    installed: dict[str, InstalledAddon] = field(default_factory=dict)


def _state_file() -> Path:
    return paths.HOME / "assets" / "state.json"


def load_state() -> State:
    path = _state_file()
    if not path.is_file():
        return State()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return State()
    installed = {
        name: InstalledAddon(**entry)
        for name, entry in data.get("installed", {}).items()
        if isinstance(entry, dict)
    }
    return State(installed=installed)


def save_state(state: State) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"installed": {name: asdict(a) for name, a in state.installed.items()}}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
