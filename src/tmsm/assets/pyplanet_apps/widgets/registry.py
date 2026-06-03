"""Widget registry — dataclasses describing a registered widget."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional


class WidgetKind(str, Enum):
    PERSISTENT = "persistent"   # always shown (subject to hide rule)
    POPUP = "popup"             # shown only on demand for a duration


@dataclass
class HideRule:
    """Conditions that cause a widget to animate out.

    `named` is a list of named conditions evaluated client-side in the
    widget frame's ManiaScript loop. Built-in names:

        in_menu, in_race, spectator, paused,
        speed_above:<N>, speed_below:<N>

    A leading '!' inverts the named condition (e.g. ``!in_race``).
    All names are OR'd together — any one matching hides the widget.

    `raw` is an optional ManiaScript boolean expression evaluated each
    frame; the variables ``Speed``, ``InRace``, ``Spectator``, ``Paused``,
    ``InMenu`` are available. Returning ``True`` hides the widget.
    """
    named: list[str] = field(default_factory=list)
    raw: str = ""


@dataclass
class Animation:
    """Animation parameters applied when a widget shows / hides."""
    direction: str = "right"   # up | down | left | right | none
    duration_ms: int = 300      # animation duration in milliseconds
    delay_ms: int = 0           # delay before animation starts


@dataclass
class WidgetEntry:
    """A widget registered with the widgets app."""
    key: str
    name: str
    description: str = ""
    icon: str = "object-group"
    # default position / size (manialink units)
    default_x: float = 0.0
    default_y: float = 0.0
    default_w: float = 40.0
    default_h: float = 10.0
    kind: WidgetKind = WidgetKind.PERSISTENT
    # for popups: how long to stay visible if no override given
    popup_duration_ms: int = 4000
    # hide + animation config (defaults apply if not customised per-player)
    hide_rule: HideRule = field(default_factory=HideRule)
    animation: Animation = field(default_factory=Animation)
    # informational
    enabled: bool = True
    author: str = ""
    version: str = ""
    # When False the editor hides the Personal scope option and the app
    # rejects per-player position overrides for this widget. Useful for
    # widgets where the position is structural (e.g. toast anchor) or
    # purely admin-controlled (e.g. hub launcher button).
    allow_personal: bool = True
    # Optional group slot key. Widgets sharing the same non-empty key are
    # resolved as one slot per player: only one winner is visible at runtime.
    group_key: str = ""
    # Higher priority wins inside a group.
    group_priority: int = 0
    # Stable tie-breaker inside a group (lower order wins when priority ties).
    group_order: int = 0
    # optional callback invoked when an admin clicks "Show now" for a popup
    popup_trigger: Optional[Callable[[str], Awaitable[None]]] = None
