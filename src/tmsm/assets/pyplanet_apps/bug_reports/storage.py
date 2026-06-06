"""DB-backed storage for bug reports.

Mirrors the widget_engine storage pattern: peewee model for typed
selects/updates, raw SQL (via `Model.raw(sql)` with `raw.database`
swapped to the live database) for schema bootstrap so peewee_async's
Proxy doesn't reject our queries.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Iterable, Optional

from .models import BugReport

logger = logging.getLogger(__name__)


_COLUMN_DDL: tuple[tuple[str, str], ...] = (
    ("login",              "VARCHAR(64) NOT NULL"),
    ("nickname",           "VARCHAR(255) NULL"),
    ("map_uid",            "VARCHAR(64) NULL"),
    ("map_name",           "VARCHAR(255) NULL"),
    ("mode_script",        "VARCHAR(128) NULL"),
    ("subject",            "VARCHAR(200) NOT NULL"),
    ("details",            "TEXT NULL"),
    ("status",             "VARCHAR(16) NOT NULL DEFAULT 'open'"),
    ("auth_level",         "VARCHAR(16) NULL"),
    ("game_phase",         "VARCHAR(32) NULL"),
    ("about_widgets",      "TINYINT(1) NULL"),
    ("about_ui",           "TINYINT(1) NULL"),
    ("input_device",       "VARCHAR(16) NULL"),
    ("game_version",       "VARCHAR(255) NULL"),
    ("client_version",     "VARCHAR(255) NULL"),
    ("uses_openplanet",    "TINYINT(1) NULL"),
    ("pyplanet_uptime_s",  "INT NULL"),
    ("dedicated_uptime_s", "INT NULL"),
    ("delivered_at",       "DATETIME NULL"),
    ("created_at",         "DATETIME NULL"),
    ("updated_at",         "DATETIME NULL"),
)

# Columns whose width has grown since first release. For existing DBs we
# unconditionally MODIFY them at startup so they match `_COLUMN_DDL`.
# Keep entries here forever (idempotent — MariaDB no-ops a same-width MODIFY).
_WIDEN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("client_version", "VARCHAR(255) NULL"),
    ("game_version",   "VARCHAR(255) NULL"),
    ("map_name",       "VARCHAR(255) NULL"),
    ("nickname",       "VARCHAR(255) NULL"),
)

# Per-column max char length used to defensively truncate values before
# insert, so a TM client returning an extra-verbose version string never
# blows up the whole submit.
_MAX_LEN: dict[str, int] = {
    "login":         64,
    "nickname":      255,
    "map_uid":       64,
    "map_name":      255,
    "mode_script":   128,
    "subject":       200,
    "auth_level":    16,
    "game_phase":    32,
    "input_device":  16,
    "game_version":  255,
    "client_version": 255,
}


def _clip(value: Optional[str], col: str) -> Optional[str]:
    if value is None:
        return None
    s = str(value)
    n = _MAX_LEN.get(col)
    if n is not None and len(s) > n:
        return s[:n]
    return s

STATUS_OPEN = "open"
STATUS_FIXED = "fixed"
STATUS_WONTFIX = "wontfix"
VALID_STATUSES = frozenset({STATUS_OPEN, STATUS_FIXED, STATUS_WONTFIX})


class BugReportStorage:
    def __init__(self, instance):
        self.instance = instance
        self._ready = False

    # ── schema ──────────────────────────────────────────────────────────

    @property
    def _db(self):
        db = getattr(self.instance, "db", None)
        if db is None or not hasattr(db, "objects"):
            return None
        return db

    async def ensure_schema(self) -> None:
        db = self._db
        if db is None:
            return
        existing = await self._existing_columns("tmsm_bug_reports")
        if not existing:
            cols_sql = ", ".join(f"`{n}` {ddl}" for n, ddl in _COLUMN_DDL)
            create_sql = (
                "CREATE TABLE IF NOT EXISTS `tmsm_bug_reports` ("
                "`id` INT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
                f"{cols_sql}"
                ") DEFAULT CHARSET=utf8mb4"
            )
            await self._exec_raw(create_sql)
            logger.info("bug_reports: created table tmsm_bug_reports")
        else:
            for name, ddl in _COLUMN_DDL:
                if name in existing:
                    continue
                await self._exec_raw(
                    f"ALTER TABLE `tmsm_bug_reports` ADD COLUMN `{name}` {ddl}"
                )
                logger.info("bug_reports: added column tmsm_bug_reports.%s", name)
            # Widen any columns whose declared size grew since first release.
            for name, ddl in _WIDEN_COLUMNS:
                if name not in existing:
                    continue  # ADD COLUMN above already used the wide DDL
                try:
                    await self._exec_raw(
                        f"ALTER TABLE `tmsm_bug_reports` MODIFY COLUMN `{name}` {ddl}"
                    )
                except Exception:
                    logger.warning(
                        "bug_reports: widen column %s failed (will rely on Python truncation)",
                        name, exc_info=True,
                    )
        self._ready = True

    async def _existing_columns(self, table: str) -> set[str]:
        db = self._db
        if db is None:
            return set()
        try:
            raw = BugReport.raw(f"SHOW COLUMNS FROM `{table}`")
            raw.database = db.objects.database
            rows = await db.objects.execute(raw)
        except Exception:
            logger.warning(
                "bug_reports: failed to inspect columns for %s", table,
                exc_info=True,
            )
            return set()
        out: set[str] = set()
        for row in rows:
            field = None
            if isinstance(row, dict):
                field = row.get("Field")
            else:
                field = getattr(row, "Field", None)
                if field is None:
                    try:
                        field = row[0]
                    except Exception:
                        field = None
            if field:
                out.add(str(field))
        return out

    async def _exec_raw(self, sql: str) -> None:
        db = self._db
        if db is None:
            return
        raw = BugReport.raw(sql)
        raw.database = db.objects.database
        await db.objects.execute(raw)

    # ── CRUD ────────────────────────────────────────────────────────────

    async def create(
        self, *, login: str, nickname: str, map_uid: Optional[str],
        map_name: Optional[str], mode_script: Optional[str],
        subject: str, details: str,
        auth_level: Optional[str] = None,
        game_phase: Optional[str] = None,
        about_widgets: Optional[bool] = None,
        about_ui: Optional[bool] = None,
        input_device: Optional[str] = None,
        game_version: Optional[str] = None,
        client_version: Optional[str] = None,
        uses_openplanet: Optional[bool] = None,
        pyplanet_uptime_s: Optional[int] = None,
        dedicated_uptime_s: Optional[int] = None,
    ) -> Optional[int]:
        if not self._ready:
            await self.ensure_schema()
        now = _dt.datetime.utcnow()
        try:
            row = await BugReport.objects.create(
                BugReport,
                login=_clip(login, "login"),
                nickname=_clip(nickname, "nickname"),
                map_uid=_clip(map_uid, "map_uid"),
                map_name=_clip(map_name, "map_name"),
                mode_script=_clip(mode_script, "mode_script"),
                subject=_clip(subject, "subject"),
                details=details,
                status=STATUS_OPEN,
                auth_level=_clip(auth_level, "auth_level"),
                game_phase=_clip(game_phase, "game_phase"),
                about_widgets=about_widgets,
                about_ui=about_ui,
                input_device=_clip(input_device, "input_device"),
                game_version=_clip(game_version, "game_version"),
                client_version=_clip(client_version, "client_version"),
                uses_openplanet=uses_openplanet,
                pyplanet_uptime_s=pyplanet_uptime_s,
                dedicated_uptime_s=dedicated_uptime_s,
                created_at=now,
                updated_at=now,
            )
            return int(row.id)
        except Exception:
            logger.exception("bug_reports: create failed (login=%s)", login)
            return None

    async def list_all(self, *, status: Optional[str] = None) -> list[dict[str, Any]]:
        if not self._ready:
            await self.ensure_schema()
        try:
            q = BugReport.select().order_by(BugReport.id.desc())
            if status and status in VALID_STATUSES:
                q = q.where(BugReport.status == status)
            rows = await BugReport.objects.execute(q)
        except Exception:
            logger.exception("bug_reports: list_all failed")
            return []
        return [self._row_to_dict(r) for r in rows]

    async def counts_by_status(self) -> dict[str, int]:
        rows = await self.list_all()
        out: dict[str, int] = {"open": 0, "fixed": 0, "wontfix": 0, "total": len(rows)}
        for r in rows:
            s = str(r.get("status") or "open")
            if s in out:
                out[s] += 1
        return out

    async def get(self, report_id: int) -> Optional[dict[str, Any]]:
        if not self._ready:
            await self.ensure_schema()
        try:
            row = await BugReport.objects.get(BugReport, BugReport.id == int(report_id))
        except Exception:
            return None
        return self._row_to_dict(row)

    async def set_status(self, report_id: int, status: str) -> bool:
        if status not in VALID_STATUSES:
            return False
        if not self._ready:
            await self.ensure_schema()
        now = _dt.datetime.utcnow()
        try:
            await BugReport.objects.execute(
                BugReport.update(status=status, updated_at=now)
                .where(BugReport.id == int(report_id))
            )
            return True
        except Exception:
            logger.exception("bug_reports: set_status failed id=%s", report_id)
            return False

    async def delete(self, report_id: int) -> bool:
        if not self._ready:
            await self.ensure_schema()
        try:
            await BugReport.objects.execute(
                BugReport.delete().where(BugReport.id == int(report_id))
            )
            return True
        except Exception:
            logger.exception("bug_reports: delete failed id=%s", report_id)
            return False

    async def list_pending_delivery(self) -> list[dict[str, Any]]:
        """All reports with `delivered_at IS NULL`, oldest first."""
        if not self._ready:
            await self.ensure_schema()
        try:
            q = (BugReport.select()
                 .where(BugReport.delivered_at.is_null(True))
                 .order_by(BugReport.id.asc()))
            rows = await BugReport.objects.execute(q)
        except Exception:
            logger.exception("bug_reports: list_pending_delivery failed")
            return []
        return [self._row_to_dict(r) for r in rows]

    async def mark_delivered(self, ids: list[int]) -> int:
        if not ids:
            return 0
        if not self._ready:
            await self.ensure_schema()
        now = _dt.datetime.utcnow()
        try:
            await BugReport.objects.execute(
                BugReport.update(delivered_at=now)
                .where(BugReport.id.in_([int(i) for i in ids]))
            )
            return len(ids)
        except Exception:
            logger.exception("bug_reports: mark_delivered failed")
            return 0

    async def delete_delivered(self) -> int:
        """Drop rows that have been delivered (delivered_at IS NOT NULL).
        Used when `store_locally` is False."""
        if not self._ready:
            await self.ensure_schema()
        try:
            rows = await BugReport.objects.execute(
                BugReport.select(BugReport.id)
                .where(BugReport.delivered_at.is_null(False))
            )
            ids = [int(r.id) for r in rows]
            if not ids:
                return 0
            await BugReport.objects.execute(
                BugReport.delete().where(BugReport.id.in_(ids))
            )
            return len(ids)
        except Exception:
            logger.exception("bug_reports: delete_delivered failed")
            return 0

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: BugReport) -> dict[str, Any]:
        return {
            "id":                 int(row.id),
            "login":              row.login or "",
            "nickname":           row.nickname or "",
            "map_uid":            row.map_uid or "",
            "map_name":           row.map_name or "",
            "mode_script":        row.mode_script or "",
            "subject":            row.subject or "",
            "details":            row.details or "",
            "status":             row.status or STATUS_OPEN,
            "auth_level":         getattr(row, "auth_level", None) or "",
            "game_phase":         getattr(row, "game_phase", None) or "",
            "about_widgets":      bool(getattr(row, "about_widgets", False) or False),
            "about_ui":           bool(getattr(row, "about_ui", False) or False),
            "input_device":       getattr(row, "input_device", None) or "",
            "game_version":       getattr(row, "game_version", None) or "",
            "client_version":     getattr(row, "client_version", None) or "",
            "uses_openplanet":    bool(getattr(row, "uses_openplanet", False) or False),
            "pyplanet_uptime_s":  int(getattr(row, "pyplanet_uptime_s", 0) or 0),
            "dedicated_uptime_s": int(getattr(row, "dedicated_uptime_s", 0) or 0),
            "delivered_at":       getattr(row, "delivered_at", None),
            "created_at":         row.created_at,
            "updated_at":         row.updated_at,
        }


def rows_to_markdown(rows: Iterable[dict[str, Any]]) -> str:
    """Render the export markdown. Stable, readable, and friendly to copy
    into a chat message for an AI agent."""
    rows = list(rows)
    now = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    out: list[str] = []
    out.append(f"# Bug reports export ({now})")
    out.append("")
    out.append(f"Total: {len(rows)}")
    out.append("")
    if not rows:
        out.append("_No reports._")
        return "\n".join(out) + "\n"
    for r in rows:
        created = r.get("created_at")
        created_s = created.strftime("%Y-%m-%d %H:%M:%S") if hasattr(created, "strftime") else str(created or "")
        out.append(f"## #{r.get('id')} — {r.get('subject') or '(no subject)'}")
        out.append("")
        out.append(f"- **Status:** {r.get('status') or 'open'}")
        out.append(f"- **Reporter:** {r.get('nickname') or r.get('login')} (`{r.get('login')}`)")
        if r.get("auth_level"):
            out.append(f"- **Auth level:** {r.get('auth_level')}")
        if r.get("map_name") or r.get("map_uid"):
            out.append(f"- **Map:** {r.get('map_name')} (`{r.get('map_uid')}`)")
        if r.get("mode_script"):
            out.append(f"- **Mode:** `{r.get('mode_script')}`")
        if r.get("game_phase"):
            out.append(f"- **Game phase:** {r.get('game_phase')}")
        tags = []
        if r.get("about_widgets"):
            tags.append("widgets")
        if r.get("about_ui"):
            tags.append("UI windows")
        if tags:
            out.append(f"- **About:** {', '.join(tags)}")
        if r.get("input_device"):
            out.append(f"- **Input device:** {r.get('input_device')}")
        if r.get("game_version"):
            out.append(f"- **Server (dedicated) version:** {r.get('game_version')}")
        if r.get("client_version"):
            out.append(f"- **Client (game) version:** {r.get('client_version')}")
        out.append(f"- **Openplanet:** {'yes' if r.get('uses_openplanet') else 'no'}")
        if r.get("pyplanet_uptime_s"):
            out.append(f"- **PyPlanet uptime:** {_fmt_seconds(int(r.get('pyplanet_uptime_s') or 0))}")
        if r.get("dedicated_uptime_s"):
            out.append(f"- **Dedicated uptime:** {_fmt_seconds(int(r.get('dedicated_uptime_s') or 0))}")
        out.append(f"- **Created:** {created_s} UTC")
        out.append("")
        details = (r.get("details") or "").strip()
        if details:
            out.append("```")
            out.append(details)
            out.append("```")
        else:
            out.append("_No details._")
        out.append("")
    return "\n".join(out) + "\n"


def _fmt_seconds(s: int) -> str:
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"
