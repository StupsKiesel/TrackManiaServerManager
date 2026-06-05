"""tmsm_gamemodes - orchestrator AppConfig.

Hosts the shared services (vote engine, map picker), drives the active
game-mode lifecycle from Maniaplanet flow callbacks, and exposes the
operator + player UIs. Modes themselves live in ``modes/`` and register
via the ``@register`` decorator from ``base``.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from pyplanet.apps.config import AppConfig
from pyplanet.apps.core.maniaplanet import callbacks as mp_signals

from . import state as _state
from . import modes as _builtin_modes  # noqa: F401  side-effect: registers built-ins
from .base import REGISTRY, GameMode, GameModeContext
from .picker import MapPicker
from .views import OperatorView, VotePanelView
from .votes import VoteEngine

try:
    from pyplanet.apps.tmsm.hub import HubAppEntry, Role
    _HAS_HUB = True
except Exception:
    _HAS_HUB = False

logger = logging.getLogger(__name__)


class TmsmGamemodesApp(AppConfig):
    name = "pyplanet.apps.tmsm.tmsm_gamemodes"
    label = "tmsm_gamemodes"
    app_dependencies = ["core.maniaplanet"]
    game_dependencies = ["trackmania", "trackmania_next", "shootmania"]

    LEVEL_OPERATOR = 1
    CONFIG_PAGE_SIZE = 6

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.view: OperatorView | None = None
        self.vote_view: VotePanelView | None = None

        # Services.
        self.picker = MapPicker(self)
        self.votes = VoteEngine(self)

        # Persisted state.
        self._state: dict[str, Any] = _state.default_state()

        # Active mode instance + ctx (None while idle).
        self._active: GameMode | None = None
        self._active_ctx: GameModeContext | None = None

        # Per-operator UI session: which mode is being viewed/edited.
        self._operator_selection: dict[str, str] = {}   # login -> mode key
        self._operator_drafts: dict[str, dict[str, dict[str, Any]]] = {}
        # ^ login -> mode_key -> draft config (live entry-field values)
        self._operator_cfg_page: dict[str, dict[str, int]] = {}
        # ^ login -> mode_key -> 0-based config page index

        # Status lines published by the active mode (mirror for the UI).
        self._mode_status_lines: list[str] = []

        # Coalesce refresh requests fired from sync code (mode.status etc).
        self._refresh_pending: bool = False

    # ---- lifecycle -----------------------------------------------------

    async def on_start(self) -> None:
        try:
            self._state = _state.load()
        except Exception:
            logger.exception("gamemodes: state load failed")
            self._state = _state.default_state()

        # Permission gate (admin-level): only admins can toggle modes.
        try:
            await self.instance.permission_manager.register(
                "manage", "Manage tmsm game modes",
                app=self, min_level=2,
            )
        except Exception:
            logger.exception("gamemodes: permission register failed")

        # Maniaplanet flow signals - drive the active mode.
        self.context.signals.listen(mp_signals.flow.podium_start,
                                    self._on_podium_start)
        self.context.signals.listen("maniaplanet:map_begin",
                                    self._on_map_begin)
        self.context.signals.listen("maniaplanet:map_end",
                                    self._on_map_end)
        self.context.signals.listen("trackmania:finish",
                        self._on_player_finish)
        self.context.signals.listen("maniaplanet:player_chat",
                        self._on_player_chat)

        # Views.
        try:
            self.view = OperatorView(self)
            self.view.connect("mode_stop", self._on_mode_stop)
            self.view.connect("mode_save", self._on_mode_save)
            self.view.handle_catch_all = self._catch_all  # type: ignore[assignment]

            self.vote_view = VotePanelView(self)
            self.vote_view.handle_catch_all = self._catch_all  # type: ignore[assignment]
        except Exception:
            logger.exception("gamemodes: view init failed")
            return

        # Re-activate any mode that was running before a restart.
        if self._state.get("active") and self._state["active"] in REGISTRY:
            try:
                await self._activate(self._state["active"], announce=False)
            except Exception:
                logger.exception("gamemodes: auto-resume failed")
                self._state["active"] = None
                self._save_state()

        await self._register_with_hub()

    async def on_stop(self) -> None:
        if self._active is not None:
            try:
                await self._active.on_disable()
            except Exception:
                logger.exception("gamemodes: on_disable on stop failed")
        for v in (self.view, self.vote_view):
            if v is not None:
                try:
                    await v.destroy()
                except Exception:
                    logger.exception("gamemodes: view destroy failed")
        self.view = None
        self.vote_view = None

    # ---- persistence ---------------------------------------------------

    def _save_state(self) -> None:
        try:
            _state.save(self._state)
        except Exception:
            logger.exception("gamemodes: state save failed")

    # ---- hub -----------------------------------------------------------

    async def _register_with_hub(self) -> None:
        if not _HAS_HUB:
            return
        try:
            sig = self.context.signals.get_signal("tmsm_hub:register")
        except KeyError:
            logger.info("gamemodes: tmsm_hub:register not yet available")
            return
        entry = HubAppEntry(
            key="gamemodes",
            name="Game Modes",
            icon="cogs",
            color="0c4",
            role=Role.OPERATOR,
            order=60,
            description="Run custom scripted game modes (Evolution, ...).",
            open=self._open,
            command="gamemodes",
        )
        await sig.send_robust({"entry": entry}, raw=True)

    async def _open(self, player) -> None:
        if self.view is None:
            return
        try:
            await self.view.display(player_logins=[player.login])
            self.view._visible = True
        except Exception:
            logger.exception("gamemodes: open failed")

    # ---- helpers -------------------------------------------------------

    def _is_admin(self, player) -> bool:
        try:
            return int(getattr(player, "level", 0)) >= 2
        except Exception:
            return False

    def _config_for(self, mode_key: str, defaults: dict[str, Any]) -> dict[str, Any]:
        persisted = dict(self._state.get("configs", {}).get(mode_key) or {})
        return {**defaults, **persisted}

    @staticmethod
    def _parse_csv_list(raw: Any) -> list[str]:
        out: list[str] = []
        for part in str(raw or "").split(","):
            k = str(part or "").strip()
            if not k or k in out:
                continue
            out.append(k)
        return out

    @staticmethod
    def _join_csv_list(items: list[str]) -> str:
        return ",".join([str(x).strip() for x in items if str(x).strip()])

    def _known_widget_keys(self) -> list[str]:
        try:
            widgets_app = getattr(self.instance.apps, "apps", {}).get("tmsm_widgets")
        except Exception:
            widgets_app = None
        if widgets_app is None:
            return []
        try:
            return sorted(str(k) for k in getattr(widgets_app, "entries", {}).keys())
        except Exception:
            return []

    def _mode_widget_sets(self, login: str, mode_key: str) -> tuple[list[str], list[str], bool]:
        if mode_key not in REGISTRY:
            return [], [], False
        inst = REGISTRY[mode_key](GameModeContext(self, mode_key))
        defaults = inst.default_config()
        schema_keys = {str(f.get("key")) for f in inst.config_schema()}
        editable = "required_widgets_csv" in schema_keys and "extra_widgets_csv" in schema_keys
        if not editable:
            return [], [], False
        stored = self._config_for(mode_key, defaults)
        draft = self._operator_drafts.get(login, {}).get(mode_key, {})
        req_raw = draft.get("required_widgets_csv", stored.get("required_widgets_csv", ""))
        ext_raw = draft.get("extra_widgets_csv", stored.get("extra_widgets_csv", ""))
        req = self._parse_csv_list(req_raw)
        ext = self._parse_csv_list(ext_raw)
        ext = [k for k in ext if k not in req]
        return req, ext, True

    def _mode_layout_values(self, login: str, mode_key: str) -> tuple[dict[str, Any], bool]:
        if mode_key not in REGISTRY:
            return {}, False
        inst = REGISTRY[mode_key](GameModeContext(self, mode_key))
        schema_keys = {str(f.get("key")) for f in inst.config_schema()}
        needed = {
            "random_mx_points_x",
            "random_mx_points_y",
            "random_mx_points_w",
            "random_mx_points_h",
            "random_mx_points_drive_mode",
            "random_mx_points_anim_dir",
            "random_mx_points_anim_duration_ms",
            "random_mx_points_anim_delay_ms",
        }
        if not needed.issubset(schema_keys):
            return {}, False
        defaults = inst.default_config()
        stored = self._config_for(mode_key, defaults)
        draft = self._operator_drafts.get(login, {}).get(mode_key, {})

        def _num(key: str, fallback: int) -> int:
            raw = draft.get(key, stored.get(key, fallback))
            try:
                return int(raw)
            except (TypeError, ValueError):
                return int(fallback)

        anim_dir = str(
            draft.get(
                "random_mx_points_anim_dir",
                stored.get("random_mx_points_anim_dir", "left"),
            )
            or "left"
        ).strip().lower()
        if anim_dir not in {"none", "left", "right", "up", "down"}:
            anim_dir = "left"

        drive_mode = str(
            draft.get(
                "random_mx_points_drive_mode",
                stored.get("random_mx_points_drive_mode", "fixed"),
            )
            or "fixed"
        ).strip().lower()
        if drive_mode not in {"fixed", "hide_while_driving", "only_shown_while_driving"}:
            drive_mode = "fixed"

        return {
            "x": _num("random_mx_points_x", -126),
            "y": _num("random_mx_points_y", 70),
            "w": _num("random_mx_points_w", 58),
            "h": _num("random_mx_points_h", 22),
            "drive_mode": drive_mode,
            "anim_dir": anim_dir,
            "anim_duration_ms": _num("random_mx_points_anim_duration_ms", 180),
            "anim_delay_ms": _num("random_mx_points_anim_delay_ms", 0),
        }, True

    async def _on_widget_set_change(self, login: str, mode_key: str,
                                    req: list[str], ext: list[str]) -> None:
        ext = [k for k in ext if k not in req]
        await self._on_cfg_change(login, mode_key, "required_widgets_csv", self._join_csv_list(req))
        await self._on_cfg_change(login, mode_key, "extra_widgets_csv", self._join_csv_list(ext))

    async def _on_mode_widgets_edit(self, login: str, op: str, key: str) -> None:
        mode_key = self._operator_selection.get(login)
        if not mode_key:
            return
        req, ext, editable = self._mode_widget_sets(login, mode_key)
        if not editable:
            return
        if op == "addreq":
            if key not in req:
                req.append(key)
            ext = [k for k in ext if k != key]
        elif op == "addextra":
            if key not in req and key not in ext:
                ext.append(key)
        elif op == "delreq":
            await self._notify(
                "Required widgets cannot be removed.",
                "warning",
                login=login,
            )
            return
        elif op == "delextra":
            ext = [k for k in ext if k != key]
        else:
            return
        await self._on_widget_set_change(login, mode_key, req, ext)

    async def _on_mode_layout_edit(self, login: str, op: str, val: str) -> None:
        mode_key = self._operator_selection.get(login)
        if not mode_key:
            return
        current, editable = self._mode_layout_values(login, mode_key)
        if not editable:
            return

        mapping = {
            "x": "random_mx_points_x",
            "y": "random_mx_points_y",
            "w": "random_mx_points_w",
            "h": "random_mx_points_h",
            "dur": "random_mx_points_anim_duration_ms",
            "delay": "random_mx_points_anim_delay_ms",
        }
        if op in mapping:
            try:
                delta = int(val)
            except (TypeError, ValueError):
                return
            key = mapping[op]
            cur = int(current.get({
                "x": "x",
                "y": "y",
                "w": "w",
                "h": "h",
                "dur": "anim_duration_ms",
                "delay": "anim_delay_ms",
            }[op], 0))
            await self._on_cfg_change(login, mode_key, key, cur + delta)
            return

        if op == "anim":
            direction = str(val or "").strip().lower()
            if direction not in {"none", "left", "right", "up", "down"}:
                return
            await self._on_cfg_change(
                login,
                mode_key,
                "random_mx_points_anim_dir",
                direction,
            )
            return

        if op == "drive":
            mode = str(val or "").strip().lower()
            if mode not in {"fixed", "hide_while_driving", "only_shown_while_driving"}:
                return
            await self._on_cfg_change(
                login,
                mode_key,
                "random_mx_points_drive_mode",
                mode,
            )
            return

    def _mode_meta(self, mode_key: str) -> dict[str, Any]:
        cls = REGISTRY[mode_key]
        return {
            "key":         cls.key,
            "name":        cls.name,
            "description": cls.description,
            "icon":        cls.icon,
            "color":       cls.color,
            "category":    cls.category,
            "is_active":   (self._active is not None and self._active.key == cls.key),
        }

    def _cfg_page_for(self, login: str, mode_key: str, total_rows: int) -> tuple[int, int]:
        pages = max(1, (max(0, int(total_rows)) + self.CONFIG_PAGE_SIZE - 1) // self.CONFIG_PAGE_SIZE)
        store = self._operator_cfg_page.setdefault(login, {})
        page = int(store.get(mode_key, 0) or 0)
        if page < 0:
            page = 0
        if page >= pages:
            page = pages - 1
        store[mode_key] = page
        return page, pages

    # ---- notification --------------------------------------------------

    async def _notify(self, message: str, severity: str = "info",
                      login: str | None = None,
                      duration_ms: int = 4000) -> None:
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
                "message":     message,
                "severity":    severity,
                "login":       login,
                "duration_ms": duration_ms,
                "source":      "tmsm_gamemodes",
            }, raw=True)
        except Exception:
            logger.exception("gamemodes: notify failed")

    # ---- context builders ---------------------------------------------

    async def operator_context(self, login: str) -> dict[str, Any]:
        modes_meta = [self._mode_meta(k) for k in sorted(REGISTRY.keys())]
        selected = self._operator_selection.get(login)
        # Auto-select the active mode (or the first available) so the right
        # pane is never empty on first open.
        if selected is None or selected not in REGISTRY:
            selected = (self._active.key if self._active else
                        (modes_meta[0]["key"] if modes_meta else None))
            if selected:
                self._operator_selection[login] = selected

        cfg_rows: list[dict[str, Any]] = []
        cfg_page = 1
        cfg_pages_total = 1
        cfg_has_prev = False
        cfg_has_next = False
        mode_widgets_editable = False
        mode_required_widgets: list[str] = []
        mode_extra_widgets: list[str] = []
        mode_available_widgets: list[str] = []
        mode_layout_editable = False
        mode_layout: dict[str, Any] = {}
        if selected:
            cls = REGISTRY[selected]
            tmp_ctx = GameModeContext(self, selected)
            tmp_instance = cls(tmp_ctx)
            defaults = tmp_instance.default_config()
            stored = self._config_for(selected, defaults)
            draft = self._operator_drafts.get(login, {}).get(selected, {})
            all_rows: list[dict[str, Any]] = []
            for f in tmp_instance.config_schema():
                val = draft.get(f["key"], stored.get(f["key"], f.get("default")))
                all_rows.append({
                    "key":     f["key"],
                    "label":   f["label"],
                    "type":    f["type"],
                    "value":   val,
                    "help":    f.get("help", ""),
                    "min":     f.get("min"),
                    "max":     f.get("max"),
                    "choices": f.get("choices") or [],
                })
            page0, cfg_pages_total = self._cfg_page_for(login, selected, len(all_rows))
            start = page0 * self.CONFIG_PAGE_SIZE
            end = start + self.CONFIG_PAGE_SIZE
            cfg_rows = all_rows[start:end]
            cfg_page = page0 + 1
            cfg_has_prev = page0 > 0
            cfg_has_next = page0 < (cfg_pages_total - 1)

            mode_required_widgets, mode_extra_widgets, mode_widgets_editable = self._mode_widget_sets(login, selected)
            if mode_widgets_editable:
                active = set(mode_required_widgets + mode_extra_widgets)
                known = self._known_widget_keys()
                mode_available_widgets = [k for k in known if k not in active]
            mode_layout, mode_layout_editable = self._mode_layout_values(login, selected)

        return {
            "modes":                modes_meta,
            "active_key":           (self._active.key if self._active else None),
            "active_name":          (self._active.name if self._active else ""),
            "active_status_lines":  list(self._mode_status_lines),
            "active_config":        cfg_rows,
            "cfg_page":             cfg_page,
            "cfg_pages_total":      cfg_pages_total,
            "cfg_has_prev":         cfg_has_prev,
            "cfg_has_next":         cfg_has_next,
            "mode_widgets_editable": mode_widgets_editable,
            "mode_required_widgets": mode_required_widgets,
            "mode_extra_widgets":    mode_extra_widgets,
            "mode_available_widgets": mode_available_widgets,
            "mode_layout_editable": mode_layout_editable,
            "mode_layout":          mode_layout,
            "selected_key":         selected,
            "editing_key":          selected,
            "vote_snapshot":        self.votes.snapshot(),
            "is_admin":             True,   # gate enforced on actions
        }

    async def vote_panel_context(self, login: str) -> dict[str, Any]:
        snap = self.votes.snapshot()
        picked = None
        if snap and login in snap["ballots"]:
            picked = snap["ballots"][login]
        return {
            "vote":         snap,
            "has_voted":    bool(snap and login in snap["ballots"]),
            "picked_value": picked,
        }

    # ---- mode activation ----------------------------------------------

    async def _activate(self, mode_key: str, *, announce: bool = True) -> None:
        if mode_key not in REGISTRY:
            return
        if self._active is not None and self._active.key == mode_key:
            return
        # Deactivate current first.
        if self._active is not None:
            await self._deactivate(announce=False)
        cls = REGISTRY[mode_key]
        ctx = GameModeContext(self, mode_key)
        instance = cls(ctx)
        cfg = self._config_for(mode_key, instance.default_config())
        self._active = instance
        self._active_ctx = ctx
        self._state["active"] = mode_key
        self._save_state()
        try:
            await instance.on_enable(cfg)
        except Exception:
            logger.exception("gamemodes: %s on_enable failed", mode_key)
            try:
                await ctx.clear_widget_overrides()
            except Exception:
                logger.exception("gamemodes: %s clear_widget_overrides after failed enable", mode_key)
            await self._notify(f"Failed to start {cls.name}", "error")
            self._active = None
            self._active_ctx = None
            self._state["active"] = None
            self._save_state()
            return
        if announce:
            await self._notify(f"Game mode '{cls.name}' is now active",
                               "success")
        self._mode_status_lines = list(instance.status_lines())
        await self._refresh_operator()

    async def _deactivate(self, *, announce: bool = True) -> None:
        if self._active is None:
            return
        cls = type(self._active)
        ctx = self._active_ctx
        try:
            await self._active.on_disable()
        except Exception:
            logger.exception("gamemodes: %s on_disable failed", cls.key)
        if ctx is not None:
            try:
                await ctx.clear_widget_overrides()
            except Exception:
                logger.exception("gamemodes: %s clear_widget_overrides on disable failed", cls.key)
        self._active = None
        self._active_ctx = None
        self._mode_status_lines = []
        self._state["active"] = None
        self._save_state()
        # Cancel any vote left dangling.
        if self.votes.is_active:
            try:
                await self.votes.cancel()
            except Exception:
                logger.exception("gamemodes: cancel vote on deactivate failed")
        if announce:
            await self._notify(f"Game mode '{cls.name}' stopped", "info")
        await self._refresh_operator()

    # ---- maniaplanet flow ---------------------------------------------

    async def _on_map_begin(self, *args, **kwargs) -> None:
        if self._active is None:
            return
        try:
            await self._active.on_map_begin(kwargs.get("map"))
        except Exception:
            logger.exception("gamemodes: %s on_map_begin failed", self._active.key)

    async def _on_map_end(self, *args, **kwargs) -> None:
        if self._active is None:
            return
        try:
            await self._active.on_map_end(kwargs.get("map"))
        except Exception:
            logger.exception("gamemodes: %s on_map_end failed", self._active.key)

    async def _on_podium_start(self, *args, **kwargs) -> None:
        if self._active is None:
            return
        try:
            await self._active.on_podium_start()
        except Exception:
            logger.exception("gamemodes: %s on_podium_start failed",
                             self._active.key)

    async def _on_player_finish(self, *args, **kwargs) -> None:
        if self._active is None:
            return
        try:
            data = dict(kwargs)
            player = data.pop("player", None)
            await self._active.on_player_finish(player, **data)
        except Exception:
            logger.exception("gamemodes: %s on_player_finish failed",
                             self._active.key)

    async def _on_player_chat(self, *args, **kwargs) -> None:
        if self._active is None:
            return
        try:
            data = dict(kwargs)
            player = data.pop("player", None)
            text = data.get("text")
            if text is None:
                text = data.get("message")
            if text is None:
                text = data.get("msg")
            data.pop("text", None)
            data.pop("message", None)
            data.pop("msg", None)
            await self._active.on_player_chat(
                player,
                text=str(text or ""),
                **data,
            )
        except Exception:
            logger.exception("gamemodes: %s on_player_chat failed",
                             self._active.key)

    # ---- vote engine hooks --------------------------------------------

    async def _on_vote_started(self) -> None:
        if self.vote_view is not None:
            try:
                await self.vote_view.show()
            except Exception:
                logger.exception("gamemodes: vote_view show failed")
        await self._refresh_operator()

    async def _on_vote_progress(self) -> None:
        if self.vote_view is not None:
            try:
                await self.vote_view.refresh()
            except Exception:
                logger.exception("gamemodes: vote_view refresh failed")

    async def _on_vote_ended(self, result: dict[str, Any] | None) -> None:
        if self.vote_view is not None:
            try:
                await self.vote_view.hide()
            except Exception:
                logger.exception("gamemodes: vote_view hide failed")
        await self._refresh_operator()

    # ---- refresh helpers ----------------------------------------------

    def _schedule_refresh(self) -> None:
        """Coalesce sync calls into one async operator refresh."""
        if self._refresh_pending:
            return
        self._refresh_pending = True

        async def _go():
            try:
                await asyncio.sleep(0)
                await self._refresh_operator()
            finally:
                self._refresh_pending = False
        asyncio.ensure_future(_go())

    async def _refresh_operator(self) -> None:
        if self.view is not None:
            try:
                await self.view.refresh()
            except Exception:
                logger.exception("gamemodes: operator view refresh failed")

    # ---- action handlers ----------------------------------------------

    async def _on_mode_select(self, player, mode_key: str) -> None:
        if mode_key not in REGISTRY:
            return
        self._operator_selection[player.login] = mode_key
        self._operator_cfg_page.setdefault(player.login, {}).setdefault(mode_key, 0)
        await self._refresh_operator()

    async def _on_cfg_page(self, login: str, mode_key: str, direction: str) -> None:
        if mode_key not in REGISTRY:
            return
        total = len(REGISTRY[mode_key](GameModeContext(self, mode_key)).config_schema())
        page0, pages = self._cfg_page_for(login, mode_key, total)
        if direction == "prev":
            page0 = max(0, page0 - 1)
        elif direction == "next":
            page0 = min(pages - 1, page0 + 1)
        self._operator_cfg_page.setdefault(login, {})[mode_key] = page0
        await self._refresh_operator()

    async def _on_mode_start(self, player, mode_key: str) -> None:
        if not self._is_admin(player):
            await self._notify("Admin only.", "warning", login=player.login)
            return
        await self._activate(mode_key)

    async def _on_mode_stop(self, player) -> None:
        if not self._is_admin(player):
            await self._notify("Admin only.", "warning", login=player.login)
            return
        await self._deactivate()

    async def _on_mode_save(self, player) -> None:
        if not self._is_admin(player):
            await self._notify("Admin only.", "warning", login=player.login)
            return
        selected = self._operator_selection.get(player.login)
        if not selected:
            return
        draft = self._operator_drafts.get(player.login, {}).get(selected)
        if not draft:
            await self._notify("Nothing changed.", "info", login=player.login)
            return
        # Persist into state.configs[<mode>].
        cfg = self._config_for(selected, REGISTRY[selected](GameModeContext(self, selected)).default_config())
        for k, v in draft.items():
            cfg[k] = v
        self._state.setdefault("configs", {})[selected] = cfg
        self._save_state()
        # Clear the local draft now that it lives in state.
        self._operator_drafts.get(player.login, {}).pop(selected, None)
        await self._notify(f"{REGISTRY[selected].name} config saved.",
                           "success", login=player.login)
        await self._refresh_operator()

    async def _on_cfg_change(self, login: str, mode_key: str,
                             field_key: str, raw_value: Any) -> None:
        """Stash a draft change for the operator's session."""
        if mode_key not in REGISTRY:
            return
        instance = REGISTRY[mode_key](GameModeContext(self, mode_key))
        schema = instance.config_schema()
        field = next((f for f in schema if f["key"] == field_key), None)
        if field is None:
            return
        if field["type"] == "int":
            try:
                value: Any = int(str(raw_value).strip())
            except (TypeError, ValueError):
                return
            if field.get("min") is not None:
                value = max(int(field["min"]), value)
            if field.get("max") is not None:
                value = min(int(field["max"]), value)
        elif field["type"] == "bool":
            value = bool(raw_value)
        else:
            value = str(raw_value)
        drafts = self._operator_drafts.setdefault(login, {})
        mode_draft = drafts.setdefault(mode_key, {})
        mode_draft[field_key] = value
        await self._refresh_operator()

    # ---- catch-all router ---------------------------------------------

    async def _catch_all(self, player, action, values, **kwargs) -> None:
        login = player.login
        self._absorb(login, values)

        # mode selection
        m = re.match(r"^mode_select__(\w+)$", action)
        if m:
            await self._on_mode_select(player, m.group(1))
            return
        m = re.match(r"^mode_start__(\w+)$", action)
        if m:
            await self._on_mode_start(player, m.group(1))
            return

        # config: bool toggle (check_box fires "<name>" on click)
        m = re.match(r"^cfg__(\w+)$", action)
        if m:
            selected = self._operator_selection.get(login)
            if selected:
                # Only treat as a toggle if the field is actually a bool;
                # int/str fields are pure entry inputs and reach us via
                # `_absorb` on the surrounding submit action.
                instance = REGISTRY[selected](GameModeContext(self, selected))
                field = next((f for f in instance.config_schema()
                              if f["key"] == m.group(1)), None)
                if field is not None and field["type"] == "bool":
                    cfg = self._config_for(selected, instance.default_config())
                    draft = self._operator_drafts.get(login, {}).get(selected, {})
                    cur = bool(draft.get(m.group(1), cfg.get(m.group(1), False)))
                    await self._on_cfg_change(login, selected, m.group(1), not cur)
                    return

        m = re.match(r"^cfgpage__(prev|next)$", action)
        if m:
            selected = self._operator_selection.get(login)
            if selected:
                await self._on_cfg_page(login, selected, m.group(1))
            return

        m = re.match(r"^gmw__(addreq|addextra|delreq|delextra)__([0-9a-zA-Z_\-]+)$", action)
        if m:
            await self._on_mode_widgets_edit(login, m.group(1), m.group(2))
            return

        m = re.match(r"^gml__(x|y|w|h|dur|delay)__(-?\d+)$", action)
        if m:
            await self._on_mode_layout_edit(login, m.group(1), m.group(2))
            return

        m = re.match(r"^gml__anim__(none|left|right|up|down)$", action)
        if m:
            await self._on_mode_layout_edit(login, "anim", m.group(1))
            return

        m = re.match(r"^gml__drive__(fixed|hide_while_driving|only_shown_while_driving)$", action)
        if m:
            await self._on_mode_layout_edit(login, "drive", m.group(1))
            return

        # vote ballot
        m = re.match(r"^vote__pick__(.+)$", action)
        if m:
            await self.votes.cast(login, m.group(1))
            return

    def _absorb(self, login: str, values: dict[str, Any]) -> None:
        """Pull every ``entry_<viewid>__cfg__<key>`` field out of the form
        submission and feed it through the draft store. Runs at the top of
        every action so the most recent values are always captured.
        """
        if not values:
            return
        selected = self._operator_selection.get(login)
        if not selected or self.view is None:
            return
        prefix = f"entry_{self.view.id}__cfg__"
        for raw_key, raw_val in values.items():
            if not raw_key.startswith(prefix):
                continue
            field_key = raw_key[len(prefix):]
            # Push through the draft path synchronously; refresh happens at
            # the end of the outer action so we don't double-fire.
            drafts = self._operator_drafts.setdefault(login, {})
            mode_draft = drafts.setdefault(selected, {})
            mode_draft[field_key] = (raw_val or "")
