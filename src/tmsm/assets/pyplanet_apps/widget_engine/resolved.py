"""Resolved widget state — the immutable record a renderer consumes.

Slice 1: the resolver returns a `ResolvedWidget` built directly from the
widget's class defaults (no DB, no phase, no transient overrides). Later
slices add layers without changing this contract.
"""
from __future__ import annotations

from dataclasses import dataclass

from .registry import AnimDir, DriveMode, WidgetEntry


@dataclass(frozen=True)
class ResolvedWidget:
    """Final per-player state after all resolution layers have run."""
    key: str
    x: float
    y: float
    w: float
    h: float
    drive_mode: DriveMode
    anim_dir: AnimDir
    anim_duration_ms: int
    anim_in_delay_ms: int
    anim_out_delay_ms: int
    disabled: bool
    # Display look (resolved against engine globals once those exist).
    bg_color: str
    strip_color: str
    strip_enabled: bool
    strip_edge: str            # 'top' | 'bottom' | 'left' | 'right' | ''
    strip_thickness: float

    @classmethod
    def from_entry(
        cls,
        entry: WidgetEntry,
        *,
        strip_prefer_top: bool,
        strip_thickness: float,
        row: "dict | None" = None,
    ) -> "ResolvedWidget":
        # `row` is a we_widget dict from storage. When present its non-NULL
        # values override the entry defaults; NULLs fall through.
        r = row or {}
        x = r.get("x"); y = r.get("y"); w = r.get("w"); h = r.get("h")
        drive_mode = r.get("drive_mode")
        anim_dir = r.get("anim_dir")
        anim_dur = r.get("anim_duration_ms")
        anim_in = r.get("anim_in_delay_ms")
        anim_out = r.get("anim_out_delay_ms")
        disabled = bool(r.get("disabled") or False)
        dm = DriveMode(drive_mode) if drive_mode else entry.drive_mode
        ad = AnimDir(anim_dir) if anim_dir else entry.animation.direction
        return cls(
            key=entry.key,
            x=float(x) if x is not None else entry.default_x,
            y=float(y) if y is not None else entry.default_y,
            w=float(w) if w is not None else entry.default_w,
            h=float(h) if h is not None else entry.default_h,
            drive_mode=dm,
            anim_dir=ad,
            anim_duration_ms=int(anim_dur) if anim_dur is not None else entry.animation.duration_ms,
            anim_in_delay_ms=int(anim_in) if anim_in is not None else entry.animation.in_delay_ms,
            anim_out_delay_ms=int(anim_out) if anim_out is not None else entry.animation.out_delay_ms,
            disabled=disabled,
            bg_color=entry.bg_color,
            strip_color=entry.strip_color,
            strip_enabled=entry.strip_enabled,
            strip_edge=_strip_edge(ad, strip_prefer_top) if entry.strip_enabled else "",
            strip_thickness=strip_thickness,
        )


# Strip edge derived from the animation direction. Strip sits on the
# opposite edge from where the widget slides off-screen.
_STRIP_EDGE_FROM_ANIM: dict[AnimDir, str] = {
    AnimDir.RIGHT: "left",
    AnimDir.LEFT:  "right",
    AnimDir.UP:    "bottom",
    AnimDir.DOWN:  "top",
    AnimDir.NONE:  "",
}


def _strip_edge(direction: AnimDir, prefer_top: bool) -> str:
    if prefer_top and direction in (AnimDir.LEFT, AnimDir.RIGHT):
        return "top"
    return _STRIP_EDGE_FROM_ANIM.get(direction, "")
