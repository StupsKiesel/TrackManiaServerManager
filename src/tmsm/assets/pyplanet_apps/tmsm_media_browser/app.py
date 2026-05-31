"""tmsm_media_browser - master-admin tool to preview Manialink media assets.

Lists curated game-bundled images grouped by category, plus a custom-URL
tester. Clicking a thumbnail opens a detail window with a big preview and
a read-only entry box pre-filled with the URL so it can be copied to the
clipboard.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from pyplanet.apps.config import AppConfig

from . import catalog as _catalog
from .views import GridView, DetailView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)


class TmsmMediaBrowserApp(AppConfig):
    name = "pyplanet.apps.tmsm.tmsm_media_browser"
    label = "tmsm_media_browser"
    app_dependencies = ["core.maniaplanet"]
    game_dependencies = ["trackmania", "trackmania_next", "shootmania"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.grid: GridView | None = None
        self.detail: DetailView | None = None
        # Per-login UI state: which tab is active, current custom URL, and
        # which item is being previewed (label / url / note).
        self._state: dict[str, dict[str, Any]] = {}

    # ---- lifecycle -----------------------------------------------------

    async def on_start(self) -> None:
        try:
            self.grid = GridView(self)
            self.grid.connect("custom_preview", self._on_custom_preview)
            self.grid.handle_catch_all = self._catch_all  # type: ignore[assignment]

            self.detail = DetailView(self)
            self.detail.connect("_crumb__media", self._on_back_to_grid)
            self.detail.handle_catch_all = self._catch_all  # type: ignore[assignment]
        except Exception:
            logger.exception("media_browser: view init failed")
            return

        await self._register_with_hub()

    async def on_stop(self) -> None:
        for v in (self.grid, self.detail):
            if v is not None:
                try:
                    await v.destroy()
                except Exception:
                    logger.exception("media_browser: destroy failed")
        self.grid = None
        self.detail = None

    # ---- hub -----------------------------------------------------------

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("media_browser: tmsm_hub:register signal not registered yet")
            return
        entry = HubAppEntry(
            key="media_browser",
            name="Media browser",
            icon="image",
            color="9c4",
            role=Role.MASTER,
            order=70,
            description="Preview game-bundled images / icons usable in Manialinks.",
            open=self._open,
            command="media",
        )
        await sig.send_robust({"entry": entry}, raw=True)

    async def _open(self, player) -> None:
        if self.grid is None:
            return
        self._state.setdefault(player.login, self._default_state())
        try:
            await self.grid.display(player_logins=[player.login])
            self.grid._visible = True
            self.grid._visible_logins.add(player.login)
        except Exception:
            logger.exception("media_browser: open failed")

    def _default_state(self) -> dict[str, Any]:
        first = _catalog.CATALOG[0]["key"] if _catalog.CATALOG else None
        return {
            "active_cat": first,
            "page":       1,
            "custom_url": "",
            "preview":    None,  # {label, url, style, substyle, note}
        }

    # ---- contexts ------------------------------------------------------

    async def grid_context(self, login: str) -> dict[str, Any]:
        st = self._state.setdefault(login, self._default_state())
        categories = [{"key": c["key"], "label": c["label"]}
                      for c in _catalog.CATALOG]
        cat = _catalog.category(st["active_cat"]) if st["active_cat"] else None
        all_items = list(cat["items"]) if cat else []
        per_page = 20  # 5 cols x 4 rows
        total_pages = max(1, (len(all_items) + per_page - 1) // per_page)
        page = max(1, min(int(st.get("page", 1) or 1), total_pages))
        st["page"] = page
        start = (page - 1) * per_page
        items = all_items[start:start + per_page]
        return {
            "categories":  categories,
            "active_cat":  st["active_cat"],
            "items":       items,
            "page":        page,
            "total_pages": total_pages,
            "custom_url":  st["custom_url"],
        }

    async def detail_context(self, login: str) -> dict[str, Any]:
        st = self._state.setdefault(login, self._default_state())
        p = st.get("preview") or {}
        return {
            "label":    p.get("label", ""),
            "url":      p.get("url", ""),
            "style":    p.get("style", ""),
            "substyle": p.get("substyle", ""),
            "note":     p.get("note", ""),
        }

    # ---- actions -------------------------------------------------------

    async def _on_back_to_grid(self, player) -> None:
        if self.detail is not None:
            try:
                self.detail._visible_logins.discard(player.login)
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.detail, player_logins=[player.login])
            except Exception:
                logger.exception("media_browser: detail hide failed")
        if self.grid is not None:
            try:
                await self.grid.display(player_logins=[player.login])
                self.grid._visible = True
                self.grid._visible_logins.add(player.login)
            except Exception:
                logger.exception("media_browser: grid re-show failed")

    async def _on_custom_preview(self, player, values=None) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        url = ""
        if values:
            # `entry_<viewid>__custom_url` carries the form value.
            for k, v in values.items():
                if k.endswith("__custom_url"):
                    url = (v or "").strip()
                    break
        st["custom_url"] = url
        if not url:
            return
        st["preview"] = {"label": "Custom URL", "url": url,
                          "style": "", "substyle": "", "note": ""}
        await self._show_detail(player)

    async def _show_detail(self, player) -> None:
        if self.grid is not None:
            try:
                self.grid._visible_logins.discard(player.login)
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.grid, player_logins=[player.login])
            except Exception:
                logger.exception("media_browser: grid hide failed")
        if self.detail is None:
            return
        try:
            await self.detail.display(player_logins=[player.login])
            self.detail._visible = True
            self.detail._visible_logins.add(player.login)
        except Exception:
            logger.exception("media_browser: detail display failed")

    # ---- catch-all router ---------------------------------------------

    async def _catch_all(self, player, action, values, **kwargs) -> None:
        login = player.login
        st = self._state.setdefault(login, self._default_state())

        # Tabs (tab__<key>) -- reset to page 1 on tab switch.
        m = re.match(r"^cat__tab__([a-z0-9_]+)$", action)
        if m:
            st["active_cat"] = m.group(1)
            st["page"] = 1
            if self.grid is not None:
                await self.grid.refresh()
            return

        # Pagination (pg__page__<n> / pg__first / pg__prev / pg__next / pg__last)
        m = re.match(r"^pg__(first|prev|next|last|page__(\d+))$", action)
        if m:
            cur = int(st.get("page", 1) or 1)
            kind = m.group(1)
            if kind == "first":
                st["page"] = 1
            elif kind == "prev":
                st["page"] = max(1, cur - 1)
            elif kind == "next":
                st["page"] = cur + 1
            elif kind == "last":
                st["page"] = 10_000  # clamped in grid_context
            else:
                st["page"] = int(m.group(2))
            if self.grid is not None:
                await self.grid.refresh()
            return

        # Thumbnail click (preview_<itemkey>)
        m = re.match(r"^preview__([a-zA-Z0-9_]+)$", action)
        if m:
            item_key = m.group(1)
            cat = _catalog.category(st["active_cat"]) if st["active_cat"] else None
            if cat:
                for it in cat["items"]:
                    if it["key"] == item_key:
                        st["preview"] = {
                            "label":    it["label"],
                            "url":      it.get("url", ""),
                            "style":    it.get("style", ""),
                            "substyle": it.get("substyle", ""),
                            "note":     it.get("note", ""),
                        }
                        await self._show_detail(player)
                        return
            return
