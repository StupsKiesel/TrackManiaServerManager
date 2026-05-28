"""Self-update: `git pull` the source checkout and refresh the venv install."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

import tmsm

Log = Callable[[str], None]


def _repo_root() -> Path | None:
    """Return the git checkout root if tmsm was installed editable from one."""
    pkg = Path(tmsm.__file__).resolve().parent          # .../src/tmsm
    for candidate in (pkg.parents[1], pkg.parents[0]):  # .../  and src/
        if (candidate / ".git").is_dir():
            return candidate
    return None


def _run(cmd: list[str], cwd: Path, log: Log) -> None:
    log(f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd, cwd=str(cwd),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        log(line.rstrip())
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"command failed (exit {rc}): {' '.join(cmd)}")


def update_tmsm(log: Log) -> None:
    repo = _repo_root()
    if repo is None:
        raise RuntimeError(
            "Could not locate the tmsm source checkout (no .git directory found "
            "next to the installed package). Self-update only works when tmsm "
            "was installed in editable mode from a git clone (the default for "
            "install.sh)."
        )
    log(f"Source checkout: {repo}")

    if not (repo / ".git").is_dir():
        raise RuntimeError(f"{repo} is not a git working tree.")

    # Refuse to pull over uncommitted local changes — would silently lose work.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo),
        capture_output=True, text=True, check=False,
    )
    if status.stdout.strip():
        raise RuntimeError(
            "Local uncommitted changes in the source checkout:\n"
            f"{status.stdout}"
            "Commit, stash, or discard them before updating."
        )

    _run(["git", "fetch", "--prune"], repo, log)
    _run(["git", "pull", "--ff-only"], repo, log)

    # Refresh editable install so dependency changes in pyproject.toml take effect.
    pip = [sys.executable, "-m", "pip", "install", "--upgrade", "-e", str(repo)]
    env = os.environ.copy()
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    log("")
    log("Refreshing venv install...")
    proc = subprocess.Popen(
        pip, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=env,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        log(line.rstrip())
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"pip install failed (exit {rc})")

    log("")
    log("Update complete. Quit and relaunch tmsm to load the new code.")
