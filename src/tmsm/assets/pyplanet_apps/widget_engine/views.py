"""Views for the widget engine — slice 8: read-only Widget Manager.

`WidgetEngineManagerView` is a master/detail window: left rail lists the
6 lifecycle phases with widget counts, right pane lists widgets in the
selected phase (paginated). Action buttons (Add/Edit/Remove/Enable/Debug)
are present but stubbed — they toast 'not wired yet' so the layout can
be validated before the engine wiring lands in later slices.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pyplanet.apps.tmsm.ui.audience import Audience
from pyplanet.apps.tmsm.ui.views import BaseView

from .registry import AnimDir, DriveMode, Phase

if TYPE_CHECKING:
    from .app import WidgetsApp

logger = logging.getLogger(__name__)


# Lifecycle order shown in the left rail.
PHASE_ORDER: tuple[Phase, ...] = (
    Phase.LOADING_MAP,
    Phase.PRE_RACE,
    Phase.WARMUP,
    Phase.IN_RACE,
    Phase.IN_PODIUM,
    Phase.POST_RACE,
)

PHASE_LABEL: dict[Phase, str] = {
    Phase.LOADING_MAP: "Loading map",
    Phase.PRE_RACE:    "Pre-race",
    Phase.WARMUP:      "Warmup",
    Phase.IN_RACE:     "In race",
    Phase.IN_PODIUM:   "Podium",
    Phase.POST_RACE:   "Post-race",
}

ROWS_PER_PAGE = 10


class WidgetEngineManagerView(BaseView):
    template_name = "widget_engine/manager.xml"
    audience: Audience = Audience.everyone()
    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    async def _on_close(self, player) -> None:
        # Design rule: the window's red × button always fully exits back to
        # the game (no back-stack). Breadcrumbs handle hierarchical
        # navigation; the close button is a hard exit.
        login = player.login
        st = self._state.get(login)
        if st:
            if st.get("editor_open"):
                # Tear down editor state (clear transient overlay, exit
                # edit-mode) before closing the manager.
                await self._editor_close(login)
            st["picker_open"] = False
            st["confirm_remove"] = None
            st["settings_open"] = False
        await super()._on_close(player)

    def __init__(self, app: "WidgetsApp") -> None:
        super().__init__(app)
        self.host = app
        # Per-login UI state: {login: {"phase": Phase, "page": {phase_key: int},
        #                              "confirm_remove": str|None}}
        self._state: dict[str, dict[str, Any]] = {}

    # ---- state helpers ------------------------------------------------

    def _ensure_state(self, login: str) -> dict[str, Any]:
        st = self._state.get(login)
        if st is None:
            engine_phase = self.host.engine.current_phase
            default = engine_phase if engine_phase in PHASE_ORDER else PHASE_ORDER[0]
            st = {
                "phase": default,
                "page": {},
                "confirm_remove": None,
                "picker_open": False,
                "picker_page": 1,
                "editor_open": False,
                "editor_key": None,
                "editor_phase": None,
                # Draft override values for the edited phase. None = inherit
                # from base row / addon defaults. Covers every column that
                # the phase overlay can carry.
                "editor_draft": {
                    "x": None, "y": None, "w": None, "h": None,
                    "disabled": None,
                    "drive_mode": None,
                    "anim_dir": None,
                    "anim_duration_ms": None,
                    "anim_in_delay_ms": None,
                    "anim_out_delay_ms": None,
                },
                # Nudge step (in manialink units) for the position dpad and
                # the W/H steppers. User-configurable via the editor.
                "editor_pos_step": 1.0,
                # Engine-wide settings sub-view.
                "settings_open": False,
                # Editor "copy effective to..." combo open state.
                "copy_combo_open": False,
            }
            self._state[login] = st
        return st

    def _available_not_installed(self) -> list[Any]:
        installed = self.host._entries
        avail = [
            e for k, e in self.host._available.items() if k not in installed
            # Replacement widgets are managed in their own panel; they
            # must never appear in the normal Add picker.
            and not e.gbx_replace
        ]
        avail.sort(key=lambda e: e.name.lower())
        return avail

    def _entries_for_phase(self, phase: Phase) -> list[Any]:
        entries = []
        for entry in self.host._entries.values():
            # Replacement widgets are not phase-bound and have their
            # own management UI; exclude them from the normal list.
            if entry.gbx_replace:
                continue
            vp = entry.visible_phases
            if vp is None or phase in vp:
                entries.append(entry)
        entries.sort(key=lambda e: e.key)
        return entries

    def _replacement_entries(self) -> list[Any]:
        """All installed widgets that override a GBX manialink id."""
        out = [e for e in self.host._entries.values() if e.gbx_replace]
        out.sort(key=lambda e: e.name.lower())
        return out

    def _phase_counts(self, login: str) -> dict[Phase, int]:
        counts: dict[Phase, int] = {}
        engine = self.host.engine
        for phase in PHASE_ORDER:
            active = 0
            for entry in self._entries_for_phase(phase):
                resolved = engine.resolve(entry.key, login, phase=phase)
                if resolved is not None and not resolved.disabled:
                    active += 1
            counts[phase] = active
        return counts

    def _build_replacements_rows(self, login: str) -> list[dict[str, Any]]:
        """Per-login rows for the Replacements panel: one entry per
        installed widget that overrides a GBX manialink id."""
        rows: list[dict[str, Any]] = []
        for entry in self._replacement_entries():
            repl = entry.gbx_replace
            rows.append({
                "key": entry.key,
                "name": entry.name,
                "manialink_id": repl.manialink_id,
                "hide_ui_modules": list(repl.hide_ui_modules or ()),
                "enabled": self.host.is_replacement_enabled(login, entry.key),
            })
        return rows

    async def _on_replacement_toggle(self, login: str, key: str) -> None:
        entry = self.host._entries.get(key)
        if entry is None or not entry.gbx_replace:
            return
        currently = self.host.is_replacement_enabled(login, key)
        await self.host.set_replacement_enabled(login, key, not currently)
        await self.display(player_logins=[login])

    # ---- context ------------------------------------------------------

    async def get_per_player_data(self, login: str) -> dict[str, Any]:
        st = self._ensure_state(login)
        phase: Phase = st["phase"]
        page = max(1, int(st["page"].get(phase.value, 1)))

        entries = self._entries_for_phase(phase)
        total = len(entries)
        total_pages = max(1, (total + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)
        if page > total_pages:
            page = total_pages
            st["page"][phase.value] = page

        start = (page - 1) * ROWS_PER_PAGE
        slice_entries = entries[start:start + ROWS_PER_PAGE]

        # Build row dicts (template-friendly, no python objects in jinja).
        engine = self.host.engine
        rows = []
        for entry in slice_entries:
            resolved = engine.resolve(entry.key, login, phase=phase)
            disabled = bool(resolved.disabled) if resolved is not None else False
            dbg_set = engine._debug.get(login, set())
            debug_on = ("*" in dbg_set) or (entry.key in dbg_set)
            rows.append({
                "key": entry.key,
                "name": entry.name,
                "enabled": not disabled,
                "debug": debug_on,
            })

        phases_ctx = []
        counts = self._phase_counts(login)
        for p in PHASE_ORDER:
            phases_ctx.append({
                "key": p.value,
                "label": PHASE_LABEL[p],
                "count": counts[p],
                "selected": p == phase,
            })

        # Add-picker context.
        picker_open = bool(st.get("picker_open"))
        picker_rows: list[dict[str, Any]] = []
        picker_total = 0
        picker_page = 1
        picker_total_pages = 1
        if picker_open:
            avail = self._available_not_installed()
            picker_total = len(avail)
            picker_total_pages = max(1, (picker_total + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)
            picker_page = max(1, min(int(st.get("picker_page", 1)), picker_total_pages))
            st["picker_page"] = picker_page
            pstart = (picker_page - 1) * ROWS_PER_PAGE
            for e in avail[pstart:pstart + ROWS_PER_PAGE]:
                picker_rows.append({
                    "key": e.key,
                    "name": e.name,
                    "kind": e.kind.value,
                })

        # Editor context.
        editor_open = bool(st.get("editor_open"))
        editor_ctx: dict[str, Any] = {}
        if editor_open:
            ekey = st.get("editor_key")
            ephase: Phase = st.get("editor_phase")
            entry = self.host._entries.get(ekey) if ekey else None
            if entry is None or ephase is None:
                editor_open = False
                st["editor_open"] = False
            else:
                draft = st["editor_draft"]
                effective = self._editor_effective(ekey, ephase, draft)
                base = self.host.storage.get(ekey) or {}
                phase_row = self.host.storage.phase_get(ekey, ephase) or {}

                def _override(field: str) -> bool:
                    return (draft.get(field) is not None
                            or phase_row.get(field) is not None)

                editor_ctx = {
                    "key": ekey,
                    "name": entry.name,
                    "phase_key": ephase.value,
                    "phase_label": PHASE_LABEL[ephase],
                    "engine_matches": engine.current_phase == ephase,
                    "dirty": any(v is not None for v in draft.values()),
                }
                # Numeric fields with stepper display + base value badge.
                num_fields = [
                    ("x", "X (pos)",  "%.1f", entry.default_x),
                    ("y", "Y (pos)",  "%.1f", entry.default_y),
                    ("w", "Width",    "%.1f", entry.default_w),
                    ("h", "Height",   "%.1f", entry.default_h),
                    ("anim_duration_ms", "Duration", "%d ms", entry.animation.duration_ms),
                    ("anim_in_delay_ms",  "In delay", "%d ms", entry.animation.in_delay_ms),
                    ("anim_out_delay_ms", "Out delay", "%d ms", entry.animation.out_delay_ms),
                ]
                fields_ctx: dict[str, dict[str, Any]] = {}
                for fname, flabel, fmt, fdefault in num_fields:
                    base_v = base.get(fname)
                    if base_v is None:
                        base_v = fdefault
                    fields_ctx[fname] = {
                        "label": flabel,
                        "value": effective[fname],
                        "display": fmt % effective[fname],
                        "base_display": fmt % base_v,
                        "overridden": _override(fname),
                    }
                # Enum / bool fields.
                base_disabled = base.get("disabled")
                if base_disabled is None:
                    base_disabled = False
                base_drive = base.get("drive_mode") or entry.drive_mode.value
                base_anim = base.get("anim_dir") or entry.animation.direction.value
                fields_ctx["disabled"] = {
                    "label": "Disabled",
                    "value": effective["disabled"],
                    "display": "on" if effective["disabled"] else "off",
                    "base_display": "on" if base_disabled else "off",
                    "overridden": _override("disabled"),
                }
                fields_ctx["drive_mode"] = {
                    "label": "Drive mode",
                    "value": effective["drive_mode"],
                    "display": self._DRIVE_LABEL.get(
                        effective["drive_mode"], effective["drive_mode"],
                    ),
                    "base_display": self._DRIVE_LABEL.get(base_drive, base_drive),
                    "overridden": _override("drive_mode"),
                }
                fields_ctx["anim_dir"] = {
                    "label": "Animation",
                    "value": effective["anim_dir"],
                    "display": self._ANIM_LABEL.get(
                        effective["anim_dir"], effective["anim_dir"],
                    ),
                    "base_display": self._ANIM_LABEL.get(base_anim, base_anim),
                    "overridden": _override("anim_dir"),
                }
                editor_ctx["fields"] = fields_ctx
                # combo_box state + option lists
                combos = st.get("editor_combo_open", {}) or {}
                editor_ctx["drive_combo_open"] = bool(combos.get("drive_mode"))
                editor_ctx["anim_combo_open"] = bool(combos.get("anim_dir"))
                editor_ctx["drive_options"] = [
                    (v, self._DRIVE_LABEL.get(v, v)) for v in self._DRIVE_CYCLE
                ]
                editor_ctx["anim_options"] = [
                    (v, self._ANIM_LABEL.get(v, v)) for v in self._ANIM_CYCLE
                ]
                # Position-nudge step (for the dpad / W·H steppers).
                pos_step = float(st.get("editor_pos_step", 1.0))
                editor_ctx["pos_step"] = pos_step
                editor_ctx["pos_step_display"] = (
                    "%d" % pos_step if pos_step == int(pos_step) else "%.2f" % pos_step
                )
                # Copy-to combo state + option list (every phase except
                # the one being edited, plus an "all others" entry).
                editor_ctx["copy_combo_open"] = bool(st.get("copy_combo_open"))
                copy_opts = [("__all", "All other phases")]
                for ph in PHASE_ORDER:
                    if ph == ephase:
                        continue
                    copy_opts.append((ph.value, PHASE_LABEL[ph]))
                editor_ctx["copy_options"] = copy_opts

        # Engine-wide settings context.
        engine_settings = {
            "strip_prefer_top": engine.strip_prefer_top,
            "strip_thickness": engine.strip_thickness,
            "strip_thickness_display": (
                "%.1f" % engine.strip_thickness
            ),
        }

        return {
            "phases": phases_ctx,
            "selected_phase_key": phase.value,
            "selected_phase_label": PHASE_LABEL[phase],
            "rows": rows,
            "total_widgets": total,
            "page": page,
            "total_pages": total_pages,
            "engine_phase": (engine.current_phase.value if engine.current_phase else "?"),
            "confirm_remove_key": st.get("confirm_remove"),
            "confirm_remove_name": (
                self.host._entries[st["confirm_remove"]].name
                if st.get("confirm_remove") and st["confirm_remove"] in self.host._entries
                else ""
            ),
            "picker_open": picker_open,
            "picker_rows": picker_rows,
            "picker_total": picker_total,
            "picker_page": picker_page,
            "picker_total_pages": picker_total_pages,
            "editor_open": editor_open,
            "editor": editor_ctx,
            "settings_open": bool(st.get("settings_open")),
            "engine_settings": engine_settings,
            "replacements_open": bool(st.get("replacements_open")),
            "replacements_rows": self._build_replacements_rows(login),
        }
    # ---- actions ------------------------------------------------------

    async def handle_catch_all(self, player, action, values, **kwargs):
        # Receivers get the action with the view-id prefix already stripped.
        # Patterns:
        #   phase__select__<phase_key>
        #   pag__page__<n> | pag__first | pag__prev | pag__next | pag__last
        #   add__open
        #   row__<key>__enable | __debug | __edit | __remove
        #   confirm_remove__ok | confirm_remove__cancel
        login = player.login
        try:
            if action.startswith("phase__select__"):
                key = action[len("phase__select__"):]
                await self._on_phase_select(login, key)
                return
            if action.startswith("pag__"):
                await self._on_pagination(login, action[len("pag__"):])
                return
            if action == "add__open":
                st = self._ensure_state(login)
                st["picker_open"] = True
                st["picker_page"] = 1
                await self.display(player_logins=[login])
                return
            if action == "settings__open":
                st = self._ensure_state(login)
                st["settings_open"] = True
                await self.display(player_logins=[login])
                return
            if action == "replacements__open":
                st = self._ensure_state(login)
                st["replacements_open"] = True
                await self.display(player_logins=[login])
                return
            if action.startswith("replacements__row__"):
                # replacements__row__<key>__toggle
                # replacements__row__<key>__edit
                rest = action[len("replacements__row__"):]
                if rest.endswith("__toggle"):
                    key = rest[:-len("__toggle")]
                    await self._on_replacement_toggle(login, key)
                    return
                if rest.endswith("__edit"):
                    key = rest[:-len("__edit")]
                    st = self._ensure_state(login)
                    # Close the replacements panel so the editor isn't
                    # gated out by the manager.xml top `if` chain.
                    st["replacements_open"] = False
                    await self._editor_open(login, key, st["phase"])
                    return
                return
            if action == "_crumb__settings":
                # Current-page crumb: no-op.
                return
            if action == "settings__strip_top__toggle":
                await self._on_setting_toggle_strip_top(login)
                return
            if action == "settings__strip_thickness__inc":
                await self._on_setting_thickness(login, +0.1)
                return
            if action == "settings__strip_thickness__dec":
                await self._on_setting_thickness(login, -0.1)
                return
            if action == "_crumb__widget_engine":
                # Picker / editor / settings breadcrumb back to the manager.
                st = self._ensure_state(login)
                if st.get("editor_open"):
                    await self._editor_close(login)
                    return
                if st.get("picker_open"):
                    st["picker_open"] = False
                    await self.display(player_logins=[login])
                    return
                if st.get("settings_open"):
                    st["settings_open"] = False
                    await self.display(player_logins=[login])
                    return
                if st.get("replacements_open"):
                    st["replacements_open"] = False
                    await self.display(player_logins=[login])
                return
            if action.startswith("picker__pag__"):
                await self._on_picker_pagination(login, action[len("picker__pag__"):])
                return
            if action.startswith("picker__install__"):
                pkey = action[len("picker__install__"):]
                await self._on_picker_install(login, pkey)
                return
            if action.startswith("editor__nudge__"):
                # editor__nudge__<field>__<inc|dec>
                rest = action[len("editor__nudge__"):]
                if "__" in rest:
                    field, direction = rest.rsplit("__", 1)
                    await self._on_editor_nudge(login, field, direction)
                return
            if action.startswith("editor__pos__"):
                # editor__pos__<up|down|left|right|reset>
                what = action[len("editor__pos__"):]
                if what == "reset":
                    await self._on_editor_pos_reset(login)
                else:
                    await self._on_editor_pos_nudge(login, what)
                return
            if action.startswith("editor__posstep__"):
                # editor__posstep__<inc|dec>
                await self._on_editor_step_nudge(
                    login, action[len("editor__posstep__"):],
                )
                return
            if action == "editor__toggle__disabled":
                await self._on_editor_toggle_disabled(login)
                return
            # combo_box(name='editor__drive_mode') fires:
            #   editor__drive_mode__toggle
            #   editor__drive_mode__pick__<value>
            if action == "editor__drive_mode__toggle":
                await self._on_editor_combo_toggle(login, "drive_mode")
                return
            if action.startswith("editor__drive_mode__pick__"):
                val = action[len("editor__drive_mode__pick__"):]
                await self._on_editor_combo_pick(login, "drive_mode", val)
                return
            if action == "editor__anim_dir__toggle":
                await self._on_editor_combo_toggle(login, "anim_dir")
                return
            if action.startswith("editor__anim_dir__pick__"):
                val = action[len("editor__anim_dir__pick__"):]
                await self._on_editor_combo_pick(login, "anim_dir", val)
                return
            # 5-tile direction dpad mirrors the old widgets app:
            #   editor__setdir__<value>
            if action.startswith("editor__setdir__"):
                val = action[len("editor__setdir__"):]
                if val in self._ANIM_CYCLE:
                    await self._on_editor_combo_pick(login, "anim_dir", val)
                return
            if action == "editor__apply":
                await self._on_editor_apply(login)
                return
            if action == "editor__cancel":
                await self._editor_close(login)
                return
            if action == "editor__reset":
                await self._on_editor_reset(login)
                return
            # Copy effective draft to another phase (or all others).
            #   editor__copy__toggle
            #   editor__copy__pick__<phase|__all>
            if action == "editor__copy__toggle":
                st = self._ensure_state(login)
                st["copy_combo_open"] = not st.get("copy_combo_open", False)
                await self.display(player_logins=[login])
                return
            if action.startswith("editor__copy__pick__"):
                target = action[len("editor__copy__pick__"):]
                await self._on_editor_copy_to(login, target)
                return
            if action == "confirm_remove__ok":
                await self._on_confirm_remove(login, True)
                return
            if action == "confirm_remove__cancel":
                await self._on_confirm_remove(login, False)
                return
            if action.startswith("row__"):
                # row__<key>__<verb>; widget keys may contain underscores so
                # split on the final '__'.
                rest = action[len("row__"):]
                if "__" not in rest:
                    return
                key, verb = rest.rsplit("__", 1)
                await self._on_row_action(login, key, verb)
                return
        except Exception:
            logger.exception("WidgetEngineManagerView: action '%s' failed", action)
        await super().handle_catch_all(player, action, values, **kwargs)

    async def _on_phase_select(self, login: str, phase_key: str) -> None:
        try:
            phase = Phase(phase_key)
        except ValueError:
            return
        st = self._ensure_state(login)
        if st["phase"] == phase:
            return
        st["phase"] = phase
        await self.display(player_logins=[login])

    async def _on_pagination(self, login: str, verb: str) -> None:
        st = self._ensure_state(login)
        phase: Phase = st["phase"]
        entries = self._entries_for_phase(phase)
        total_pages = max(1, (len(entries) + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)
        cur = max(1, int(st["page"].get(phase.value, 1)))
        new = cur
        if verb == "first":
            new = 1
        elif verb == "prev":
            new = max(1, cur - 1)
        elif verb == "next":
            new = min(total_pages, cur + 1)
        elif verb == "last":
            new = total_pages
        elif verb.startswith("page__"):
            try:
                new = max(1, min(total_pages, int(verb[len("page__"):])))
            except ValueError:
                return
        if new == cur:
            return
        st["page"][phase.value] = new
        await self.display(player_logins=[login])

    async def _on_row_action(self, login: str, key: str, verb: str) -> None:
        entry = self.host._entries.get(key)
        if entry is None:
            return
        engine = self.host.engine
        st = self._ensure_state(login)
        phase: Phase = st["phase"]

        if verb == "enable":
            resolved = engine.resolve(key, login, phase=phase)
            cur_disabled = bool(resolved.disabled) if resolved is not None else False
            new_disabled = not cur_disabled
            await self.host.storage.phase_set(
                key, phase, {"disabled": new_disabled},
            )
            await self.host._redisplay(key)
            await self.display(player_logins=[login])
            return

        if verb == "debug":
            on_now = engine.is_debug(login, key)
            await engine.set_debug(login, key, not on_now)
            await self.host._redisplay(key)
            await self.display(player_logins=[login])
            return

        if verb == "remove":
            st["confirm_remove"] = key
            await self.display(player_logins=[login])
            return

        if verb == "edit":
            st = self._ensure_state(login)
            await self._editor_open(login, key, st["phase"])
            return

    async def _on_confirm_remove(self, login: str, accepted: bool) -> None:
        st = self._ensure_state(login)
        key = st.get("confirm_remove")
        phase: Phase = st["phase"]
        st["confirm_remove"] = None
        if not accepted or not key:
            await self.display(player_logins=[login])
            return
        entry = self.host._entries.get(key)
        if entry is None:
            await self.display(player_logins=[login])
            return
        name = entry.name
        try:
            await self.host.storage.phase_set(key, phase, {"disabled": True})
            await self.host._redisplay(key)
            await self._toast(
                login,
                f"Removed '{name}' from {PHASE_LABEL[phase]}",
                "success",
            )
        except Exception:
            logger.exception("WidgetEngineManagerView: phase remove failed")
            await self._toast(login, f"Remove '{name}' failed (see log)", "error")
        await self.display(player_logins=[login])

    async def _on_picker_pagination(self, login: str, verb: str) -> None:
        st = self._ensure_state(login)
        avail = self._available_not_installed()
        total_pages = max(1, (len(avail) + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)
        cur = max(1, int(st.get("picker_page", 1)))
        new = cur
        if verb == "first":
            new = 1
        elif verb == "prev":
            new = max(1, cur - 1)
        elif verb == "next":
            new = min(total_pages, cur + 1)
        elif verb == "last":
            new = total_pages
        elif verb.startswith("page__"):
            try:
                new = max(1, min(total_pages, int(verb[len("page__"):])))
            except ValueError:
                return
        if new == cur:
            return
        st["picker_page"] = new
        await self.display(player_logins=[login])

    async def _on_picker_install(self, login: str, key: str) -> None:
        entry = self.host._available.get(key)
        if entry is None:
            await self._toast(login, f"'{key}' not available", "error")
            return
        name = entry.name
        ok = await self.host.install_widget(key)
        if not ok:
            await self._toast(login, f"Install '{name}' failed (see log)", "error")
        else:
            await self._toast(login, f"Installed '{name}'", "success")
        await self.display(player_logins=[login])

    async def _toast(self, login: str, message: str, severity: str = "info") -> None:
        sig = None
        for code in ("notification_engine:notify", "tmsm_status:notify"):
            try:
                sig = self.app.context.signals.get_signal(code)
                break
            except KeyError:
                continue
        if sig is None:
            return
        try:
            await sig.send_robust({
                "message": message,
                "severity": severity,
                "login": login,
                "source": "widget_engine",
            })
        except Exception:
            logger.exception("WidgetEngineManagerView: toast failed")

    # ---- editor (slice 11) -------------------------------------------

    _DRAFT_FIELDS: tuple[str, ...] = (
        "x", "y", "w", "h",
        "disabled",
        "drive_mode",
        "anim_dir",
        "anim_duration_ms", "anim_in_delay_ms", "anim_out_delay_ms",
    )

    def _empty_draft(self) -> dict[str, Any]:
        return {f: None for f in self._DRAFT_FIELDS}

    # Nudge step per numeric field. x/y/w/h use the per-login configurable
    # `editor_pos_step` instead and are not listed here.
    _EDITOR_STEP: dict[str, float] = {
        "anim_duration_ms": 50.0,
        "anim_in_delay_ms": 50.0,
        "anim_out_delay_ms": 50.0,
    }
    # Selectable values for the position-nudge step stepper.
    _POS_STEP_CYCLE: tuple[float, ...] = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0)
    # Min clamps for fields where 0/negative would be nonsense.
    _EDITOR_MIN: dict[str, float] = {
        "w": 1.0, "h": 1.0,
        "anim_duration_ms": 0.0,
        "anim_in_delay_ms": 0.0,
        "anim_out_delay_ms": 0.0,
    }
    # Cycle order for enum fields.
    _DRIVE_CYCLE: tuple[str, ...] = (
        DriveMode.FIXED.value,
        DriveMode.HIDE_WHILE_DRIVING.value,
        DriveMode.ONLY_SHOWN_WHILE_DRIVING.value,
    )
    _ANIM_CYCLE: tuple[str, ...] = (
        AnimDir.NONE.value,
        AnimDir.LEFT.value,
        AnimDir.RIGHT.value,
        AnimDir.UP.value,
        AnimDir.DOWN.value,
    )
    # Compact human labels for the cycle button caption.
    _DRIVE_LABEL: dict[str, str] = {
        "fixed": "Always shown",
        "hide_while_driving": "Hide while driving",
        "only_shown_while_driving": "Only while driving",
    }
    _ANIM_LABEL: dict[str, str] = {
        "none": "None",
        "left": "Slide left",
        "right": "Slide right",
        "up": "Slide up",
        "down": "Slide down",
    }

    def _editor_effective(
        self, key: str, phase: Phase, draft: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve every editable field: addon defaults → base row → stored
        phase override → in-flight draft."""
        entry = self.host._entries[key]
        base = self.host.storage.get(key) or {}
        phase_row = self.host.storage.phase_get(key, phase) or {}
        defaults: dict[str, Any] = {
            "x": entry.default_x,
            "y": entry.default_y,
            "w": entry.default_w,
            "h": entry.default_h,
            "disabled": False,
            "drive_mode": entry.drive_mode.value,
            "anim_dir": entry.animation.direction.value,
            "anim_duration_ms": entry.animation.duration_ms,
            "anim_in_delay_ms": entry.animation.in_delay_ms,
            "anim_out_delay_ms": entry.animation.out_delay_ms,
        }
        out: dict[str, Any] = {}
        for f, dflt in defaults.items():
            v = dflt
            if base.get(f) is not None:
                v = base[f]
            if phase_row.get(f) is not None:
                v = phase_row[f]
            if draft.get(f) is not None:
                v = draft[f]
            out[f] = v
        # Normalise numeric fields.
        for f in ("x", "y", "w", "h"):
            out[f] = float(out[f])
        for f in ("anim_duration_ms", "anim_in_delay_ms", "anim_out_delay_ms"):
            out[f] = int(out[f])
        out["disabled"] = bool(out["disabled"])
        return out

    async def _editor_open(self, login: str, key: str, phase: Phase) -> None:
        if key not in self.host._entries:
            return
        st = self._ensure_state(login)
        st["editor_open"] = True
        st["editor_key"] = key
        st["editor_phase"] = phase
        st["editor_draft"] = self._empty_draft()
        st["editor_combo_open"] = {}
        st["copy_combo_open"] = False
        # Mark this player as editing so per-player redisplay paths kick in.
        try:
            await self.host.engine.enter_edit(login, key)
        except Exception:
            logger.exception("editor: enter_edit failed")
        try:
            await self.host.show_edit_overlay(login)
        except Exception:
            logger.exception("editor: show edit overlay failed")
        await self.display(player_logins=[login])

    async def _editor_close(self, login: str) -> None:
        st = self._ensure_state(login)
        key = st.get("editor_key")
        st["editor_open"] = False
        st["editor_key"] = None
        st["editor_phase"] = None
        st["editor_draft"] = self._empty_draft()
        st["editor_combo_open"] = {}
        st["copy_combo_open"] = False
        if key:
            try:
                await self.host.engine.clear_transient(login, key)
            except Exception:
                logger.exception("editor: clear_transient failed")
            try:
                await self.host.engine.exit_edit(login)
            except AttributeError:
                pass
            except Exception:
                logger.exception("editor: exit_edit failed")
        try:
            await self.host.hide_edit_overlay(login)
        except Exception:
            logger.exception("editor: hide edit overlay failed")
        await self.display(player_logins=[login])

    async def _editor_push_preview(self, login: str) -> None:
        st = self._ensure_state(login)
        key = st.get("editor_key")
        phase: Phase = st.get("editor_phase")
        if not key or phase is None:
            return
        effective = self._editor_effective(key, phase, st["editor_draft"])
        # Push the full xywh as transient so the widget renders with the
        # draft values for this player (regardless of phase mismatch the
        # engine treats transient as unconditional overlay).
        try:
            await self.host.engine.set_transient(login, key, effective)
        except Exception:
            logger.exception("editor: set_transient failed")
        try:
            await self.host.refresh_edit_overlay(login)
        except Exception:
            logger.exception("editor: refresh edit overlay failed")

    async def _on_editor_nudge(self, login: str, field: str, direction: str) -> None:
        if direction not in ("inc", "dec"):
            return
        st = self._ensure_state(login)
        # w/h share the configurable position step. ms fields keep their
        # hardcoded step (configurable later if needed).
        if field in ("w", "h"):
            step = float(st.get("editor_pos_step", 1.0))
        elif field in self._EDITOR_STEP:
            step = self._EDITOR_STEP[field]
        else:
            return
        if direction == "dec":
            step = -step
        key = st.get("editor_key")
        phase: Phase = st.get("editor_phase")
        if not key or phase is None:
            return
        # Seed the draft from the current effective value the first time
        # this field is touched, so nudges are relative to what's on screen.
        if st["editor_draft"].get(field) is None:
            current = self._editor_effective(key, phase, st["editor_draft"])[field]
            st["editor_draft"][field] = current
        new_val = st["editor_draft"][field] + step
        # Clamp where it matters.
        min_v = self._EDITOR_MIN.get(field)
        if min_v is not None and new_val < min_v:
            new_val = min_v
        # Integer fields stay int; floats keep one-decimal precision.
        if field.endswith("_ms"):
            new_val = int(round(new_val))
        else:
            new_val = round(new_val, 3)
        st["editor_draft"][field] = new_val
        await self._editor_push_preview(login)
        await self.display(player_logins=[login])

    async def _on_editor_pos_nudge(self, login: str, direction: str) -> None:
        """Position dpad: up/down move y, left/right move x, by editor_pos_step."""
        if direction not in ("up", "down", "left", "right"):
            return
        st = self._ensure_state(login)
        step = float(st.get("editor_pos_step", 1.0))
        if direction == "up":
            field, delta = "y", step
        elif direction == "down":
            field, delta = "y", -step
        elif direction == "right":
            field, delta = "x", step
        else:
            field, delta = "x", -step
        key = st.get("editor_key")
        phase: Phase = st.get("editor_phase")
        if not key or phase is None:
            return
        if st["editor_draft"].get(field) is None:
            current = self._editor_effective(key, phase, st["editor_draft"])[field]
            st["editor_draft"][field] = current
        st["editor_draft"][field] = round(st["editor_draft"][field] + delta, 3)
        await self._editor_push_preview(login)
        await self.display(player_logins=[login])

    async def _on_editor_pos_reset(self, login: str) -> None:
        """Drop x/y draft overrides; preview snaps back to stored/base."""
        st = self._ensure_state(login)
        st["editor_draft"]["x"] = None
        st["editor_draft"]["y"] = None
        await self._editor_push_preview(login)
        await self.display(player_logins=[login])

    async def _on_editor_step_nudge(self, login: str, direction: str) -> None:
        """Cycle editor_pos_step through _POS_STEP_CYCLE."""
        if direction not in ("inc", "dec"):
            return
        st = self._ensure_state(login)
        cur = float(st.get("editor_pos_step", 1.0))
        cycle = self._POS_STEP_CYCLE
        try:
            idx = cycle.index(cur)
        except ValueError:
            # Snap to nearest listed value.
            idx = min(range(len(cycle)), key=lambda i: abs(cycle[i] - cur))
        idx += 1 if direction == "inc" else -1
        idx = max(0, min(len(cycle) - 1, idx))
        st["editor_pos_step"] = cycle[idx]
        await self.display(player_logins=[login])

    async def _on_editor_toggle_disabled(self, login: str) -> None:
        st = self._ensure_state(login)
        key = st.get("editor_key")
        phase: Phase = st.get("editor_phase")
        if not key or phase is None:
            return
        cur = self._editor_effective(key, phase, st["editor_draft"])["disabled"]
        st["editor_draft"]["disabled"] = not cur
        await self._editor_push_preview(login)
        await self.display(player_logins=[login])

    async def _on_editor_combo_toggle(self, login: str, field: str) -> None:
        st = self._ensure_state(login)
        combos = st.setdefault("editor_combo_open", {})
        # Close other combos when opening one (only one dropdown at a time).
        new_state = not combos.get(field, False)
        for k in list(combos.keys()):
            combos[k] = False
        combos[field] = new_state
        await self.display(player_logins=[login])

    async def _on_editor_combo_pick(
        self, login: str, field: str, value: str,
    ) -> None:
        st = self._ensure_state(login)
        key = st.get("editor_key")
        phase: Phase = st.get("editor_phase")
        if not key or phase is None:
            return
        valid = self._DRIVE_CYCLE if field == "drive_mode" else self._ANIM_CYCLE
        if value not in valid:
            return
        st["editor_draft"][field] = value
        combos = st.setdefault("editor_combo_open", {})
        combos[field] = False
        await self._editor_push_preview(login)
        await self.display(player_logins=[login])

    async def _on_editor_reset(self, login: str) -> None:
        """Discard the in-flight draft and snap preview back to stored."""
        st = self._ensure_state(login)
        st["editor_draft"] = self._empty_draft()
        key = st.get("editor_key")
        if key:
            try:
                await self.host.engine.clear_transient(login, key)
            except Exception:
                logger.exception("editor: reset clear_transient failed")
        await self.display(player_logins=[login])

    async def _on_editor_apply(self, login: str) -> None:
        st = self._ensure_state(login)
        key = st.get("editor_key")
        phase: Phase = st.get("editor_phase")
        draft = st.get("editor_draft") or {}
        if not key or phase is None:
            return
        # Build a typed patch: floats for x/y/w/h, ints for ms, str for
        # enum-valued fields, bool for disabled.
        patch: dict[str, Any] = {}
        for f, v in draft.items():
            if v is None:
                continue
            if f in ("x", "y", "w", "h"):
                patch[f] = float(v)
            elif f.endswith("_ms"):
                patch[f] = int(v)
            elif f == "disabled":
                patch[f] = bool(v)
            else:
                patch[f] = str(v)
        if not patch:
            await self._toast(login, "Nothing to apply", "info")
            return
        try:
            await self.host.storage.phase_set(key, phase, patch)
        except Exception:
            logger.exception("editor: phase_set failed")
            await self._toast(login, "Apply failed (see log)", "error")
            return
        try:
            await self.host.engine.clear_transient(login, key)
        except Exception:
            logger.exception("editor: apply clear_transient failed")
        await self.host._redisplay(key)
        await self._toast(
            login,
            f"Saved {len(patch)} field(s) for '{key}' / {phase.value}",
            "success",
        )
        st["editor_draft"] = self._empty_draft()
        await self.display(player_logins=[login])

    async def _on_editor_copy_to(self, login: str, target: str) -> None:
        """Copy the currently-previewed (effective) values to one or all
        OTHER phases. `target` is a Phase.value or the literal '__all'."""
        st = self._ensure_state(login)
        st["copy_combo_open"] = False
        key = st.get("editor_key")
        cur_phase: Phase = st.get("editor_phase")
        if not key or cur_phase is None:
            await self.display(player_logins=[login])
            return
        effective = self._editor_effective(key, cur_phase, st["editor_draft"])
        # Build a typed patch covering every overlay column.
        patch: dict[str, Any] = {}
        for f, v in effective.items():
            if f in ("x", "y", "w", "h"):
                patch[f] = float(v)
            elif f.endswith("_ms"):
                patch[f] = int(v)
            elif f == "disabled":
                patch[f] = bool(v)
            else:
                patch[f] = str(v)
        # Resolve targets.
        if target == "__all":
            targets = [p for p in PHASE_ORDER if p != cur_phase]
        else:
            try:
                target_phase = Phase(target)
            except ValueError:
                await self.display(player_logins=[login])
                return
            if target_phase == cur_phase:
                await self._toast(login, "Already on that phase", "info")
                await self.display(player_logins=[login])
                return
            targets = [target_phase]
        ok = 0
        for tp in targets:
            try:
                await self.host.storage.phase_set(key, tp, patch)
                ok += 1
            except Exception:
                logger.exception(
                    "editor: copy_to %s/%s failed", key, tp.value,
                )
        await self.host._redisplay(key)
        await self._toast(
            login,
            f"Copied to {ok} phase(s)",
            "success" if ok else "error",
        )
        await self.display(player_logins=[login])

    # ── engine-wide settings handlers ─────────────────────────────────

    async def _on_setting_toggle_strip_top(self, login: str) -> None:
        engine = self.host.engine
        new_val = not engine.strip_prefer_top
        engine.strip_prefer_top = new_val
        try:
            await self.host.storage.setting_set(
                "strip_prefer_top", "1" if new_val else "0",
            )
        except Exception:
            logger.exception("settings: persist strip_prefer_top failed")
        await self.host._refresh_all()
        await self.display(player_logins=[login])

    async def _on_setting_thickness(self, login: str, delta: float) -> None:
        engine = self.host.engine
        new_val = round(engine.strip_thickness + delta, 2)
        if new_val < 0.1:
            new_val = 0.1
        if new_val > 5.0:
            new_val = 5.0
        engine.strip_thickness = new_val
        try:
            await self.host.storage.setting_set("strip_thickness", str(new_val))
        except Exception:
            logger.exception("settings: persist strip_thickness failed")
        await self.host._refresh_all()
        await self.display(player_logins=[login])


class WidgetEditOverlayView(BaseView):
    """Failsafe edit marker rendered independently of widget templates.

    Some legacy widgets don't import `widget_engine/frame.xml`, so they
    won't show frame-level edit helpers. This overlay ensures the currently
    edited widget is still visible for the editing player.
    """

    template_name = "widget_engine/edit_overlay.xml"
    audience: Audience = Audience.everyone()

    async def get_context_data(self):
        ctx = await super().get_context_data() or {}
        ctx.update(
            overlay_enabled=False,
            overlay_x=0.0,
            overlay_y=0.0,
            overlay_w=1.0,
            overlay_h=1.0,
        )
        return ctx

    async def get_per_player_data(self, login: str) -> dict[str, Any]:
        return self.app.edit_overlay_context(login)