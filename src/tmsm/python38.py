"""Locate or install Python 3.8.20 for the PyPlanet venv."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

PY38_VERSION = "3.8.20"
PYENV_GIT = "https://github.com/pyenv/pyenv.git"

Log = Callable[[str], None]

# Build dependencies for compiling CPython 3.8 from source via pyenv.
# Reference: https://github.com/pyenv/pyenv/wiki#suggested-build-environment
_APT_BUILD_DEPS = [
    "build-essential", "make", "libssl-dev", "zlib1g-dev", "libbz2-dev",
    "libreadline-dev", "libsqlite3-dev", "wget", "curl", "llvm",
    "xz-utils", "tk-dev", "libxml2-dev",
    "libxmlsec1-dev", "libffi-dev", "liblzma-dev",
]
# ncurses dev headers: name differs across releases. Either satisfies pyenv.
_APT_NCURSES_CANDIDATES = ["libncurses-dev", "libncursesw5-dev"]


def find_python38() -> Path | None:
    # 1. On PATH
    for name in ("python3.8", f"python{PY38_VERSION}"):
        p = shutil.which(name)
        if p:
            return Path(p)
    # 2. pyenv
    pyenv_root = Path(os.environ.get("PYENV_ROOT") or (Path.home() / ".pyenv"))
    candidate = pyenv_root / "versions" / PY38_VERSION / "bin" / "python3.8"
    if candidate.is_file():
        return candidate
    # 3. tmsm-managed pyenv
    from . import paths
    candidate = paths.PYENV_DIR / "versions" / PY38_VERSION / "bin" / "python3.8"
    if candidate.is_file():
        return candidate
    return None


def _run(cmd: list[str], log: Log, env: dict | None = None, cwd: Path | None = None) -> None:
    log(f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        env=env,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        log("  " + line.rstrip())
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"command failed (exit {rc}): {' '.join(cmd)}")


def _ensure_build_deps(log: Log) -> None:
    """Best-effort install of CPython build deps on apt-based systems."""
    if not shutil.which("apt-get"):
        log("Non-apt system: skipping automatic build-dep install. "
            "Ensure CPython build deps are present.")
        return
    def _installed(pkg: str) -> bool:
        rc = subprocess.run(
            ["dpkg-query", "-W", "-f=${Status}", pkg],
            capture_output=True, text=True,
        )
        return "install ok installed" in rc.stdout

    def _apt_has(pkg: str) -> bool:
        rc = subprocess.run(
            ["apt-cache", "policy", pkg], capture_output=True, text=True,
        )
        return "Candidate: " in rc.stdout and "Candidate: (none)" not in rc.stdout

    missing: list[str] = []
    if shutil.which("dpkg-query"):
        for pkg in _APT_BUILD_DEPS:
            if not _installed(pkg):
                missing.append(pkg)
        ncurses_states = {p: _installed(p) for p in _APT_NCURSES_CANDIDATES}
        log(f"ncurses dev candidates: {ncurses_states}")
        if not any(ncurses_states.values()):
            for cand in _APT_NCURSES_CANDIDATES:
                if _apt_has(cand):
                    missing.append(cand)
                    break
    else:
        missing = list(_APT_BUILD_DEPS) + _APT_NCURSES_CANDIDATES[:1]
    log(f"Missing build deps: {missing}")
    if not missing:
        log("CPython build deps already present.")
        return
    # Try non-interactive sudo only; an interactive sudo password prompt
    # would hang invisibly behind the TUI's captured stdout.
    if os.geteuid() != 0:
        if not shutil.which("sudo"):
            raise RuntimeError(
                "Missing CPython build dependencies and no sudo available: "
                + " ".join(missing)
                + "\nInstall them as root, then retry."
            )
        check = subprocess.run(
            ["sudo", "-n", "true"], capture_output=True, text=True,
        )
        if check.returncode != 0:
            raise RuntimeError(
                "Missing CPython build dependencies and sudo requires a password.\n"
                "Run this in a terminal first:\n"
                f"  sudo apt-get install -y {' '.join(missing)}\n"
                "Then retry the PyPlanet install."
            )
        sudo = ["sudo", "-n"]
    else:
        sudo = []
    log(f"Installing CPython build deps via apt: {' '.join(missing)}")
    _run(sudo + ["apt-get", "update", "-qq"], log)
    _run(sudo + ["apt-get", "install", "-y"] + missing, log)


def _ensure_pyenv(log: Log) -> Path:
    """Clone/update tmsm-managed pyenv. Returns PYENV_ROOT path."""
    from . import paths
    pyenv_root = paths.PYENV_DIR
    if (pyenv_root / "bin" / "pyenv").is_file():
        log(f"pyenv already present at {pyenv_root}")
        return pyenv_root
    pyenv_root.parent.mkdir(parents=True, exist_ok=True)
    if pyenv_root.exists():
        # Stale/incomplete dir — wipe before clone.
        log(f"Removing incomplete {pyenv_root}")
        shutil.rmtree(pyenv_root)
    log(f"Cloning pyenv into {pyenv_root}")
    _run(["git", "clone", "--depth", "1", PYENV_GIT, str(pyenv_root)], log)
    return pyenv_root


def _pyenv_env(pyenv_root: Path) -> dict:
    env = os.environ.copy()
    env["PYENV_ROOT"] = str(pyenv_root)
    env["PATH"] = f"{pyenv_root}/bin:{pyenv_root}/shims:" + env.get("PATH", "")
    return env


def ensure_python38(log: Log) -> Path:
    """Locate Python 3.8.20, installing via tmsm-managed pyenv if missing."""
    found = find_python38()
    if found is not None:
        log(f"Found Python {PY38_VERSION} at {found}")
        return found

    log(f"Python {PY38_VERSION} not found — installing via pyenv.")
    _ensure_build_deps(log)
    pyenv_root = _ensure_pyenv(log)
    pyenv = pyenv_root / "bin" / "pyenv"
    env = _pyenv_env(pyenv_root)
    log(f"Building Python {PY38_VERSION} (this can take several minutes)...")
    _run([str(pyenv), "install", "-s", PY38_VERSION], log, env=env)

    found = find_python38()
    if found is None:
        raise RuntimeError(
            f"pyenv install completed but python3.8 still not found under {pyenv_root}"
        )
    log(f"Installed Python {PY38_VERSION} at {found}")
    return found
