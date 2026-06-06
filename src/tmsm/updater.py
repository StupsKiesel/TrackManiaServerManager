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


def get_uncommitted_changes() -> str:
    """Return `git status --porcelain` output for tracked, content-modified
    files in the source checkout (empty string when clean).

    Mirrors the guard inside :func:`update_tmsm` so the TUI can offer to
    discard them before invoking the update.
    """
    repo = _repo_root()
    if repo is None:
        return ""
    try:
        diff = subprocess.run(
            ["git", "-c", "core.fileMode=false", "diff", "--quiet", "HEAD"],
            cwd=str(repo), check=False,
        )
        if diff.returncode == 0:
            return ""
        info = subprocess.run(
            ["git", "-c", "core.fileMode=false", "status",
             "--porcelain", "--untracked-files=no"],
            cwd=str(repo), capture_output=True, text=True, check=False,
        )
        return info.stdout or ""
    except OSError:
        return ""


def discard_uncommitted_changes(log: Log | None = None) -> None:
    """Hard-reset tracked, content-modified files in the source checkout.

    Untracked files are left alone (consistent with the updater's policy
    of allowing local notes/scripts in the tree)."""
    repo = _repo_root()
    if repo is None:
        raise RuntimeError("No git checkout to reset.")
    cmd = ["git", "-c", "core.fileMode=false", "checkout", "--", "."]
    if log is not None:
        _run(cmd, repo, log)
        return
    r = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(
            f"git checkout failed (exit {r.returncode}): {r.stderr.strip() or r.stdout.strip()}"
        )


def check_update_available(timeout: float = 10.0) -> bool:
    """Return True if the upstream branch has commits we don't have locally.

    Safe to call in a worker thread. Network failures and non-git installs
    return False rather than raising.
    """
    repo = _repo_root()
    if repo is None:
        return False
    try:
        subprocess.run(
            ["git", "fetch", "--quiet", "--prune"],
            cwd=str(repo), check=False,
            capture_output=True, timeout=timeout,
        )
        r = subprocess.run(
            ["git", "rev-list", "--count", "HEAD..@{u}"],
            cwd=str(repo), check=False,
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            return False
        return int(r.stdout.strip() or "0") > 0
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return False


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

    # Refuse to pull over uncommitted *content* changes. We deliberately
    # ignore:
    #   * untracked files — the user may keep local notes/scripts in the tree
    #   * file-mode (chmod +x) differences — common after running install.sh
    #     on filesystems that set the exec bit on first execution
    diff = subprocess.run(
        ["git", "-c", "core.fileMode=false", "diff", "--quiet", "HEAD"],
        cwd=str(repo), check=False,
    )
    if diff.returncode != 0:
        info = subprocess.run(
            ["git", "-c", "core.fileMode=false", "status",
             "--porcelain", "--untracked-files=no"],
            cwd=str(repo), capture_output=True, text=True, check=False,
        )
        raise RuntimeError(
            "Local uncommitted changes in the source checkout:\n"
            f"{info.stdout}"
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
    log("Update complete. Restarting tmsm...")
