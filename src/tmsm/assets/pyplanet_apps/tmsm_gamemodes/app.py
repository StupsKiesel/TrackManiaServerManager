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
from pyplanet.views.template import TemplateView

from . import state as _state
from . import modes as _builtin_modes  # noqa: F401  side-effect: registers built-ins
from .base import REGISTRY, GameMode, GameModeContext
from .picker import MapPicker
from .views import AdminView, OperatorView, RmcResultsView, VotePanelView
from .votes import VoteEngine
from .widget_layout_storage import WidgetLayoutStorage

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
    CONFIG_ROW_HEIGHT = 7
    CONFIG_GROUP_BASE_HEIGHT = 18
    CONFIG_PAG_Y = -143  # -win_h + 7 (win_h=150 in template)
    CONFIG_PAG_BTN_H = 4.5
    CONFIG_PAG_TOP_GAP = 1.0
    REQUIRED_WIDGET_VIS_OWNER = "gamemodes:required_visibility"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.operator_view: OperatorView | None = None
        self.admin_view: AdminView | None = None
        self.vote_view: VotePanelView | None = None
        self.rmc_results_view: RmcResultsView | None = None
        # Latest RMC run row id used to feed `rmc_results_context` after a
        # run finishes; None until a run is persisted.
        self._rmc_last_run_id: int | None = None

        # Services.
        self.picker = MapPicker(self)
        self.votes = VoteEngine(self)
        self.widget_layout = WidgetLayoutStorage(self.instance)

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
        self._operator_cfg_combo_open: dict[str, dict[str, str | None]] = {}
        # ^ login -> mode_key -> field_key currently open as combo (choice)
        self._operator_wprof_draft: dict[str, dict[str, dict[str, Any]]] = {}
        # ^ login -> mode_key -> widget profile draft fields
        self._operator_wprof_window_open: dict[str, bool] = {}
        # ^ login -> widget profile sub-window visibility
        self._operator_wprof_view: dict[str, str] = {}
        # ^ login -> 'list' | 'picker' | 'editor' (active sub-view)
        self._operator_wprof_list_page: dict[str, dict[str, int]] = {}
        # ^ login -> mode_key -> 0-based page index for LIST view
        self._operator_wprof_picker_page: dict[str, dict[str, int]] = {}
        # ^ login -> mode_key -> 0-based page index for PICKER view

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
        self._ensure_policy_defaults()

        # Widget layout overrides (per-mode) live in the PyPlanet DB.
        try:
            await self.widget_layout.load()
        except Exception:
            logger.exception("gamemodes: widget layout load failed")
        await self._migrate_widget_layout_from_json()

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
            self.operator_view = OperatorView(self)
            self.operator_view.connect("mode_stop", self._on_mode_stop)
            self.operator_view.connect("mode_save", self._on_mode_save)
            self.operator_view.handle_catch_all = self._catch_all  # type: ignore[assignment]

            self.admin_view = AdminView(self)
            self.admin_view.connect("mode_stop", self._on_mode_stop)
            self.admin_view.connect("mode_save", self._on_mode_save)
            self.admin_view.handle_catch_all = self._catch_all  # type: ignore[assignment]

            self.vote_view = VotePanelView(self)
            self.vote_view.handle_catch_all = self._catch_all  # type: ignore[assignment]

            self.rmc_results_view = RmcResultsView(self)
            self.rmc_results_view.handle_catch_all = self._catch_all  # type: ignore[assignment]
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

        await self._sync_required_widget_visibility()

        await self._register_with_hub()

    async def on_stop(self) -> None:
        if self._active is not None:
            try:
                await self._active.on_disable()
            except Exception:
                logger.exception("gamemodes: on_disable on stop failed")
        for v in (self.operator_view, self.admin_view, self.vote_view, self.rmc_results_view):
            if v is not None:
                try:
                    await v.destroy()
                except Exception:
                    logger.exception("gamemodes: view destroy failed")
        self.operator_view = None
        self.admin_view = None
        self.vote_view = None
        self.rmc_results_view = None

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
            key="gamemodes_ops",
            name="Game Modes",
            icon="cogs",
            color="0c4",
            role=Role.OPERATOR,
            order=60,
            description="Operator game mode control (start/stop and allowed settings).",
            open=self._open_operator,
            command="gamemodes",
        )
        await sig.send_robust({"entry": entry}, raw=True)

        admin_entry = HubAppEntry(
            key="gamemodes_admin",
            name="Game Modes Admin",
            icon="cogs",
            color="0c4",
            role=Role.ADMIN,
            order=61,
            description="Admin policy + full game mode configuration.",
            open=self._open_admin,
            command="gamemodes",
        )
        await sig.send_robust({"entry": admin_entry}, raw=True)

    async def _open_operator(self, player) -> None:
        if self.operator_view is None:
            return
        if not self._is_operator(player):
            await self._notify("Operator only.", "warning", login=getattr(player, "login", None))
            return
        try:
            await self.operator_view.display(player_logins=[player.login])
            self.operator_view._visible = True
            self.operator_view._visible_logins.add(player.login)
        except Exception:
            logger.exception("gamemodes: open operator failed")

    async def _open_admin(self, player) -> None:
        if self.admin_view is None:
            return
        if not self._is_admin(player):
            await self._notify("Admin only.", "warning", login=getattr(player, "login", None))
            return
        try:
            await self.admin_view.display(player_logins=[player.login])
            self.admin_view._visible = True
            self.admin_view._visible_logins.add(player.login)
        except Exception:
            logger.exception("gamemodes: open admin failed")

    # ---- helpers -------------------------------------------------------

    def _is_admin(self, player) -> bool:
        try:
            return int(getattr(player, "level", 0)) >= 2
        except Exception:
            return False

    def _is_operator(self, player) -> bool:
        try:
            return int(getattr(player, "level", 0)) >= self.LEVEL_OPERATOR
        except Exception:
            return False

    def _policy(self) -> dict[str, Any]:
        p = self._state.setdefault("operator_policy", {})
        p.setdefault("allowed_modes", [])
        p.setdefault("allowed_fields", {})
        return p

    def _schema_field_keys(self, mode_key: str) -> list[str]:
        if mode_key not in REGISTRY:
            return []
        inst = REGISTRY[mode_key](GameModeContext(self, mode_key))
        return [str(f.get("key")) for f in inst.config_schema() if str(f.get("key"))]

    def _ensure_policy_defaults(self) -> None:
        p = self._policy()
        all_modes = sorted(REGISTRY.keys())
        if "allowed_modes" not in p:
            p["allowed_modes"] = list(all_modes)
        p["allowed_modes"] = [m for m in p.get("allowed_modes", []) if m in REGISTRY]

        af = p.setdefault("allowed_fields", {})
        cleaned: dict[str, list[str]] = {}
        for mode_key in all_modes:
            schema_keys = self._schema_field_keys(mode_key)
            raw = af.get(mode_key)
            if raw is None:
                cleaned[mode_key] = list(schema_keys)
                continue
            allowed = [str(k) for k in (raw or []) if str(k) in schema_keys]
            cleaned[mode_key] = allowed
        p["allowed_fields"] = cleaned

    def _operator_allowed_modes(self) -> set[str]:
        self._ensure_policy_defaults()
        return set(self._policy().get("allowed_modes", []))

    def _operator_allowed_fields(self, mode_key: str) -> set[str]:
        self._ensure_policy_defaults()
        af = self._policy().get("allowed_fields", {})
        return set(str(k) for k in (af.get(mode_key, []) or []))

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

    def _widget_engine_app(self):
        try:
            apps_map = getattr(self.instance.apps, "apps", {}) or {}
        except Exception:
            return None
        for label in ("widget_engine", "tmsm_widgets"):
            app = apps_map.get(label)
            if app is not None:
                return app
        return None

    def _known_widget_keys(self) -> list[str]:
        app = self._widget_engine_app()
        if app is None:
            return []
        keys: set[str] = set()
        for attr in ("_entries", "entries", "_available"):
            container = getattr(app, attr, None)
            if not container:
                continue
            try:
                for k in container.keys():
                    if k:
                        keys.add(str(k))
            except Exception:
                continue
        return sorted(keys)

    def _mode_widget_layout_profile(self, mode_key: str) -> list[dict[str, Any]]:
        return self.widget_layout.get(mode_key)

    async def _set_mode_widget_layout_profile(
        self, mode_key: str, rows: list[dict[str, Any]],
    ) -> None:
        await self.widget_layout.replace_mode(mode_key, rows)

    async def _migrate_widget_layout_from_json(self) -> None:
        """One-shot: migrate `widget_layout_profiles` from the legacy JSON
        state file into `gm_widget_config`, then drop the key from state."""
        legacy = self._state.pop("widget_layout_profiles", None)
        if not isinstance(legacy, dict) or not legacy:
            return
        if self.widget_layout.all():
            # DB already populated; legacy file is stale, just discard.
            self._save_state()
            return
        migrated = 0
        for mode_key, rows in legacy.items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict):
                    await self.widget_layout.upsert(str(mode_key), row)
                    migrated += 1
        self._save_state()
        if migrated:
            logger.info(
                "gamemodes: migrated %d widget override(s) from JSON to gm_widget_config",
                migrated,
            )

    def _wprof_default_draft(self) -> dict[str, Any]:
        return {
            "widget_key": "",
            "x": 0.0,
            "y": 0.0,
            "w": 60.0,
            "h": 24.0,
            "disabled": False,
            "drive_mode": None,
            "anim_dir": None,
            "anim_duration_ms": None,
            "anim_in_delay_ms": None,
            "anim_out_delay_ms": None,
            "pos_step": 1.0,
            "anchor_row": None,
        }

    WPROF_STEP_CYCLE: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0)

    WPROF_LIST_PAGE_SIZE = 10
    WPROF_PICKER_PAGE_SIZE = 12

    @staticmethod
    def _wprof_format(value: float) -> str:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return str(value)
        if abs(f - round(f)) < 1e-6:
            return str(int(round(f)))
        return f"{f:.1f}"

    def _wprof_cycle_step(self, current: float, direction: int) -> float:
        try:
            idx = self.WPROF_STEP_CYCLE.index(float(current))
        except (ValueError, TypeError):
            idx = 0
        nxt = (idx + (1 if direction > 0 else -1)) % len(self.WPROF_STEP_CYCLE)
        return self.WPROF_STEP_CYCLE[nxt]

    @staticmethod
    def _wprof_paginate(rows: list[Any], page0: int, per_page: int
                        ) -> tuple[list[Any], int, int]:
        per_page = max(1, int(per_page or 1))
        total = len(rows)
        pages = max(1, (total + per_page - 1) // per_page)
        page0 = max(0, min(int(page0 or 0), pages - 1))
        start = page0 * per_page
        return rows[start:start + per_page], page0, pages

    def _wprof_list_page(self, login: str, mode_key: str, total_rows: int
                         ) -> tuple[int, int]:
        store = self._operator_wprof_list_page.setdefault(login, {})
        page0 = int(store.get(mode_key, 0) or 0)
        _, page0, pages = self._wprof_paginate([None] * total_rows, page0,
                                               self.WPROF_LIST_PAGE_SIZE)
        store[mode_key] = page0
        return page0, pages

    def _wprof_picker_page(self, login: str, mode_key: str, total_rows: int
                           ) -> tuple[int, int]:
        store = self._operator_wprof_picker_page.setdefault(login, {})
        page0 = int(store.get(mode_key, 0) or 0)
        _, page0, pages = self._wprof_paginate([None] * total_rows, page0,
                                               self.WPROF_PICKER_PAGE_SIZE)
        store[mode_key] = page0
        return page0, pages

    def _wprof_apply_page_action(self, page0: int, pages: int, action: str) -> int:
        if action == "first":
            return 0
        if action == "prev":
            return max(0, page0 - 1)
        if action == "next":
            return min(pages - 1, page0 + 1)
        if action == "last":
            return max(0, pages - 1)
        if action.startswith("page__"):
            try:
                n = int(action.split("__", 1)[1])
                return max(0, min(pages - 1, n - 1))
            except (TypeError, ValueError):
                return page0
        return page0

    def _wprof_get_draft(self, login: str, mode_key: str) -> dict[str, Any]:
        by_mode = self._operator_wprof_draft.setdefault(login, {})
        return by_mode.setdefault(mode_key, self._wprof_default_draft())

    def _wprof_load_from_row(self, login: str, mode_key: str, widget_key: str) -> dict[str, Any]:
        row = next((r for r in self._mode_widget_layout_profile(mode_key)
                    if str(r.get("widget_key")) == widget_key), None)
        draft = self._wprof_get_draft(login, mode_key)
        draft["widget_key"] = widget_key
        if row is not None:
            draft["x"] = float(row.get("x", 0.0))
            draft["y"] = float(row.get("y", 0.0))
            draft["w"] = float(row.get("w", 60.0))
            draft["h"] = float(row.get("h", 24.0))
            draft["disabled"] = bool(row.get("disabled", False))
            draft["drive_mode"] = row.get("drive_mode")
            draft["anim_dir"] = row.get("anim_dir")
            draft["anim_duration_ms"] = row.get("anim_duration_ms")
            draft["anim_in_delay_ms"] = row.get("anim_in_delay_ms")
            draft["anim_out_delay_ms"] = row.get("anim_out_delay_ms")
            draft["anchor_row"] = {
                "x": float(row.get("x", 0.0)),
                "y": float(row.get("y", 0.0)),
                "w": float(row.get("w", 60.0)),
                "h": float(row.get("h", 24.0)),
                "disabled": bool(row.get("disabled", False)),
                "drive_mode": row.get("drive_mode"),
                "anim_dir": row.get("anim_dir"),
                "anim_duration_ms": row.get("anim_duration_ms"),
                "anim_in_delay_ms": row.get("anim_in_delay_ms"),
                "anim_out_delay_ms": row.get("anim_out_delay_ms"),
            }
        else:
            d = self._wprof_default_draft()
            for k in ("x", "y", "w", "h", "disabled",
                      "drive_mode", "anim_dir",
                      "anim_duration_ms", "anim_in_delay_ms", "anim_out_delay_ms"):
                draft[k] = d[k]
            draft["anchor_row"] = None
        draft.setdefault("pos_step", 1.0)
        return draft

    def _wprof_is_dirty(self, draft: dict[str, Any]) -> bool:
        anchor = draft.get("anchor_row")
        if anchor is None:
            return True
        for key in ("x", "y", "w", "h"):
            if float(draft.get(key, 0.0) or 0.0) != float(anchor.get(key, 0.0) or 0.0):
                return True
        if bool(draft.get("disabled", False)) != bool(anchor.get("disabled", False)):
            return True
        for key in ("drive_mode", "anim_dir",
                    "anim_duration_ms", "anim_in_delay_ms", "anim_out_delay_ms"):
            if draft.get(key) != anchor.get(key):
                return True
        return False

    async def _apply_mode_widget_layout_profile(self, mode_key: str) -> None:
        if self._active_ctx is None or self._active is None or self._active.key != mode_key:
            return
        rows = self._mode_widget_layout_profile(mode_key)
        await self._active_ctx.apply_widget_layout(rows)

    # ---- widget-layout editor preview / highlight ----------------------
    #
    # While the admin sub-window is showing the editor for one widget, we
    # mirror the widget_engine "edit mode" highlight (green tint on the
    # live widget) and push the draft as a runtime layout override under
    # a per-login owner so the widget visibly snaps to the draft x/y/w/h
    # and anim settings in real-time. Both are torn down on close / back
    # / cancel / apply so nothing leaks across sessions.

    @staticmethod
    def _wprof_preview_owner(login: str) -> str:
        return f"gm_wprof_edit:{login}"

    def _wprof_engine(self):
        we_app = self._widget_engine_app()
        return getattr(we_app, "engine", None) if we_app else None

    async def _wprof_apply_preview(self, login: str) -> None:
        engine = self._wprof_engine()
        if engine is None:
            return
        mode_key = self._operator_selection.get(login)
        view = self._operator_wprof_view.get(login)
        if not mode_key or view != "editor":
            await self._wprof_clear_preview(login)
            return
        draft = (self._operator_wprof_draft.get(login, {}) or {}).get(mode_key) or {}
        widget_key = str(draft.get("widget_key") or "").strip()
        if not widget_key:
            await self._wprof_clear_preview(login)
            return
        patch: dict[str, Any] = {
            "x": float(draft.get("x", 0.0)),
            "y": float(draft.get("y", 0.0)),
            "w": float(draft.get("w", 60.0)),
            "h": float(draft.get("h", 24.0)),
            "disabled": bool(draft.get("disabled", False)),
        }
        for k in ("drive_mode", "anim_dir",
                  "anim_duration_ms", "anim_in_delay_ms", "anim_out_delay_ms"):
            v = draft.get(k)
            if v is not None and v != "":
                patch[k] = v
        owner = self._wprof_preview_owner(login)
        try:
            await engine.set_runtime_layout(owner, widget_key, patch)
        except Exception:
            logger.exception("gamemodes: wprof preview set_runtime_layout failed")
        try:
            await engine.enter_edit(login, widget_key)
        except Exception:
            logger.exception("gamemodes: wprof enter_edit failed")

    async def _wprof_clear_preview(self, login: str) -> None:
        engine = self._wprof_engine()
        if engine is None:
            return
        owner = self._wprof_preview_owner(login)
        try:
            await engine.clear_runtime_owner(owner)
        except Exception:
            logger.exception("gamemodes: wprof clear_runtime_owner failed")
        try:
            await engine.exit_edit(login)
        except Exception:
            logger.exception("gamemodes: wprof exit_edit failed")

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

    def _mode_required_widgets(self, mode_key: str) -> list[str]:
        if mode_key not in REGISTRY:
            return []
        inst = REGISTRY[mode_key](GameModeContext(self, mode_key))
        cfg = self._config_for(mode_key, inst.default_config())
        return self._parse_csv_list(cfg.get("required_widgets_csv", ""))

    def _mode_widget_keys(self, mode_key: str) -> list[str]:
        """Return all configured widget keys for a mode (required + extra)."""
        if mode_key not in REGISTRY:
            return []
        inst = REGISTRY[mode_key](GameModeContext(self, mode_key))
        cfg = self._config_for(mode_key, inst.default_config())
        required = self._parse_csv_list(cfg.get("required_widgets_csv", ""))
        extra = self._parse_csv_list(cfg.get("extra_widgets_csv", ""))
        merged = list(required)
        for key in extra:
            if key and key not in merged:
                merged.append(key)
        return merged

    async def _refresh_widget_keys(self, keys: list[str]) -> None:
        if not keys:
            return
        try:
            sig_new = self.context.signals.get_signal("widget_engine:refresh")
        except Exception:
            sig_new = None
        try:
            sig_old = self.context.signals.get_signal("tmsm_widgets:refresh")
        except Exception:
            sig_old = None
        if sig_new is None and sig_old is None:
            return
        for key in keys:
            payload = {"key": str(key)}
            if sig_new is not None:
                try:
                    await sig_new.send_robust(payload, raw=True)
                except Exception:
                    logger.exception("gamemodes: widget_engine refresh failed for '%s'", key)
            if sig_old is not None:
                try:
                    await sig_old.send_robust(payload, raw=True)
                except Exception:
                    logger.exception("gamemodes: legacy widget refresh failed for '%s'", key)

    async def _sync_required_widget_visibility(self) -> None:
        """Force required widgets off for inactive modes via runtime layout."""
        try:
            sig_apply = self.context.signals.get_signal("tmsm_widgets:runtime_layout_apply")
            sig_clear = self.context.signals.get_signal("tmsm_widgets:runtime_layout_clear_owner")
        except Exception:
            return

        active_key = str(getattr(self._active, "key", "") or "")
        # When no mode is active, don't force-disable required widgets.
        # This keeps widget_engine editor toggles fully editable at idle.
        if not active_key:
            await sig_clear.send_robust({"owner": self.REQUIRED_WIDGET_VIS_OWNER}, raw=True)
            return
        disable_keys: set[str] = set()
        active_required: set[str] = set()

        for mode_key in REGISTRY.keys():
            req = set(self._mode_required_widgets(mode_key))
            if not req:
                continue
            if mode_key == active_key:
                active_required.update(req)
            else:
                disable_keys.update(req)

        # If a widget is also required by the active mode, keep it enabled.
        disable_keys.difference_update(active_required)

        if not disable_keys:
            await sig_clear.send_robust({"owner": self.REQUIRED_WIDGET_VIS_OWNER}, raw=True)
            return

        rows = [{"widget_key": key, "disabled": True} for key in sorted(disable_keys)]
        await sig_apply.send_robust({
            "owner": self.REQUIRED_WIDGET_VIS_OWNER,
            "widgets": rows,
        }, raw=True)

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

    def _config_page_size_for(self, mode_key: str, *, admin_view: bool) -> int:
        ay = -30
        is_active = self._active is not None and self._active.key == mode_key
        cfg_y = (ay - 34) if is_active else (ay - 8)
        cfg_h_min_target_y = (
            self.CONFIG_PAG_Y + (self.CONFIG_PAG_BTN_H / 2) - self.CONFIG_PAG_TOP_GAP
        )
        available_h = cfg_y - cfg_h_min_target_y
        rows = int((available_h - self.CONFIG_GROUP_BASE_HEIGHT) // self.CONFIG_ROW_HEIGHT)
        return max(1, rows)

    def _cfg_page_for(self, login: str, mode_key: str, total_rows: int,
                      page_size: int | None = None) -> tuple[int, int]:
        size = max(1, int(page_size or self.CONFIG_PAGE_SIZE))
        pages = max(1, (max(0, int(total_rows)) + size - 1) // size)
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

    async def operator_context(self, login: str, *, admin_view: bool = False) -> dict[str, Any]:
        allowed_modes = self._operator_allowed_modes()
        mode_keys = sorted(REGISTRY.keys()) if admin_view else sorted([k for k in REGISTRY.keys() if k in allowed_modes])
        modes_meta = [self._mode_meta(k) for k in mode_keys]
        selected = self._operator_selection.get(login)
        # Auto-select the active mode (or the first available) so the right
        # pane is never empty on first open.
        if selected is None or selected not in REGISTRY or (not admin_view and selected not in allowed_modes):
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
        widget_profile_rows: list[dict[str, Any]] = []
        wprof_list_rows: list[dict[str, Any]] = []
        wprof_list_page0 = 0
        wprof_list_pages_total = 1
        wprof_draft: dict[str, Any] = self._wprof_default_draft()
        wprof_window_open = bool(self._operator_wprof_window_open.get(login, False))
        wprof_view = str(self._operator_wprof_view.get(login, "list") or "list")
        wprof_picker_rows: list[dict[str, Any]] = []
        wprof_picker_page_rows: list[dict[str, Any]] = []
        wprof_picker_page0 = 0
        wprof_picker_pages_total = 1
        wprof_editor: dict[str, Any] | None = None
        if selected:
            cls = REGISTRY[selected]
            tmp_ctx = GameModeContext(self, selected)
            tmp_instance = cls(tmp_ctx)
            defaults = tmp_instance.default_config()
            stored = self._config_for(selected, defaults)
            draft = self._operator_drafts.get(login, {}).get(selected, {})
            op_allowed_fields = self._operator_allowed_fields(selected)
            all_rows: list[dict[str, Any]] = []
            for f in tmp_instance.config_schema():
                if not admin_view and str(f.get("key")) not in op_allowed_fields:
                    continue
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
                    "combo_open": (
                        str(
                            self._operator_cfg_combo_open
                            .get(login, {})
                            .get(selected)
                            or ""
                        )
                        == str(f["key"])
                    ),
                })
            page_size = self._config_page_size_for(selected, admin_view=admin_view)
            page0, cfg_pages_total = self._cfg_page_for(login, selected, len(all_rows), page_size)
            start = page0 * page_size
            end = start + page_size
            cfg_rows = all_rows[start:end]
            cfg_page = page0 + 1
            cfg_has_prev = page0 > 0
            cfg_has_next = page0 < (cfg_pages_total - 1)

            mode_required_widgets, mode_extra_widgets, mode_widgets_editable = self._mode_widget_sets(login, selected)
            if not admin_view:
                need = {"required_widgets_csv", "extra_widgets_csv"}
                mode_widgets_editable = mode_widgets_editable and need.issubset(op_allowed_fields)
            if mode_widgets_editable:
                active = set(mode_required_widgets + mode_extra_widgets)
                known = self._known_widget_keys()
                mode_available_widgets = [k for k in known if k not in active]
            mode_layout, mode_layout_editable = self._mode_layout_values(login, selected)
            if not admin_view:
                needed_layout = {
                    "random_mx_points_x",
                    "random_mx_points_y",
                    "random_mx_points_w",
                    "random_mx_points_h",
                    "random_mx_points_drive_mode",
                    "random_mx_points_anim_dir",
                    "random_mx_points_anim_duration_ms",
                    "random_mx_points_anim_delay_ms",
                }
                mode_layout_editable = mode_layout_editable and needed_layout.issubset(op_allowed_fields)

            if admin_view:
                widget_profile_rows = self._mode_widget_layout_profile(selected)
                wprof_list_page0, wprof_list_pages_total = self._wprof_list_page(
                    login, selected, len(widget_profile_rows))
                wprof_list_rows = widget_profile_rows[
                    wprof_list_page0 * self.WPROF_LIST_PAGE_SIZE
                    : (wprof_list_page0 + 1) * self.WPROF_LIST_PAGE_SIZE
                ]
                draft = self._wprof_get_draft(login, selected)
                wprof_draft = {
                    "widget_key": str(draft.get("widget_key") or ""),
                    "x": self._wprof_format(draft.get("x", 0.0)),
                    "y": self._wprof_format(draft.get("y", 0.0)),
                    "w": self._wprof_format(draft.get("w", 60.0)),
                    "h": self._wprof_format(draft.get("h", 24.0)),
                    "disabled": bool(draft.get("disabled", False)),
                }
                taken = {r["widget_key"] for r in widget_profile_rows}
                we_app = self._widget_engine_app()
                entries_map: dict[str, Any] = {}
                if we_app is not None:
                    for attr in ("_entries", "entries", "_available"):
                        cont = getattr(we_app, attr, None)
                        if not cont:
                            continue
                        try:
                            for k, v in cont.items():
                                if k and k not in entries_map:
                                    entries_map[str(k)] = v
                        except Exception:
                            continue

                def _entry_name(key: str) -> str:
                    e = entries_map.get(key)
                    if e is None:
                        return key
                    nm = getattr(e, "name", None) or key
                    return str(nm)

                wprof_picker_rows = [
                    {"key": k, "name": _entry_name(k)}
                    for k in self._known_widget_keys() if k not in taken
                ]
                wprof_picker_page0, wprof_picker_pages_total = self._wprof_picker_page(
                    login, selected, len(wprof_picker_rows))
                wprof_picker_page_rows = wprof_picker_rows[
                    wprof_picker_page0 * self.WPROF_PICKER_PAGE_SIZE
                    : (wprof_picker_page0 + 1) * self.WPROF_PICKER_PAGE_SIZE
                ]
                if wprof_view == "editor" and draft.get("widget_key"):
                    wkey = str(draft.get("widget_key"))
                    drive_val = str(draft.get("drive_mode") or "fixed")
                    dir_val = str(draft.get("anim_dir") or "none")
                    wprof_editor = {
                        "widget_key": wkey,
                        "fields": {
                            "x": {"label": "X", "display": self._wprof_format(draft.get("x", 0.0))},
                            "y": {"label": "Y", "display": self._wprof_format(draft.get("y", 0.0))},
                            "w": {"label": "W", "display": self._wprof_format(draft.get("w", 60.0))},
                            "h": {"label": "H", "display": self._wprof_format(draft.get("h", 24.0))},
                            "disabled": {"label": "Disabled", "value": bool(draft.get("disabled", False))},
                            "drive_mode": {
                                "label": "Driving",
                                "value": drive_val,
                                "display": drive_val,
                                "overridden": draft.get("drive_mode") is not None,
                            },
                            "anim_dir": {
                                "label": "Anim",
                                "value": dir_val,
                                "display": dir_val,
                                "overridden": draft.get("anim_dir") is not None,
                            },
                            "anim_duration_ms": {
                                "label": "Dur",
                                "display": str(int(draft.get("anim_duration_ms") or 180)),
                                "overridden": draft.get("anim_duration_ms") is not None,
                            },
                            "anim_in_delay_ms": {
                                "label": "In",
                                "display": str(int(draft.get("anim_in_delay_ms") or 0)),
                                "overridden": draft.get("anim_in_delay_ms") is not None,
                            },
                            "anim_out_delay_ms": {
                                "label": "Out",
                                "display": str(int(draft.get("anim_out_delay_ms") or 0)),
                                "overridden": draft.get("anim_out_delay_ms") is not None,
                            },
                        },
                        "pos_step_display": self._wprof_format(draft.get("pos_step", 1.0)),
                        "dirty": self._wprof_is_dirty(draft),
                        "is_new": draft.get("anchor_row") is None,
                    }

        operator_mode_allowed = bool(selected and selected in allowed_modes)
        operator_field_policy: list[dict[str, Any]] = []
        if admin_view and selected and selected in REGISTRY:
            op_allowed_fields = self._operator_allowed_fields(selected)
            inst = REGISTRY[selected](GameModeContext(self, selected))
            for f in inst.config_schema():
                fkey = str(f.get("key") or "")
                if not fkey:
                    continue
                operator_field_policy.append({
                    "key": fkey,
                    "label": str(f.get("label") or fkey),
                    "allowed": fkey in op_allowed_fields,
                })

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
            "is_admin":             admin_view,
            "runtime_controls_enabled": (not admin_view),
            "operator_mode_allowed": operator_mode_allowed,
            "operator_field_policy": operator_field_policy,
            "widget_profile_rows": widget_profile_rows,
            "wprof_list_rows": wprof_list_rows,
            "wprof_list_page": wprof_list_page0 + 1,
            "wprof_list_pages_total": wprof_list_pages_total,
            "wprof_list_has_prev": wprof_list_page0 > 0,
            "wprof_list_has_next": wprof_list_page0 < (wprof_list_pages_total - 1),
            "wprof_draft": wprof_draft,
            "wprof_window_open": wprof_window_open,
            "wprof_view": wprof_view,
            "wprof_picker_rows": wprof_picker_rows,
            "wprof_picker_page_rows": wprof_picker_page_rows,
            "wprof_picker_page": wprof_picker_page0 + 1,
            "wprof_picker_pages_total": wprof_picker_pages_total,
            "wprof_picker_has_prev": wprof_picker_page0 > 0,
            "wprof_picker_has_next": wprof_picker_page0 < (wprof_picker_pages_total - 1),
            "wprof_editor": wprof_editor,
            "known_widget_keys": self._known_widget_keys(),
        }

    async def admin_context(self, login: str) -> dict[str, Any]:
        return await self.operator_context(login, admin_view=True)

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

    # ---- RMC end-of-run results ---------------------------------------

    _RMC_MEDAL_SUBSTYLE = {
        "at":      "MedalNadeo",
        "gold":    "MedalGold",
        "silver":  "MedalSilver",
        "bronze":  "MedalBronze",
    }
    _RMC_MEDAL_LABEL = {
        "at":     "AT",
        "gold":   "Gold",
        "silver": "Silver",
        "bronze": "Bronze",
    }

    async def show_rmc_results(self, run_id: int) -> None:
        """Open the end-of-run results panel for everyone online."""
        if self.rmc_results_view is None:
            return
        self._rmc_last_run_id = int(run_id)
        try:
            await self.rmc_results_view.show()
        except Exception:
            logger.exception("gamemodes: rmc_results_view show failed")

    async def hide_rmc_results(self, *, login: str | None = None) -> None:
        view = self.rmc_results_view
        if view is None:
            return
        try:
            if login:
                view._visible_logins.discard(login)
                await TemplateView.hide(view, player_logins=[login])
            else:
                await TemplateView.hide(view)
                view._visible = False
                view._visible_logins.clear()
        except Exception:
            logger.exception("gamemodes: rmc_results_view hide failed")

    async def rmc_results_context(self, login: str) -> dict[str, Any]:
        run_id = self._rmc_last_run_id
        if not run_id:
            return {"results": None, "can_close": True}
        try:
            from .models import RmcRun, RmcRunPlayer
        except Exception:
            logger.exception("gamemodes: rmc results models import failed")
            return {"results": None, "can_close": True}
        try:
            run = await RmcRun.objects.get(RmcRun, RmcRun.id == int(run_id))
        except Exception:
            logger.exception("gamemodes: rmc_run %s lookup failed", run_id)
            return {"results": None, "can_close": True}
        try:
            rows_q = (
                RmcRunPlayer.select()
                .where(RmcRunPlayer.run == run)
            )
            rows = list(await RmcRunPlayer.objects.execute(rows_q))
        except Exception:
            logger.exception("gamemodes: rmc_run_player query failed")
            rows = []

        # Rank: goal clears desc, then secondary clears desc, then finishes desc.
        rows.sort(
            key=lambda r: (
                -int(r.goal_clears or 0),
                -int(r.secondary_clears or 0),
                -int(r.finishes or 0),
            )
        )
        ranked = []
        for i, r in enumerate(rows, start=1):
            # Show only players who actually contributed a medal-tracked event.
            if int(r.goal_clears or 0) <= 0 and int(r.secondary_clears or 0) <= 0:
                continue
            ranked.append({
                "rank": i,
                "nickname": str(r.nickname or r.login),
                "goal_clears": int(r.goal_clears or 0),
                "secondary_clears": int(r.secondary_clears or 0),
            })

        goal_key = str(run.goal_medal or "at").lower()
        sec_key = str(run.secondary_medal or "gold").lower()
        return {
            "results": {
                "run_id":              int(run.id),
                "reason":              str(run.reason or ""),
                "goal_label":          self._RMC_MEDAL_LABEL.get(goal_key, goal_key.title()),
                "secondary_label":     self._RMC_MEDAL_LABEL.get(sec_key, sec_key.title()),
                "goal_substyle":       self._RMC_MEDAL_SUBSTYLE.get(goal_key, "MedalNadeo"),
                "secondary_substyle":  self._RMC_MEDAL_SUBSTYLE.get(sec_key, "MedalGold"),
                "goal_count":          int(run.maps_cleared or 0),
                "secondary_count":     int(run.secondary_cleared or 0),
                "players_count":       int(run.players_count or 0),
                "rows":                ranked,
            },
            "can_close": True,
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
            await ctx.apply_widget_layout(self._mode_widget_layout_profile(mode_key))
        except Exception:
            logger.exception("gamemodes: %s on_enable failed", mode_key)
            try:
                await ctx.clear_widget_overrides()
            except Exception:
                logger.exception("gamemodes: %s clear_widget_overrides after failed enable", mode_key)
            try:
                await ctx.clear_widget_layout()
            except Exception:
                logger.exception("gamemodes: %s clear_widget_layout after failed enable", mode_key)
            await self._notify(f"Failed to start {cls.name}", "error")
            self._active = None
            self._active_ctx = None
            self._state["active"] = None
            self._save_state()
            return
        if announce:
            await self._notify(f"Game mode '{cls.name}' is now active",
                               "success")
        await self._sync_required_widget_visibility()
        self._mode_status_lines = list(instance.status_lines())
        await self._refresh_operator()

    async def _deactivate(self, *, announce: bool = True) -> None:
        if self._active is None:
            return
        cls = type(self._active)
        stopped_mode_key = str(getattr(self._active, "key", "") or "")
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
            try:
                await ctx.clear_widget_layout()
            except Exception:
                logger.exception("gamemodes: %s clear_widget_layout on disable failed", cls.key)
        self._active = None
        self._active_ctx = None
        self._mode_status_lines = []
        self._state["active"] = None
        self._save_state()
        await self._sync_required_widget_visibility()
        # on_disable/clear_widget_layout can refresh widgets before _active is
        # cleared, which leaves mode widgets visible with stale data. Re-render
        # once more after we are fully inactive.
        await self._refresh_widget_keys(self._mode_widget_keys(stopped_mode_key))
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
            # BaseView.hide() calls destroy(), which unregisters the action
            # listener and clears self.data — making the view unusable for
            # subsequent votes. Use TemplateView.hide() directly so the view
            # stays alive and re-shows cleanly next time.
            #
            # We MUST hide both per-player and globally: BaseView.refresh()
            # re-displays per-login on every progress tick, so by the time
            # the vote times out the manialink has been pushed both as a
            # global frame AND as per-player frames. A bare hide(None) only
            # clears the global flag and leaves the per-player frames on
            # the clients, so the panel sticks around forever.
            try:
                logins = list(self.vote_view._visible_logins)
                if logins:
                    await TemplateView.hide(self.vote_view, player_logins=logins)
                await TemplateView.hide(self.vote_view)
            except Exception:
                logger.exception("gamemodes: vote_view hide failed")
            self.vote_view._visible = False
            self.vote_view._visible_logins.clear()
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
        for v, label in (
            (self.operator_view, "operator"),
            (self.admin_view, "admin"),
        ):
            if v is None:
                continue
            if not getattr(v, "_visible", False):
                continue
            logins = list(getattr(v, "_visible_logins", set()) or [])
            if not logins:
                continue
            try:
                await v.display(player_logins=logins)
            except Exception:
                logger.exception("gamemodes: %s view refresh failed", label)

    # ---- action handlers ----------------------------------------------

    async def _on_mode_select(self, player, mode_key: str) -> None:
        if mode_key not in REGISTRY:
            return
        self._operator_selection[player.login] = mode_key
        self._operator_cfg_page.setdefault(player.login, {}).setdefault(mode_key, 0)
        await self._refresh_operator()

    async def _on_cfg_page(self, login: str, mode_key: str, action: str, *, admin_view: bool) -> None:
        if mode_key not in REGISTRY:
            return
        op_allowed_fields = self._operator_allowed_fields(mode_key)
        schema = REGISTRY[mode_key](GameModeContext(self, mode_key)).config_schema()
        total = len(schema) if admin_view else len([
            f for f in schema if str(f.get("key")) in op_allowed_fields
        ])
        page_size = self._config_page_size_for(mode_key, admin_view=admin_view)
        page0, pages = self._cfg_page_for(login, mode_key, total, page_size)
        if action == "first":
            page0 = 0
        elif action == "prev":
            page0 = max(0, page0 - 1)
        elif action == "next":
            page0 = min(pages - 1, page0 + 1)
        elif action == "last":
            page0 = max(0, pages - 1)
        elif action.startswith("page__"):
            try:
                n = int(action.split("__", 1)[1])
                page0 = max(0, min(pages - 1, n - 1))
            except (TypeError, ValueError):
                pass
        self._operator_cfg_page.setdefault(login, {})[mode_key] = page0
        await self._refresh_operator()

    async def _on_mode_start(self, player, mode_key: str) -> None:
        if self._is_admin(player):
            await self._activate(mode_key)
            return
        if not self._is_operator(player):
            await self._notify("Operator only.", "warning", login=player.login)
            return
        if mode_key not in self._operator_allowed_modes():
            await self._notify("This mode is not allowed for operators.", "warning", login=player.login)
            return
        await self._activate(mode_key)

    async def _on_mode_stop(self, player) -> None:
        if self._is_admin(player):
            await self._deactivate()
            return
        if not self._is_operator(player):
            await self._notify("Operator only.", "warning", login=player.login)
            return
        if self._active is not None and self._active.key not in self._operator_allowed_modes():
            await self._notify("Active mode cannot be stopped by operator.", "warning", login=player.login)
            return
        await self._deactivate()

    async def _on_mode_save(self, player) -> None:
        is_admin = self._is_admin(player)
        is_operator = self._is_operator(player)
        if not (is_admin or is_operator):
            await self._notify("Operator only.", "warning", login=player.login)
            return
        selected = self._operator_selection.get(player.login)
        if not selected:
            return
        if not is_admin and selected not in self._operator_allowed_modes():
            await self._notify("This mode is not allowed for operators.", "warning", login=player.login)
            return
        draft = self._operator_drafts.get(player.login, {}).get(selected)
        if not draft:
            await self._notify("Nothing changed.", "info", login=player.login)
            return
        # Persist into state.configs[<mode>].
        cfg = self._config_for(selected, REGISTRY[selected](GameModeContext(self, selected)).default_config())
        allowed = self._operator_allowed_fields(selected)
        for k, v in draft.items():
            if not is_admin and k not in allowed:
                continue
            cfg[k] = v
        self._state.setdefault("configs", {})[selected] = cfg
        self._save_state()
        # Push the new config into a live mode instance so changes apply
        # immediately without requiring a stop/start cycle.
        if self._active is not None and getattr(self._active, "key", None) == selected:
            try:
                self._active._config = {**self._active.default_config(), **cfg}
            except Exception:
                logger.exception("gamemodes: live config refresh failed for %s", selected)
        # Clear the local draft now that it lives in state.
        self._operator_drafts.get(player.login, {}).pop(selected, None)
        await self._notify(f"{REGISTRY[selected].name} config saved.",
                           "success", login=player.login)
        await self._sync_required_widget_visibility()
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
        elif field["type"] == "choice":
            allowed_values = [
                str((c.get("value") if isinstance(c, dict) else c[0]))
                for c in (field.get("choices") or [])
            ]
            value = str(raw_value)
            if allowed_values and value not in allowed_values:
                return
        else:
            value = str(raw_value)
        drafts = self._operator_drafts.setdefault(login, {})
        mode_draft = drafts.setdefault(mode_key, {})
        mode_draft[field_key] = value
        await self._refresh_operator()

    # ---- catch-all router ---------------------------------------------

    async def _catch_all(self, player, action, values, **kwargs) -> None:
        login = player.login
        self._absorb(player, values)

        # admin policy: allow mode for operators
        m = re.match(r"^opmode__(\w+)$", action)
        if m:
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            mode_key = m.group(1)
            if mode_key not in REGISTRY:
                return
            p = self._policy()
            allowed = set(p.get("allowed_modes", []))
            if mode_key in allowed:
                allowed.discard(mode_key)
            else:
                allowed.add(mode_key)
            p["allowed_modes"] = sorted(allowed)
            self._ensure_policy_defaults()
            self._save_state()
            await self._refresh_operator()
            return

        # admin policy: allow/disallow individual config field for operators
        m = re.match(r"^opfield__(\w+)__(\w+)$", action)
        if m:
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            mode_key = m.group(1)
            field_key = m.group(2)
            if mode_key not in REGISTRY:
                return
            if field_key not in set(self._schema_field_keys(mode_key)):
                return
            p = self._policy()
            af = p.setdefault("allowed_fields", {})
            cur = set(str(k) for k in (af.get(mode_key, []) or []))
            if field_key in cur:
                cur.discard(field_key)
            else:
                cur.add(field_key)
            af[mode_key] = sorted(cur)
            self._ensure_policy_defaults()
            self._save_state()
            await self._refresh_operator()
            return

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
                if (not self._is_admin(player)) and m.group(1) not in self._operator_allowed_fields(selected):
                    return
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

        m = re.match(r"^cfg__(\w+)__toggle$", action)
        if m:
            selected = self._operator_selection.get(login)
            if selected:
                field_key = m.group(1)
                if (not self._is_admin(player)) and field_key not in self._operator_allowed_fields(selected):
                    return
                instance = REGISTRY[selected](GameModeContext(self, selected))
                field = next((f for f in instance.config_schema() if f["key"] == field_key), None)
                if field is None or field.get("type") != "choice":
                    return
                by_mode = self._operator_cfg_combo_open.setdefault(login, {})
                cur = by_mode.get(selected)
                by_mode[selected] = None if cur == field_key else field_key
                await self._refresh_operator()
            return

        m = re.match(r"^cfg__(\w+)__pick__(.+)$", action)
        if m:
            selected = self._operator_selection.get(login)
            if selected:
                field_key = m.group(1)
                picked = m.group(2)
                if (not self._is_admin(player)) and field_key not in self._operator_allowed_fields(selected):
                    return
                instance = REGISTRY[selected](GameModeContext(self, selected))
                field = next((f for f in instance.config_schema() if f["key"] == field_key), None)
                if field is None or field.get("type") != "choice":
                    return
                allowed_values = [
                    str((c.get("value") if isinstance(c, dict) else c[0]))
                    for c in (field.get("choices") or [])
                ]
                if allowed_values and picked not in allowed_values:
                    return
                await self._on_cfg_change(login, selected, field_key, picked)
                self._operator_cfg_combo_open.setdefault(login, {})[selected] = None
            return

        m = re.match(r"^cfgpage__(first|prev|next|last|page__\d+)$", action)
        if m:
            selected = self._operator_selection.get(login)
            if selected:
                await self._on_cfg_page(login, selected, m.group(1), admin_view=self._is_admin(player))
            return

        if action == "wprof_open":
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            self._operator_wprof_window_open[login] = True
            self._operator_wprof_view[login] = "list"
            await self._refresh_operator()
            return

        if action == "wprof_close":
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            self._operator_wprof_window_open[login] = False
            self._operator_wprof_view[login] = "list"
            await self._wprof_clear_preview(login)
            await self._refresh_operator()
            return

        if action == "wprof_view__list":
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            self._operator_wprof_view[login] = "list"
            await self._wprof_clear_preview(login)
            await self._refresh_operator()
            return

        if action == "wprof_view__picker":
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            self._operator_wprof_view[login] = "picker"
            await self._wprof_clear_preview(login)
            await self._refresh_operator()
            return

        m = re.match(r"^wprof_listpage__(first|prev|next|last|page__\d+)$", action)
        if m:
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            selected = self._operator_selection.get(login)
            if not selected:
                return
            total = len(self._mode_widget_layout_profile(selected))
            page0, pages = self._wprof_list_page(login, selected, total)
            page0 = self._wprof_apply_page_action(page0, pages, m.group(1))
            self._operator_wprof_list_page.setdefault(login, {})[selected] = page0
            await self._refresh_operator()
            return

        m = re.match(r"^wprof_pickpage__(first|prev|next|last|page__\d+)$", action)
        if m:
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            selected = self._operator_selection.get(login)
            if not selected:
                return
            taken = {r["widget_key"] for r in self._mode_widget_layout_profile(selected)}
            total = len([k for k in self._known_widget_keys() if k not in taken])
            page0, pages = self._wprof_picker_page(login, selected, total)
            page0 = self._wprof_apply_page_action(page0, pages, m.group(1))
            self._operator_wprof_picker_page.setdefault(login, {})[selected] = page0
            await self._refresh_operator()
            return

        m = re.match(r"^wprof_pick__([0-9a-zA-Z_\-]+)$", action)
        if m:
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            selected = self._operator_selection.get(login)
            if not selected:
                return
            widget_key = m.group(1)
            if widget_key not in set(self._known_widget_keys()):
                await self._notify("Unknown widget key.", "warning", login=login)
                return
            self._wprof_load_from_row(login, selected, widget_key)
            self._operator_wprof_view[login] = "editor"
            await self._wprof_apply_preview(login)
            await self._refresh_operator()
            return

        m = re.match(r"^wprof_edit__([0-9a-zA-Z_\-]+)$", action)
        if m:
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            selected = self._operator_selection.get(login)
            if not selected:
                return
            widget_key = m.group(1)
            self._wprof_load_from_row(login, selected, widget_key)
            self._operator_wprof_view[login] = "editor"
            self._operator_wprof_window_open[login] = True
            await self._wprof_apply_preview(login)
            await self._refresh_operator()
            return

        m = re.match(r"^wprof_del__([0-9a-zA-Z_\-]+)$", action)
        if m:
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            selected = self._operator_selection.get(login)
            if not selected:
                return
            victim = m.group(1)
            await self.widget_layout.delete(selected, victim)
            await self._apply_mode_widget_layout_profile(selected)
            await self._refresh_operator()
            return

        m = re.match(r"^wprof_pos__(up|down|left|right|reset)$", action)
        if m:
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            selected = self._operator_selection.get(login)
            if not selected:
                return
            draft = self._wprof_get_draft(login, selected)
            if not draft.get("widget_key"):
                return
            op = m.group(1)
            step = float(draft.get("pos_step", 1.0))
            if op == "up":
                draft["y"] = float(draft.get("y", 0.0)) + step
            elif op == "down":
                draft["y"] = float(draft.get("y", 0.0)) - step
            elif op == "left":
                draft["x"] = float(draft.get("x", 0.0)) - step
            elif op == "right":
                draft["x"] = float(draft.get("x", 0.0)) + step
            elif op == "reset":
                anchor = draft.get("anchor_row") or {}
                draft["x"] = float(anchor.get("x", 0.0))
                draft["y"] = float(anchor.get("y", 0.0))
            await self._wprof_apply_preview(login)
            await self._refresh_operator()
            return

        m = re.match(r"^wprof_nudge__(w|h|anim_duration_ms|anim_in_delay_ms|anim_out_delay_ms)__(inc|dec)$", action)
        if m:
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            selected = self._operator_selection.get(login)
            if not selected:
                return
            draft = self._wprof_get_draft(login, selected)
            if not draft.get("widget_key"):
                return
            field, op = m.group(1), m.group(2)
            if field in ("w", "h"):
                step = float(draft.get("pos_step", 1.0))
                cur = float(draft.get(field, 0.0))
                new = cur + (step if op == "inc" else -step)
                if new < 1.0:
                    new = 1.0
                draft[field] = new
            else:
                step_ms = 20
                base = int(draft.get(field) if draft.get(field) is not None
                           else (180 if field == "anim_duration_ms" else 0))
                new_val = base + (step_ms if op == "inc" else -step_ms)
                if new_val < 0:
                    new_val = 0
                draft[field] = new_val
            await self._wprof_apply_preview(login)
            await self._refresh_operator()
            return

        m = re.match(r"^wprof_setdir__(none|left|right|up|down)$", action)
        if m:
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            selected = self._operator_selection.get(login)
            if not selected:
                return
            draft = self._wprof_get_draft(login, selected)
            if not draft.get("widget_key"):
                return
            draft["anim_dir"] = m.group(1)
            await self._wprof_apply_preview(login)
            await self._refresh_operator()
            return

        m = re.match(r"^wprof_drive__(fixed|hide_while_driving|only_shown_while_driving)$", action)
        if m:
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            selected = self._operator_selection.get(login)
            if not selected:
                return
            draft = self._wprof_get_draft(login, selected)
            if not draft.get("widget_key"):
                return
            draft["drive_mode"] = m.group(1)
            await self._wprof_apply_preview(login)
            await self._refresh_operator()
            return

        m = re.match(r"^wprof_posstep__(inc|dec)$", action)
        if m:
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            selected = self._operator_selection.get(login)
            if not selected:
                return
            draft = self._wprof_get_draft(login, selected)
            cur = float(draft.get("pos_step", 1.0))
            draft["pos_step"] = self._wprof_cycle_step(cur, 1 if m.group(1) == "inc" else -1)
            await self._refresh_operator()
            return

        if action == "wprof_toggle__disabled":
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            selected = self._operator_selection.get(login)
            if not selected:
                return
            draft = self._wprof_get_draft(login, selected)
            if not draft.get("widget_key"):
                return
            draft["disabled"] = not bool(draft.get("disabled", False))
            await self._wprof_apply_preview(login)
            await self._refresh_operator()
            return

        if action == "wprof_editor_reset":
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            selected = self._operator_selection.get(login)
            if not selected:
                return
            draft = self._wprof_get_draft(login, selected)
            anchor = draft.get("anchor_row")
            if anchor:
                draft["x"] = float(anchor.get("x", 0.0))
                draft["y"] = float(anchor.get("y", 0.0))
                draft["w"] = float(anchor.get("w", 60.0))
                draft["h"] = float(anchor.get("h", 24.0))
                draft["disabled"] = bool(anchor.get("disabled", False))
                for k in ("drive_mode", "anim_dir",
                          "anim_duration_ms", "anim_in_delay_ms", "anim_out_delay_ms"):
                    draft[k] = anchor.get(k)
            await self._wprof_apply_preview(login)
            await self._refresh_operator()
            return

        if action == "wprof_editor_cancel":
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            self._operator_wprof_view[login] = "list"
            await self._wprof_clear_preview(login)
            await self._refresh_operator()
            return

        if action == "wprof_editor_apply":
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            selected = self._operator_selection.get(login)
            if not selected:
                return
            draft = self._wprof_get_draft(login, selected)
            widget_key = str(draft.get("widget_key") or "").strip()
            if not widget_key:
                await self._notify("No widget selected.", "warning", login=login)
                return
            rows = self._mode_widget_layout_profile(selected)
            kept = [r for r in rows if str(r.get("widget_key")) != widget_key]
            row: dict[str, Any] = {
                "widget_key": widget_key,
                "x": float(draft.get("x", 0.0)),
                "y": float(draft.get("y", 0.0)),
                "w": float(draft.get("w", 60.0)),
                "h": float(draft.get("h", 24.0)),
                "disabled": bool(draft.get("disabled", False)),
            }
            for k in ("drive_mode", "anim_dir",
                      "anim_duration_ms", "anim_in_delay_ms", "anim_out_delay_ms"):
                v = draft.get(k)
                if v is not None and v != "":
                    row[k] = v
            kept.append(row)
            await self._set_mode_widget_layout_profile(
                selected,
                sorted(kept, key=lambda r: str(r.get("widget_key") or "")),
            )
            await self._apply_mode_widget_layout_profile(selected)
            await self._notify(f"Saved {widget_key}.", "success", login=login)
            self._operator_wprof_view[login] = "list"
            await self._wprof_clear_preview(login)
            await self._refresh_operator()
            return

        if action == "wprof_clear_all":
            if not self._is_admin(player):
                await self._notify("Admin only.", "warning", login=login)
                return
            selected = self._operator_selection.get(login)
            if not selected:
                return
            await self.widget_layout.clear(selected)
            await self._apply_mode_widget_layout_profile(selected)
            await self._notify("Widget profile cleared.", "success", login=login)
            await self._refresh_operator()
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

        # rmc results panel close (per-player)
        if action == "close":
            await self.hide_rmc_results(login=login)
            return

    def _absorb(self, player, values: dict[str, Any]) -> None:
        """Pull every ``entry_<viewid>__cfg__<key>`` field out of the form
        submission and feed it through the draft store. Runs at the top of
        every action so the most recent values are always captured.
        """
        if not values:
            return
        login = player.login
        selected = self._operator_selection.get(login)
        if not selected:
            return
        prefixes: list[str] = []
        if self.operator_view is not None:
            prefixes.append(f"entry_{self.operator_view.id}__cfg__")
        if self.admin_view is not None:
            prefixes.append(f"entry_{self.admin_view.id}__cfg__")
        if not prefixes:
            return
        is_admin = self._is_admin(player)
        allowed = self._operator_allowed_fields(selected)
        for raw_key, raw_val in values.items():
            prefix = next((p for p in prefixes if raw_key.startswith(p)), None)
            if prefix is None:
                continue
            field_key = raw_key[len(prefix):]
            if not is_admin and field_key not in allowed:
                continue
            # Push through the draft path synchronously; refresh happens at
            # the end of the outer action so we don't double-fire.
            drafts = self._operator_drafts.setdefault(login, {})
            mode_draft = drafts.setdefault(selected, {})
            mode_draft[field_key] = (raw_val or "")
