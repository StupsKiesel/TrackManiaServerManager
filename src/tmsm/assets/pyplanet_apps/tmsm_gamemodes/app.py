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

    # ---- notification --------------------------------------------------

    async def _notify(self, message: str, severity: str = "info",
                      login: str | None = None,
                      duration_ms: int = 4000) -> None:
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
        if selected:
            cls = REGISTRY[selected]
            tmp_ctx = GameModeContext(self, selected)
            tmp_instance = cls(tmp_ctx)
            defaults = tmp_instance.default_config()
            stored = self._config_for(selected, defaults)
            draft = self._operator_drafts.get(login, {}).get(selected, {})
            for f in tmp_instance.config_schema():
                val = draft.get(f["key"], stored.get(f["key"], f.get("default")))
                cfg_rows.append({
                    "key":     f["key"],
                    "label":   f["label"],
                    "type":    f["type"],
                    "value":   val,
                    "help":    f.get("help", ""),
                    "min":     f.get("min"),
                    "max":     f.get("max"),
                    "choices": f.get("choices") or [],
                })

        return {
            "modes":                modes_meta,
            "active_key":           (self._active.key if self._active else None),
            "active_name":          (self._active.name if self._active else ""),
            "active_status_lines":  list(self._mode_status_lines),
            "active_config":        cfg_rows,
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
        try:
            await self._active.on_disable()
        except Exception:
            logger.exception("gamemodes: %s on_disable failed", cls.key)
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
