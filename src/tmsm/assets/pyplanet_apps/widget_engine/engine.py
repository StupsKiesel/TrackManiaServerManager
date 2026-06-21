"""Public façade exposed to widget addons.

`WidgetEngine` is the ONLY object widget code is allowed to touch on the
engine app. The host app (`WidgetsApp`) instantiates one and pins it to
itself; widget bases reach it as `self.engine`.

Slice 1: methods return values derived from the registered `WidgetEntry`
only — no DB, no editor state, no phase, no transient overrides.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
import time
from typing import TYPE_CHECKING, Any, Optional

from .registry import Phase, WidgetEntry
from .resolved import ResolvedWidget
from .resolver import _provenance, resolve

if TYPE_CHECKING:
    from .app import WidgetsApp

logger = logging.getLogger(__name__)


# Columns a transient overlay is allowed to carry (mirror of
# storage._PHASE_OVERLAY_COLUMNS — kept inline to avoid a circular import).
_TRANSIENT_COLUMNS: frozenset = frozenset({
    "x", "y", "w", "h",
    "drive_mode", "anim_dir",
    "anim_duration_ms", "anim_in_delay_ms", "anim_out_delay_ms",
    "disabled",
})

_RUNTIME_LAYOUT_COLUMNS: frozenset = frozenset({
    "x", "y", "w", "h",
    "drive_mode", "anim_dir",
    "anim_duration_ms", "anim_in_delay_ms", "anim_out_delay_ms",
    "disabled",
})


class WidgetEngine:
    def __init__(self, host: "WidgetsApp") -> None:
        self._host = host
        # Engine-wide display flags (later slices: persisted in we_setting).
        self.strip_prefer_top: bool = False
        self.strip_thickness: float = 1.0
        # Global color overrides: when enabled, every widget's bg/strip
        # color is replaced by these values at resolve time.
        self.global_colors_enabled: bool = False
        self.global_bg_color: str = "40404080"
        self.global_strip_color: str = "ffae00"
        # Current race phase. None until the host sets it (or stays None
        # forever for hosts that don't report phases). Slice 3.
        self.current_phase: Optional[Phase] = None
        # Slice 5: transient per-player overlays.
        # (login, key) -> (patch_dict, expires_at_monotonic | None)
        self._transient: dict[tuple[str, str], tuple[dict[str, Any], Optional[float]]] = {}
        # (login, key) -> asyncio.Task scheduled to clear the entry at TTL.
        self._transient_tasks: dict[tuple[str, str], asyncio.Task] = {}
        # Slice 5.5: owner-scoped runtime layout overlays (global, non-persistent).
        # owner -> key -> patch_dict
        self._runtime_layouts: dict[str, dict[str, dict[str, Any]]] = {}
        # precedence order (later wins).
        self._runtime_layout_order: list[str] = []
        # Slice 6: per-player debug overlay toggles.
        # login -> set of widget keys (the sentinel "*" means all widgets).
        self._debug: dict[str, set[str]] = {}
        # Slice 7: per-player edit mode. One widget at a time per player.
        # login -> widget_key currently being edited.
        self._editing: dict[str, str] = {}
        # Per-player monitor calibration (used by monitor app).
        # edge offset: horizontal outward push + optional vertical shift.
        self._ui_offset: dict[str, tuple[float, float]] = {}
        # vertical unstretch percentage (positive compresses Y/H).
        self._ui_stretch: dict[str, float] = {}

    # ---- registration -------------------------------------------------

    def register(self, entry: WidgetEntry) -> None:
        self._host._register_entry(entry)

    def entry(self, key: str) -> Optional[WidgetEntry]:
        return self._host._entries.get(key)

    # ---- phase tracking -----------------------------------------------

    async def set_phase(self, phase: Optional[Phase]) -> None:
        """Update the engine's current phase. Widgets whose visibility
        changes at this boundary are re-rendered (incoming widgets appear,
        outgoing widgets disappear); widgets that remain visible are left
        alone so they don't flicker.
        """
        if phase == self.current_phase:
            return
        previous = self.current_phase

        def _visible_in(p: Optional[Phase]) -> set[str]:
            if p is None:
                return set()
            keys: set[str] = set()
            for key, entry in self._host._entries.items():
                phases = entry.visible_phases
                if phases is not None and p not in phases:
                    continue
                # A widget that is statically allowed in phase `p` is
                # still INVISIBLE there when the operator disabled it
                # — either globally (base row) or per-phase override.
                # Skipping this check made `incoming`/`outgoing` purely
                # static, so a phase-only disable toggled while the
                # current phase was already showing the widget never
                # caused a refresh on later phase transitions (and a
                # copy-to-other-phases never took effect either).
                row = self._host.storage.get(key)
                if row and bool(row.get("disabled")):
                    continue
                phase_row = self._host.storage.phase_get(key, p)
                if phase_row and bool(phase_row.get("disabled")):
                    continue
                keys.add(key)
            return keys

        old_visible = _visible_in(previous)
        new_visible = _visible_in(phase)

        logger.debug(
            "widget_engine: phase %s -> %s (out=%d in=%d)",
            previous.value if previous else "?",
            phase.value if phase else "?",
            len(old_visible),
            len(new_visible),
        )

        self.current_phase = phase

        # Bootstrap / shutdown: render every widget cold.
        if previous is None or phase is None:
            try:
                await self._host._refresh_all()
            except Exception:
                logger.exception(
                    "widget_engine: bootstrap refresh after phase change failed",
                )
            return

        # Only re-render widgets whose visibility actually changed at this
        # boundary. Continuing widgets keep their manialink — no flicker.
        outgoing = old_visible - new_visible
        incoming = new_visible - old_visible
        if outgoing or incoming:
            try:
                await self._host._refresh_phase_change(outgoing, incoming)
            except Exception:
                logger.exception(
                    "widget_engine: phase-change refresh failed",
                )

    # ---- resolution ---------------------------------------------------

    def resolve(
        self,
        key: str,
        login: str,
        *,
        phase: Optional[Phase] = None,
    ) -> Optional[ResolvedWidget]:
        entry = self._host._entries.get(key)
        if entry is None:
            return None
        row = self._host.storage.get(key)
        effective_phase = phase if phase is not None else self.current_phase
        phase_row = (
            self._host.storage.phase_get(key, effective_phase)
            if effective_phase is not None else None
        )
        transient_row = self.get_transient(login, key)
        runtime_row = self.get_runtime_layout(key)
        if runtime_row:
            # The master kill-switch (operator-level `disabled` on the
            # base row, or a phase-level `disabled` override) MUST win
            # over any runtime layout. Gamemode/profile overlays publish
            # a layout that always carries `disabled=False` for every
            # widget in the profile (it's a layout, not an enable
            # decision), and without this guard that `False` would
            # silently re-enable a widget the operator just disabled.
            base_disabled = bool(row.get("disabled")) if row else False
            phase_disabled = (
                phase_row is not None and bool(phase_row.get("disabled"))
            )
            if base_disabled or phase_disabled:
                runtime_row = dict(runtime_row)
                runtime_row.pop("disabled", None)
            merged = dict(runtime_row)
            if transient_row:
                merged.update(transient_row)
            transient_row = merged
        resolved = resolve(
            entry,
            row=row,
            phase_row=phase_row,
            transient_row=transient_row,
            phase=effective_phase,
            strip_prefer_top=self.strip_prefer_top,
            strip_thickness=self.strip_thickness,
            global_bg_color=(self.global_bg_color if self.global_colors_enabled else None),
            global_strip_color=(self.global_strip_color if self.global_colors_enabled else None),
        )
        return self._apply_ui_calibration(login, resolved)

    # ---- monitor calibration -----------------------------------------

    def get_ui_offset(self, login: str) -> dict[str, float]:
        x, y = self._ui_offset.get(login, (0.0, 0.0))
        return {"x": float(x), "y": float(y)}

    async def set_ui_offset(self, login: str, x: float, y: float) -> None:
        if not login:
            return
        self._ui_offset[login] = (float(x), float(y))
        await self._redisplay_login(login)

    async def clear_ui_offset(self, login: str) -> None:
        if not login:
            return
        self._ui_offset.pop(login, None)
        await self._redisplay_login(login)

    def get_ui_stretch(self, login: str) -> float:
        return float(self._ui_stretch.get(login, 0.0))

    async def set_ui_stretch(self, login: str, value: float) -> None:
        if not login:
            return
        value = float(value)
        if abs(value) < 1e-9:
            self._ui_stretch.pop(login, None)
        else:
            self._ui_stretch[login] = value
        await self._redisplay_login(login)

    def _apply_ui_calibration(
        self, login: str, resolved: Optional[ResolvedWidget],
    ) -> Optional[ResolvedWidget]:
        if resolved is None or not login:
            return resolved
        off_x, off_y = self._ui_offset.get(login, (0.0, 0.0))
        stretch = float(self._ui_stretch.get(login, 0.0))
        if off_x == 0.0 and off_y == 0.0 and stretch == 0.0:
            return resolved

        x = resolved.x
        # Edge-fit: push widgets away from center when positive.
        if off_x != 0.0:
            if x > 0.0:
                x += off_x
            elif x < 0.0:
                x -= off_x
        y = resolved.y + off_y
        h = resolved.h

        # Vertical unstretch: +value compresses Y/H, -value expands.
        if stretch != 0.0:
            factor = 1.0 - (stretch / 100.0)
            if factor < 0.1:
                factor = 0.1
            y = y * factor
            h = max(0.5, h * factor)

        return replace(resolved, x=x, y=y, h=h)

    async def _redisplay_login(self, login: str) -> None:
        if not login:
            return
        for key in list(self._host._widget_apps.keys()):
            await self._host._redisplay_for(key, login)

    # ---- gbx manialink-id replacement façade --------------------------

    def is_replacement_active(self, key: str) -> bool:
        return self._host.is_replacement_active(key)

    async def push_replacement(
        self, key: str, logins: Optional[list[str]] = None,
    ) -> None:
        await self._host.push_replacement(key, logins=logins)

    # ---- transient overrides (slice 5) --------------------------------

    def get_transient(self, login: str, key: str) -> Optional[dict[str, Any]]:
        """Return the live transient overlay for `(login, key)` or None.
        Expired entries are dropped lazily on read."""
        if not login:
            return None
        ck = (login, key)
        item = self._transient.get(ck)
        if item is None:
            return None
        patch, expires = item
        if expires is not None and time.monotonic() >= expires:
            self._transient.pop(ck, None)
            return None
        return patch

    async def set_transient(
        self,
        login: str,
        key: str,
        patch: dict[str, Any],
        ttl_s: Optional[float] = None,
    ) -> None:
        """Set/replace a transient overlay for one player. `ttl_s` of
        None or <=0 means no expiry (persists until clear or restart).
        Triggers a per-player redisplay for the affected widget."""
        if not login or key not in self._host._entries:
            return
        clean = {k: v for k, v in patch.items() if k in _TRANSIENT_COLUMNS}
        if not clean:
            return
        ck = (login, key)
        # Cancel any pending expiry task from a prior call.
        prev_task = self._transient_tasks.pop(ck, None)
        if prev_task is not None and not prev_task.done():
            prev_task.cancel()
        expires = None
        if ttl_s is not None and ttl_s > 0:
            expires = time.monotonic() + float(ttl_s)
        self._transient[ck] = (clean, expires)
        await self._host._redisplay_for(key, login)
        if expires is not None:
            self._transient_tasks[ck] = asyncio.create_task(
                self._expire_transient(login, key, float(ttl_s))
            )

    async def _expire_transient(self, login: str, key: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        ck = (login, key)
        self._transient_tasks.pop(ck, None)
        # Only act if the entry is still present and actually expired (a
        # later set_transient may have replaced it).
        item = self._transient.get(ck)
        if item is None:
            return
        _patch, expires = item
        if expires is None or time.monotonic() < expires:
            return
        self._transient.pop(ck, None)
        try:
            await self._host._redisplay_for(key, login)
        except Exception:
            logger.exception(
                "widget_engine: expire redisplay '%s'/'%s' failed", key, login,
            )

    async def clear_transient(self, login: str, key: str) -> None:
        if not login:
            return
        ck = (login, key)
        task = self._transient_tasks.pop(ck, None)
        if task is not None and not task.done():
            task.cancel()
        if self._transient.pop(ck, None) is None:
            return
        await self._host._redisplay_for(key, login)

    # ---- runtime layout overlays (slice 5.5) -------------------------

    def get_runtime_layout(self, key: str) -> Optional[dict[str, Any]]:
        out: dict[str, Any] = {}
        for owner in self._runtime_layout_order:
            rows = self._runtime_layouts.get(owner) or {}
            patch = rows.get(key)
            if patch:
                out.update(patch)
        return out or None

    async def set_runtime_layout(
        self,
        owner: str,
        key: str,
        patch: dict[str, Any],
    ) -> None:
        owner = str(owner or "").strip()
        if not owner or key not in self._host._entries:
            return
        clean = {k: v for k, v in (patch or {}).items() if k in _RUNTIME_LAYOUT_COLUMNS}
        if not clean:
            return
        bucket = self._runtime_layouts.setdefault(owner, {})
        bucket[key] = clean
        if owner in self._runtime_layout_order:
            self._runtime_layout_order.remove(owner)
        self._runtime_layout_order.append(owner)
        await self._host._redisplay(key)

    async def clear_runtime_layout(
        self,
        owner: str,
        key: str,
    ) -> None:
        owner = str(owner or "").strip()
        if not owner:
            return
        bucket = self._runtime_layouts.get(owner)
        if not bucket or key not in bucket:
            return
        bucket.pop(key, None)
        if not bucket:
            self._runtime_layouts.pop(owner, None)
            if owner in self._runtime_layout_order:
                self._runtime_layout_order.remove(owner)
        await self._host._redisplay(key)

    async def clear_runtime_owner(self, owner: str) -> None:
        owner = str(owner or "").strip()
        if not owner:
            return
        bucket = self._runtime_layouts.pop(owner, None)
        if owner in self._runtime_layout_order:
            self._runtime_layout_order.remove(owner)
        if not bucket:
            return
        for key in list(bucket.keys()):
            await self._host._redisplay(key)

    async def clear_runtime_all(self) -> None:
        keys: set[str] = set()
        for bucket in self._runtime_layouts.values():
            keys.update(bucket.keys())
        self._runtime_layouts.clear()
        self._runtime_layout_order.clear()
        for key in sorted(keys):
            await self._host._redisplay(key)

    def runtime_layout_all(self, owner: Optional[str] = None) -> dict[tuple[str, str], dict[str, Any]]:
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for own, rows in self._runtime_layouts.items():
            if owner is not None and own != owner:
                continue
            for key, patch in rows.items():
                out[(own, key)] = dict(patch)
        return out

    def transient_all(
        self, login: Optional[str] = None,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Snapshot of live (non-expired) transient overlays. Filters by
        `login` when supplied."""
        now = time.monotonic()
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for ck, (patch, expires) in list(self._transient.items()):
            if expires is not None and now >= expires:
                self._transient.pop(ck, None)
                continue
            if login is not None and ck[0] != login:
                continue
            out[ck] = patch
        return out

    # ---- editor / debug -----------------------------------------------

    def is_editing(self, login: str, key: Optional[str] = None) -> bool:
        """Slice 7. With no `key`, return True when the player is editing
        any widget. With a `key`, only True when that specific widget is
        being edited."""
        cur = self._editing.get(login)
        if cur is None:
            return False
        return key is None or cur == key

    def editing_key(self, login: str) -> Optional[str]:
        return self._editing.get(login)

    async def enter_edit(self, login: str, key: str) -> bool:
        """Mark a widget as being edited by this player. Returns True if
        the call changed anything. Debug overlay is NOT toggled here —
        it is a separate, explicit per-player choice (otherwise leaving
        edit mode would strand the overlay enabled)."""
        if not login or key not in self._host._entries:
            return False
        prev = self._editing.get(login)
        if prev == key:
            return False
        self._editing[login] = key
        # If switching from another widget, refresh that one too so its
        # hide rules re-engage.
        if prev is not None and prev != key:
            await self._host._redisplay_for(prev, login)
        await self._host._redisplay_for(key, login)
        return True

    async def exit_edit(self, login: str) -> Optional[str]:
        """Leave edit mode. Returns the key that was being edited (or None).
        Debug overlay state is independent and is not touched here."""
        prev = self._editing.pop(login, None)
        if prev is None:
            return None
        await self._host._redisplay_for(prev, login)
        return prev

    def is_debug(self, login: str, key: str) -> bool:
        keys = self._debug.get(login)
        if not keys:
            return False
        return "*" in keys or key in keys

    async def set_debug(self, login: str, key: str, on: bool) -> None:
        """Toggle a per-player debug overlay. `key` may be a widget key or
        the sentinel "*" meaning every widget."""
        if not login:
            return
        keys = self._debug.setdefault(login, set())
        changed = False
        if on:
            if key not in keys:
                keys.add(key)
                changed = True
        else:
            if key in keys:
                keys.discard(key)
                changed = True
            if not keys:
                self._debug.pop(login, None)
        if not changed:
            return
        if key == "*":
            for k in list(self._host._widget_apps.keys()):
                await self._host._redisplay_for(k, login)
        else:
            await self._host._redisplay_for(key, login)

    async def clear_debug(self, login: str) -> None:
        keys = self._debug.pop(login, None)
        if not keys:
            return
        targets = list(self._host._widget_apps.keys()) if "*" in keys else list(keys)
        for k in targets:
            await self._host._redisplay_for(k, login)

    def debug_keys(self, login: str) -> set[str]:
        return set(self._debug.get(login, ()))

    def debug_status(self, login: str, key: str) -> str:
        """One-line provenance summary for the widget — feeds the live
        debug label rendered by the frame template."""
        entry = self._host._entries.get(key)
        if entry is None:
            return ""
        row = self._host.storage.get(key)
        effective_phase = self.current_phase
        phase_row = (
            self._host.storage.phase_get(key, effective_phase)
            if effective_phase is not None else None
        )
        transient_row = self.get_transient(login, key)
        prov = _provenance(row, phase_row, transient_row)
        # Compress: count per-source for the geometry block and call out
        # the top non-default winners explicitly.
        winners = [c for c, src in prov.items() if src != "default"]
        sources = [prov.get(c, "default") for c in ("x", "y", "w", "h")]
        geom = "/".join(s[0].upper() for s in sources)  # e.g. T/T/B/B
        phase_txt = effective_phase.value if effective_phase else "?"
        return f"ph={phase_txt} geom={geom} layers={len(winners)}"

    def debug_lines(self, login: str, key: str) -> list[dict]:
        """Multi-row provenance dump shown in the rebuilt debug overlay.
        Each entry is `{"text": "<coloured>", "plain": "<uncoloured>"}`;
        the template sizes the per-row backdrop from `plain`'s length so
        backgrounds hug the actual content.

        Source tags:  D=default  B=we_widget  P=we_phase_override  T=transient
        """
        entry = self._host._entries.get(key)
        if entry is None:
            return []
        row = self._host.storage.get(key)
        effective_phase = self.current_phase
        phase_row = (
            self._host.storage.phase_get(key, effective_phase)
            if effective_phase is not None else None
        )
        transient_row = self.get_transient(login, key)
        prov = _provenance(row, phase_row, transient_row)
        resolved = self.resolve(key, login)
        if resolved is None:
            return []

        def tag(col: str) -> str:
            return prov.get(col, "default")[0].upper()

        phase_txt = effective_phase.value if effective_phase else "?"
        editing = self.is_editing(login, key)
        flags: list[str] = []
        if editing:
            flags.append(("$f8f", "EDIT"))
        if resolved.disabled:
            flags.append(("$f80", "DISABLED"))
        flags_col = ("  " + " ".join(f"{c}{t}" for c, t in flags)) if flags else ""
        flags_plain = ("  " + " ".join(t for _c, t in flags)) if flags else ""

        counts: dict[str, int] = {"default": 0, "base": 0, "phase": 0, "transient": 0}
        for src in prov.values():
            counts[src] = counts.get(src, 0) + 1
        layers_col = (
            f"D$fff{counts['default']}$888  B$fff{counts['base']}$888  "
            f"P$fff{counts['phase']}$888  T$fff{counts['transient']}"
        )
        layers_plain = (
            f"D{counts['default']}  B{counts['base']}  "
            f"P{counts['phase']}  T{counts['transient']}"
        )

        rows: list[tuple[str, str]] = [
            (
                f"$0afphase $fff{phase_txt}$888{flags_col}",
                f"phase {phase_txt}{flags_plain}",
            ),
            (
                f"$888layers   $aaa{layers_col}",
                f"layers   {layers_plain}",
            ),
            (
                f"$888X $fff{resolved.x:7.2f} $f0f[{tag('x')}]    "
                f"$888Y $fff{resolved.y:7.2f} $f0f[{tag('y')}]",
                f"X {resolved.x:7.2f} [{tag('x')}]    "
                f"Y {resolved.y:7.2f} [{tag('y')}]",
            ),
            (
                f"$888W $fff{resolved.w:7.2f} $f0f[{tag('w')}]    "
                f"$888H $fff{resolved.h:7.2f} $f0f[{tag('h')}]",
                f"W {resolved.w:7.2f} [{tag('w')}]    "
                f"H {resolved.h:7.2f} [{tag('h')}]",
            ),
            (
                f"$888drive $fff{resolved.drive_mode.value} "
                f"$f0f[{tag('drive_mode')}]",
                f"drive {resolved.drive_mode.value} [{tag('drive_mode')}]",
            ),
            (
                f"$888anim  $fff{resolved.anim_dir.value} "
                f"$f0f[{tag('anim_dir')}]    "
                f"$888dur $fff{resolved.anim_duration_ms}ms "
                f"$f0f[{tag('anim_duration_ms')}]",
                f"anim  {resolved.anim_dir.value} [{tag('anim_dir')}]    "
                f"dur {resolved.anim_duration_ms}ms [{tag('anim_duration_ms')}]",
            ),
            (
                f"$888delays  in $fff{resolved.anim_in_delay_ms}ms "
                f"$f0f[{tag('anim_in_delay_ms')}]    "
                f"$888out $fff{resolved.anim_out_delay_ms}ms "
                f"$f0f[{tag('anim_out_delay_ms')}]",
                f"delays  in {resolved.anim_in_delay_ms}ms "
                f"[{tag('anim_in_delay_ms')}]    "
                f"out {resolved.anim_out_delay_ms}ms "
                f"[{tag('anim_out_delay_ms')}]",
            ),
        ]
        return [{"text": col, "plain": plain} for col, plain in rows]

    # ---- signal helper ------------------------------------------------

    async def emit(self, code: str, **payload) -> None:
        try:
            sig = self._host.context.signals.get_signal(f"widget_engine:{code}")
        except KeyError:
            logger.debug("widget_engine: signal %r not registered yet", code)
            return
        await sig.send_robust(payload, raw=True)
