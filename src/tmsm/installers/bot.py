"""Install/delete tmsm-managed Discord bots from a zip file or URL."""
from __future__ import annotations

import os
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from .. import paths
from ..instances.bot import BotMeta

Log = Callable[[str], None]


REQUIRED_FILES = ("run.sh", "run.bat", "requirements.txt")


def _is_url(source: str) -> bool:
    s = source.lower()
    return s.startswith("http://") or s.startswith("https://")


def _download(url: str, dest: Path, log: Log) -> None:
    log(f"Downloading {url}")
    with urllib.request.urlopen(url) as resp, dest.open("wb") as fh:  # nosec - user-supplied URL
        shutil.copyfileobj(resp, fh)
    log(f"Downloaded to {dest} ({dest.stat().st_size} bytes)")


def _extract_zip(zip_path: Path, target: Path, log: Log) -> None:
    """Extract zip into target. If the zip has a single top-level dir, strip it."""
    with zipfile.ZipFile(zip_path) as zf:
        # Normalise backslashes -> forward slashes. PowerShell's
        # Compress-Archive and some other Windows tools produce zips
        # whose member names use "\" as the separator, which is not spec-
        # compliant. Without this, a member named "dir\file.py" would be
        # written as a single file literally called "dir\file.py" on
        # POSIX instead of as dir/file.py inside a "dir" subdirectory.
        names = [
            n.replace("\\", "/")
            for n in zf.namelist()
            if n and not n.startswith("__MACOSX/")
        ]
        # Detect a common top-level directory to strip.
        top_parts = {n.split("/", 1)[0] for n in names}
        strip_prefix: str | None = None
        if len(top_parts) == 1:
            only = next(iter(top_parts))
            if any(n.startswith(only + "/") for n in names) and only + "/" in names:
                strip_prefix = only + "/"
            elif any(n.startswith(only + "/") for n in names) and only not in (
                "run.sh", "run.bat", "requirements.txt"
            ):
                # Folder without an explicit directory entry, e.g. some zip tools.
                strip_prefix = only + "/"
        log(f"Extracting {len(names)} entries"
            + (f" (stripping top-level '{strip_prefix.rstrip('/')}/')" if strip_prefix else ""))
        for member in zf.infolist():
            raw = member.filename
            if not raw or raw.startswith("__MACOSX/"):
                continue
            name = raw.replace("\\", "/")
            rel = name[len(strip_prefix):] if strip_prefix and name.startswith(strip_prefix) else name
            if not rel:
                continue
            out_path = target / rel
            # Treat zip entries as directories either via the explicit
            # is_dir() flag (proper zips) or by a trailing slash (some
            # tools emit only the latter).
            if member.is_dir() or name.endswith("/"):
                out_path.mkdir(parents=True, exist_ok=True)
                continue
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, out_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            # Preserve executable bits if the zip recorded them (unix perms in upper 16 bits).
            mode = (member.external_attr >> 16) & 0o777
            if mode:
                try:
                    out_path.chmod(mode)
                except OSError:
                    pass


def _validate_layout(root: Path) -> None:
    missing = [f for f in REQUIRED_FILES if not (root / f).is_file()]
    if missing:
        raise RuntimeError(
            "Zip is missing required top-level files: " + ", ".join(missing)
            + ". Expected layout: run.sh, run.bat, requirements.txt at the zip's top level."
        )


def _patch_env_file(env_path: Path, updates: dict[str, str]) -> None:
    """Update KEY=value lines in a .env-style file (preserves comments/order). Adds missing keys at the end."""
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    seen: set[str] = set()
    new_lines: list[str] = []
    for ln in lines:
        stripped = ln.lstrip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(ln)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            new_lines.append(ln)
    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def install_bot(name: str, source: str, provision_db: bool, log: Log) -> Path:
    """Install a Discord bot from a local zip path or http(s) URL.

    On `provision_db`, creates a MariaDB database + user and writes DB_* into .env.
    """
    paths.BOTS_DIR.mkdir(parents=True, exist_ok=True)
    root = paths.BOTS_DIR / name
    if root.exists():
        raise FileExistsError(f"Bot '{name}' already exists at {root}")

    with tempfile.TemporaryDirectory(prefix="tmsm-bot-") as td:
        td_path = Path(td)
        if _is_url(source):
            zip_path = td_path / "bot.zip"
            _download(source, zip_path, log)
        else:
            zip_path = Path(source).expanduser()
            if not zip_path.is_file():
                raise FileNotFoundError(f"Zip not found: {zip_path}")

        # Stage extraction so we can validate before committing to the final dir.
        stage = td_path / "stage"
        stage.mkdir()
        _extract_zip(zip_path, stage, log)
        _validate_layout(stage)

        # Ensure run.sh is executable even if the zip didn't preserve perms.
        run_sh = stage / "run.sh"
        try:
            run_sh.chmod(run_sh.stat().st_mode | 0o111)
        except OSError:
            pass

        log(f"Installing bot to {root}")
        shutil.move(str(stage), str(root))

    # Seed .env from .env.example so the user has something to edit.
    env_path = root / ".env"
    env_example = root / ".env.example"
    if not env_path.is_file() and env_example.is_file():
        shutil.copy2(env_example, env_path)
        log("Copied .env.example -> .env")

    db_name = ""
    db_user = ""
    db_password = ""
    if provision_db:
        from . import mariadb as mariadb_installer
        if not mariadb_installer.is_installed():
            log("WARNING: MariaDB is not installed — skipping database provisioning.")
        else:
            safe = name.replace("-", "_")
            db_name = f"bot_{safe}"
            db_user = f"bot_{safe}"[:32]
            log("Provisioning database in MariaDB...")
            db_password = mariadb_installer.provision_database(db_name, db_user, log)
            # Update .env with the new DB creds (uses Rule 11 Bot's naming convention).
            from .. import config as _cfg_mod
            cfg = _cfg_mod.load()
            updates = {
                "DB_HOST": cfg.mariadb.host,
                "DB_PORT": str(cfg.mariadb.port),
                "DB_USER": db_user,
                "DB_PASS": db_password,
                "DB_NAME": db_name,
            }
            _patch_env_file(env_path, updates)
            log(f"Wrote DB_* into {env_path}")

    BotMeta(
        name=name,
        source=source,
        run_script="run.sh",
        db_name=db_name,
        db_user=db_user,
        db_password=db_password,
    ).save(root)
    (root / "logs").mkdir(exist_ok=True)
    log(f"Bot '{name}' installed at {root}")
    return root


def update_bot(name: str, source: str, log: Log) -> Path:
    """Overlay the contents of a new zip on top of an existing bot install.

    Files present in the zip overwrite the corresponding files on disk.
    Files already in the install directory but NOT in the zip are preserved
    (this is how user-managed config like ``.env``, the ``.venv`` virtualenv,
    sqlite fallback files, etc. survive an update).
    """
    root = paths.BOTS_DIR / name
    if not (root / "bot.toml").is_file():
        raise FileNotFoundError(f"Bot '{name}' not found at {root}")

    with tempfile.TemporaryDirectory(prefix="tmsm-bot-update-") as td:
        td_path = Path(td)
        if _is_url(source):
            zip_path = td_path / "bot.zip"
            _download(source, zip_path, log)
        else:
            zip_path = Path(source).expanduser()
            if not zip_path.is_file():
                raise FileNotFoundError(f"Zip not found: {zip_path}")

        stage = td_path / "stage"
        stage.mkdir()
        _extract_zip(zip_path, stage, log)
        _validate_layout(stage)

        overwritten = 0
        added = 0
        skipped: list[str] = []
        log(f"Overlaying zip contents onto {root}")
        for src in sorted(stage.rglob("*")):
            rel = src.relative_to(stage)
            dst = root / rel
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            existed = dst.exists() or dst.is_symlink()
            # Force-remove the destination first so copy2 can't silently
            # follow a symlink, hit a read-only file, or trip over a
            # type mismatch (e.g. file replacing a directory).
            if existed:
                try:
                    if dst.is_dir() and not dst.is_symlink():
                        shutil.rmtree(dst)
                    else:
                        dst.unlink()
                except OSError as e:
                    skipped.append(f"{rel} ({e})")
                    log(f"  SKIP {rel}: could not remove existing entry ({e})")
                    continue
            try:
                shutil.copy2(src, dst)
            except OSError as e:
                skipped.append(f"{rel} ({e})")
                log(f"  SKIP {rel}: copy failed ({e})")
                continue
            if existed:
                overwritten += 1
                log(f"  OVERWRITE {rel}")
            else:
                added += 1
                log(f"  ADD       {rel}")

        # Ensure run.sh is executable even if the zip didn't preserve perms.
        run_sh = root / "run.sh"
        if run_sh.is_file():
            try:
                run_sh.chmod(run_sh.stat().st_mode | 0o111)
            except OSError:
                pass

    log(f"Update done: {overwritten} file(s) overwritten, {added} new file(s); "
        f"untouched files in {root} were kept.")
    if skipped:
        log(f"WARNING: {len(skipped)} file(s) could not be written:")
        for entry in skipped:
            log(f"  - {entry}")
    log("Note: dependencies will be (re)installed by run.sh on next start.")

    # Record where this update came from for the detail pane.
    try:
        meta = BotMeta.load(root)
        meta.source = source
        meta.save(root)
    except Exception as e:
        log(f"WARNING: could not update bot.toml source field: {e}")

    return root


def delete_bot(name: str, log: Log) -> None:
    root = paths.BOTS_DIR / name
    if not root.exists():
        raise FileNotFoundError(f"Bot '{name}' not found at {root}")

    # Best-effort drop of the provisioned database/user.
    try:
        meta = BotMeta.load(root)
    except Exception:
        meta = None
    if meta and meta.db_name and meta.db_user:
        try:
            from . import mariadb as mariadb_installer
            if mariadb_installer.is_installed():
                _drop_bot_database(meta.db_name, meta.db_user, log)
        except Exception as e:
            log(f"WARNING: could not drop database '{meta.db_name}': {e}")

    log(f"Removing {root}")
    shutil.rmtree(root)
    log(f"Bot '{name}' deleted.")


def _drop_bot_database(db_name: str, db_user: str, log: Log) -> None:
    from .. import config as _cfg_mod
    from . import mariadb as mariadb_installer
    from ..instances.service import MariaDBInstance
    from ..supervisor import Status

    cfg = _cfg_mod.load()
    root_pw = mariadb_installer.get_root_password()
    if not root_pw:
        log("MariaDB root password not on file; skipping DB drop.")
        return
    mariadb = MariaDBInstance(cfg)
    if mariadb.status().status != Status.RUNNING:
        log("Starting MariaDB to drop bot database...")
        mariadb.start()
        if not mariadb_installer.wait_until_ready():
            raise RuntimeError("MariaDB did not become ready in time")
    sql = (
        f"DROP DATABASE IF EXISTS `{db_name}`;\n"
        f"DROP USER IF EXISTS '{db_user}'@'{cfg.mariadb.host}';\n"
        f"FLUSH PRIVILEGES;\n"
    )
    mariadb_installer._exec_sql(root_pw, sql, log)  # type: ignore[attr-defined]
    log(f"Dropped database '{db_name}' and user '{db_user}'.")
