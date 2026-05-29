"""Design tokens — colors, sizes, z-index lanes.

Structured as a single swappable namespace so v2 theming is a drop-in
(replace `theme` with a different instance). For v1 there is one default
theme; everything reads from `theme.*`.

ManiaLink color format is 4 hex digits 'RGBA' (each 0-f). Background alphas
match PyPlanet's translucent style.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class Z:
    """Z-index lanes. Reserve bands so addons don't fight for the foreground."""
    BACKGROUND = 100   # full-screen backdrops, HUD frames behind everything
    CONTENT    = 200   # default for widgets (panels, buttons, scoreboards)
    OVERLAY    = 400   # detail popovers, tooltips, dropdowns
    MODAL      = 500   # blocking dialogs
    TOAST      = 900   # transient notifications, always on top


@dataclass(frozen=True)
class Color:
    # Surfaces
    surface:        str = "0006"
    surface_high:   str = "0008"
    surface_strong: str = "000b"

    # Text
    text:        str = "fff"
    text_muted:  str = "aaa"
    text_dim:    str = "666"
    text_dark:   str = "000"

    # Button variants (idle / hover handled by Maniascript in views)
    primary:        str = "0d48"
    primary_hover:  str = "0d4c"
    danger:         str = "f448"
    danger_hover:   str = "f44c"
    ghost:          str = "fff2"
    ghost_hover:    str = "fff5"
    success:        str = "0a48"
    success_hover:  str = "0a4c"
    warning:        str = "f808"

    # Checkbox states
    cb_on:  str = "0d48"
    cb_off: str = "4448"


@dataclass(frozen=True)
class Size:
    # Button sizes: (width, height)
    btn_sm: tuple[float, float] = (16.0, 5.0)
    btn_md: tuple[float, float] = (22.0, 7.0)
    btn_lg: tuple[float, float] = (32.0, 9.0)

    # Checkbox square side
    cb_sm: float = 2.8
    cb_md: float = 3.5
    cb_lg: float = 4.5

    # Entry (line_edit) heights match button heights of same size

    # Font sizes
    font_sm: float = 0.9
    font_md: float = 1.1
    font_lg: float = 1.4

    # Heading sizes by level (1 = largest)
    heading: dict[int, float] = field(default_factory=lambda: {1: 1.8, 2: 1.4, 3: 1.1})

    # Padding scale
    pad_sm: float = 1.0
    pad_md: float = 2.0
    pad_lg: float = 4.0


@dataclass(frozen=True)
class Theme:
    color: Color = field(default_factory=Color)
    size: Size = field(default_factory=Size)
    z: type = Z


# Default theme used everywhere in v1. Swap in v2 for theming.
theme = Theme()
