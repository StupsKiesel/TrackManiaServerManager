"""Install / update / remove PyPlanet addons.

Community addons:  git clone into ASSETS_CACHE/<name>/, then symlink the
detected app dir into  PYPLANET_SRC/pyplanet/apps/contrib/<install_name>/.

Bundled addons:  symlink the source dir from inside the tmsm package into
PYPLANET_SRC/pyplanet/apps/tmsm/<install_name>/.

After every mutating op we call _sync_all_pools() to update the tmsm-managed
block in every pool's apps.py.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Iterable

from .. import paths
from . import apps_py as apps_py_mod
from .catalog import Addon, AddonSource, list_bundled, list_catalog
from .state import InstalledAddon, State, load_state, save_state

Log = Callable[[str], None]


# -------- paths --------

def _assets_root() -> Path:
    return paths.HOME / "assets"


def _cache_root() -> Path:
    return _assets_root() / "cache"


def _pyplanet_apps_root() -> Path:
    return paths.PYPLANET_SRC / "pyplanet" / "apps"


# -------- helpers --------

def _run(cmd: list[str], log: Log, cwd: Path | None = None) -> None:
    log(f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        log("  " + line.rstrip())
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"command failed (exit {rc}): {' '.join(cmd)}")


def _has_app_config(pkg_dir: Path) -> bool:
    """True if `pkg_dir` looks like a PyPlanet app: it's a package (has
    `__init__.py`) and at least one of its top-level .py files mentions
    AppConfig (the base class apps subclass)."""
    init = pkg_dir / "__init__.py"
    if not init.is_file():
        return False
    try:
        for py in pkg_dir.iterdir():
            if py.is_file() and py.suffix == ".py":
                try:
                    if "AppConfig" in py.read_text(encoding="utf-8", errors="ignore"):
                        return True
                except OSError:
                    continue
    except OSError:
        return False
    return False


_SKIP_DIRS = {"tests", "docs", "examples", "venv", ".venv", "node_modules", "dist", "build"}


def _detect_app_dirs(repo_path: Path, max_depth: int = 4) -> list[Path]:
    """Find candidate PyPlanet app dirs inside a cloned repo.

    Walks up to `max_depth` levels (root = depth 0). Some plugins use a
    reverse-DNS / namespace nesting like `it/thexivn/random_maps_together/`,
    so we descend a few levels. Stops descending a branch as soon as it finds
    an AppConfig (an app inside an app makes no sense).
    """
    if _has_app_config(repo_path):
        return [repo_path]

    found: list[Path] = []

    def walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        for sub in sorted(d.iterdir()):
            if not sub.is_dir() or sub.name.startswith((".", "_")):
                continue
            if sub.name in _SKIP_DIRS:
                continue
            if _has_app_config(sub):
                found.append(sub)
                continue  # don't descend into a confirmed app
            walk(sub, depth + 1)

    walk(repo_path, 1)
    return found


def _current_branch(repo: Path) -> str:
    """Return the current branch name, or '' on detached HEAD / failure."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True, capture_output=True, text=True,
        )
        name = out.stdout.strip()
        return "" if name == "HEAD" else name
    except subprocess.CalledProcessError:
        return ""


def _checkout_ref(repo: Path, ref: str, log: Log) -> None:
    """Best-effort checkout of `ref`. If unavailable, stay on the default branch."""
    current = _current_branch(repo)
    if not ref or ref == current:
        return
    # Fetch the requested ref (works for branches AND tags) before checking out.
    try:
        _run(["git", "-C", str(repo), "fetch", "--depth", "1", "origin", ref], log)
        _run(["git", "-C", str(repo), "checkout", "FETCH_HEAD"], log)
        # Move local branch pointer if a real branch was requested.
        _run(["git", "-C", str(repo), "checkout", "-B", ref, "FETCH_HEAD"], log)
    except RuntimeError as e:
        log(f"  note: ref '{ref}' not available — staying on default branch ({current or 'HEAD'}): {e}")


def _link(src: Path, dst: Path, log: Log) -> None:
    """Symlink src -> dst, with copy as a fallback (Windows / cross-fs)."""
    if dst.exists() or dst.is_symlink():
        if dst.is_symlink():
            dst.unlink()
        else:
            shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(src, dst, target_is_directory=True)
        log(f"  symlink: {dst} -> {src}")
    except OSError as e:
        log(f"  symlink failed ({e}); copying instead")
        shutil.copytree(src, dst)


def _ensure_namespace_init(namespace: str) -> None:
    """Make sure apps/<namespace>/__init__.py exists so PyPlanet can import it."""
    ns_dir = _pyplanet_apps_root() / namespace
    ns_dir.mkdir(parents=True, exist_ok=True)
    init = ns_dir / "__init__.py"
    if not init.is_file():
        init.write_text(
            f'"""{namespace} addon namespace — managed by tmsm."""\n',
            encoding="utf-8",
        )


def _remove_install(addon: InstalledAddon, log: Log) -> None:
    target = _pyplanet_apps_root() / addon.namespace / addon.install_dir
    if target.is_symlink():
        target.unlink()
        log(f"  unlinked {target}")
    elif target.is_dir():
        shutil.rmtree(target)
        log(f"  removed {target}")


def _sync_all_pools(state: State) -> None:
    modules = [a.module_name for a in state.installed.values()]
    pools_root = paths.PYPLANET_POOLS
    if not pools_root.is_dir():
        return
    for pool_dir in pools_root.iterdir():
        apps_py = pool_dir / "settings" / "apps.py"
        if apps_py.is_file():
            apps_py_mod.sync_apps_py(apps_py, modules)


# -------- public API --------

def list_installed() -> list[InstalledAddon]:
    return list(load_state().installed.values())


def install_addon(addon: Addon, log: Log) -> list[InstalledAddon]:
    """Install a bundled or community addon. Returns the records created."""
    if not (paths.PYPLANET_SRC / "pyplanet" / "apps").is_dir():
        raise RuntimeError(
            "PyPlanet source not found — install PyPlanet first "
            f"(expected {paths.PYPLANET_SRC})."
        )
    _cache_root().mkdir(parents=True, exist_ok=True)
    state = load_state()
    created: list[InstalledAddon] = []

    if addon.source is AddonSource.BUNDLED:
        if addon.bundled_path is None or not addon.bundled_path.is_dir():
            raise RuntimeError(f"bundled addon source missing for {addon.name}")
        install_dir = addon.install_name or addon.name
        _ensure_namespace_init("tmsm")
        target = _pyplanet_apps_root() / "tmsm" / install_dir
        log(f"Installing bundled addon '{addon.name}'")
        _link(addon.bundled_path, target, log)
        record = InstalledAddon(
            name=addon.name, source="bundled",
            install_dir=install_dir, namespace="tmsm",
        )
        state.installed[addon.name] = record
        created.append(record)

    else:  # COMMUNITY
        cache_dir = _cache_root() / addon.name
        log(f"Installing community addon '{addon.name}' from {addon.repo}")
        if (cache_dir / ".git").is_dir():
            log("  repo cached — fetching latest")
            try:
                _run(["git", "-C", str(cache_dir), "fetch", "--prune"], log)
            except RuntimeError as e:
                log(f"  fetch failed: {e}")
            _checkout_ref(cache_dir, addon.ref, log)
        else:
            if cache_dir.exists():
                shutil.rmtree(cache_dir)
            # Clone the default branch first — avoids hard failure if the
            # catalog's `ref` is wrong (a very common case across 35 repos
            # that mix main/master/develop). We retarget the ref after.
            _run(["git", "clone", "--depth", "1", addon.repo, str(cache_dir)], log)
            _checkout_ref(cache_dir, addon.ref, log)

        # Determine source dir(s).
        if addon.subpath:
            sources = [cache_dir / addon.subpath]
        else:
            sources = _detect_app_dirs(cache_dir)

        if not sources:
            raise RuntimeError(
                f"could not find a PyPlanet AppConfig inside {cache_dir}. "
                f"Set 'subpath' in catalog.json for '{addon.name}'."
            )
        if not addon.multi and len(sources) > 1:
            raise RuntimeError(
                f"{addon.name}: detected {len(sources)} candidate app dirs "
                f"({[s.name for s in sources]}). Mark this entry 'multi: true' "
                f"or set 'subpath' to pick one."
            )

        _ensure_namespace_init("contrib")
        for src in sources:
            if addon.multi:
                install_dir = src.name
                record_name = f"{addon.name}:{src.name}"
            else:
                install_dir = addon.install_name or addon.name
                record_name = addon.name
            target = _pyplanet_apps_root() / "contrib" / install_dir
            _link(src, target, log)
            record = InstalledAddon(
                name=record_name, source="community",
                install_dir=install_dir, namespace="contrib",
                repo=addon.repo, ref=addon.ref, cache_path=str(cache_dir),
            )
            state.installed[record_name] = record
            created.append(record)

    save_state(state)
    _sync_all_pools(state)
    log(f"Done. Activate by uncommenting the entry in each pool's settings/apps.py.")
    return created


def update_addon(name: str, log: Log) -> None:
    state = load_state()
    record = state.installed.get(name)
    if record is None:
        raise RuntimeError(f"'{name}' is not installed")
    if record.source != "community" or not record.cache_path:
        log(f"'{name}' is bundled — updates ship with tmsm itself.")
        return
    cache_dir = Path(record.cache_path)
    if not (cache_dir / ".git").is_dir():
        raise RuntimeError(f"cache for '{name}' missing at {cache_dir}; reinstall")
    log(f"Updating '{name}' ({record.repo})")
    try:
        _run(["git", "-C", str(cache_dir), "fetch", "--prune"], log)
    except RuntimeError as e:
        log(f"  fetch failed: {e}")
    _checkout_ref(cache_dir, record.ref, log)
    try:
        _run(["git", "-C", str(cache_dir), "pull", "--ff-only"], log)
    except RuntimeError as e:
        log(f"  pull skipped: {e}")
    log("Done.")


def remove_addon(name: str, log: Log) -> None:
    state = load_state()
    record = state.installed.pop(name, None)
    if record is None:
        raise RuntimeError(f"'{name}' is not installed")
    log(f"Removing '{name}'")
    _remove_install(record, log)
    save_state(state)
    # Drop the module from every pool's tmsm-managed block.
    pools_root = paths.PYPLANET_POOLS
    if pools_root.is_dir():
        to_remove = {record.module_name}
        for pool_dir in pools_root.iterdir():
            apps_py = pool_dir / "settings" / "apps.py"
            if apps_py.is_file():
                apps_py_mod.remove_modules(apps_py, to_remove)
    log("Done.")


def sync_pools() -> None:
    """Idempotent — refresh every pool's tmsm-managed block from current state."""
    _sync_all_pools(load_state())
