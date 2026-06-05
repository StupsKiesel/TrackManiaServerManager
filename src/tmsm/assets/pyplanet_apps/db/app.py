"""tmsm db — master database inspection/editing tool.

Lists every Peewee model registered in `instance.db.registry.models`,
shows paginated rows of a selected table with search, allows editing
individual columns of a row, deleting a row, exporting a table to CSV,
and dropping a table (two-click confirmation).
"""
from __future__ import annotations

import csv
import datetime as _dt
import logging
import os
from pathlib import Path
from typing import Any

import peewee
from pyplanet.apps.config import AppConfig

from .views import DbView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────

_STR_FIELD_TYPES = (
    peewee.CharField, peewee.TextField, peewee.FixedCharField,
)

_INT_FIELD_TYPES = tuple(
    t for t in (
        getattr(peewee, "IntegerField", None),
        getattr(peewee, "BigIntegerField", None),
        getattr(peewee, "SmallIntegerField", None),
        getattr(peewee, "AutoField", None),
        getattr(peewee, "BigAutoField", None),
        getattr(peewee, "PrimaryKeyField", None),
    ) if t is not None
)

_AUTO_FIELD_TYPES = tuple(
    t for t in (
        getattr(peewee, "AutoField", None),
        getattr(peewee, "BigAutoField", None),
        getattr(peewee, "PrimaryKeyField", None),
    ) if t is not None
)


def _is_str_field(f: peewee.Field) -> bool:
    return isinstance(f, _STR_FIELD_TYPES)


def _is_bool_field(f: peewee.Field) -> bool:
    return isinstance(f, peewee.BooleanField)


def _is_int_field(f: peewee.Field) -> bool:
    return isinstance(f, _INT_FIELD_TYPES)


def _is_float_field(f: peewee.Field) -> bool:
    return isinstance(f, (peewee.FloatField, peewee.DoubleField,
                          peewee.DecimalField))


def _is_dt_field(f: peewee.Field) -> bool:
    return isinstance(f, (peewee.DateTimeField, peewee.DateField,
                          peewee.TimeField))


def _is_fk_field(f: peewee.Field) -> bool:
    return isinstance(f, peewee.ForeignKeyField)


def _field_kind(f: peewee.Field) -> str:
    if _is_bool_field(f):
        return "bool"
    if _is_int_field(f) or _is_fk_field(f):
        return "int"
    if _is_float_field(f):
        return "float"
    if _is_dt_field(f):
        return "datetime"
    return "text"


def _field_type_label(f: peewee.Field) -> str:
    return type(f).__name__.replace("Field", "").lower() or "field"


def _render_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat(sep=" ") if isinstance(v, _dt.datetime) \
            else v.isoformat()
    return str(v)


def _coerce(raw: str, f: peewee.Field) -> Any:
    raw = "" if raw is None else str(raw)
    if _is_bool_field(f):
        v = raw.strip().lower()
        if v in ("1", "true", "yes", "on", "y", "t"):
            return True
        if v in ("0", "false", "no", "off", "n", "f", ""):
            return False
        raise ValueError(f"not a bool: {raw!r}")
    if _is_int_field(f) or _is_fk_field(f):
        if raw.strip() == "":
            if getattr(f, "null", False):
                return None
            raise ValueError("integer required")
        return int(raw.strip())
    if _is_float_field(f):
        if raw.strip() == "":
            if getattr(f, "null", False):
                return None
            raise ValueError("number required")
        return float(raw.strip())
    if _is_dt_field(f):
        if raw.strip() == "":
            if getattr(f, "null", False):
                return None
            raise ValueError("datetime required")
        if isinstance(f, peewee.DateTimeField):
            return _dt.datetime.fromisoformat(raw.strip())
        if isinstance(f, peewee.DateField):
            return _dt.date.fromisoformat(raw.strip())
        return _dt.time.fromisoformat(raw.strip())
    if raw == "" and getattr(f, "null", False):
        return None
    return raw


# ──────────────────────────────────────────────────────────────────────
# AppConfig
# ──────────────────────────────────────────────────────────────────────

class App_Db(AppConfig):
    name = "pyplanet.apps.tmsm.db"
    label = "tmsm_db"
    app_dependencies = ["core.maniaplanet", "tmsm_ui", "tmsm_hub"]

    PAGE_SIZE_TABLES = 14
    PAGE_SIZE_ROWS = 19
    EDIT_FIELDS_PER_PAGE = 13
    LEVEL_MASTER = 3

    # rows-mode column layout (UI units)
    ROW_AREA_X = 4              # left edge of column area
    ROW_AREA_RIGHT_PAD = 10     # right margin
    VIEW_WIDTH = 280            # must match db.xml `W`
    CHAR_W = 1.6                # ~width of one 'sm' char in UI units
    COL_PADDING = 4             # gap between columns (UI units)
    PK_MIN_CHARS = 3
    PK_MAX_CHARS = 8
    COL_MIN_CHARS = 4
    COL_MAX_CHARS = 28

    EXPORT_DIR = Path.home() / ".tmsm" / "db_exports"

    _SEV_COLOR = {"success": "0f0", "error": "f44",
                  "warning": "fc4", "info": "888"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.view: DbView | None = None
        # per-login UI state
        self._state: dict[str, dict[str, Any]] = {}
        # per-login draft edits {fieldname: str}
        self._draft: dict[str, dict[str, str]] = {}
        # per-login baseline of currently rendered row values
        self._baseline: dict[str, dict[str, str]] = {}
        # per-login set of table keys "armed" for DROP confirmation
        self._drop_armed: dict[str, set[str]] = {}
        # cached row counts {table_key: int}
        self._count_cache: dict[str, int] = {}
        # keys of tables that have been physically dropped this session
        self._dropped_keys: set[str] = set()

    # ---- lifecycle ---------------------------------------------------

    async def on_start(self) -> None:
        self.view = DbView(self)
        self.view.connect("back", self._on_back)
        self.view.connect("save", self._on_save)
        self.view.connect("refresh", self._on_refresh)
        self.view.connect("_crumb__tables", self._crumb_tables)
        self.view.connect("_crumb__rows", self._crumb_rows)
        self.view.handle_catch_all = self._catch_all
        await self._register_with_hub()

    async def on_stop(self) -> None:
        if self.view is not None:
            try:
                await self.view.destroy()
            except Exception:
                logger.exception("db: destroy failed")

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            return
        await sig.send_robust({"entry": HubAppEntry(
            key="db", name="Database", icon="database",
            role=Role.MASTER, order=50,
            description="Inspect & edit PyPlanet database tables.",
            open=self._open_view,
        )}, raw=True)

    # ---- state helpers -----------------------------------------------

    def _gstate(self, login: str) -> dict[str, Any]:
        return self._state.setdefault(login, {
            "mode": "tables",
            "search_tables": "",
            "tbl_page": 0,
            "table_key": "",
            "search_rows": "",
            "row_page": 0,
            "col_scroll": 0,
            "sel_pk": None,
            "sort_key": None,
            "sort_dir": "asc",
            "edit_pk": None,
            "edit_page": 0,
            "confirm_delete": None,  # pk_str pending confirmation
            "status": "",
            "status_color": "aaa",
        })

    async def _toast(self, player, msg: str, severity: str = "info") -> None:
        st = self._gstate(player.login)
        st["status"] = msg
        st["status_color"] = self._SEV_COLOR.get(severity, "888")
        sig = None
        for code in ("notification_engine:notify", "tmsm_status:notify"):
            try:
                sig = self.context.signals.get_signal(code)
                break
            except KeyError:
                continue
        if sig is None:
            return
        try:
            await sig.send_robust({
                "message": msg, "severity": severity,
                "login": player.login, "source": "db",
            })
        except Exception:
            logger.exception("db: toast emit failed")

    async def _open_view(self, player) -> None:
        # Reset per-login transient state on a fresh open
        st = self._gstate(player.login)
        st["mode"] = "tables"
        st["status"] = ""
        self._draft.pop(player.login, None)
        self._baseline.pop(player.login, None)
        self._drop_armed.pop(player.login, None)
        await self._open(player)

    async def _open(self, player) -> None:
        if self.view is None:
            return
        try:
            await self.view.display(player_logins=[player.login])
        except Exception:
            logger.exception("db: display failed")

    async def _on_back(self, player, **_) -> None:
        try:
            from pyplanet.views.template import TemplateView
            await TemplateView.hide(self.view, player_logins=[player.login])
        except Exception:
            logger.exception("db: hide failed")
        try:
            sig = self.context.signals.get_signal("tmsm_hub:show")
            await sig.send_robust({"player": player}, raw=True)
        except KeyError:
            pass

    async def _crumb_tables(self, player, **_) -> None:
        st = self._gstate(player.login)
        st["mode"] = "tables"
        self._draft.pop(player.login, None)
        self._baseline.pop(player.login, None)
        await self._open(player)

    async def _crumb_rows(self, player, **_) -> None:
        st = self._gstate(player.login)
        if st.get("table_key"):
            st["mode"] = "rows"
            self._draft.pop(player.login, None)
            self._baseline.pop(player.login, None)
            await self._open(player)

    # ---- model registry ---------------------------------------------

    def _models(self) -> dict[str, tuple[Any, str, Any]]:
        """Return {key: (app, name, model)} where key='<app_label>.<Name>'."""
        try:
            all_models = dict(self.instance.db.registry.models)
        except Exception:
            logger.exception("db: registry.models read failed")
            return {}
        # Exclude tables that were physically dropped this session.
        if self._dropped_keys:
            all_models = {k: v for k, v in all_models.items() if k not in self._dropped_keys}
        return all_models

    def _model_by_key(self, key: str):
        return self._models().get(key)

    @staticmethod
    def _table_name(model) -> str:
        return getattr(model._meta, "table_name", model.__name__)

    @staticmethod
    def _pk_field(model) -> peewee.Field:
        return model._meta.primary_key

    @staticmethod
    def _ordered_fields(model) -> list[tuple[str, peewee.Field]]:
        return list(model._meta.sorted_fields and
                    [(f.name, f) for f in model._meta.sorted_fields]
                    or model._meta.fields.items())

    # ---- counts ------------------------------------------------------

    async def _count(self, key: str, model) -> int:
        if key in self._count_cache:
            return self._count_cache[key]
        try:
            c = await model.objects.count(model.select())
        except peewee.ProgrammingError as e:
            # Table not yet created (e.g. migration pending) — not an error.
            logger.debug("db: count %s skipped (table missing): %s", key, e)
            c = -1
        except Exception:
            logger.exception("db: count %s failed", key)
            c = -1
        self._count_cache[key] = c
        return c

    # ---- context builders -------------------------------------------

    async def db_context(self, player) -> dict[str, Any]:
        login = player.login
        level = int(getattr(player, "level", 0))
        is_master = level >= self.LEVEL_MASTER
        st = self._gstate(login)
        mode = st.get("mode", "tables")

        # Dynamic breadcrumbs (overrides view.breadcrumbs for this render).
        crumbs: list[dict[str, str]] = [{"key": "hub", "label": "Hub"}]
        if mode in ("rows", "edit"):
            crumbs.append({"key": "tables", "label": "Tables"})
        if mode == "edit":
            tname = st.get("table_key", "").split(".")[-1] or "rows"
            crumbs.append({"key": "rows", "label": tname})

        ctx: dict[str, Any] = {
            "mode": mode,
            "is_master": is_master,
            "status": st.get("status", ""),
            "status_color": st.get("status_color", "aaa"),
            "view_crumbs": crumbs,
        }

        if mode == "tables":
            ctx.update(await self._ctx_tables(login))
        elif mode == "rows":
            ctx.update(await self._ctx_rows(login))
        elif mode == "edit":
            ctx.update(await self._ctx_edit(login))
        return ctx

    async def _ctx_tables(self, login: str) -> dict[str, Any]:
        st = self._gstate(login)
        armed = self._drop_armed.get(login, set())
        all_keys = sorted(self._models().keys(), key=str.lower)
        search = (st.get("search_tables") or "").strip().lower()
        if search:
            keys = [k for k in all_keys if search in k.lower()]
        else:
            keys = all_keys

        page = max(0, int(st.get("tbl_page", 0)))
        total_pages = max(1, -(-len(keys) // self.PAGE_SIZE_TABLES))
        page = min(page, total_pages - 1)
        st["tbl_page"] = page
        slice_ = keys[page * self.PAGE_SIZE_TABLES:
                      (page + 1) * self.PAGE_SIZE_TABLES]

        rows = []
        for key in slice_:
            app, name, model = self._model_by_key(key)
            cnt = await self._count(key, model)
            rows.append({
                "key": key,
                "app_label": app.label,
                "model_name": name,
                "table_name": self._table_name(model),
                "count": (cnt if cnt >= 0 else "?"),
                "drop_armed": key in armed,
            })

        return {
            "title": "Database — Tables",
            "tables": rows,
            "tables_count": len(keys),
            "search_tables": st.get("search_tables", ""),
            "tbl_page": page,
            "tbl_total_pages": total_pages,
        }

    async def _ctx_rows(self, login: str) -> dict[str, Any]:
        st = self._gstate(login)
        key = st.get("table_key") or ""
        meta = self._model_by_key(key)
        if not meta:
            st["mode"] = "tables"
            return await self._ctx_tables(login)
        app, name, model = meta
        table_name = self._table_name(model)

        all_fields = list(model._meta.sorted_fields)
        pk = self._pk_field(model)
        non_pk = [f for f in all_fields if f is not pk]

        search = (st.get("search_rows") or "").strip()
        query = model.select()
        if search:
            clauses = []
            for f in all_fields:
                if _is_str_field(f):
                    clauses.append(f.contains(search))
            if clauses:
                expr = clauses[0]
                for c in clauses[1:]:
                    expr = expr | c
                query = query.where(expr)

        # ordering: explicit sort_key/sort_dir or default to pk asc
        sort_key = st.get("sort_key")
        sort_dir = st.get("sort_dir", "asc")
        sort_field = None
        if sort_key:
            for f in all_fields:
                if f.name == sort_key:
                    sort_field = f
                    break
        if sort_field is None:
            sort_field = pk
            sort_dir = "asc"
            st["sort_key"] = None
        query = query.order_by(
            sort_field.desc() if sort_dir == "desc" else sort_field.asc()
        )

        try:
            total = await model.objects.count(query)
        except Exception:
            logger.exception("db: count rows %s failed", key)
            total = 0

        page_size = self.PAGE_SIZE_ROWS
        total_pages = max(1, -(-total // page_size))
        page = max(0, min(int(st.get("row_page", 0)), total_pages - 1))
        st["row_page"] = page
        paged = query.limit(page_size).offset(page * page_size)

        try:
            rows_objs = list(await model.objects.execute(paged))
        except Exception:
            logger.exception("db: rows query failed")
            rows_objs = []

        # raw string values keyed by field name (per row) — used both
        # for width measurement and rendering.
        raw_rows: list[dict[str, Any]] = []
        for inst in rows_objs:
            d = {"_pk": _render_value(getattr(inst, pk.name))}
            for f in all_fields:
                try:
                    v = getattr(inst, f.name)
                except Exception:
                    v = None
                d[f.name] = _render_value(v)
            raw_rows.append(d)

        # ---- column width computation -------------------------------
        def _chars_for(field, hard_min, hard_max) -> int:
            longest = len(field.name) + (5 if field is pk else 0)  # " (pk)"
            for r in raw_rows:
                v = r.get(field.name, "")
                if len(v) > longest:
                    longest = len(v)
            return max(hard_min, min(hard_max, longest))

        pk_chars = _chars_for(pk, self.PK_MIN_CHARS, self.PK_MAX_CHARS)
        pk_w = pk_chars * self.CHAR_W + self.COL_PADDING

        try:
            view_w = float(self.VIEW_WIDTH)
        except Exception:
            view_w = 280.0
        avail = view_w - self.ROW_AREA_X - self.ROW_AREA_RIGHT_PAD - pk_w

        # pack non-pk cols from col_scroll forward until they overflow
        col_scroll = max(0, int(st.get("col_scroll", 0)))
        if col_scroll > len(non_pk):
            col_scroll = max(0, len(non_pk) - 1)
        st["col_scroll"] = col_scroll

        visible_non_pk: list[tuple[Any, int, float]] = []  # (field, chars, w)
        used = 0.0
        for f in non_pk[col_scroll:]:
            ch = _chars_for(f, self.COL_MIN_CHARS, self.COL_MAX_CHARS)
            w = ch * self.CHAR_W + self.COL_PADDING
            if visible_non_pk and used + w > avail:
                break
            visible_non_pk.append((f, ch, w))
            used += w
        if not visible_non_pk and non_pk:
            # always show at least one column
            f = non_pk[col_scroll if col_scroll < len(non_pk) else 0]
            ch = _chars_for(f, self.COL_MIN_CHARS, self.COL_MAX_CHARS)
            visible_non_pk = [(f, ch, ch * self.CHAR_W + self.COL_PADDING)]

        visible_fields: list[tuple[Any, int, float]] = (
            [(pk, pk_chars, pk_w)] + visible_non_pk
        )

        # build column descriptors with x offsets
        columns: list[dict[str, Any]] = []
        x = float(self.ROW_AREA_X)
        for f, ch, w in visible_fields:
            label = f.name + (" (pk)" if f is pk else "")
            columns.append({
                "key": f.name,
                "label": label,
                "name": f.name,
                "type": _field_type_label(f),
                "is_pk": (f is pk),
                "chars": ch,
                "w": w,
                "x": x,
                "align": "left",
                "sortable": True,
            })
            x += w

        # build row dicts keyed by column key (for data_table macro)
        rows: list[dict[str, Any]] = []
        for r in raw_rows:
            row = {"_pk": r["_pk"]}
            for col in columns:
                raw = r.get(col["name"], "")
                if len(raw) > col["chars"]:
                    raw = raw[: max(1, col["chars"] - 1)] + "…"
                row[col["key"]] = raw
            rows.append(row)

        sel_pk_raw = st.get("sel_pk")
        sel_pk_str = _render_value(sel_pk_raw) if sel_pk_raw is not None else None
        sel_idx = -1
        if sel_pk_str is not None:
            for i, r in enumerate(rows):
                if r["_pk"] == sel_pk_str:
                    sel_idx = i
                    break

        # persist visible pk order so row__N catch-all can resolve to a pk
        st["_visible_pks"] = [r["_pk"] for r in rows]
        st["_cols_visible"] = len(visible_non_pk)

        cols_total = len(non_pk)
        col_can_prev = col_scroll > 0
        col_can_next = (col_scroll + len(visible_non_pk)) < cols_total

        return {
            "title": f"Database — {table_name}",
            "table_key": key,
            "table_name": table_name,
            "table_app": app.label,
            "columns": columns,
            "rows": rows,
            "rows_count": total,
            "search_rows": st.get("search_rows", ""),
            "row_page": page,
            "row_total_pages": total_pages,
            "pk_field": pk.name,
            "sel_pk": sel_pk_str,
            "sel_idx": sel_idx,
            "sort_key": st.get("sort_key"),
            "sort_dir": st.get("sort_dir", "asc"),
            "confirm_delete_open": bool(st.get("confirm_delete")),
            "confirm_delete_pk": _render_value(st.get("confirm_delete")) if st.get("confirm_delete") else "",
            "col_can_prev": col_can_prev,
            "col_can_next": col_can_next,
            "col_scroll": col_scroll,
            "cols_total": cols_total,
            "cols_visible": len(visible_non_pk),
        }

    async def _ctx_edit(self, login: str) -> dict[str, Any]:
        st = self._gstate(login)
        key = st.get("table_key") or ""
        meta = self._model_by_key(key)
        if not meta:
            st["mode"] = "tables"
            return await self._ctx_tables(login)
        app, name, model = meta
        pk = self._pk_field(model)
        pk_value = st.get("edit_pk")
        table_name = self._table_name(model)

        is_new = (pk_value is None)
        inst = None
        if not is_new:
            try:
                inst = await model.objects.get(
                    model, pk == pk_value
                )
            except Exception:
                logger.exception("db: edit get failed %s", pk_value)
                await self._toast(
                    type("P", (), {"login": login})(),
                    f"row #{pk_value} missing", "error",
                )
                st["mode"] = "rows"
                return await self._ctx_rows(login)

        baseline = self._baseline.setdefault(login, {})
        baseline.clear()
        draft = self._draft.get(login, {})

        field_rows: list[dict[str, Any]] = []
        for f in model._meta.sorted_fields:
            if is_new and f is pk and isinstance(f, _AUTO_FIELD_TYPES):
                continue  # AutoField filled by DB on insert
            cur = "" if inst is None else _render_value(getattr(inst, f.name))
            baseline[f.name] = cur
            edit = draft.get(f.name, cur)
            field_rows.append({
                "name": f.name,
                "type": _field_type_label(f),
                "kind": _field_kind(f),
                "null": bool(getattr(f, "null", False)),
                "is_pk": (f is pk),
                "value": cur,
                "value_edit": edit,
                "dirty": (f.name in draft and draft[f.name] != cur),
            })

        dirty = sum(1 for r in field_rows if r["dirty"])
        # paginate fields
        total = len(field_rows)
        per_page = self.EDIT_FIELDS_PER_PAGE
        total_pages = max(1, -(-total // per_page))
        page = max(0, min(int(st.get("edit_page", 0)), total_pages - 1))
        st["edit_page"] = page
        page_fields = field_rows[page * per_page:(page + 1) * per_page]

        title_suffix = (
            f"#{pk_value} (edit)" if not is_new else "new row"
        )
        return {
            "title": f"Database — {table_name} {title_suffix}",
            "table_key": key,
            "table_name": table_name,
            "pk_field": pk.name,
            "edit_pk": "" if is_new else _render_value(pk_value),
            "edit_fields": page_fields,
            "edit_fields_total": total,
            "edit_page": page,
            "edit_total_pages": total_pages,
            "dirty_count": dirty,
            "is_new": is_new,
            "confirm_delete_open": bool(st.get("confirm_delete")),
            "confirm_delete_pk": _render_value(st.get("confirm_delete")) if st.get("confirm_delete") else "",
        }

    # ---- input absorption -------------------------------------------

    def _absorb(self, login: str, values) -> None:
        if not values or self.view is None:
            return
        st = self._gstate(login)
        mode = st.get("mode")
        # search fields
        st_search_key = f"entry_{self.view.id}__search_tables"
        rw_search_key = f"entry_{self.view.id}__search_rows"
        if st_search_key in values:
            new = str(values[st_search_key] or "")
            if new != st.get("search_tables", ""):
                st["search_tables"] = new
                st["tbl_page"] = 0
        if rw_search_key in values:
            new = str(values[rw_search_key] or "")
            if new != st.get("search_rows", ""):
                st["search_rows"] = new
                st["row_page"] = 0

        if mode != "edit":
            return
        prefix = f"entry_{self.view.id}__field__"
        baseline = self._baseline.get(login, {})
        draft = self._draft.setdefault(login, {})
        for k, v in values.items():
            if not k.startswith(prefix):
                continue
            fname = k[len(prefix):]
            new = str(v if v is not None else "")
            base = baseline.get(fname)
            if base is None:
                # No baseline yet (first interaction before render finished, or
                # field outside current page). Treat any non-empty value as a
                # draft change so we don't silently drop user input.
                if new:
                    draft[fname] = new
                continue
            if new == base:
                draft.pop(fname, None)
            else:
                draft[fname] = new
        if not draft:
            self._draft.pop(login, None)

    # ---- catch-all ---------------------------------------------------

    async def _catch_all(self, player, action, values):
        login = player.login
        level = int(getattr(player, "level", 0))
        st = self._gstate(login)
        self._absorb(login, values)

        # Reserved
        if action == "back":
            await self._on_back(player)
            return
        if action == "save":
            await self._on_save(player, values=values)
            return
        if action == "refresh":
            await self._on_refresh(player)
            return

        # Navigation
        if action == "mode_tables":
            st["mode"] = "tables"
            self._draft.pop(login, None)
            self._baseline.pop(login, None)
            self._drop_armed.pop(login, None)
            await self._open(player)
            return
        if action == "mode_rows":
            if st.get("table_key"):
                st["mode"] = "rows"
                self._draft.pop(login, None)
                self._baseline.pop(login, None)
                await self._open(player)
            return

        # Tables-mode pager
        if action in ("tbl_prev", "tbl_next"):
            st["tbl_page"] = max(0, st.get("tbl_page", 0)
                                 + (-1 if action == "tbl_prev" else 1))
            await self._open(player)
            return
        if action in ("row_prev", "row_next"):
            st["row_page"] = max(0, st.get("row_page", 0)
                                 + (-1 if action == "row_prev" else 1))
            await self._open(player)
            return

        # open table
        if action.startswith("open__"):
            key = action[len("open__"):]
            if key in self._models():
                st["table_key"] = key
                st["mode"] = "rows"
                st["row_page"] = 0
                st["search_rows"] = ""
                st["col_scroll"] = 0
                st["sel_pk"] = None
                st["sort_key"] = None
                st["sort_dir"] = "asc"
                await self._open(player)
            return

        # export / drop / refresh-count per table key
        if action.startswith("export__"):
            if level < self.LEVEL_MASTER:
                await self._toast(player, "master required", "error")
                return
            await self._export(player, action[len("export__"):])
            await self._open(player)
            return
        if action.startswith("drop__"):
            if level < self.LEVEL_MASTER:
                await self._toast(player, "master required", "error")
                return
            await self._drop(player, action[len("drop__"):])
            await self._open(player)
            return
        if action.startswith("recount__"):
            self._count_cache.pop(action[len("recount__"):], None)
            await self._open(player)
            return

        # rows-mode actions
        if action.startswith("edit__"):
            pk_raw = action[len("edit__"):]
            await self._enter_edit(player, pk_raw)
            return
        if action.startswith("selrow__"):
            pk_raw = action[len("selrow__"):]
            cur = st.get("sel_pk")
            cur_str = _render_value(cur) if cur is not None else None
            st["sel_pk"] = None if cur_str == pk_raw else pk_raw
            await self._open(player)
            return
        if action.startswith("rows__row__"):
            try:
                idx = int(action[len("rows__row__"):])
            except ValueError:
                return
            vis = st.get("_visible_pks") or []
            if 0 <= idx < len(vis):
                pk_raw = vis[idx]
                cur = st.get("sel_pk")
                cur_str = _render_value(cur) if cur is not None else None
                st["sel_pk"] = None if cur_str == pk_raw else pk_raw
                await self._open(player)
            return
        if action.startswith("rows__sort__"):
            key = action[len("rows__sort__"):]
            if st.get("sort_key") == key:
                st["sort_dir"] = "desc" if st.get("sort_dir", "asc") == "asc" else "asc"
            else:
                st["sort_key"] = key
                st["sort_dir"] = "asc"
            st["row_page"] = 0
            st["sel_pk"] = None
            await self._open(player)
            return
        if action in ("cols_prev", "cols_next"):
            vis = max(1, int(st.get("_cols_visible", 1)))
            delta = -vis if action == "cols_prev" else vis
            st["col_scroll"] = max(0, int(st.get("col_scroll", 0)) + delta)
            await self._open(player)
            return
        if action == "edit_sel":
            pk_raw = st.get("sel_pk")
            if not pk_raw:
                await self._toast(player, "select a row first", "warning")
                return
            await self._enter_edit(player, str(pk_raw))
            return
        if action == "del_sel":
            if level < self.LEVEL_MASTER:
                await self._toast(player, "master required", "error")
                return
            pk_raw = st.get("sel_pk")
            if not pk_raw:
                await self._toast(player, "select a row first", "warning")
                return
            st["confirm_delete"] = pk_raw
            await self._open(player)
            return
        if action == "confirm_del__ok":
            if level < self.LEVEL_MASTER:
                st["confirm_delete"] = None
                await self._open(player)
                return
            pk_raw = st.get("confirm_delete")
            st["confirm_delete"] = None
            if pk_raw:
                await self._delete_row(player, str(pk_raw))
                # if we were in edit mode, return to rows after delete
                if st.get("mode") == "edit":
                    st["mode"] = "rows"
                    st["edit_pk"] = None
                    self._draft.pop(login, None)
                    self._baseline.pop(login, None)
                st["sel_pk"] = None
            await self._open(player)
            return
        if action == "confirm_del__cancel":
            st["confirm_delete"] = None
            await self._open(player)
            return
        if action.startswith("delrow__"):
            if level < self.LEVEL_MASTER:
                await self._toast(player, "master required", "error")
                return
            await self._delete_row(player, action[len("delrow__"):])
            await self._open(player)
            return
        if action == "new_row":
            if level < self.LEVEL_MASTER:
                await self._toast(player, "master required", "error")
                return
            st["mode"] = "edit"
            st["edit_pk"] = None
            st["edit_page"] = 0
            self._draft.pop(login, None)
            self._baseline.pop(login, None)
            await self._open(player)
            return

        # edit-mode actions
        if action.startswith("toggle__"):
            fname = action[len("toggle__"):]
            base = self._baseline.get(login, {}).get(fname)
            draft = self._draft.setdefault(login, {})
            cur = draft.get(fname, base or "0")
            new = "0" if cur == "1" else "1"
            if base is not None and new == base:
                draft.pop(fname, None)
            else:
                draft[fname] = new
            if not draft:
                self._draft.pop(login, None)
            await self._open(player)
            return
        if action == "delete_row":
            if level < self.LEVEL_MASTER:
                await self._toast(player, "master required", "error")
                return
            pk_val = st.get("edit_pk")
            if pk_val is None:
                await self._toast(player, "no row selected", "warning")
                return
            st["confirm_delete"] = _render_value(pk_val)
            await self._open(player)
            return
        if action in ("edit_prev", "edit_next"):
            st["edit_page"] = max(0, int(st.get("edit_page", 0))
                                  + (-1 if action == "edit_prev" else 1))
            await self._open(player)
            return

    # ---- edit entry --------------------------------------------------

    async def _enter_edit(self, player, pk_raw: str) -> None:
        login = player.login
        st = self._gstate(login)
        key = st.get("table_key") or ""
        meta = self._model_by_key(key)
        if not meta:
            return
        _, _, model = meta
        pk = self._pk_field(model)
        try:
            pk_val = _coerce(pk_raw, pk)
        except Exception:
            pk_val = pk_raw
        st["mode"] = "edit"
        st["edit_pk"] = pk_val
        st["edit_page"] = 0
        self._draft.pop(login, None)
        self._baseline.pop(login, None)
        await self._open(player)

    # ---- export ------------------------------------------------------

    async def _export(self, player, key: str) -> None:
        meta = self._model_by_key(key)
        if not meta:
            await self._toast(player, "unknown table", "error")
            return
        _, _, model = meta
        try:
            rows = list(await model.objects.execute(model.select()))
        except Exception as e:
            await self._toast(player, f"export query failed: {e}", "error")
            return
        try:
            os.makedirs(self.EXPORT_DIR, exist_ok=True)
        except Exception as e:
            await self._toast(player, f"mkdir failed: {e}", "error")
            return
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = key.replace(".", "_")
        out = self.EXPORT_DIR / f"{safe}_{ts}.csv"
        fields = [f.name for f in model._meta.sorted_fields]
        try:
            with open(out, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(fields)
                for inst in rows:
                    w.writerow([_render_value(getattr(inst, n, None))
                                for n in fields])
        except Exception as e:
            await self._toast(player, f"write failed: {e}", "error")
            return
        await self._toast(
            player, f"exported {len(rows)} → {out.name}", "success"
        )

    # ---- drop --------------------------------------------------------

    async def _drop(self, player, key: str) -> None:
        meta = self._model_by_key(key)
        if not meta:
            await self._toast(player, "unknown table", "error")
            return
        _, _, model = meta
        armed = self._drop_armed.setdefault(player.login, set())
        if key not in armed:
            armed.add(key)
            await self._toast(
                player, f"click again to DROP {self._table_name(model)}",
                "warning",
            )
            return
        armed.discard(key)
        try:
            with self.instance.db.allow_sync():
                # fail_silently=True, cascade=False — MySQL/MariaDB does not
                # support DROP TABLE ... CASCADE so passing True there raises.
                model.drop_table(fail_silently=True, cascade=False)
        except Exception as e:
            await self._toast(player, f"drop failed: {e}", "error")
            return
        self._count_cache.pop(key, None)
        self._dropped_keys.add(key)
        await self._toast(
            player, f"dropped {self._table_name(model)}", "success"
        )

    # ---- row delete --------------------------------------------------

    async def _delete_row(self, player, pk_raw: str) -> None:
        login = player.login
        st = self._gstate(login)
        key = st.get("table_key") or ""
        meta = self._model_by_key(key)
        if not meta:
            return
        _, _, model = meta
        pk = self._pk_field(model)
        try:
            pk_val = _coerce(pk_raw, pk)
        except Exception as e:
            await self._toast(player, f"bad pk: {e}", "error")
            return
        try:
            n = await model.objects.execute(
                model.delete().where(pk == pk_val)
            )
        except Exception as e:
            await self._toast(player, f"delete failed: {e}", "error")
            return
        self._count_cache.pop(key, None)
        await self._toast(player, f"deleted {n} row(s)", "success")

    # ---- save (refresh/reset & save) --------------------------------

    async def _on_refresh(self, player, **_) -> None:
        login = player.login
        st = self._gstate(login)
        if st.get("mode") == "tables":
            self._count_cache.clear()
            await self._toast(player, "counts refreshed", "info")
        else:
            self._draft.pop(login, None)
            await self._toast(player, "draft cleared", "info")
        await self._open(player)

    async def _on_save(self, player, values=None, **_) -> None:
        login = player.login
        level = int(getattr(player, "level", 0))
        if level < self.LEVEL_MASTER:
            await self._toast(player, "master required to save", "error")
            return
        self._absorb(login, values)
        st = self._gstate(login)
        if st.get("mode") != "edit":
            await self._toast(player, "nothing to save here", "warning")
            return

        key = st.get("table_key") or ""
        meta = self._model_by_key(key)
        if not meta:
            await self._toast(player, "table missing", "error")
            return
        _, _, model = meta
        pk = self._pk_field(model)
        pk_val = st.get("edit_pk")
        is_new = (pk_val is None)

        # Build draft directly from form values as a safety net: for each
        # form-supplied field, if value differs from baseline (or no baseline
        # exists) include it. This decouples save from any prior absorb state.
        baseline = self._baseline.get(login, {})
        prefix = f"entry_{self.view.id}__field__"
        form_draft: dict[str, str] = {}
        if values:
            for k, v in values.items():
                if not k.startswith(prefix):
                    continue
                fname = k[len(prefix):]
                new = str(v if v is not None else "")
                base = baseline.get(fname)
                if base is None or new != base:
                    form_draft[fname] = new
        # merge with any previously absorbed draft (e.g. bool toggles, fields
        # from other edit pages)
        merged = dict(self._draft.get(login, {}))
        merged.update(form_draft)
        # drop entries that match baseline (clean fields)
        draft = {k: v for k, v in merged.items()
                 if baseline.get(k) != v or baseline.get(k) is None and v}

        if not draft and not is_new:
            await self._toast(player, "no changes", "warning")
            return

        # Coerce all draft values
        coerced: dict[str, Any] = {}
        rejected: list[str] = []
        for fname, raw in draft.items():
            f = model._meta.fields.get(fname)
            if f is None:
                rejected.append(f"{fname} (unknown)")
                continue
            try:
                coerced[fname] = _coerce(raw, f)
            except Exception as e:
                rejected.append(f"{fname} ({e})")

        if rejected and not coerced:
            await self._toast(
                player, f"all rejected: {rejected[0]}", "error"
            )
            return

        try:
            if is_new:
                # Build kwargs from non-pk fields
                kwargs = dict(coerced)
                inst = await model.objects.create(model, **kwargs)
                new_pk = getattr(inst, pk.name)
                st["edit_pk"] = new_pk
                self._count_cache.pop(key, None)
                msg = f"created #{_render_value(new_pk)}"
                if rejected:
                    msg += f" ({len(rejected)} rejected)"
                await self._toast(player, msg, "success")
            else:
                if coerced:
                    await model.objects.execute(
                        model.update(**coerced).where(pk == pk_val)
                    )
                msg = f"saved {len(coerced)} field(s)"
                if rejected:
                    msg += f", {len(rejected)} rejected: {rejected[0]}"
                await self._toast(
                    player, msg,
                    "warning" if rejected else "success",
                )
        except Exception as e:
            await self._toast(player, f"save failed: {e}", "error")
            return

        self._draft.pop(login, None)
        await self._open(player)
