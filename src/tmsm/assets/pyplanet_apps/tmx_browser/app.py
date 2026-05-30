"""tmx_browser - hub-mounted, operator-only browser for Trackmania Exchange.

Detects the running game and talks to the matching TMX site:

* ``tmnext`` → ``trackmania.exchange`` (TM2020)
* ``tm``     → ``tm.mania-exchange.com`` (Maniaplanet TM2)
* ``sm``     → ``sm.mania-exchange.com`` (ShootMania)

Per-player draft state holds the active query, page and last result set. The
``Add`` action on a row downloads the map gbx bytes from TMX and hands them
to ``MapManager.upload_map`` so the file lands in the dedicated server's
``UserData/Maps/`` and is appended to the live playlist. Two checkboxes in
the footer control optional follow-up actions:

* ``Play next`` → call ``MapManager.set_next_map`` after upload.
* ``Save matchsettings`` → persist the updated playlist to disk.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
from typing import Any

import aiohttp

from pyplanet.apps.config import AppConfig

from .tmx import download as tmx_download
from .tmx import flow_description
from .tmx import search as tmx_search
from .tmx import site_for
from .views import TmxBrowserView, TmxDetailView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)


# Section key -> (display label, TMX-search kwargs). Order is render order.
SECTIONS: list[tuple[str, str, dict[str, Any]]] = [
    ("recent",  "Recent",  {"order": 2}),
    ("awarded", "Awarded", {"order": 4}),
    ("random",  "Random",  {"random": True}),
    ("search",  "Search",  {}),
]
SECTION_KEYS = [s[0] for s in SECTIONS]


def _safe_filename(name: str, track_id: int, ext: str) -> str:
    """Produce a filesystem-safe filename relative to the dedicated's Maps tree."""
    base = re.sub(r"\$[0-9a-fA-F]{3}", "", name or "")          # strip TMX $-codes
    base = re.sub(r"[^A-Za-z0-9._ \-]+", "_", base).strip()
    base = (base or "map")[:60]
    return f"tmx/{base}_#{int(track_id)}{ext}"


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
        # per-login draft state
        self._state: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    # ---- lifecycle -----------------------------------------------------

    async def on_start(self) -> None:
        try:
            self.view = TmxBrowserView(self)
            self.view.connect("search", self._on_search)
            self.view.connect("query__clear", self._on_clear_query)
            self.view.connect("prev", self._on_prev)
            self.view.connect("next", self._on_next)
            self.view.connect("refresh", self._on_refresh)
            self.view.connect("toggle_juke", self._on_toggle_juke)
            self.view.connect("toggle_save", self._on_toggle_save)
            self.view.handle_catch_all = self._catch_all  # type: ignore[assignment]

            self.detail_view = TmxDetailView(self)
            self.detail_view.connect("_crumb__tmx", self._on_crumb_back_to_list)
            self.detail_view.handle_catch_all = self._catch_all  # type: ignore[assignment]
        except Exception:
            logger.exception("tmx_browser: view init failed")
            return
        await self._register_with_hub()

    async def on_stop(self) -> None:
        for v in (self.view, self.detail_view):
            if v is not None:
                try:
                    await v.destroy()
                except Exception:
                    logger.exception("tmx_browser: destroy failed")
        self.view = None
        self.detail_view = None

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
        sec = st["sections"][st["section"]]
        first_open = not sec["results"] and not sec["loaded"]
        try:
            await self.view.display(player_logins=[player.login])
            self.view._visible = True
        except Exception:
            logger.exception("tmx_browser: open failed")
            return
        if first_open:
            # auto-load the default section so the user sees content immediately
            asyncio.ensure_future(self._load_current(player))

    # ---- per-player state ---------------------------------------------

    def _default_state(self) -> dict[str, Any]:
        return {
            "section": "recent",
            "query": "",
            "juke_after": True,
            "save_match": False,
            "status": "",
            "status_color": "aaa",
            # cached map dict shown in the details sub-window
            "detail": None,
            # per-section paging/results so flipping tabs feels instant
            "sections": {
                key: {"page": 1, "results": [], "more": False,
                      "busy": False, "loaded": False}
                for key in SECTION_KEYS
            },
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

    async def view_context(self, login: str) -> dict[str, Any]:
        st = self._state.setdefault(login, self._default_state())
        sec = st["sections"][st["section"]]
        return {
            "site_label":    self._site_label(),
            "sections":      [{"key": k, "label": lbl,
                               "selected": (k == st["section"])}
                              for k, lbl, _ in SECTIONS],
            "section":       st["section"],
            "is_search":     st["section"] == "search",
            "query":         st["query"],
            "page":          sec["page"],
            "results":       list(sec["results"]),
            "more":          bool(sec["more"]),
            "busy":          bool(sec["busy"]),
            "loaded":        bool(sec["loaded"]),
            "juke_after":    bool(st["juke_after"]),
            "save_match":    bool(st["save_match"]),
            "status":        st["status"],
            "status_color":  st["status_color"],
        }

    async def detail_context(self, login: str) -> dict[str, Any]:
        """Per-player context for the TmxDetailView sub-window."""
        st = self._state.setdefault(login, self._default_state())
        m = st.get("detail") or {}
        desc_lines = flow_description(m.get("comments") or "")
        return {
            "site_label":   self._site_label(),
            "game":         self._game(),
            "map":          m,
            "desc_lines":   desc_lines,
            "status":       st["status"],
            "status_color": st["status_color"],
            "busy":         False,
        }

    def _set_status(self, login: str, text: str, color: str = "aaa") -> None:
        st = self._state.setdefault(login, self._default_state())
        st["status"] = text
        st["status_color"] = color

    async def _refresh_views(self) -> None:
        """Refresh whichever of (list, detail) views is currently active."""
        for v in (self.view, self.detail_view):
            if v is None:
                continue
            try:
                if getattr(v, "_visible", False):
                    await v.refresh()
            except Exception:
                logger.exception("tmx_browser: refresh failed")

    # ---- handlers ------------------------------------------------------

    async def _catch_all(self, player, action, values, **kwargs) -> None:
        login = player.login
        self._absorb(login, values)

        m = re.match(r"^add__(\d+)$", action)
        if m:
            await self._on_add(player, int(m.group(1)))
            return

        m = re.match(r"^details__(\d+)$", action)
        if m:
            await self._on_show_details(player, int(m.group(1)))
            return

        m = re.match(r"^section__([a-z_]+)$", action)
        if m and m.group(1) in SECTION_KEYS:
            await self._on_section(player, m.group(1))
            return

        if action == "open_tmx":
            # no-op placeholder: deep-link not opened in-game; keep silent
            return

        if action in ("_close",) or action.startswith("_crumb__"):
            return
        logger.debug("tmx_browser: unmatched action %s", action)

    def _absorb(self, login: str, values) -> None:
        if not values or self.view is None:
            return
        st = self._state.setdefault(login, self._default_state())
        qkey = f"entry_{self.view.id}__query"
        if qkey in values:
            st["query"] = str(values[qkey] or "").strip()

    async def _on_search(self, player, values=None) -> None:
        self._absorb(player.login, values)
        st = self._state.setdefault(player.login, self._default_state())
        st["section"] = "search"
        st["sections"]["search"]["page"] = 1
        await self._load_current(player)

    async def _on_clear_query(self, player) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        st["query"] = ""
        if st["section"] == "search":
            st["sections"]["search"]["page"] = 1
            await self._load_current(player)
        elif self.view is not None:
            await self.view.refresh()

    async def _on_section(self, player, key: str) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        if st["section"] == key:
            return
        st["section"] = key
        sec = st["sections"][key]
        if not sec["loaded"] and key != "search":
            await self._load_current(player)
            return
        # search tab: don't fire a query on every tab flip
        if self.view is not None:
            await self.view.refresh()

    async def _on_prev(self, player) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        sec = st["sections"][st["section"]]
        if sec["page"] > 1:
            sec["page"] -= 1
            await self._load_current(player)

    async def _on_next(self, player) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        sec = st["sections"][st["section"]]
        if sec["more"]:
            sec["page"] += 1
            await self._load_current(player)

    async def _on_refresh(self, player) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        st["sections"][st["section"]]["page"] = 1
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

    # ---- details sub-window -------------------------------------------

    async def _on_show_details(self, player, track_id: int) -> None:
        """Open the detail sub-window for ``track_id`` (must be in the cache)."""
        if self.detail_view is None:
            return
        login = player.login
        st = self._state.setdefault(login, self._default_state())
        sec = st["sections"][st["section"]]
        row = next(
            (r for r in sec["results"] if int(r.get("track_id", 0)) == track_id),
            None,
        )
        if row is None:
            self._set_status(login, "details unavailable (refresh first)", "fa0")
            if self.view is not None:
                await self.view.refresh()
            return
        st["detail"] = dict(row)
        # hide the list view so the two never overlap
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
        """Breadcrumb back-nav from the detail sub-window."""
        login = player.login
        if self.detail_view is not None:
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.detail_view, player_logins=[login])
                self.detail_view._visible = False
            except Exception:
                logger.exception("tmx_browser: hide details failed")
        await self._open(player)

    # ---- TMX fetch -----------------------------------------------------

    def _section_kwargs(self, key: str) -> dict[str, Any]:
        for k, _lbl, kw in SECTIONS:
            if k == key:
                return dict(kw)
        return {}

    async def _load_current(self, player) -> None:
        login = player.login
        st = self._state.setdefault(login, self._default_state())
        key = st["section"]
        sec = st["sections"][key]
        sec["busy"] = True
        label = next((lbl for k, lbl, _ in SECTIONS if k == key), key)
        self._set_status(login, f"loading {label}...", "fc4")
        if self.view is not None:
            try:
                await self.view.refresh()
            except Exception:
                logger.exception("tmx_browser: pre-load refresh failed")

        kwargs = self._section_kwargs(key)
        # search tab uses the query box; curated tabs ignore it
        query = st["query"] if key == "search" else ""

        try:
            data = await tmx_search(
                self._game(), query=query, page=sec["page"], limit=12, **kwargs,
            )
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
            logger.warning("tmx_browser: %s load failed: %s", key, e)
            sec["busy"] = False
            self._set_status(login, f"TMX error: {e}", "f44")
            if self.view is not None:
                await self.view.refresh()
            return

        sec["results"] = data["results"]
        sec["more"] = data["more"]
        sec["busy"] = False
        sec["loaded"] = True
        n = len(sec["results"])
        if n == 0 and sec["page"] > 1:
            sec["page"] = 1
            self._set_status(login, "no results - back to page 1", "fa0")
        elif n == 0:
            self._set_status(login, f"{label}: no results", "fa0")
        else:
            self._set_status(login, f"{label}: {n} - page {sec['page']}", "0f8")
        if self.view is not None:
            await self.view.refresh()

    # ---- add to server -------------------------------------------------

    async def _on_add(self, player, track_id: int) -> None:
        login = player.login
        st = self._state.setdefault(login, self._default_state())
        # find the row in the currently displayed section
        sec = st["sections"][st["section"]]
        row = next(
            (r for r in sec["results"] if int(r.get("track_id", 0)) == track_id),
            None,
        )
        name = (row or {}).get("name", f"tmx_{track_id}")

        self._set_status(login, f"downloading #{track_id}...", "fc4")
        await self._refresh_views()

        try:
            blob = await tmx_download(self._game(), track_id)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
            logger.warning("tmx_browser: download #%s failed: %s", track_id, e)
            self._set_status(login, f"download error: {e}", "f44")
            await self._refresh_views()
            return
        if not blob:
            self._set_status(login, "map not on TMX anymore (404)", "fa0")
            await self._refresh_views()
            return

        ext = ".Map.Gbx" if self._game() == "tmnext" else ".Challenge.Gbx"
        filename = _safe_filename(name, track_id, ext)
        self._set_status(login, "uploading to server...", "fc4")
        await self._refresh_views()

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
            self._set_status(login, f"write failed: {e}", "f44")
            await self._refresh_views()
            return

        try:
            uploaded = await self.instance.map_manager.add_map(
                filename, insert=False, save_matchsettings=False,
            )
        except Exception as e:
            logger.exception("tmx_browser: upload_map failed")
            self._set_status(login, f"upload failed: {e}", "f44")
            await self._refresh_views()
            return

        if st["juke_after"]:
            try:
                await self.instance.map_manager.set_next_map(uploaded)
            except Exception:
                logger.exception("tmx_browser: set_next_map failed")

        if st["save_match"]:
            try:
                await self.instance.map_manager.save_matchsettings()
            except Exception:
                logger.exception("tmx_browser: save_matchsettings failed")

        self._set_status(login, f"added: {name}", "0f8")
        await self._refresh_views()
