"""Maplist - hub-mounted, player-facing browser for the server's own playlist.

Two sections:

* ``all``   - every map currently on the dedicated, filterable by name/author.
* ``queue`` - the maps already juked (read from the PyPlanet ``jukebox`` app
              if present, otherwise empty).

Per-row actions:

* ``Jukebox`` - queue the map for play via ``jukebox.add_to_jukebox``.
* ``info``    - open the details sub-window.

In the details sub-window the same Jukebox action is available; if the map
is already in the queue an additional ``Drop`` button removes it (only for
the requester or an admin).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from pyplanet.apps.config import AppConfig

from .views import MaplistDetailView, MaplistView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)


PAGE_SIZE = 12

_STYLE_RE = re.compile(r"\$(?:[0-9a-fA-F]{3}|[a-zA-Z<>])")


def _strip_styles(s: str) -> str:
    return _STYLE_RE.sub("", s or "")


def _ms_to_str(ms: int) -> str:
    if not ms:
        return "-"
    s, ms = divmod(int(ms), 1000)
    m, s = divmod(s, 60)
    if m:
        return f"{m}:{s:02d}.{ms:03d}"
    return f"{s}.{ms:03d}"


def _karma_display(pct: float | None) -> tuple[str, str]:
    """Format a karma percentage (-1..+1 from sum(score)/sum(|score|)) as a
    signed integer percent string + a manialink color. Returns ("--", grey)
    when the map has no votes (``pct is None``)."""
    if pct is None:
        return ("--", "888")
    try:
        pct_i = int(round(float(pct) * 100))
    except (TypeError, ValueError):
        return ("--", "888")
    if pct_i < -100:
        pct_i = -100
    if pct_i > 100:
        pct_i = 100
    sign = "+" if pct_i >= 0 else ""
    if pct_i >= 50:
        color = "0f8"
    elif pct_i >= 0:
        color = "fc4"
    elif pct_i >= -50:
        color = "f86"
    else:
        color = "f44"
    return (f"{sign}{pct_i}%", color)


def _map_to_row(m: Any) -> dict[str, Any]:
    """Coerce a PyPlanet Map model into a JSON-safe row dict."""
    return {
        "uid":          str(getattr(m, "uid", "") or ""),
        "map_id":       int(getattr(m, "id", 0) or 0),
        "name":         str(getattr(m, "name", "") or "(unnamed)"),
        "name_clean":   _strip_styles(getattr(m, "name", "")),
        "author":       str(getattr(m, "author_nickname", None)
                            or getattr(m, "author_login", "") or ""),
        "author_clean": _strip_styles(getattr(m, "author_nickname", None)
                                      or getattr(m, "author_login", "") or ""),
        "environment":  str(getattr(m, "environment", "") or ""),
        "map_type":     str(getattr(m, "map_type", "") or ""),
        "map_style":    str(getattr(m, "map_style", "") or ""),
        "num_laps":     int(getattr(m, "num_laps", 0) or 0),
        "num_cps":      int(getattr(m, "num_checkpoints", 0) or 0),
        "time_author":  int(getattr(m, "time_author", 0) or 0),
        "time_gold":    int(getattr(m, "time_gold", 0) or 0),
        "time_silver":  int(getattr(m, "time_silver", 0) or 0),
        "time_bronze":  int(getattr(m, "time_bronze", 0) or 0),
        "time_author_s": _ms_to_str(getattr(m, "time_author", 0) or 0),
        "time_gold_s":   _ms_to_str(getattr(m, "time_gold", 0) or 0),
        "time_silver_s": _ms_to_str(getattr(m, "time_silver", 0) or 0),
        "time_bronze_s": _ms_to_str(getattr(m, "time_bronze", 0) or 0),
        "mx_id":        int(getattr(m, "mx_id", 0) or 0),
        "file":         str(getattr(m, "file", "") or ""),
    }


class App_Maplist(AppConfig):
    name = "pyplanet.apps.tmsm.maplist"
    label = "tmsm_maplist"
    app_dependencies = ["core.maniaplanet"]
    game_dependencies = ["trackmania", "trackmania_next", "shootmania"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.view: MaplistView | None = None
        self.detail_view: MaplistDetailView | None = None
        self._state: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    # ---- lifecycle -----------------------------------------------------

    async def on_start(self) -> None:
        try:
            self.view = MaplistView(self)
            self.view.connect("search", self._on_search)
            self.view.connect("query__clear", self._on_clear_query)
            self.view.connect("prev", self._on_prev)
            self.view.connect("next", self._on_next)
            self.view.connect("refresh", self._on_refresh)
            self.view.connect("open_jukebox", self._on_open_jukebox)
            self.view.handle_catch_all = self._catch_all  # type: ignore[assignment]

            self.detail_view = MaplistDetailView(self)
            self.detail_view.connect(
                "_crumb__maplist", self._on_crumb_back_to_list,
            )
            self.detail_view.handle_catch_all = self._catch_all  # type: ignore[assignment]
        except Exception:
            logger.exception("maplist: view init failed")
            return
        await self._register_with_hub()

    async def on_stop(self) -> None:
        for v in (self.view, self.detail_view):
            if v is not None:
                try:
                    await v.destroy()
                except Exception:
                    logger.exception("maplist: destroy failed")
        self.view = None
        self.detail_view = None

    # ---- hub -----------------------------------------------------------

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("maplist: tmsm_hub:register signal not registered yet")
            return
        entry = HubAppEntry(
            key="maplist",
            name="Maplist",
            icon="th-large",
            color="15f",
            role=Role.PLAYER,
            order=10,
            description="Browse the server map rotation.",
            open=self._open,
            command="list",
        )
        await sig.send_robust({"entry": entry}, raw=True)

    async def _open(self, player) -> None:
        if self.view is None:
            return
        self._state.setdefault(player.login, self._default_state())
        try:
            await self.view.display(player_logins=[player.login])
            self.view._visible_logins.add(player.login)
            self.view._visible = True
        except Exception:
            logger.exception("maplist: open failed")

    # ---- state ---------------------------------------------------------

    def _default_state(self) -> dict[str, Any]:
        return {
            "query": "",
            "page": 1,
            "detail_uid": None,
            "status": "",
            "status_color": "aaa",
        }

    def _jukebox_app(self):
        """Find the PyPlanet contrib jukebox app (not the tmsm placeholder).

        The tmsm bundle ships a WIP placeholder under the same ``jukebox``
        label that can shadow the contrib app in ``apps.apps['jukebox']``.
        Pick by capability (presence of ``add_to_jukebox``) so we always get
        the real one regardless of label collisions.
        """
        try:
            candidates = list(self.instance.apps.apps.values())
        except Exception:
            return None
        # Prefer the upstream contrib app or the tmsm port by dotted name.
        for app in candidates:
            n = getattr(app, "name", "")
            if n in ("pyplanet.apps.contrib.jukebox",
                     "pyplanet.apps.tmsm.jukebox") \
                    and hasattr(app, "add_to_jukebox"):
                return app
        # Fallback: any app exposing the right API.
        for app in candidates:
            if hasattr(app, "add_to_jukebox") and hasattr(app, "jukebox"):
                return app
        return None

    def _all_rows(self) -> list[dict[str, Any]]:
        try:
            maps = list(self.instance.map_manager.maps or [])
        except Exception:
            maps = []
        return [_map_to_row(m) for m in maps]

    def _queue_entries(self) -> list[dict[str, Any]]:
        jb = self._jukebox_app()
        if jb is None:
            return []
        try:
            return list(jb.jukebox or [])
        except Exception:
            return []

    def _filter(self, rows: list[dict[str, Any]],
                query: str) -> list[dict[str, Any]]:
        q = (query or "").strip().lower()
        if not q:
            return rows
        return [
            r for r in rows
            if q in r["name_clean"].lower() or q in r["author_clean"].lower()
        ]

    def _current_uid(self) -> str:
        try:
            cm = self.instance.map_manager.current_map
            return str(getattr(cm, "uid", "") or "")
        except Exception:
            return ""

    async def _load_karma_pct(self, map_ids: list[int]) -> dict[int, float]:
        """Aggregate the karma vote rows for the given Map PKs into
        ``{map_id: sum(score) / sum(|score|)}``. Maps with no votes are
        absent from the result; callers should treat missing keys as
        "no votes yet" (rendered as ``--`` rather than ``0%``)."""
        ids = [int(i) for i in (map_ids or []) if i]
        if not ids:
            return {}
        try:
            from pyplanet.apps.contrib.karma.models import Karma as KarmaModel
        except Exception:
            return {}
        try:
            rows = await KarmaModel.objects.execute(
                KarmaModel.select().where(KarmaModel.map_id << ids)
            )
        except Exception:
            logger.exception("maplist: karma fetch failed")
            return {}
        totals: dict[int, list[float]] = {}
        for v in rows:
            try:
                mid = int(getattr(v, "map_id", 0) or 0)
            except (TypeError, ValueError):
                continue
            if not mid:
                continue
            score = getattr(v, "score", 0)
            expanded = getattr(v, "expanded_score", None)
            if expanded is not None:
                score = expanded
            try:
                s = float(score or 0)
            except (TypeError, ValueError):
                s = 0.0
            bucket = totals.setdefault(mid, [0.0, 0.0])
            bucket[0] += s
            bucket[1] += abs(s)
        return {
            mid: (tot / abs_tot) if abs_tot > 0 else 0.0
            for mid, (tot, abs_tot) in totals.items()
            if abs_tot > 0
        }

    async def view_context(self, login: str) -> dict[str, Any]:
        st = self._state.setdefault(login, self._default_state())
        rows = self._filter(self._all_rows(), st["query"])
        is_operator = False
        try:
            from pyplanet.apps.core.maniaplanet.models import Player
            from pyplanet.apps.tmsm.ui import perms as _perms
            p = await Player.get_by_login(login)
            is_operator = bool(_perms.is_operator(p)) if p is not None else False
        except Exception:
            is_operator = False

        total = len(rows)
        page = max(1, int(st["page"]))
        start = (page - 1) * PAGE_SIZE
        end = start + PAGE_SIZE
        page_rows = rows[start:end]
        if not page_rows and page > 1:
            page = 1
            page_rows = rows[:PAGE_SIZE]
            st["page"] = 1

        queue_uids = {
            str(getattr(e.get("map"), "uid", "") or "")
            for e in self._queue_entries() if isinstance(e, dict)
        }
        current_uid = self._current_uid()
        karma_pct = await self._load_karma_pct(
            [r["map_id"] for r in page_rows if r.get("map_id")]
        )
        for r in page_rows:
            r["in_queue"]   = r["uid"] in queue_uids
            r["is_current"] = r["uid"] == current_uid
            mid = int(r.get("map_id") or 0)
            pct = karma_pct.get(mid)
            kstr, kcol = _karma_display(pct)
            r["karma_pct"]   = pct if pct is not None else 0.0
            r["karma_str"]   = kstr
            r["karma_color"] = kcol

        return {
            "query":         st["query"],
            "page":          page,
            "results":       page_rows,
            "more":          end < total,
            "total":         total,
            "jukebox_count": len(queue_uids),
            "has_jukebox":   self._jukebox_app() is not None,
            "current_uid":   current_uid,
            "is_operator":   is_operator,
            "status":        st["status"],
            "status_color":  st["status_color"],
        }

    async def detail_context(self, login: str) -> dict[str, Any]:
        st = self._state.setdefault(login, self._default_state())
        uid = st.get("detail_uid") or ""
        row: dict[str, Any] = {}
        is_operator = False
        in_queue = False
        queue_pos = 0
        requester = ""
        is_current = bool(uid) and uid == self._current_uid()
        try:
            from pyplanet.apps.core.maniaplanet.models import Player
            from pyplanet.apps.tmsm.ui import perms as _perms
            p = await Player.get_by_login(login)
            is_operator = bool(_perms.is_operator(p)) if p is not None else False
        except Exception:
            is_operator = False

        for r in self._all_rows():
            if r["uid"] == uid:
                row = r
                break

        for i, entry in enumerate(self._queue_entries(), start=1):
            if not isinstance(entry, dict):
                continue
            m = entry.get("map")
            p = entry.get("player")
            if m is not None and str(getattr(m, "uid", "")) == uid:
                in_queue = True
                queue_pos = i
                requester = str(getattr(p, "nickname", "")
                                or getattr(p, "login", "") or "")
                break

        return {
            "map":             row,
            "in_queue":        in_queue,
            "queue_pos":       queue_pos,
            "queue_requester": requester,
            "is_current":      is_current,
            "is_operator":     is_operator,
            "status":          st["status"],
            "status_color":    st["status_color"],
        }

    def _set_status(self, login: str, text: str, color: str = "aaa") -> None:
        st = self._state.setdefault(login, self._default_state())
        st["status"] = text
        st["status_color"] = color

    async def _refresh_views(self) -> None:
        for v in (self.view, self.detail_view):
            if v is None:
                continue
            try:
                if getattr(v, "_visible", False):
                    await v.refresh()
            except Exception:
                logger.exception("maplist: refresh failed")

    # ---- handlers ------------------------------------------------------

    async def _catch_all(self, player, action, values, **kwargs) -> None:
        self._absorb(player.login, values)

        m = re.match(r"^juke__([0-9a-zA-Z_\-]+)$", action)
        if m:
            await self._on_juke(player, m.group(1))
            return

        m = re.match(r"^drop__([0-9a-zA-Z_\-]+)$", action)
        if m:
            await self._on_drop(player, m.group(1))
            return

        m = re.match(r"^remove__([0-9a-zA-Z_\-]+)$", action)
        if m:
            await self._on_remove_map(player, m.group(1))
            return

        m = re.match(r"^details__([0-9a-zA-Z_\-]+)$", action)
        if m:
            await self._on_show_details(player, m.group(1))
            return

        if action in ("_close",) or action.startswith("_crumb__"):
            return

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
        st["page"] = 1
        if self.view is not None:
            await self.view.refresh()

    async def _on_clear_query(self, player) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        st["query"] = ""
        st["page"] = 1
        if self.view is not None:
            await self.view.refresh()

    async def _on_open_jukebox(self, player) -> None:
        """Hand off to the jukebox app: hide our window, open theirs."""
        jb = self._jukebox_app()
        if jb is None:
            self._set_status(player.login,
                             "jukebox app is not loaded", "f44")
            await self._refresh_views()
            return
        opener = getattr(jb, "_open", None)
        if opener is None:
            self._set_status(player.login,
                             "jukebox app has no open hook", "f44")
            await self._refresh_views()
            return
        login = player.login
        if self.view is not None:
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.view, player_logins=[login])
                self.view._visible_logins.discard(login)
                self.view._visible = bool(self.view._visible_logins)
            except Exception:
                logger.exception("maplist: hide list failed")
        try:
            await opener(player)
        except Exception:
            logger.exception("maplist: failed to open jukebox")

    async def _on_prev(self, player) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        if st["page"] > 1:
            st["page"] -= 1
            if self.view is not None:
                await self.view.refresh()

    async def _on_next(self, player) -> None:
        st = self._state.setdefault(player.login, self._default_state())
        ctx = await self.view_context(player.login)
        if ctx["more"]:
            st["page"] += 1
            if self.view is not None:
                await self.view.refresh()

    async def _on_refresh(self, player) -> None:
        if self.view is not None:
            await self.view.refresh()

    # ---- details sub-window -------------------------------------------

    async def _on_show_details(self, player, uid: str) -> None:
        if self.detail_view is None:
            return
        login = player.login
        st = self._state.setdefault(login, self._default_state())
        st["detail_uid"] = uid
        if self.view is not None:
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.view, player_logins=[login])
                self.view._visible_logins.discard(login)
                self.view._visible = bool(self.view._visible_logins)
            except Exception:
                logger.exception("maplist: hide list failed")
        try:
            await self.detail_view.display(player_logins=[login])
            self.detail_view._visible_logins.add(login)
            self.detail_view._visible = True
        except Exception:
            logger.exception("maplist: open details failed")

    async def _on_crumb_back_to_list(self, player, **_) -> None:
        login = player.login
        if self.detail_view is not None:
            try:
                from pyplanet.views.template import TemplateView
                await TemplateView.hide(self.detail_view, player_logins=[login])
                self.detail_view._visible_logins.discard(login)
                self.detail_view._visible = bool(self.detail_view._visible_logins)
            except Exception:
                logger.exception("maplist: hide details failed")
        await self._open(player)

    # ---- jukebox ops ---------------------------------------------------

    async def _resolve_map(self, uid: str):
        try:
            return await self.instance.map_manager.get_map(uid=uid)
        except Exception:
            return None

    async def _on_juke(self, player, uid: str) -> None:
        login = player.login
        jb = self._jukebox_app()
        if jb is None:
            self._set_status(login,
                             "jukebox app is not loaded on this server", "f44")
            await self._refresh_views()
            return
        m = await self._resolve_map(uid)
        if m is None:
            self._set_status(login, "map not found in current playlist", "fa0")
            await self._refresh_views()
            return
        try:
            await jb.add_to_jukebox(player, m)
        except Exception as e:
            logger.exception("maplist: add_to_jukebox failed")
            self._set_status(login, f"juke failed: {e}", "f44")
            await self._refresh_views()
            return
        # add_to_jukebox chats the outcome; we just refresh.
        await self._refresh_views()

    async def _on_drop(self, player, uid: str) -> None:
        login = player.login
        jb = self._jukebox_app()
        if jb is None:
            self._set_status(login, "jukebox not loaded", "f44")
            await self._refresh_views()
            return
        target = None
        for entry in self._queue_entries():
            if not isinstance(entry, dict):
                continue
            m = entry.get("map")
            if m is not None and str(getattr(m, "uid", "")) == uid:
                target = entry
                break
        if target is None:
            self._set_status(login, "map is not in the queue", "fa0")
            await self._refresh_views()
            return
        requester = target.get("player")
        is_owner = bool(requester is not None
                        and getattr(requester, "login", None) == login)
        from pyplanet.apps.tmsm.ui import perms as _perms
        is_admin = _perms.is_operator(player)
        if not (is_owner or is_admin):
            self._set_status(login, "only the requester or an admin can drop",
                             "fa0")
            await self._refresh_views()
            return
        try:
            jb.jukebox.remove(target)
        except ValueError:
            pass
        self._set_status(login, "dropped from queue", "0f8")
        await self._refresh_views()

    async def _delete_tmx_meta_for_map(self, m: Any) -> int:
        """Best-effort cleanup of TMX metadata for a removed server map."""
        try:
            from pyplanet.apps.tmsm.tmx_browser.models import TmxMapMeta
        except Exception:
            return 0

        deleted = 0
        map_id = int(getattr(m, "id", 0) or 0)
        uid = str(getattr(m, "uid", "") or "").strip()
        mx_id = int(getattr(m, "mx_id", 0) or 0)

        try:
            if map_id > 0:
                deleted = int(
                    await TmxMapMeta.objects.execute(
                        TmxMapMeta.delete().where(
                            TmxMapMeta.server_map_id == map_id,
                        ),
                    ),
                )
        except Exception:
            logger.exception("maplist: tmx meta delete by server_map_id failed")

        if deleted <= 0 and uid:
            try:
                deleted = int(
                    await TmxMapMeta.objects.execute(
                        TmxMapMeta.delete().where(
                            TmxMapMeta.uid == uid,
                        ),
                    ),
                )
            except Exception:
                logger.exception("maplist: tmx meta delete by uid failed")

        if deleted <= 0 and mx_id > 0:
            try:
                deleted = int(
                    await TmxMapMeta.objects.execute(
                        TmxMapMeta.delete().where(
                            TmxMapMeta.track_id == mx_id,
                        ),
                    ),
                )
            except Exception:
                logger.exception("maplist: tmx meta delete by track_id failed")

        return max(0, deleted)

    async def _on_remove_map(self, player, uid: str) -> None:
        login = player.login
        from pyplanet.apps.tmsm.ui import perms as _perms
        if not _perms.is_operator(player):
            self._set_status(login, "only operators can remove maps", "fa0")
            await self._refresh_views()
            return
        m = await self._resolve_map(uid)
        if m is None:
            self._set_status(login, "map not found in current playlist", "fa0")
            await self._refresh_views()
            return
        if uid == self._current_uid():
            self._set_status(login, "cannot remove the currently playing map", "fa0")
            await self._refresh_views()
            return
        try:
            await self.instance.map_manager.remove_map(m, delete_file=False)
        except Exception as e:
            logger.exception("maplist: remove map failed")
            self._set_status(login, f"remove failed: {e}", "f44")
            await self._refresh_views()
            return

        deleted_meta = await self._delete_tmx_meta_for_map(m)
        if deleted_meta > 0:
            self._set_status(login, "map removed from playlist (TMX metadata cleaned)", "0f8")
        else:
            self._set_status(login, "map removed from playlist", "0f8")
        # Persist the playlist into the active startup matchsettings file so
        # the removal survives a server restart (integrated //wml).
        try:
            from pyplanet.apps.tmsm.ui.maplist_io import write_active_matchsettings
            await write_active_matchsettings(self.instance)
        except Exception:
            logger.exception("maplist: auto write_maplist after remove failed")
        await self._refresh_views()