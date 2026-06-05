"""Widget engine — core types.

This module defines the small surface that widget addons depend on:

    WidgetKind        — persistent vs popup
    DriveMode         — fixed / hide_while_driving / only_shown_while_driving
    Phase             — race phase enum (consumed by the resolver in later slices)
    AnimDir           — animation direction enum
    HideRule          — named/raw client-side hide conditions
    Animation         — slide direction + timing
    WidgetEntry       — what a widget registers with the engine

Slice 1: no DB, no editor, no themes, no groups, no personalisation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class WidgetKind(str, Enum):
    PERSISTENT = "persistent"   # always shown (subject to hide rule)
    POPUP = "popup"             # shown only on demand for a duration


class DriveMode(str, Enum):
    FIXED = "fixed"
    HIDE_WHILE_DRIVING = "hide_while_driving"
    ONLY_SHOWN_WHILE_DRIVING = "only_shown_while_driving"


class AnimDir(str, Enum):
    NONE = "none"
    LEFT = "left"
    RIGHT = "right"
    UP = "up"
    DOWN = "down"


class Phase(str, Enum):
    LOADING_MAP = "loading_map"
    WARMUP = "warmup"
    PRE_RACE = "pre_race"
    IN_RACE = "in_race"
    IN_PODIUM = "in_podium"
    POST_RACE = "post_race"


@dataclass(frozen=True)
class HideRule:
    """Conditions that hide the widget. Evaluated client-side every tick.

    `named` items are compiled to ManiaScript bool expressions by the
    engine. Built-in names: `in_menu`, `in_race`, `spectator`, `paused`,
    `speed_above:<N>`, `speed_below:<N>`. A leading `!` inverts the name.

    `raw` is appended as-is; in scope: `Speed`, `InRace`, `Spectator`,
    `Paused`, `InMenu`. Returning True hides the widget.
    """
    named: tuple[str, ...] = ()
    raw: str = ""


@dataclass(frozen=True)
class Animation:
    """Slide direction + independent in/out delays."""
    direction: AnimDir = AnimDir.RIGHT
    duration_ms: int = 300
    in_delay_ms: int = 0
    out_delay_ms: int = 0


@dataclass(frozen=True)
class GbxReplacement:
    """Declare that this widget shadows an existing manialink id.

    The engine will (re-)push the addon's XML using `manialink_id`,
    overwriting whatever default/controller manialink the server or
    PyPlanet sent for the same id.

    The widget addon must expose:
        async def build_replacement_xml(self, login: str) -> str
    returning either a full `<manialink ...>` document (any id; engine
    rewrites it to `manialink_id`) or just the inner body (engine wraps).
    Return an empty string to render nothing for that player.
    """
    manialink_id: str
    # Sent to all players once on register and on phase change. Per-player
    # push also runs on player_connect (after a short delay so the
    # original manialink lands first and we can overwrite it).
    connect_delay_s: float = 0.4
    # TM2020 title-pack default UIs (e.g. the TimeAttack TAB scoreboard)
    # are rendered by ManiaScript UI modules, not manialinks, so an id
    # re-send cannot reach them. Listing module ids here causes the
    # engine to fire the modescript callback
    # `Common.UIModules.SetProperties` to hide them, so the custom
    # manialink is the only thing visible. Common ids include
    # `Race_ScoresTable`, `Race_ScoresTable2`, `Race_ScoresTable3`.
    hide_ui_modules: tuple[str, ...] = ()
    # Optional hold-to-show hotkey. When set, widget_engine wraps the
    # widget XML in a hidden root frame and injects a ManiaScript that
    # shows the frame while the key is held and auto-hides after a
    # short timeout (handles OS auto-repeat). KeyName values use the
    # ManiaScript naming (e.g. `Tab`, `Space`, `M`, `F1`).
    hotkey: str | None = None
    # When False, the engine sends the widget's XML wrapped only in a
    # `<manialink>` shell (with the engine-owned id) — no chrome frame,
    # no bg/strip quads, no slide-anim ManiaScript. The widget is fully
    # responsible for positioning, background, and any client-side
    # scripting. Required when the widget needs its own top-level
    # `<script>` block (only one is allowed per manialink, and nesting
    # one inside a frame makes ManiaScript silently drop it).
    chrome: bool = True


@dataclass(frozen=True)
class WidgetEntry:
    """What a widget addon hands to the engine at register time."""
    key: str
    name: str
    description: str = ""
    icon: str = "object-group"
    default_x: float = 0.0
    default_y: float = 0.0
    default_w: float = 40.0
    default_h: float = 10.0
    kind: WidgetKind = WidgetKind.PERSISTENT
    popup_duration_ms: int = 4000
    drive_mode: DriveMode = DriveMode.FIXED
    hide_rule: HideRule = field(default_factory=HideRule)
    animation: Animation = field(default_factory=Animation)
    bg_color: str = "40404080"
    strip_color: str = "ffae00"
    strip_enabled: bool = True
    # Phases in which the widget is allowed to display. None = always
    # visible regardless of phase. Empty tuple = never visible.
    visible_phases: tuple[Phase, ...] | None = None
    author: str = ""
    version: str = ""
    # Optional: claim a manialink id and own it (overrides whatever the
    # game/PyPlanet/another app pushed for that id). None = normal widget.
    gbx_replace: GbxReplacement | None = None
