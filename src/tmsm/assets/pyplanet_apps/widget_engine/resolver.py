"""Pure resolution function.

Slices implemented (lowest to highest precedence):
    1. code default       (from WidgetEntry)
    2. global base        (we_widget row — slice 2)
    3. phase visibility   (entry.visible_phases × engine.current_phase — slice 3)
    4. phase override     (we_phase_override row for current_phase — slice 4)
    5. transient override (in-memory per-player overlay with TTL — slice 5)
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional

from .registry import Phase, WidgetEntry
from .resolved import ResolvedWidget


# Columns the phase overlay carries; must match storage._PHASE_OVERLAY_COLUMNS.
_OVERLAY_COLUMNS: tuple[str, ...] = (
    "x", "y", "w", "h",
    "drive_mode", "anim_dir",
    "anim_duration_ms", "anim_in_delay_ms", "anim_out_delay_ms",
    "disabled",
)


def _merge_overlay(
    base: Optional[dict[str, Any]],
    overlay: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Layer `overlay` on top of `base` for the known overlay columns.
    Only non-NULL overlay values win; everything else falls through."""
    if not overlay:
        return base
    merged: dict[str, Any] = dict(base) if base else {}
    for col in _OVERLAY_COLUMNS:
        v = overlay.get(col)
        if v is not None:
            merged[col] = v
    return merged


def _provenance(
    row: Optional[dict[str, Any]],
    phase_row: Optional[dict[str, Any]],
    transient_row: Optional[dict[str, Any]],
) -> dict[str, str]:
    """Per-column winner across the overlay layers. Layers checked from
    highest precedence to lowest; anything still unset falls back to the
    code default."""
    out: dict[str, str] = {}
    for col in _OVERLAY_COLUMNS:
        if transient_row is not None and transient_row.get(col) is not None:
            out[col] = "transient"
        elif phase_row is not None and phase_row.get(col) is not None:
            out[col] = "phase"
        elif row is not None and row.get(col) is not None:
            out[col] = "base"
        else:
            out[col] = "default"
    return out


def resolve(
    entry: WidgetEntry,
    *,
    row: Optional[dict[str, Any]] = None,
    phase_row: Optional[dict[str, Any]] = None,
    transient_row: Optional[dict[str, Any]] = None,
    phase: Optional[Phase] = None,
    strip_prefer_top: bool = False,
    strip_thickness: float = 1.0,
    global_bg_color: Optional[str] = None,
    global_strip_color: Optional[str] = None,
) -> ResolvedWidget:
    """Compose the final widget state for the current frame."""
    effective = _merge_overlay(row, phase_row)
    effective = _merge_overlay(effective, transient_row)
    resolved = ResolvedWidget.from_entry(
        entry,
        strip_prefer_top=strip_prefer_top,
        strip_thickness=strip_thickness,
        row=effective,
        global_bg_color=global_bg_color,
        global_strip_color=global_strip_color,
    )
    # Phase visibility: when the widget declares a phase set and we know
    # the current phase, force-disable when out of phase.
    if (
        entry.visible_phases is not None
        and phase is not None
        and phase not in entry.visible_phases
        and not resolved.disabled
    ):
        resolved = replace(resolved, disabled=True)
    return resolved
