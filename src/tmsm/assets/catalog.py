"""Catalog of installable PyPlanet addons.

Two sources:
  * Bundled (`pyplanet_apps/<name>/`) discovered from the tmsm package.
  * Community (`catalog.json`) — a curated list of GitHub repositories.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from importlib.resources import files
from pathlib import Path
from typing import Optional


class AddonSource(str, Enum):
    BUNDLED = "bundled"      # ships inside the tmsm package
    COMMUNITY = "community"  # cloned from GitHub on demand


@dataclass
class Addon:
    name: str                       # canonical identifier (python-safe)
    source: AddonSource
    description: str = ""
    author: str = ""
    # Community-only:
    repo: str = ""                  # https://github.com/<owner>/<repo>
    ref: str = "main"               # branch or tag
    install_name: Optional[str] = None  # override directory name when installing
    subpath: Optional[str] = None       # override auto-detection of app dir in repo
    multi: bool = False             # repo contains several apps
    notes: str = ""
    # Bundled-only:
    bundled_path: Optional[Path] = None  # filesystem path to the addon dir

    @property
    def python_namespace(self) -> str:
        """`tmsm` for bundled, `contrib` for community."""
        return "tmsm" if self.source is AddonSource.BUNDLED else "contrib"

    def module_name(self, dir_name: str | None = None) -> str:
        """Full python import path used in PyPlanet's APPS list."""
        leaf = dir_name or self.install_name or self.name
        return f"pyplanet.apps.{self.python_namespace}.{leaf}"


def _catalog_file() -> Path:
    return Path(str(files("tmsm.assets").joinpath("catalog.json")))


def list_catalog() -> list[Addon]:
    """Community addons from catalog.json."""
    path = _catalog_file()
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[Addon] = []
    for entry in data.get("addons", []):
        out.append(Addon(
            name=entry["name"],
            source=AddonSource.COMMUNITY,
            description=entry.get("description", ""),
            author=entry.get("author", ""),
            repo=entry["repo"],
            ref=entry.get("ref", "main"),
            install_name=entry.get("install_name"),
            subpath=entry.get("subpath"),
            multi=bool(entry.get("multi", False)),
            notes=entry.get("notes", ""),
        ))
    return out


def list_bundled() -> list[Addon]:
    """Bundled (tmsm-shipped) addons under `pyplanet_apps/`."""
    root = Path(str(files("tmsm.assets").joinpath("pyplanet_apps")))
    if not root.is_dir():
        return []
    out: list[Addon] = []
    for sub in sorted(root.iterdir()):
        if not sub.is_dir() or sub.name.startswith((".", "_")):
            continue
        if not (sub / "__init__.py").is_file():
            continue
        meta_file = sub / "tmsm-addon.json"
        meta: dict = {}
        if meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        out.append(Addon(
            name=sub.name,
            source=AddonSource.BUNDLED,
            description=meta.get("description", ""),
            author=meta.get("author", "tmsm"),
            bundled_path=sub,
        ))
    return out
