"""Tournaments app: create, configure and run in-game tournaments.

v1 scope: a single-stage solo "cup" over a pool of maps. The operator drives
each match (one map) step by step; the app sets the chosen match mode + map,
locks non-participants to spectator, captures the end-of-map ranking at the
podium and keeps a running leaderboard.

Points come from the match mode's own end-of-map points when it awards any
(Rounds/Cup/Champion/...). For modes that award none (e.g. Time Attack) the
app falls back to a placement table based on finishing order.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from pyplanet.apps.config import AppConfig
from pyplanet.contrib.command import Command

from .storage import TournamentStorage
from .views import TournamentView

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)

_STATUS_LABELS = {
    "draft": "Draft",
    "registration": "Registration open",
    "running": "Running",
    "finished": "Finished",
}
_STATUS_COLORS = {
    "draft": "aaa",
    "registration": "0cf",
    "running": "0f8",
    "finished": "fa0",
}


class TournamentsApp(AppConfig):
    name = "pyplanet.apps.tmsm.tournaments"
    label = "tournaments"
    app_dependencies = ["core.maniaplanet"]
    game_dependencies = ["trackmania_next", "trackmania", "shootmania"]

    PAGE_SIZE = 8

    # Match modes offered in the picker (mirrors the server app's builtins).
    # (path relative to Scripts/Modes/, friendly label)
    MATCH_MODES: list[tuple[str, str]] = [
        ("Trackmania/TM_Rounds_Online.Script.txt",     "Rounds"),
        ("Trackmania/TM_Cup_Online.Script.txt",        "Cup"),
        ("Trackmania/TM_Champion_Online.Script.txt",   "Champion"),
        ("Trackmania/TM_Knockout_Online.Script.txt",   "Knockout"),
        ("Trackmania/TM_TimeAttack_Online.Script.txt", "Time Attack"),
        ("Trackmania/TM_Laps_Online.Script.txt",       "Laps"),
    ]

    # Placement points used only when the chosen mode awards no points.
    FALLBACK_POINTS = [10, 8, 6, 5, 4, 3, 2, 1]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.storage = TournamentStorage(self.instance)
        self.view: TournamentView | None = None
        self._state: dict[str, dict[str, Any]] = {}

        # Runtime (single active tournament at a time).
        self._running_tid: int | None = None
        self._active_map_id: int | None = None
        self._last_scores: list[dict[str, Any]] = []
        # Auto mode paused because too few participants were present.
        self._auto_paused: bool = False

    # ---- lifecycle -----------------------------------------------------

    async def on_start(self) -> None:
        await self.storage.ensure_schema()

        try:
            await self.instance.permission_manager.register(
                "manage", "Create and run tournaments", app=self, min_level=2,
            )
        except Exception:
            logger.exception("tournaments: permission register failed")

        await self.instance.command_manager.register(
            Command(
                command="tournament", aliases=["tourney"],
                target=self.cmd_admin, perms="tournaments:manage", admin=True,
                description="Open the tournament admin panel.",
            ).add_param(name="option", required=False),
            Command(
                command="tournament",
                target=self.cmd_player,
                description="Join or view the active tournament.",
            ).add_param(name="action", required=False),
        )

        signals = self.context.signals
        signals.listen("maniaplanet:podium_start", self._on_podium)
        signals.listen("maniaplanet:player_connect", self._on_connect)
        signals.listen("trackmania:scores", self._on_scores)

        # Resume a running tournament after a controller restart.
        try:
            for t in await self.storage.list_tournaments():
                if t.get("status") == "running":
                    self._running_tid = int(t.get("id"))
                    self._active_map_id = await self._map_id_at_index(
                        self._running_tid, int(t.get("current_map_index", 0) or 0))
                    break
        except Exception:
            logger.exception("tournaments: resume scan failed")

        try:
            self.view = TournamentView(self)
            self._wire_view()
        except Exception:
            logger.exception("tournaments: view init failed")
            self.view = None

        await self._register_with_hub()

    async def on_stop(self) -> None:
        if self.view is not None:
            try:
                await self.view.destroy()
            except Exception:
                logger.exception("tournaments: destroy failed")
            self.view = None
        await super().on_stop()

    def _wire_view(self) -> None:
        v = self.view
        if v is None:
            return
        for name, handler in (
            ("refresh", self._on_refresh),
            ("open_list", self._on_open_list),
            ("new", self._on_new),
            ("toggle_signup", self._on_toggle_signup),
            ("toggle_lock", self._on_toggle_lock),
            ("toggle_auto", self._on_toggle_auto),
            ("mode_open", self._on_mode_open),
            ("mode_close", self._on_mode_close),
            ("maps_open", self._on_maps_open),
            ("maps_close", self._on_maps_close),
            ("set_registration", self._on_set_registration),
            ("start", self._on_start_tournament),
            ("finish", self._on_finish_tournament),
            ("run_load_next", self._on_run_load_next),
            ("run_confirm", self._on_run_confirm),
            ("run_skip", self._on_run_skip),
            ("run_redo", self._on_run_redo),
        ):
            v.connect(name, handler)
        v.handle_catch_all = self._catch_all  # type: ignore[assignment]

    # ---- hub integration ----------------------------------------------

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            return
        try:
            entry = HubAppEntry(
                key="tournaments",
                name="Tournaments",
                icon="trophy",
                color="fc4",
                role=Role.OPERATOR,
                order=14,
                description="Create, configure and run tournaments.",
                open=self._open,
                command="tournament",
            )
            await sig.send_robust({"entry": entry}, raw=True)
        except Exception:
            logger.exception("tournaments: hub register failed")

    async def _open(self, player) -> None:
        if self.view is None:
            return
        self._state.setdefault(player.login, self._default_state())
        try:
            await self.view.display(player_logins=[player.login])
            self.view._visible_logins.add(player.login)
            self.view._visible = bool(self.view._visible_logins)
        except Exception:
            logger.exception("tournaments: open failed")

    # ---- helpers -------------------------------------------------------

    @staticmethod
    def _now() -> _dt.datetime:
        return _dt.datetime.utcnow()

    @staticmethod
    def _login_of(player) -> str:
        return str(getattr(player, "login", "") or "")

    @staticmethod
    def _nick_of(player, login: str) -> str:
        return str(getattr(player, "nickname", login) or login)

    @staticmethod
    def _is_spectator(player) -> bool:
        return bool(getattr(getattr(player, "flow", None), "is_spectator", False))

    async def _present_participant_count(self, tid: int) -> int:
        """Online, non-spectator participants of the tournament."""
        participants = {p["login"] for p in await self.storage.list_participants(tid)}
        count = 0
        for o in list(self.instance.player_manager.online):
            login = self._login_of(o)
            if login in participants and not self._is_spectator(o):
                count += 1
        return count

    def _default_state(self) -> dict[str, Any]:
        return {
            "screen": "list",
            "tid": None,
            "tab": "overview",
            "page": 1,
            "mode_picker": False,
            "map_picker": False,
            "status": "",
            "status_color": "aaa",
        }

    def _st(self, login: str) -> dict[str, Any]:
        return self._state.setdefault(login, self._default_state())

    def _set_status(self, login: str, text: str, color: str = "aaa") -> None:
        st = self._st(login)
        st["status"] = text
        st["status_color"] = color

    def _is_admin(self, login: str) -> bool:
        try:
            from pyplanet.apps.tmsm.ui import perms as _perms
            return bool(_perms.is_operator(login))
        except Exception:
            return False

    async def _refresh_view(self) -> None:
        if self.view is None:
            return
        try:
            if self.view._visible_logins and getattr(self.view, "_visible", False):
                await self.view.refresh()
        except Exception:
            logger.exception("tournaments: refresh failed")

    def _mode_label(self, path: str | None) -> str:
        for p, label in self.MATCH_MODES:
            if p == path:
                return label
        return path or "(not set)"

    async def _map_id_at_index(self, tid: int, index: int) -> int | None:
        maps = await self.storage.list_maps(tid)
        if 0 <= index < len(maps):
            return int(maps[index].get("id"))
        return None

    def _server_map_by_uid(self, uid: str):
        try:
            for m in (self.instance.map_manager.maps or []):
                if str(getattr(m, "uid", "") or "") == uid:
                    return m
        except Exception:
            pass
        return None

    # ---- commands ------------------------------------------------------

    async def cmd_admin(self, player, data=None, **kwargs) -> None:
        await self._open(player)

    async def cmd_player(self, player, data=None, **kwargs) -> None:
        login = self._login_of(player)
        action = str(getattr(data, "action", "") or "").lower()
        if action == "join":
            await self._player_join(player)
            return
        # default: show status / standings in chat
        await self._player_info(player)

    async def _player_join(self, player) -> None:
        login = self._login_of(player)
        target = None
        for t in await self.storage.list_tournaments():
            if t.get("status") == "registration":
                target = t
                break
        if target is None:
            await self.instance.chat(
                "$f00No tournament is currently open for registration.", login)
            return
        if not bool(target.get("self_signup", True)):
            await self.instance.chat(
                "$f00Self sign-up is disabled for this tournament.", login)
            return
        await self.storage.add_participant(
            int(target["id"]), login, self._nick_of(player, login), self._now())
        await self.instance.chat(
            f"$0f0You joined the tournament: $fff{target.get('name')}", login)
        await self._refresh_view()
        await self._maybe_auto_start()

    async def _player_info(self, player) -> None:
        login = self._login_of(player)
        active = None
        for t in await self.storage.list_tournaments():
            if t.get("status") in ("running", "registration"):
                active = t
                break
        if active is None:
            await self.instance.chat("$aaaNo active tournament.", login)
            return
        standings = await self.storage.standings(int(active["id"]))
        await self.instance.chat(
            f"$fff{active.get('name')} $aaa[{_STATUS_LABELS.get(active.get('status'))}]",
            login)
        for entry in standings[:5]:
            await self.instance.chat(
                f"$ff0{entry['rank']}.$fff {entry['nickname']} "
                f"$aaa- {entry['points']} pts", login)

    # ---- signal handlers ----------------------------------------------

    async def _on_connect(self, player=None, **kwargs) -> None:
        if self._running_tid is None:
            return
        login = self._login_of(player)
        if not login:
            return
        try:
            t = await self.storage.get_tournament(self._running_tid)
            if not t or not bool(t.get("lock_to_participants", True)):
                return
            if await self.storage.is_participant(self._running_tid, login):
                return
            await self.instance.gbx("ForceSpectator", login, 1)
        except Exception:
            logger.exception("tournaments: connect lock failed login=%s", login)

    async def _on_scores(self, section=None, players=None, **kwargs) -> None:
        if section == "PreEndRound":
            return
        snapshot: list[dict[str, Any]] = []
        for item in list(players or []):
            if not isinstance(item, dict):
                continue
            player = item.get("player")
            login = self._login_of(player)
            if not login or login.startswith("*"):
                continue
            try:
                best = int(item.get("best_race_time") or 0)
            except (TypeError, ValueError):
                best = 0
            points = 0
            for key in ("map_points", "round_points", "match_points"):
                try:
                    val = int(item.get(key) or 0)
                except (TypeError, ValueError):
                    val = 0
                if val:
                    points = val
                    break
            snapshot.append({
                "login": login,
                "nickname": self._nick_of(player, login),
                "time": best,
                "points": points,
            })
        if snapshot:
            self._last_scores = snapshot

    async def _on_podium(self, **kwargs) -> None:
        if self._running_tid is None or self._active_map_id is None:
            return
        tid = self._running_tid
        map_id = self._active_map_id
        scores = list(self._last_scores)
        try:
            await self._capture_results(tid, map_id, scores)
        except Exception:
            logger.exception("tournaments: podium capture failed")
        try:
            await self._auto_advance(tid)
        except Exception:
            logger.exception("tournaments: auto-advance failed")

    async def _capture_results(self, tid: int, map_id: int,
                               scores: list[dict[str, Any]]) -> None:
        participants = {p["login"] for p in await self.storage.list_participants(tid)}
        present = [s for s in scores if s["login"] in participants]
        now = self._now()
        if present:
            any_points = any(s["points"] > 0 for s in present)
            ordered = sorted(
                present,
                key=lambda s: (-(s["points"]), s["time"] if s["time"] > 0 else 10**12),
            )
            for idx, s in enumerate(ordered):
                if any_points:
                    pts = s["points"]
                else:
                    pts = self.FALLBACK_POINTS[idx] if idx < len(self.FALLBACK_POINTS) else 0
                await self.storage.add_result(
                    tid, map_id, s["login"], s["nickname"],
                    idx + 1, pts, s["points"], now,
                )
        await self.storage.set_map_status(map_id, "played", now)
        await self.storage.recompute_participant_points(tid)
        self._last_scores = []
        await self._refresh_view()

    # ---- automation ----------------------------------------------------

    async def _auto_advance(self, tid: int) -> None:
        """After a podium, auto-load the next pool map or auto-finish."""
        if self._running_tid != tid:
            return
        t = await self.storage.get_tournament(tid)
        if not t or t.get("status") != "running" or not bool(t.get("auto_advance", True)):
            return

        # Pause if too few participants are present to play a real match.
        if await self._present_participant_count(tid) < 2:
            self._auto_paused = True
            await self.instance.chat(
                "$f80[Tournament]$fff Paused - waiting for at least 2 players. "
                "An operator can resume from the Run tab.")
            await self._refresh_view()
            return

        maps = await self.storage.list_maps(tid)
        nxt = int(t.get("current_map_index", 0) or 0) + 1
        # Skip maps that are no longer on the server.
        while nxt < len(maps):
            row = maps[nxt]
            if self._server_map_by_uid(str(row.get("map_uid") or "")) is None:
                await self.storage.set_map_status(int(row["id"]), "skipped", self._now())
                nxt += 1
                continue
            break

        if nxt >= len(maps):
            await self._finish_tournament(tid)
            return

        row = maps[nxt]
        mobj = self._server_map_by_uid(str(row.get("map_uid") or ""))
        mode = t.get("match_mode")
        try:
            await self.instance.gbx("SetScriptName", mode)
            await self.instance.map_manager.set_next_map(mobj)
        except Exception as e:
            logger.warning("tournaments: auto queue failed: %s", e)
            return
        await self.storage.update_tournament(tid, current_map_index=nxt)
        self._active_map_id = int(row["id"])
        self._auto_paused = False
        await self._enforce_lock(tid)
        await self.instance.chat(
            f"$0f8[Tournament]$fff Next map "
            f"({nxt + 1}/{len(maps)}): {row.get('name') or row.get('map_uid')}")
        await self._refresh_view()

    async def _maybe_auto_start(self) -> None:
        """Start a registration tournament once its join threshold is met."""
        if self._running_tid is not None:
            return
        for t in await self.storage.list_tournaments():
            if t.get("status") != "registration":
                continue
            threshold = int(t.get("auto_start_threshold", 0) or 0)
            if threshold <= 0:
                continue
            tid = int(t["id"])
            participants = await self.storage.list_participants(tid)
            if len(participants) < threshold:
                continue
            if not t.get("match_mode"):
                await self.instance.chat(
                    "$f80[Tournament]$fff Auto-start threshold reached but no "
                    "match mode is set - start it manually.")
                continue
            maps = await self.storage.list_maps(tid)
            if not maps:
                await self.instance.chat(
                    "$f80[Tournament]$fff Auto-start threshold reached but the "
                    "map pool is empty - add maps first.")
                continue
            await self.storage.update_tournament(
                tid, status="running", current_map_index=0)
            self._running_tid = tid
            await self.instance.chat(
                f"$0f8[Tournament]$fff Auto-starting: {t.get('name')}")
            await self._load_map_index(tid, 0, None)
            await self._refresh_view()
            return

    async def _finish_tournament(self, tid: int) -> None:
        standings = await self.storage.standings(tid)
        winner = standings[0]["login"] if standings else None
        await self.storage.update_tournament(
            tid, status="finished", winner_login=winner)
        if self._running_tid == tid:
            self._running_tid = None
            self._active_map_id = None
            self._auto_paused = False
            await self._release_lock()
        if standings:
            top = standings[0]
            await self.instance.chat(
                f"$ff0[Tournament]$fff Finished! Winner: "
                f"{top['nickname']} $ff0({top['points']} pts)")
        else:
            await self.instance.chat("$ff0[Tournament]$fff Finished.")
        await self._refresh_view()

    # ---- run-cockpit engine -------------------------------------------

    async def _enforce_lock(self, tid: int) -> None:
        t = await self.storage.get_tournament(tid)
        if not t or not bool(t.get("lock_to_participants", True)):
            return
        participants = {p["login"] for p in await self.storage.list_participants(tid)}
        for p in list(self.instance.player_manager.online):
            login = self._login_of(p)
            if login and login not in participants:
                try:
                    await self.instance.gbx("ForceSpectator", login, 1)
                except Exception:
                    pass

    async def _release_lock(self) -> None:
        for p in list(self.instance.player_manager.online):
            login = self._login_of(p)
            if login:
                try:
                    await self.instance.gbx("ForceSpectator", login, 0)
                except Exception:
                    pass

    async def _load_map_index(self, tid: int, index: int, login: str | None) -> None:
        t = await self.storage.get_tournament(tid)
        if not t:
            return
        maps = await self.storage.list_maps(tid)
        if index >= len(maps):
            if login:
                self._set_status(login, "No more maps - finish the tournament.", "fa0")
            return
        row = maps[index]
        mode = t.get("match_mode")
        if not mode:
            if login:
                self._set_status(login, "Pick a match mode first.", "f44")
            return
        mobj = self._server_map_by_uid(str(row.get("map_uid") or ""))
        if mobj is None:
            if login:
                self._set_status(
                    login, "Map not on the server - skipping.", "f44")
            await self.storage.set_map_status(int(row["id"]), "skipped", self._now())
            return
        try:
            await self.instance.gbx("SetScriptName", mode)
            await self.instance.map_manager.set_next_map(mobj)
            await self.instance.gbx("NextMap")
        except Exception as e:
            if login:
                self._set_status(login, f"Load failed: {e}", "f44")
            return
        await self.storage.update_tournament(tid, current_map_index=index)
        self._active_map_id = int(row["id"])
        self._auto_paused = False
        await self._enforce_lock(tid)
        if login:
            self._set_status(
                login, f"Loaded map {index + 1}/{len(maps)}: "
                f"{row.get('name') or row.get('map_uid')}", "0f8")

    # ---- view context --------------------------------------------------

    async def view_context(self, login: str) -> dict[str, Any]:
        st = self._st(login)
        is_admin = self._is_admin(login)
        ctx: dict[str, Any] = {
            "screen": st["screen"],
            "is_admin": is_admin,
            "status": st["status"],
            "status_color": st["status_color"],
            "page": int(st.get("page", 1) or 1),
            "total_pages": 1,
        }

        if st["screen"] == "detail" and st.get("tid"):
            await self._fill_detail_context(login, st, ctx)
        else:
            await self._fill_list_context(login, st, ctx)
        return ctx

    async def _fill_list_context(self, login, st, ctx) -> None:
        tournaments = await self.storage.list_tournaments()
        rows = []
        for t in tournaments:
            status = str(t.get("status") or "draft")
            parts = await self.storage.list_participants(int(t["id"]))
            maps = await self.storage.list_maps(int(t["id"]))
            rows.append({
                "id": int(t["id"]),
                "name": str(t.get("name") or "(unnamed)"),
                "status": status,
                "status_label": _STATUS_LABELS.get(status, status),
                "status_color": _STATUS_COLORS.get(status, "aaa"),
                "players": len(parts),
                "maps": len(maps),
            })
        total = len(rows)
        total_pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        page = max(1, min(total_pages, int(st.get("page", 1) or 1)))
        st["page"] = page
        start = (page - 1) * self.PAGE_SIZE
        ctx.update(
            tournaments=rows[start:start + self.PAGE_SIZE],
            total=total,
            page=page,
            total_pages=total_pages,
        )

    async def _fill_detail_context(self, login, st, ctx) -> None:
        tid = int(st["tid"])
        t = await self.storage.get_tournament(tid)
        if not t:
            st["screen"] = "list"
            await self._fill_list_context(login, st, ctx)
            return
        status = str(t.get("status") or "draft")
        participants = await self.storage.list_participants(tid)
        maps = await self.storage.list_maps(tid)
        ctx.update(
            tid=tid,
            t_name=str(t.get("name") or "(unnamed)"),
            t_status=status,
            t_status_label=_STATUS_LABELS.get(status, status),
            t_status_color=_STATUS_COLORS.get(status, "aaa"),
            t_mode_label=self._mode_label(t.get("match_mode")),
            t_self_signup=bool(t.get("self_signup", True)),
            t_lock=bool(t.get("lock_to_participants", True)),
            t_auto_advance=bool(t.get("auto_advance", True)),
            t_auto_start=int(t.get("auto_start_threshold", 0) or 0),
            t_winner=str(t.get("winner_login") or ""),
            tab=st.get("tab", "overview"),
            n_players=len(participants),
            n_maps=len(maps),
            mode_picker=bool(st.get("mode_picker")),
            map_picker=bool(st.get("map_picker")),
        )

        tab = st.get("tab", "overview")
        if tab == "participants":
            ctx["participants"] = [
                {
                    "login": p["login"],
                    "nickname": str(p.get("nickname") or p["login"]),
                    "points": int(p.get("points", 0) or 0),
                }
                for p in participants
            ]
            part_logins = {p["login"] for p in participants}
            ctx["online_candidates"] = [
                {"login": self._login_of(o),
                 "nickname": self._nick_of(o, self._login_of(o))}
                for o in self.instance.player_manager.online
                if self._login_of(o) and self._login_of(o) not in part_logins
            ][:12]
        elif tab == "maps":
            ctx["maps"] = [
                {
                    "id": int(m["id"]),
                    "order": int(m.get("order_index", 0) or 0) + 1,
                    "name": str(m.get("name") or m.get("map_uid")),
                    "status": str(m.get("status") or "pending"),
                }
                for m in maps
            ]
            if st.get("map_picker"):
                existing = {str(m.get("map_uid") or "") for m in maps}
                candidates = []
                for m in (self.instance.map_manager.maps or []):
                    uid = str(getattr(m, "uid", "") or "")
                    if not uid or uid in existing:
                        continue
                    candidates.append({
                        "uid": uid,
                        "name": str(getattr(m, "name", "") or "(unnamed)"),
                    })
                page = max(1, int(st.get("page", 1) or 1))
                total_pages = max(1, (len(candidates) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
                page = min(page, total_pages)
                st["page"] = page
                start = (page - 1) * self.PAGE_SIZE
                ctx["map_candidates"] = candidates[start:start + self.PAGE_SIZE]
                ctx["page"] = page
                ctx["total_pages"] = total_pages
        elif tab == "run":
            idx = int(t.get("current_map_index", 0) or 0)
            current = maps[idx] if 0 <= idx < len(maps) else None
            nxt = maps[idx + 1] if 0 <= idx + 1 < len(maps) else None
            ctx.update(
                run_is_running=(status == "running"),
                run_index=idx + 1,
                run_total=len(maps),
                run_current=(str(current.get("name") or current.get("map_uid"))
                             if current else ""),
                run_current_status=(str(current.get("status")) if current else ""),
                run_next=(str(nxt.get("name") or nxt.get("map_uid")) if nxt else ""),
                run_auto=bool(t.get("auto_advance", True)),
                run_paused=bool(self._auto_paused and self._running_tid == tid),
            )
        elif tab == "mode":
            ctx["modes"] = [
                {"index": i, "path": p, "label": label,
                 "selected": (p == t.get("match_mode"))}
                for i, (p, label) in enumerate(self.MATCH_MODES)
            ]

        if st.get("mode_picker"):
            ctx["modes"] = [
                {"index": i, "path": p, "label": label,
                 "selected": (p == t.get("match_mode"))}
                for i, (p, label) in enumerate(self.MATCH_MODES)
            ]

        if tab in ("standings", "overview", "run"):
            ctx["standings"] = (await self.storage.standings(tid))[:15]

    # ---- static view handlers -----------------------------------------

    async def _on_refresh(self, player, **kwargs) -> None:
        await self._refresh_view()

    async def _on_open_list(self, player, **kwargs) -> None:
        st = self._st(self._login_of(player))
        st["screen"] = "list"
        st["tid"] = None
        st["mode_picker"] = False
        st["map_picker"] = False
        st["page"] = 1
        await self._open(player)

    async def _on_new(self, player, **kwargs) -> None:
        login = self._login_of(player)
        name = f"Tournament {self._now().strftime('%Y-%m-%d %H:%M')}"
        tid = await self.storage.create_tournament(name, self._now())
        if tid:
            st = self._st(login)
            st["screen"] = "detail"
            st["tid"] = tid
            st["tab"] = "overview"
            self._set_status(login, "Tournament created.", "0f8")
        await self._open(player)

    async def _on_toggle_signup(self, player, **kwargs) -> None:
        login = self._login_of(player)
        st = self._st(login)
        if not st.get("tid"):
            return
        t = await self.storage.get_tournament(int(st["tid"]))
        if t:
            await self.storage.update_tournament(
                int(st["tid"]), self_signup=not bool(t.get("self_signup", True)))
        await self._refresh_view()

    async def _on_toggle_lock(self, player, **kwargs) -> None:
        login = self._login_of(player)
        st = self._st(login)
        if not st.get("tid"):
            return
        t = await self.storage.get_tournament(int(st["tid"]))
        if t:
            await self.storage.update_tournament(
                int(st["tid"]), lock_to_participants=not bool(t.get("lock_to_participants", True)))
        await self._refresh_view()

    async def _on_toggle_auto(self, player, **kwargs) -> None:
        login = self._login_of(player)
        st = self._st(login)
        if not st.get("tid"):
            return
        t = await self.storage.get_tournament(int(st["tid"]))
        if t:
            new_val = not bool(t.get("auto_advance", True))
            await self.storage.update_tournament(int(st["tid"]), auto_advance=new_val)
            self._set_status(
                login,
                "Auto-advance enabled." if new_val else "Auto-advance disabled.",
                "0f8" if new_val else "fa0")
        await self._refresh_view()

    async def _on_mode_open(self, player, **kwargs) -> None:
        st = self._st(self._login_of(player))
        st["mode_picker"] = True
        await self._refresh_view()

    async def _on_mode_close(self, player, **kwargs) -> None:
        st = self._st(self._login_of(player))
        st["mode_picker"] = False
        await self._refresh_view()

    async def _on_maps_open(self, player, **kwargs) -> None:
        st = self._st(self._login_of(player))
        st["map_picker"] = True
        st["page"] = 1
        await self._refresh_view()

    async def _on_maps_close(self, player, **kwargs) -> None:
        st = self._st(self._login_of(player))
        st["map_picker"] = False
        await self._refresh_view()

    async def _on_set_registration(self, player, **kwargs) -> None:
        login = self._login_of(player)
        st = self._st(login)
        if not st.get("tid"):
            return
        await self.storage.update_tournament(int(st["tid"]), status="registration")
        self._set_status(login, "Registration is now open.", "0cf")
        await self._refresh_view()

    async def _on_start_tournament(self, player, **kwargs) -> None:
        login = self._login_of(player)
        st = self._st(login)
        if not st.get("tid"):
            return
        tid = int(st["tid"])
        t = await self.storage.get_tournament(tid)
        if not t:
            return
        if not t.get("match_mode"):
            self._set_status(login, "Pick a match mode first.", "f44")
            await self._refresh_view()
            return
        maps = await self.storage.list_maps(tid)
        if not maps:
            self._set_status(login, "Add at least one map first.", "f44")
            await self._refresh_view()
            return
        if self._running_tid is not None and self._running_tid != tid:
            self._set_status(login, "Another tournament is already running.", "f44")
            await self._refresh_view()
            return
        await self.storage.update_tournament(tid, status="running", current_map_index=0)
        self._running_tid = tid
        st["tab"] = "run"
        await self._load_map_index(tid, 0, login)
        await self._refresh_view()

    async def _on_finish_tournament(self, player, **kwargs) -> None:
        login = self._login_of(player)
        st = self._st(login)
        if not st.get("tid"):
            return
        tid = int(st["tid"])
        await self._finish_tournament(tid)
        self._set_status(login, "Tournament finished.", "fa0")
        await self._refresh_view()

    async def _on_run_load_next(self, player, **kwargs) -> None:
        login = self._login_of(player)
        st = self._st(login)
        if not st.get("tid") or self._running_tid != int(st["tid"]):
            self._set_status(login, "Tournament is not running.", "f44")
            await self._refresh_view()
            return
        t = await self.storage.get_tournament(int(st["tid"]))
        idx = int(t.get("current_map_index", 0) or 0) + 1
        await self._load_map_index(int(st["tid"]), idx, login)
        await self._refresh_view()

    async def _on_run_confirm(self, player, **kwargs) -> None:
        # Confirm the captured result and advance to the next map.
        await self._on_run_load_next(player, **kwargs)

    async def _on_run_skip(self, player, **kwargs) -> None:
        login = self._login_of(player)
        st = self._st(login)
        if not st.get("tid") or self._running_tid != int(st["tid"]):
            return
        tid = int(st["tid"])
        if self._active_map_id is not None:
            await self.storage.set_map_status(self._active_map_id, "skipped", self._now())
        t = await self.storage.get_tournament(tid)
        idx = int(t.get("current_map_index", 0) or 0) + 1
        await self._load_map_index(tid, idx, login)
        await self._refresh_view()

    async def _on_run_redo(self, player, **kwargs) -> None:
        login = self._login_of(player)
        st = self._st(login)
        if not st.get("tid") or self._running_tid != int(st["tid"]):
            return
        tid = int(st["tid"])
        t = await self.storage.get_tournament(tid)
        idx = int(t.get("current_map_index", 0) or 0)
        if self._active_map_id is not None:
            await self.storage.clear_results_for_map(self._active_map_id)
            await self.storage.set_map_status(self._active_map_id, "pending")
            await self.storage.recompute_participant_points(tid)
        await self._load_map_index(tid, idx, login)
        await self._refresh_view()

    # ---- dynamic actions ----------------------------------------------

    async def _catch_all(self, player, action, values, **kwargs) -> None:
        login = self._login_of(player)
        st = self._st(login)

        if action.startswith("pg__"):
            verb = action[len("pg__"):]
            cur = int(st.get("page", 1) or 1)
            tp = int((await self.view_context(login)).get("total_pages", 1) or 1)
            if verb == "first":
                cur = 1
            elif verb == "prev":
                cur = max(1, cur - 1)
            elif verb == "next":
                cur = min(tp, cur + 1)
            elif verb == "last":
                cur = tp
            elif verb.startswith("page__"):
                try:
                    cur = int(verb[len("page__"):])
                except (TypeError, ValueError):
                    cur = 1
            st["page"] = max(1, cur)
            await self._refresh_view()
            return

        if action.startswith("open__"):
            try:
                st["tid"] = int(action[len("open__"):])
                st["screen"] = "detail"
                st["tab"] = "overview"
                st["page"] = 1
                st["mode_picker"] = False
                st["map_picker"] = False
            except (TypeError, ValueError):
                return
            await self._refresh_view()
            return

        if action.startswith("delete__"):
            try:
                tid = int(action[len("delete__"):])
            except (TypeError, ValueError):
                return
            if self._running_tid == tid:
                self._running_tid = None
                self._active_map_id = None
                await self._release_lock()
            await self.storage.delete_tournament(tid)
            self._set_status(login, "Tournament deleted.", "fa0")
            await self._refresh_view()
            return

        if action.startswith("clone__"):
            try:
                src_tid = int(action[len("clone__"):])
            except (TypeError, ValueError):
                return
            new_id = await self.storage.clone_tournament(src_tid, self._now())
            if new_id:
                st["screen"] = "detail"
                st["tid"] = new_id
                st["tab"] = "overview"
                self._set_status(login, "Tournament cloned.", "0f8")
            await self._refresh_view()
            return

        if action.startswith("tab__"):
            st["tab"] = action[len("tab__"):]
            st["page"] = 1
            st["mode_picker"] = False
            st["map_picker"] = False
            await self._refresh_view()
            return

        if action in ("autostart_inc", "autostart_dec") and st.get("tid"):
            t = await self.storage.get_tournament(int(st["tid"]))
            if t:
                cur = int(t.get("auto_start_threshold", 0) or 0)
                cur += 1 if action == "autostart_inc" else -1
                cur = max(0, min(64, cur))
                await self.storage.update_tournament(
                    int(st["tid"]), auto_start_threshold=cur)
                if cur:
                    self._set_status(login, f"Auto-start at {cur} players.", "0cf")
                else:
                    self._set_status(login, "Auto-start disabled.", "fa0")
            await self._refresh_view()
            return

        if action.startswith("mode__"):
            try:
                mi = int(action[len("mode__"):])
            except (TypeError, ValueError):
                return
            if 0 <= mi < len(self.MATCH_MODES) and st.get("tid"):
                path, label = self.MATCH_MODES[mi]
                await self.storage.update_tournament(
                    int(st["tid"]), match_mode=path, match_mode_label=label)
                st["mode_picker"] = False
                self._set_status(login, f"Match mode: {label}", "0f8")
            await self._refresh_view()
            return

        if action.startswith("padd__") and st.get("tid"):
            target = action[len("padd__"):]
            nick = target
            for o in self.instance.player_manager.online:
                if self._login_of(o) == target:
                    nick = self._nick_of(o, target)
                    break
            await self.storage.add_participant(
                int(st["tid"]), target, nick, self._now())
            await self._refresh_view()
            await self._maybe_auto_start()
            return

        if action.startswith("premove__") and st.get("tid"):
            await self.storage.remove_participant(
                int(st["tid"]), action[len("premove__"):])
            await self._refresh_view()
            return

        if action.startswith("mapadd__") and st.get("tid"):
            uid = action[len("mapadd__"):]
            mobj = self._server_map_by_uid(uid)
            name = str(getattr(mobj, "name", "") or "") if mobj else ""
            await self.storage.add_map(int(st["tid"]), uid, name)
            await self._refresh_view()
            return

        if action.startswith("mapremove__"):
            try:
                await self.storage.remove_map(int(action[len("mapremove__"):]))
            except (TypeError, ValueError):
                return
            await self._refresh_view()
            return

        if action.startswith("mapup__") and st.get("tid"):
            try:
                await self.storage.move_map(int(st["tid"]), int(action[len("mapup__"):]), -1)
            except (TypeError, ValueError):
                return
            await self._refresh_view()
            return

        if action.startswith("mapdown__") and st.get("tid"):
            try:
                await self.storage.move_map(int(st["tid"]), int(action[len("mapdown__"):]), 1)
            except (TypeError, ValueError):
                return
            await self._refresh_view()
            return
