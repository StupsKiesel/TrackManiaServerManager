"""tmx_browser - hub-mounted, operator-only browser for Trackmania Exchange.

Detects the running game and talks to the matching TMX site:

* ``tmnext`` → ``trackmania.exchange`` (TM2020)
* ``tm``     → ``tm.mania-exchange.com`` (Maniaplanet TM2)
* ``sm``     → ``sm.mania-exchange.com`` (ShootMania)

UI model is single-pane: the main window shows a search box, a filter icon
button (opens a separate sub-window with all the granular filters), a result
data-table and a control bar with prev/next, an info button for the selected
row, an add button, and two checkboxes (play-next, save-matchsettings).

The ``Add`` action downloads the GBX bytes from TMX and hands them to
``MapManager.upload_map`` so the file lands in the dedicated server's
``UserData/Maps/`` and is appended to the live playlist.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import aiohttp

from pyplanet.apps.config import AppConfig

from .tmx import download as tmx_download
from .tmx import flow_description
from .tmx import search as tmx_search
from .tmx import site_for
from .tmx import tags as tmx_tags
from .tmx import (
    COLLECTIONS,
    ORDER_AWARDED,
    ORDER_RECENT,
    _DIFFICULTIES,
    _ENVIRONMENTS,
    _MOODS,
    _ROUTES,
    _VEHICLES,
)
from .tmx import thumbnail_url as tmx_thumbnail_url
from . import policy as _policy
from .views import (
    TmxBrowserView,
    TmxDetailView,
    TmxFiltersView,
    TmxPolicyView,
)

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)


# Common map-type identifiers for TM2020 (string sent verbatim to TMX).
MAPTYPES_TMNEXT: list[tuple[str, str]] = [
    ("TM_Race",     "Race"),
    ("TM_Stunt",    "Stunt"),
    ("TM_Platform", "Platform"),
    ("TM_Royal",    "Royal"),
    ("TMFL",        "Flag Rush"),
]

# Order menu (subset; matches the codes we use elsewhere).
ORDERS: list[tuple[int, str]] = [
    (ORDER_RECENT,  "Newest"),
    (ORDER_AWARDED, "Most awarded"),
]


# Result-table column manifest. First entry is sticky (always visible);
# the rest are paged horizontally by the col_scroll state. ``w`` is in UI
# units, used both for header packing and to compute scrollbar geometry.
ALL_COLS: list[dict[str, Any]] = [
    {"key": "name",        "label": "Name",   "w": 80, "align": "left"},
    {"key": "author",      "label": "Author", "w": 42, "align": "left",  "color": "ccc"},
    {"key": "environment", "label": "Vista",  "w": 28, "align": "left",  "color": "ccc"},
    {"key": "map_type",    "label": "Type",   "w": 32, "align": "left",  "color": "ccc"},
    {"key": "length",      "label": "Len",    "w": 16, "align": "right", "color": "ccc"},
    {"key": "difficulty",  "label": "Diff",   "w": 26, "align": "left",  "color": "ccc"},
    {"key": "awards",      "label": "Awd",    "w": 14, "align": "right", "color": "fc4"},
    {"key": "mood",        "label": "Mood",   "w": 22, "align": "left",  "color": "ccc"},
    {"key": "style",       "label": "Style",  "w": 26, "align": "left",  "color": "ccc"},
    {"key": "routes",      "label": "Routes", "w": 26, "align": "left",  "color": "ccc"},
    {"key": "vehicle",     "label": "Car",    "w": 22, "align": "left",  "color": "ccc"},
]

# Table viewport: must match what the template gives the data_table macro.
TABLE_VIEW_W = 232.0   # win_w (240) - 2*margin (4)


def _safe_filename(name: str, track_id: int, ext: str) -> str:
    """Produce a filesystem-safe filename relative to the dedicated's Maps tree."""
    base = re.sub(r"\$[0-9a-fA-F]{3}", "", name or "")          # strip TMX $-codes
    base = re.sub(r"[^A-Za-z0-9._ \-]+", "_", base).strip()
    base = (base or "map")[:60]
    return f"tmx/{base}_#{int(track_id)}{ext}"


# Accept "1h 2m 30s", "5m", "90s", "1:30", or a bare number (seconds).
_LENGTH_TOKEN_RE = re.compile(r"(\d+)\s*([hms]?)", re.IGNORECASE)


def _parse_length_seconds(text: str) -> int | None:
    """Parse a human duration string into seconds, or ``None`` on empty/garbage."""
    t = (text or "").strip().lower()
    if not t:
        return None
    if ":" in t:
        parts = t.split(":")
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            return None
        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
        if len(nums) == 3:
            return nums[0] * 3600 + nums[1] * 60 + nums[2]
        return None
    total = 0
    found = False
    for n, unit in _LENGTH_TOKEN_RE.findall(t):
        v = int(n)
        if unit == "h":
            total += v * 3600
        elif unit == "m":
            total += v * 60
        elif unit == "s":
            total += v
        else:
            # bare number => seconds
            total += v
        found = True
    return total if found else None


def _fmt_length_seconds(s: int | None) -> str:
    if not s or s <= 0:
        return ""
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    out = []
    if h:
        out.append(f"{h}h")
    if m:
        out.append(f"{m}m")
    if sec or not out:
        out.append(f"{sec}s")
    return " ".join(out)


class TmxBrowserApp(AppConfig):
    name = "pyplanet.apps.tmsm.tmx_browser"
    label = "tmsm_tmx_browser"
    app_dependencies = ["core.maniaplanet"]
    game_dependencies = ["trackmania", "trackmania_next", "shootmania"]

    LEVEL_OPERATOR = 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.view: TmxBrowserView | None = None
        self.detail_view: TmxDetailView | None = None
        self.filters_view: TmxFiltersView | None = None
        self.policy_view: TmxPolicyView | None = None
        # per-login draft state
        self._state: dict[str, dict[str, Any]] = {}
        # per-admin draft of the global policy (key = login)
        self._policy_drafts: dict[str, dict[str, Any]] = {}
        self._tags_cache: list[dict[str, Any]] = []
        self._policy: dict[str, Any] = _policy.default_policy()
        self._lock = asyncio.Lock()

    # ---- lifecycle -----------------------------------------------------

    async def on_start(self) -> None:
        # Server-wide search restrictions (admin-gated). Load early so the
        # first paint already reflects them and the operator UI can never
        # render a "default" period before policy comes in.
        try:
            self._policy = _policy.load()
        except Exception:
            logger.exception("tmx_browser: policy load failed")
            self._policy = _policy.default_policy()

        # PyPlanet permissions (admin-level by default).
        try:
            await self.instance.permission_manager.register(
                "policy", "Edit the TMX browser admin policy",
                app=self, min_level=2,
            )
        except Exception:
            logger.exception("tmx_browser: permission register failed")

        try:
            self.view = TmxBrowserView(self)
            self.view.connect("search", self._on_search)
            self.view.connect("query__clear", self._on_clear_query)
            self.view.connect("prev", self._on_prev)
            self.view.connect("next", self._on_next)
            self.view.connect("refresh", self._on_refresh)
            self.view.connect("open_filters", self._on_open_filters)
            self.view.connect("open_policy", self._on_open_policy)
            self.view.connect("info_selected", self._on_info_selected)
            self.view.connect("add_selected", self._on_add_selected)
            self.view.connect("toggle_juke", self._on_toggle_juke)
            self.view.connect("toggle_save", self._on_toggle_save)
            self.view.handle_catch_all = self._catch_all  # type: ignore[assignment]

            self.detail_view = TmxDetailView(self)
            self.detail_view.connect("_crumb__tmx", self._on_crumb_back_to_list)
            self.detail_view.handle_catch_all = self._catch_all  # type: ignore[assignment]

            self.filters_view = TmxFiltersView(self)
            self.filters_view.connect("_crumb__tmx", self._on_filters_close)
            self.filters_view.connect("filters_apply", self._on_filters_apply)
            self.filters_view.connect("filters_reset", self._on_filters_reset)
            self.filters_view.connect("filters_close", self._on_filters_close)
            self.filters_view.handle_catch_all = self._catch_all  # type: ignore[assignment]

            self.policy_view = TmxPolicyView(self)
            self.policy_view.connect("_crumb__tmx", self._on_policy_close)
            self.policy_view.connect("policy_save", self._on_policy_save)
            self.policy_view.connect("policy_reset", self._on_policy_reset)
            self.policy_view.connect("policy_close", self._on_policy_close)
            self.policy_view.connect("policy_tagmode__req", self._on_policy_tagmode_req)
            self.policy_view.connect("policy_tagmode__blk", self._on_policy_tagmode_blk)
            self.policy_view.handle_catch_all = self._catch_all  # type: ignore[assignment]
        except Exception:
            logger.exception("tmx_browser: view init failed")
            return

        # Lazily warm the tag cache on first start (best-effort).
        asyncio.ensure_future(self._warm_tag_cache())
        await self._register_with_hub()

    async def on_stop(self) -> None:
        for v in (self.view, self.detail_view, self.filters_view, self.policy_view):
            if v is not None:
                try:
                    await v.destroy()
                except Exception:
                    logger.exception("tmx_browser: destroy failed")
        self.view = None
        self.detail_view = None
        self.filters_view = None
        self.policy_view = None

    async def _warm_tag_cache(self) -> None:
        try:
            self._tags_cache = await tmx_tags(self._game())
        except Exception:
            logger.exception("tmx_browser: tag cache warm failed")
            self._tags_cache = []

    # ---- hub -----------------------------------------------------------

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("tmx_browser: tmsm_hub:register signal not registered yet")
            return
        entry = HubAppEntry(
            key="tmx_browser",
            name="Trackmania Exchange",
            icon="globe",
            icon_image="https://images.mania.exchange/logos/tmx/square_sm.png",
            color="15f",
            role=Role.OPERATOR,
            order=55,
            description="Browse and add maps from trackmania.exchange.",
            open=self._open,
            command="tmx",
        )
        await sig.send_robust({"entry": entry}, raw=True)

    async def _open(self, player) -> None:
        if self.view is None:
            return
        st = self._state.setdefault(player.login, self._default_state())
        first_open = not st["results"] and not st["loaded"]
        try:
            await self.view.display(player_logins=[player.login])
            self.view._visible = True
        except Exception:
            logger.exception("tmx_browser: open failed")
            return
        if first_open:
            asyncio.ensure_future(self._load_current(player))

    # ---- per-player state ---------------------------------------------

    def _default_filters(self) -> dict[str, Any]:
        return {
            "author":       "",
            "environment":  None,
            "vehicle":      None,
            "maptype":      "",
            "mood":         None,
            "difficulty":   None,
            "routes":       None,
            "tags":         [],
            "collection":   None,
            "length_min_s": None,
            "length_max_s": None,
            "order1":       ORDER_RECENT,
            "order2":       None,
        }

    def _default_state(self) -> dict[str, Any]:
        return {
            "query":        "",
            "page":         1,
            "cursors":      [None],
            "results":      [],
            "more":         False,
            "busy":         False,
            "loaded":       False,
            "selected_id":  None,
            "juke_after":   True,
            "save_match":   True,
            "status":       "",
            "status_color": "aaa",
            "detail":       None,
            "col_scroll":   0,
            # filter window state
            "filters":       self._default_filters(),
            "filters_draft": self._default_filters(),
            "draft_length_min_text": "",
            "draft_length_max_text": "",
            "open_combo":   None,   # which combo dropdown is open in filter window
        }

    def _site_label(self) -> str:
        game = self._game()
        base, _ = site_for(game)
        host = base.split("//", 1)[-1]
        return host

    def _game(self) -> str:
        try:
            return str(self.instance.game.game or "tmnext")
        except Exception:
            return "tmnext"

    # ---- context builders ---------------------------------------------

    def _maptype_options(self) -> list[tuple[str, str]]:
        return MAPTYPES_TMNEXT if self._game() == "tmnext" else []

    def _enum_options(self, mapping: dict[int, str]) -> list[tuple[Any, str]]:
        return [(k, v) for k, v in sorted(mapping.items())]

    def _env_options(self) -> list[tuple[Any, str]]:
        return self._enum_options(_ENVIRONMENTS.get(self._game(), {}))

    def _vehicle_options(self) -> list[tuple[Any, str]]:
        return self._enum_options(_VEHICLES.get(self._game(), {}))

    def _collection_options(self) -> list[tuple[Any, str]]:
        return [
            ("beta",          "Beta maps"),
            ("featured",      "Featured maps"),
            ("supporter",     "Supporter maps"),
            ("collaborative", "Collaborative maps"),
            ("totd",          "Track of the day"),
        ]

    def _filter_summary(self, f: dict[str, Any]) -> int:
        """Count of non-default filter slots, for the badge on the filter button."""
        n = 0
        for k in ("author", "maptype"):
            if (f.get(k) or "").strip():
                n += 1
        for k in ("environment", "vehicle", "mood", "difficulty", "routes",
                  "collection", "length_min_s", "length_max_s"):
            if f.get(k) not in (None, ""):
                n += 1
        if f.get("tags"):
            n += 1
        if f.get("order1") not in (None, ORDER_RECENT):
            n += 1
        if f.get("order2") is not None:
            n += 1
        return n

    async def _is_admin(self, login: str) -> bool:
        """True if the player has the policy-edit permission."""
        try:
            player = await self.instance.player_manager.get_player(login=login)
        except Exception:
            return False
        try:
            return bool(
                await self.instance.permission_manager.has_permission(
                    player, "tmsm_tmx_browser:policy",
                )
            )
        except Exception:
            return False

    def _policy_active(self) -> bool:
        p = self._policy or {}
        return bool(
            p.get("locked") or p.get("hidden")
            or p.get("length_min_s_floor") or p.get("length_max_s_cap")
            or p.get("tags_required_any") or p.get("tags_blocked")
        )

    async def view_context(self, login: str) -> dict[str, Any]:
        st = self._state.setdefault(login, self._default_state())
        sel = st.get("selected_id")
        # selected row index (for highlighting in the data table)
        sel_idx = -1
        if sel is not None:
            for i, r in enumerate(st["results"]):
                if int(r.get("track_id", 0)) == int(sel):
                    sel_idx = i
                    break

        # ---- horizontal column packing ---------------------------------
        # First column ("Name") is sticky; the remainder is paged via
        # ``col_scroll``. We pack from ``col_scroll`` forward into the
        # remaining width budget, always rendering at least one scrolled
        # column so the user can never get stuck on an empty view.
        sticky = ALL_COLS[0]
        scrollables = ALL_COLS[1:]
        col_scroll = max(0, min(int(st.get("col_scroll") or 0),
                                max(0, len(scrollables) - 1)))
        st["col_scroll"] = col_scroll
        budget = TABLE_VIEW_W - float(sticky["w"])
        visible_rest: list[dict[str, Any]] = []
        used = 0.0
        for c in scrollables[col_scroll:]:
            if visible_rest and used + float(c["w"]) > budget:
                break
            visible_rest.append(c)
            used += float(c["w"])
        if not visible_rest and scrollables:
            visible_rest = [scrollables[col_scroll]]
        columns = [sticky, *visible_rest]
        cols_total = len(scrollables)
        cols_visible = len(visible_rest)
        col_can_prev = col_scroll > 0
        col_can_next = (col_scroll + cols_visible) < cols_total
        st["col_scroll_step"] = max(1, cols_visible)

        return {
            "site_label":    self._site_label(),
            "query":         st["query"],
            "page":          st["page"],
            "results":       list(st["results"]),
            "more":          bool(st["more"]),
            "busy":          bool(st["busy"]),
            "loaded":        bool(st["loaded"]),
            "selected_idx":  sel_idx,
            "selected_id":   sel,
            "filter_count":  self._filter_summary(st["filters"]),
            "juke_after":    bool(st["juke_after"]),
            "save_match":    bool(st["save_match"]),
            "status":        st["status"],
            "status_color":  st["status_color"],
            "columns":       columns,
            "col_scroll":    col_scroll,
            "cols_total":    cols_total,
            "cols_visible":  cols_visible,
            "col_can_prev":  col_can_prev,
            "col_can_next":  col_can_next,
            "is_admin":      await self._is_admin(login),
            "policy_active": self._policy_active(),
        }

    async def detail_context(self, login: str) -> dict[str, Any]:
        st = self._state.setdefault(login, self._default_state())
        m = st.get("detail") or {}
        desc_lines = flow_description(m.get("comments") or "")
        thumb_url = ""
        tid = m.get("track_id")
        if tid and m.get("has_thumbnail", True):
            try:
                thumb_url = tmx_thumbnail_url(self._game(), int(tid))
            except Exception:
                thumb_url = ""
        return {
            "site_label":   self._site_label(),
            "game":         self._game(),
            "map":          m,
            "thumb_url":    thumb_url,
            "desc_lines":   desc_lines,
            "status":       st["status"],
            "status_color": st["status_color"],
            "busy":         False,
        }

    async def filters_context(self, login: str) -> dict[str, Any]:
        st = self._state.setdefault(login, self._default_state())
        d = st["filters_draft"]
        # tag chips for currently selected ids
        sel_tag_ids = set(int(t) for t in d.get("tags") or [])
        tag_chips = []
        for t in self._tags_cache:
            if int(t["id"]) in sel_tag_ids:
                tag_chips.append({"value": t["id"], "label": t["name"]})
        pol = self._policy or {}
        return {
            "site_label":   self._site_label(),
            "draft":        d,
            "draft_length_min_text": st.get("draft_length_min_text", ""),
            "draft_length_max_text": st.get("draft_length_max_text", ""),
            "open_combo":   st.get("open_combo"),
            "tags_all":     list(self._tags_cache),
            "tag_chips":    tag_chips,
            "env_opts":     self._env_options(),
            "vehicle_opts": self._vehicle_options(),
            "maptype_opts": self._maptype_options(),
            "mood_opts":    self._enum_options(_MOODS),
            "diff_opts":    self._enum_options(_DIFFICULTIES),
            "route_opts":   self._enum_options(_ROUTES),
            "collection_opts": self._collection_options(),
            "order_opts":   ORDERS,
            "status":       st["status"],
            "status_color": st["status_color"],
            "policy_locked": dict(pol.get("locked") or {}),
            "policy_hidden": list(pol.get("hidden") or []),
            "policy_min_text": _fmt_length_seconds(pol.get("length_min_s_floor")),
            "policy_max_text": _fmt_length_seconds(pol.get("length_max_s_cap")),
            "policy_req_tag_ids":     list(pol.get("tags_required_any") or []),
            "policy_blocked_tag_ids": list(pol.get("tags_blocked") or []),
        }

    def _set_status(self, login: str, text: str, color: str = "aaa") -> None:
        st = self._state.setdefault(login, self._default_state())
        st["status"] = text
        st["status_color"] = color

    async def _notify(self, login: str, message: str,
                      severity: str = "info", duration_ms: int = 4000) -> None:
        """Fire a transient toast via tmsm_status (no-op if not loaded)."""
        try:
            sig = self.context.signals.get_signal("tmsm_status:notify")
        except KeyError:
            return
        try:
            await sig.send_robust({
                "message":     message,
                "severity":    severity,
                "login":       login,
                "duration_ms": duration_ms,
                "source":      "tmx_browser",
            }, raw=True)
        except Exception:
            logger.exception("tmx_browser: notify failed")

    async def _refresh_views(self) -> None:
        for v in (self.view, self.detail_view, self.filters_view, self.policy_view):
            if v is None:
                continue
            try:
                if getattr(v, "_visible", False):
                    await v.refresh()
            except Exception:
                logger.exception("tmx_browser: refresh failed")

    # ---- catch-all router ---------------------------------------------

    async def _catch_all(self, player, action, values, **kwargs) -> None:
        login = player.login
        self._absorb(login, values)

        # table row selection
        m = re.match(r"^tmxtable__row__(\d+)$", action)
        if m:
            await self._on_row_select(player, int(m.group(1)))
            return
        if re.match(r"^tmxtable__sort__", action):
            return  # sorting not implemented (API drives order)

        # horizontal column paging
        if action in ("cols_prev", "cols_next"):
            await self._on_cols_scroll(player, action == "cols_next")
            return

        # filter combo toggles + picks
        m = re.match(r"^fcombo_([a-z0-9_]+)__toggle$", action)
        if m:
            await self._on_combo_toggle(player, m.group(1))
            return
        m = re.match(r"^fcombo_([a-z0-9_]+)__pick__(.+)$", action)
        if m:
            await self._on_combo_pick(player, m.group(1), m.group(2))
            return

        # tag picker (multi-select toggle)
        m = re.match(r"^ftag__(\d+)$", action)
        if m:
            await self._on_tag_toggle(player, int(m.group(1)))
            return
        m = re.match(r"^ftagchip__remove__(\d+)$", action)
        if m:
            await self._on_tag_toggle(player, int(m.group(1)))
            return

        # ---- policy editor actions ------------------------------------
        m = re.match(r"^pcombo_([a-z0-9_]+)__toggle$", action)
        if m:
            await self._on_policy_combo_toggle(player, m.group(1))
            return
        m = re.match(r"^pcombo_([a-z0-9_]+)__pick__(.+)$", action)
        if m:
            await self._on_policy_combo_pick(player, m.group(1), m.group(2))
            return
        m = re.match(r"^plock__([a-z0-9_]+)$", action)
        if m:
            await self._on_policy_lock_toggle(player, m.group(1))
            return
        m = re.match(r"^phide__([a-z0-9_]+)$", action)
        if m:
            await self._on_policy_hide_toggle(player, m.group(1))
            return
        m = re.match(r"^ptag__(\d+)$", action)
        if m:
            await self._on_policy_tag_toggle(player, int(m.group(1)))
            return
        m = re.match(r"^ptagchip__remove__(\d+)$", action)
        if m:
            await self._on_policy_tag_toggle(player, int(m.group(1)))
            return
        m = re.match(r"^policy_tabs__tab__([a-z]+)$", action)
        if m:
            await self._on_policy_tab(player, m.group(1))
            return

        if action in ("_close",) or action.startswith("_crumb__"):
            return
        logger.debug("tmx_browser: unmatched action %s", action)

    def _absorb(self, login: str, values) -> None:
        """Soak up edited line_edit / search_input values from any view."""
        if not values:
            return
        st = self._state.setdefault(login, self._default_state())
        # Main view: search box
        if self.view is not None:
            qkey = f"entry_{self.view.id}__query"
            if qkey in values:
                st["query"] = str(values[qkey] or "").strip()
        # Filter view inputs
        if self.filters_view is not None:
            fv = self.filters_view
            akey = f"entry_{fv.id}__fauthor"
            if akey in values:
                st["filters_draft"]["author"] = str(values[akey] or "").strip()
            mkey = f"entry_{fv.id}__fmaptype"
            if mkey in values:
                st["filters_draft"]["maptype"] = str(values[mkey] or "").strip()
            lmin = f"entry_{fv.id}__flmin"
            if lmin in values:
                st["draft_length_min_text"] = str(values[lmin] or "").strip()
            lmax = f"entry_{fv.id}__flmax"
            if lmax in values:
                st["draft_length_max_text"] = str(values[lmax] or "").strip()
        # Policy view inputs (admin only - draft keyed by login)
        if self.policy_view is not None and login in self._policy_drafts:
            pv = self.policy_view
            d = self._policy_drafts[login]
            akey = f"entry_{pv.id}__pauthor"
            if akey in values:
                v = str(values[akey] or "").strip()
                d["author"] = v
                if "author" in (d.get("locked") or {}):
                    d["locked"]["author"] = v
            mkey = f"entry_{pv.id}__pmaptype"
            if mkey in values:
                v = str(values[mkey] or "").strip()
                d["maptype"] = v
                if "maptype" in (d.get("locked") or {}):
                    d["locked"]["maptype"] = v
            lmin = f"entry_{pv.id}__plmin"
            if lmin in values:
                d["min_text"] = str(values[lmin] or "").strip()
            lmax = f"entry_{pv.id}__plmax"
            if lmax in values:
                d["max_text"] = str(values[lmax] or "").strip()

    # ---- main view handlers --------------------------------------------

    async def _on_search(self, player, values=None) -> None:
        self._absorb(player.login, values)
        st = self._state.setdefault(player.login, self._default_state())
        st["page"] = 1
        st["cursors"] = [None]
        st["selected_id"] = None
        await self._load_current(player)

    async def _on_clear_query(self, player) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        st["query"] = ""
        st["page"] = 1
        st["cursors"] = [None]
        st["selected_id"] = None
        await self._load_current(player)

    async def _on_prev(self, player) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        if st["page"] > 1:
            st["page"] -= 1
            st["selected_id"] = None
            await self._load_current(player)

    async def _on_next(self, player) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        results = st.get("results") or []
        if not results:
            return
        next_page = st["page"] + 1
        if len(st["cursors"]) < next_page:
            st["cursors"].append(int(results[-1].get("track_id") or 0))
        st["page"] = next_page
        st["selected_id"] = None
        await self._load_current(player)

    async def _on_refresh(self, player) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        st["page"] = 1
        st["cursors"] = [None]
        st["selected_id"] = None
        await self._load_current(player)

    async def _on_toggle_juke(self, player) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        st["juke_after"] = not st["juke_after"]
        if self.view is not None:
            await self.view.refresh()

    async def _on_toggle_save(self, player) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        st["save_match"] = not st["save_match"]
        if self.view is not None:
            await self.view.refresh()

    async def _on_row_select(self, player, idx: int) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        if 0 <= idx < len(st["results"]):
            row = st["results"][idx]
            st["selected_id"] = int(row.get("track_id") or 0) or None
            if self.view is not None:
                await self.view.refresh()

    async def _on_cols_scroll(self, player, forward: bool) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        scrollables = len(ALL_COLS) - 1
        # Page by the current visible count so each click advances by a full
        # screen of scrollable columns (sticky "Name" excluded).
        step = max(1, int(st.get("col_scroll_step", 1)))
        cur = int(st.get("col_scroll") or 0)
        if forward:
            cur = min(scrollables - 1, cur + step)
        else:
            cur = max(0, cur - step)
        st["col_scroll"] = cur
        if self.view is not None:
            await self.view.refresh()

    async def _on_info_selected(self, player) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        sel = st.get("selected_id")
        if sel is None:
            await self._notify(player.login, "Select a map first", "warning", 3000)
            return
        await self._on_show_details(player, int(sel))

    async def _on_add_selected(self, player) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        sel = st.get("selected_id")
        if sel is None:
            await self._notify(player.login, "Select a map first", "warning", 3000)
            return
        await self._on_add(player, int(sel))

    # ---- details sub-window -------------------------------------------

    async def _on_show_details(self, player, track_id: int) -> None:
        if self.detail_view is None:
            return
        login = player.login
        st = self._state.setdefault(login, self._default_state())
        row = next(
            (r for r in st["results"] if int(r.get("track_id", 0)) == track_id),
            None,
        )
        if row is None:
            self._set_status(login, "details unavailable (refresh first)", "fa0")
            if self.view is not None:
                await self.view.refresh()
            return
        st["detail"] = dict(row)
        if self.view is not None:
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.view, player_logins=[login])
                self.view._visible = False
            except Exception:
                logger.exception("tmx_browser: hide list failed")
        try:
            await self.detail_view.display(player_logins=[login])
            self.detail_view._visible = True
        except Exception:
            logger.exception("tmx_browser: open details failed")

    async def _on_crumb_back_to_list(self, player, **_) -> None:
        login = player.login
        if self.detail_view is not None:
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.detail_view, player_logins=[login])
                self.detail_view._visible = False
            except Exception:
                logger.exception("tmx_browser: hide details failed")
        await self._open(player)

    # ---- filter window -------------------------------------------------

    async def _on_open_filters(self, player, values=None) -> None:
        if self.filters_view is None:
            return
        self._absorb(player.login, values)
        login = player.login
        st = self._state.setdefault(login, self._default_state())
        # Seed draft from the currently committed filters.
        st["filters_draft"] = dict(st["filters"])
        st["filters_draft"]["tags"] = list(st["filters"].get("tags") or [])
        st["draft_length_min_text"] = _fmt_length_seconds(
            st["filters"].get("length_min_s")
        )
        st["draft_length_max_text"] = _fmt_length_seconds(
            st["filters"].get("length_max_s")
        )
        st["open_combo"] = None
        # Make sure tag cache is populated (best-effort retry).
        if not self._tags_cache:
            try:
                self._tags_cache = await tmx_tags(self._game())
            except Exception:
                pass
        # Hide main view while filter sub-window is open.
        if self.view is not None:
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.view, player_logins=[login])
                self.view._visible = False
            except Exception:
                logger.exception("tmx_browser: hide list failed")
        try:
            await self.filters_view.display(player_logins=[login])
            self.filters_view._visible = True
        except Exception:
            logger.exception("tmx_browser: open filters failed")

    async def _on_filters_close(self, player, **_) -> None:
        login = player.login
        if self.filters_view is not None:
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.filters_view, player_logins=[login])
                self.filters_view._visible = False
            except Exception:
                logger.exception("tmx_browser: hide filters failed")
        await self._open(player)

    async def _on_filters_reset(self, player, values=None) -> None:
        login = player.login
        st = self._state.setdefault(login, self._default_state())
        st["filters_draft"] = self._default_filters()
        st["draft_length_min_text"] = ""
        st["draft_length_max_text"] = ""
        st["open_combo"] = None
        if self.filters_view is not None:
            await self.filters_view.refresh()

    async def _on_filters_apply(self, player, values=None) -> None:
        login = player.login
        self._absorb(login, values)
        st = self._state.setdefault(login, self._default_state())
        d = dict(st["filters_draft"])
        d["tags"] = list(st["filters_draft"].get("tags") or [])
        # Parse length text now (Apply == commit).
        d["length_min_s"] = _parse_length_seconds(st.get("draft_length_min_text", ""))
        d["length_max_s"] = _parse_length_seconds(st.get("draft_length_max_text", ""))
        st["filters"] = d
        st["page"] = 1
        st["cursors"] = [None]
        st["selected_id"] = None
        st["open_combo"] = None
        # Close the filter sub-window and reload the main view.
        if self.filters_view is not None:
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.filters_view, player_logins=[login])
                self.filters_view._visible = False
            except Exception:
                logger.exception("tmx_browser: hide filters failed")
        if self.view is not None:
            try:
                await self.view.display(player_logins=[login])
                self.view._visible = True
            except Exception:
                logger.exception("tmx_browser: reopen list failed")
        await self._load_current(player)

    async def _on_combo_toggle(self, player, name: str) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        st["open_combo"] = None if st.get("open_combo") == name else name
        if self.filters_view is not None:
            await self.filters_view.refresh()

    async def _on_combo_pick(self, player, name: str, raw_val: str) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        d = st["filters_draft"]
        # "any" sentinel clears the filter slot.
        if raw_val in ("any", "_any"):
            d[name] = "" if name in ("maptype",) else None
        elif name in ("environment", "vehicle", "mood", "difficulty",
                      "routes", "order1", "order2"):
            try:
                val: Any = int(raw_val)
            except ValueError:
                val = None
            d[name] = val
        elif name == "collection":
            d[name] = raw_val if raw_val in COLLECTIONS else None
        else:
            d[name] = raw_val
        st["open_combo"] = None
        if self.filters_view is not None:
            await self.filters_view.refresh()

    async def _on_tag_toggle(self, player, tag_id: int) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        tags = list(st["filters_draft"].get("tags") or [])
        if tag_id in tags:
            tags.remove(tag_id)
        else:
            tags.append(tag_id)
        st["filters_draft"]["tags"] = tags
        if self.filters_view is not None:
            await self.filters_view.refresh()

    # ---- admin policy sub-window --------------------------------------

    def _policy_default_draft(self) -> dict[str, Any]:
        # Snapshot the active policy into a draft (deep-copied so the editor
        # never mutates the live policy until Save is pressed).
        p = self._policy or _policy.default_policy()
        return {
            "locked":   dict(p.get("locked") or {}),
            "hidden":   list(p.get("hidden") or []),
            "length_min_s_floor": p.get("length_min_s_floor"),
            "length_max_s_cap":   p.get("length_max_s_cap"),
            "tags_required_any":  list(p.get("tags_required_any") or []),
            "tags_blocked":       list(p.get("tags_blocked") or []),
            # textual length fields for editing
            "min_text": _fmt_length_seconds(p.get("length_min_s_floor")),
            "max_text": _fmt_length_seconds(p.get("length_max_s_cap")),
            "open_combo": None,
            # current tag-list editor mode ("req" required-any | "blk" blocked)
            "tag_mode": "req",
            # currently visible tab in the editor ("filters" | "length" | "tags")
            "tab": "filters",
        }

    async def _on_open_policy(self, player, values=None) -> None:
        if self.policy_view is None:
            return
        login = player.login
        if not await self._is_admin(login):
            await self._notify(login, "tmsm_tmx_browser:policy required",
                               "warning", 4000)
            return
        self._absorb(login, values)
        self._policy_drafts[login] = self._policy_default_draft()
        # Best-effort tag-cache warm.
        if not self._tags_cache:
            try:
                self._tags_cache = await tmx_tags(self._game())
            except Exception:
                pass
        if self.view is not None:
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.view, player_logins=[login])
                self.view._visible = False
            except Exception:
                logger.exception("tmx_browser: hide list failed")
        try:
            await self.policy_view.display(player_logins=[login])
            self.policy_view._visible = True
        except Exception:
            logger.exception("tmx_browser: open policy failed")

    async def _on_policy_close(self, player, **_) -> None:
        login = player.login
        self._policy_drafts.pop(login, None)
        if self.policy_view is not None:
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.policy_view, player_logins=[login])
                self.policy_view._visible = False
            except Exception:
                logger.exception("tmx_browser: hide policy failed")
        await self._open(player)

    async def _on_policy_reset(self, player, values=None) -> None:
        login = player.login
        if not await self._is_admin(login):
            return
        self._absorb(login, values)
        # Reset draft to a *blank* policy (not the live one).
        blank = _policy.default_policy()
        self._policy_drafts[login] = {
            "locked":   {},
            "hidden":   [],
            "length_min_s_floor": None,
            "length_max_s_cap":   None,
            "tags_required_any":  [],
            "tags_blocked":       [],
            "min_text": "",
            "max_text": "",
            "open_combo": None,
            "tag_mode": "req",
            "tab": "filters",
        }
        if self.policy_view is not None:
            await self.policy_view.refresh()

    async def _on_policy_save(self, player, values=None) -> None:
        login = player.login
        if not await self._is_admin(login):
            await self._notify(login, "tmsm_tmx_browser:policy required",
                               "warning", 4000)
            return
        self._absorb(login, values)
        d = self._policy_drafts.get(login)
        if d is None:
            return
        # Parse the length-clamp text fields at save time.
        floor = _parse_length_seconds(d.get("min_text") or "")
        cap   = _parse_length_seconds(d.get("max_text") or "")
        new_policy = {
            "version":            1,
            "locked":             dict(d.get("locked") or {}),
            "hidden":             list(d.get("hidden") or []),
            "length_min_s_floor": floor,
            "length_max_s_cap":   cap,
            "tags_required_any":  [int(x) for x in d.get("tags_required_any") or []],
            "tags_blocked":       [int(x) for x in d.get("tags_blocked") or []],
        }
        self._policy = new_policy
        try:
            _policy.save(new_policy)
        except Exception:
            logger.exception("tmx_browser: policy save failed")
            await self._notify(login, "Policy save failed", "error", 6000)
            return
        await self._notify(login, "Policy saved", "success", 3000)
        self._policy_drafts.pop(login, None)
        await self._on_policy_close(player)

    async def _on_policy_tagmode_req(self, player) -> None:
        d = self._policy_drafts.get(player.login)
        if d is None:
            return
        d["tag_mode"] = "req"
        if self.policy_view is not None:
            await self.policy_view.refresh()

    async def _on_policy_tagmode_blk(self, player) -> None:
        d = self._policy_drafts.get(player.login)
        if d is None:
            return
        d["tag_mode"] = "blk"
        if self.policy_view is not None:
            await self.policy_view.refresh()

    async def _on_policy_lock_toggle(self, player, key: str) -> None:
        """Toggle "lock current draft value" for a single filter key."""
        d = self._policy_drafts.get(player.login)
        if d is None or key not in _policy.LOCKABLE_KEYS:
            return
        locked = dict(d.get("locked") or {})
        if key in locked:
            locked.pop(key)
        else:
            # Lock to the draft's current value (None / "" -> stored as-is
            # so the policy explicitly forces "no filter").
            locked[key] = d.get(key)
        d["locked"] = locked
        if self.policy_view is not None:
            await self.policy_view.refresh()

    async def _on_policy_hide_toggle(self, player, key: str) -> None:
        d = self._policy_drafts.get(player.login)
        if d is None or key not in _policy.HIDEABLE_KEYS:
            return
        hidden = list(d.get("hidden") or [])
        if key in hidden:
            hidden.remove(key)
        else:
            hidden.append(key)
        d["hidden"] = hidden
        if self.policy_view is not None:
            await self.policy_view.refresh()

    async def _on_policy_combo_toggle(self, player, name: str) -> None:
        d = self._policy_drafts.get(player.login)
        if d is None:
            return
        d["open_combo"] = None if d.get("open_combo") == name else name
        if self.policy_view is not None:
            await self.policy_view.refresh()

    async def _on_policy_combo_pick(self, player, name: str, raw_val: str) -> None:
        d = self._policy_drafts.get(player.login)
        if d is None:
            return
        if raw_val in ("any", "_any"):
            val: Any = "" if name in ("maptype",) else None
        elif name in ("environment", "vehicle", "mood", "difficulty", "routes"):
            try:
                val = int(raw_val)
            except ValueError:
                val = None
        elif name == "collection":
            val = raw_val if raw_val in COLLECTIONS else None
        else:
            val = raw_val
        d[name] = val
        # If this slot is currently locked, update the locked value too.
        if name in (d.get("locked") or {}):
            d["locked"][name] = val
        d["open_combo"] = None
        if self.policy_view is not None:
            await self.policy_view.refresh()

    async def _on_policy_tag_toggle(self, player, tag_id: int) -> None:
        d = self._policy_drafts.get(player.login)
        if d is None:
            return
        key = "tags_required_any" if d.get("tag_mode", "req") == "req" else "tags_blocked"
        lst = [int(x) for x in d.get(key) or []]
        if tag_id in lst:
            lst.remove(tag_id)
        else:
            lst.append(tag_id)
        d[key] = lst
        if self.policy_view is not None:
            await self.policy_view.refresh()

    async def _on_policy_tab(self, player, tab: str) -> None:
        d = self._policy_drafts.get(player.login)
        if d is None or tab not in ("filters", "length", "tags"):
            return
        d["tab"] = tab
        d["open_combo"] = None
        if self.policy_view is not None:
            await self.policy_view.refresh()

    async def policy_context(self, login: str) -> dict[str, Any]:
        d = self._policy_drafts.setdefault(login, self._policy_default_draft())
        req_ids = set(int(x) for x in d.get("tags_required_any") or [])
        blk_ids = set(int(x) for x in d.get("tags_blocked") or [])
        req_chips = [{"value": t["id"], "label": t["name"]}
                     for t in self._tags_cache if int(t["id"]) in req_ids]
        blk_chips = [{"value": t["id"], "label": t["name"]}
                     for t in self._tags_cache if int(t["id"]) in blk_ids]
        return {
            "site_label":   self._site_label(),
            "draft":        d,
            "draft_length_min_text": d.get("min_text", ""),
            "draft_length_max_text": d.get("max_text", ""),
            "locked":       dict(d.get("locked") or {}),
            "hidden":       list(d.get("hidden") or []),
            "open_combo":   d.get("open_combo"),
            "tags_all":     list(self._tags_cache),
            "tags_req_chips": req_chips,
            "tags_blk_chips": blk_chips,
            "tag_mode":     d.get("tag_mode", "req"),
            "env_opts":     self._env_options(),
            "vehicle_opts": self._vehicle_options(),
            "maptype_opts": self._maptype_options(),
            "mood_opts":    self._enum_options(_MOODS),
            "diff_opts":    self._enum_options(_DIFFICULTIES),
            "route_opts":   self._enum_options(_ROUTES),
            "collection_opts": self._collection_options(),
            "status":       "",
            "status_color": "aaa",
            "tab":          d.get("tab", "filters"),
        }

    # ---- TMX fetch -----------------------------------------------------

    async def _load_current(self, player) -> None:
        login = player.login
        st = self._state.setdefault(login, self._default_state())
        st["busy"] = True
        self._set_status(login, "loading...", "fc4")
        if self.view is not None:
            try:
                await self.view.refresh()
            except Exception:
                logger.exception("tmx_browser: pre-load refresh failed")

        idx = st["page"] - 1
        cursor = st["cursors"][idx] if 0 <= idx < len(st["cursors"]) else None
        # Operator filters get the server-wide admin policy stacked on top
        # (locks override values, length clamps applied, tag white/blocklist
        # enforced) so any value the operator might smuggle in via state is
        # neutralised before it reaches the API.
        f = _policy.apply_to_filters(st["filters"], self._policy)
        kwargs: dict[str, Any] = {
            "author":        f.get("author") or "",
            "environment":   f.get("environment"),
            "vehicle":       f.get("vehicle"),
            "maptype":       f.get("maptype") or "",
            "mood":          f.get("mood"),
            "difficulty":    f.get("difficulty"),
            "routes":        f.get("routes"),
            "tags":          list(f.get("tags") or []),
            "collection":    f.get("collection"),
            "length_min_ms": (f.get("length_min_s") or 0) * 1000 or None,
            "length_max_ms": (f.get("length_max_s") or 0) * 1000 or None,
            "order2":        f.get("order2"),
        }
        order1 = f.get("order1")
        try:
            data = await tmx_search(
                self._game(),
                query=st["query"],
                after=cursor,
                limit=12,
                order=order1,
                **kwargs,
            )
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
            logger.warning("tmx_browser: load failed: %s", e)
            st["busy"] = False
            self._set_status(login, f"TMX error: {e}", "f44")
            if self.view is not None:
                await self.view.refresh()
            return

        st["results"] = data["results"]
        st["more"] = data["more"]
        st["busy"] = False
        st["loaded"] = True
        n = len(st["results"])
        if n == 0 and st["page"] > 1:
            st["page"] = 1
            st["cursors"] = [None]
            self._set_status(login, "no results - back to page 1", "fa0")
        elif n == 0:
            self._set_status(login, "no results", "fa0")
        else:
            # Clear any stale status (e.g. "loading...") once results are in.
            self._set_status(login, "", "aaa")
        if self.view is not None:
            await self.view.refresh()

    # ---- add to server -------------------------------------------------

    async def _on_add(self, player, track_id: int) -> None:
        login = player.login
        st = self._state.setdefault(login, self._default_state())
        row = next(
            (r for r in st["results"] if int(r.get("track_id", 0)) == track_id),
            None,
        )
        name = (row or {}).get("name", f"tmx_{track_id}")

        try:
            blob = await tmx_download(self._game(), track_id)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
            logger.warning("tmx_browser: download #%s failed: %s", track_id, e)
            await self._notify(login, f"Download error: {e}", "error", 6000)
            return
        if not blob:
            await self._notify(login,
                               f"Map #{track_id} is no longer on TMX (404)",
                               "warning", 6000)
            return

        ext = ".Map.Gbx" if self._game() == "tmnext" else ".Challenge.Gbx"
        filename = _safe_filename(name, track_id, ext)

        # PyPlanet's MapManager.upload_map is buggy on subdirectories
        # (it `touch`es ``'{MAP_FOLDER}{filename}'`` with no separator,
        # so it fails for any nested path). Write the file ourselves
        # through the storage driver, then ask the dedicated to add it.
        storage = self.instance.storage
        try:
            tmx_dir = f"{storage.MAP_FOLDER}/tmx"
            if not await storage.driver.exists(tmx_dir):
                await storage.driver.mkdir(tmx_dir)
            async with storage.open_map(filename, "wb+") as fw:
                await fw.write(blob)
        except Exception as e:
            logger.exception("tmx_browser: write map file failed")
            await self._notify(login, f"Write failed: {e}", "error", 6000)
            return

        try:
            uploaded = await self.instance.map_manager.add_map(
                filename, insert=False, save_matchsettings=False,
            )
        except Exception as e:
            logger.exception("tmx_browser: upload_map failed")
            await self._notify(login, f"Upload failed: {e}", "error", 6000)
            return

        if st["juke_after"]:
            try:
                await self.instance.map_manager.set_next_map(uploaded)
            except Exception:
                logger.exception("tmx_browser: set_next_map failed")

        if st["save_match"]:
            try:
                from pyplanet.conf import settings as _pp_settings
                setting = getattr(_pp_settings, "MAP_MATCHSETTINGS", None)
                if isinstance(setting, dict):
                    setting = (setting.get(self.instance.process_name)
                               or setting.get("default"))
                if not isinstance(setting, str) or not setting:
                    raise RuntimeError(
                        "MAP_MATCHSETTINGS not configured in settings/base.py"
                    )
                file_name = setting.format(
                    server_login=self.instance.game.server_player_login
                )
                file_path = f"MatchSettings/{file_name}"
                await self.instance.map_manager.save_matchsettings(file_path)
                await self.instance.map_manager.update_list(full_update=True)
                logger.info("tmx_browser: saved matchsettings to %s", file_path)
            except Exception as e:
                logger.exception("tmx_browser: save_matchsettings failed")
                await self._notify(login,
                                   f"Added but matchsettings save failed: {e}",
                                   "warning", 6000)
                return

        await self._notify(login, f"Added: {name}", "success", 4000)
