"""Launch the external DB TUI (lazysql by default) against an instance."""
from __future__ import annotations

import shutil
from urllib.parse import quote

from . import config
from .instances import Instance, Kind


def _conn_for(inst: Instance, cfg: config.Config) -> dict | None:
    """Build a connection dict {user,host,port,password,database} or None."""
    if inst.kind is Kind.SERVICE and inst.name == "mariadb":
        from .installers import mariadb as mariadb_installer
        pw = mariadb_installer.get_root_password()
        if not pw:
            return None
        return {
            "user": "root",
            "host": cfg.mariadb.host,
            "port": cfg.mariadb.port,
            "password": pw,
            "database": "mysql",
        }
    if inst.kind is Kind.POOL:
        meta = inst.meta  # type: ignore[attr-defined]
        if not meta.db_name or not meta.db_user:
            return None
        return {
            "user": meta.db_user,
            "host": cfg.mariadb.host,
            "port": cfg.mariadb.port,
            "password": meta.db_password or "",
            "database": meta.db_name,
        }
    return None


def launch(inst: Instance, app) -> str | None:
    """Run the DB TUI against `inst`, suspending the Textual app while it runs.

    Returns None on success, or an error message to surface in the UI.
    """
    cfg = config.load()
    cmd_name = cfg.db_tool.command or "lazysql"
    exe = shutil.which(cmd_name)
    if not exe:
        return (
            f"`{cmd_name}` not found on PATH.\n"
            "Install it from https://github.com/jorgerojas26/lazysql/releases\n"
            "or set [db_tool].command in ~/.tmsm/config.toml"
        )

    conn = _conn_for(inst, cfg)
    if conn is None:
        if inst.kind is Kind.SERVICE and inst.name == "mariadb":
            return (
                "No MariaDB root password on file.\n"
                "This MariaDB was likely installed before tmsm started saving it.\n"
                "Add it manually to ~/.tmsm/config.toml under [mariadb] root_password,\n"
                "or to ~/.tmsm/mariadb/root.pw (chmod 600), then retry."
            )
        return f"No database connection available for {inst.name}."

    from .instances import registry as _registry
    mariadb_inst = next(
        (i for i in _registry.discover_all(cfg)
         if i.kind is Kind.SERVICE and i.name == "mariadb"),
        None,
    )
    if mariadb_inst is None or not mariadb_inst.is_running:
        return (
            "MariaDB is not running.\n"
            "Start it from the main screen first (select 'mariadb', press Enter, then Start)."
        )

    user_q = quote(conn["user"], safe="")
    pass_q = quote(conn["password"], safe="")
    # lazysql expects a plain URL: mysql://user:pass@host:port/db
    # Do NOT use the Go DSN @tcp(...) form — that's only for the raw driver.
    url = f"mysql://{user_q}:{pass_q}@{conn['host']}:{conn['port']}/{conn['database']}"
    cmd = [exe, url]

    # Run lazysql and capture its output so we can see *why* it exits if it does.
    import subprocess
    from . import paths
    log_path = paths.LOGS_DIR / "dbtool-last.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with app.suspend():
        with open(log_path, "wb") as fh:
            fh.write(f"$ {' '.join(cmd)}\n".encode())
            fh.flush()
            try:
                rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT).returncode
            except FileNotFoundError:
                return f"Failed to launch {cmd_name}."
    try:
        tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-15:])
    except OSError:
        tail = ""
    if rc != 0:
        return (
            f"{cmd_name} exited immediately (rc={rc}).\n"
            f"Output saved to {log_path}\n"
            + (f"Last lines:\n{tail}" if tail else "")
        )
    return None
