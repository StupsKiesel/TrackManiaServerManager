"""Built-in widget themes.

A theme is a flat dict of design tokens. Widget templates that opt into
themeing receive a resolved ``theme`` dict in their frame context with
the active theme's tokens merged on top of master-admin overrides.

Color values are ManiaLink hex strings (RGB ``rrggbb`` or RGBA
``rrggbbaa``); fonts are ManiaLink font names; opacities are floats in
``[0.0, 1.0]``; integer pixel-ish values are integers.

Editing tokens at runtime: overrides are stored in
``tmsm_widget_theme_overrides`` and resolved per-token, per-theme, so
a customised ``nord.accent`` survives switching to ``dark`` and back.
"""
from __future__ import annotations

# Token list — kept in sync with the editor grid. Order is the render order.
TOKENS: tuple[str, ...] = (
    # Surface
    "bg", "bg_alt", "bg_elev", "panel", "panel_alt",
    "border", "border_strong", "divider", "scrim",
    # Text
    "text", "text_muted", "text_strong", "text_on_accent",
    "text_disabled", "link",
    # Accents
    "accent", "accent_alt", "accent_soft",
    "success", "warning", "danger", "info",
    # State
    "state_active", "state_hover", "state_pressed", "state_disabled",
    # Header / title bar
    "header_bg", "header_text", "header_accent",
    # Data series
    "chart_1", "chart_2", "chart_3", "chart_4", "chart_5", "chart_6",
    # Effects
    "shadow", "glow", "opacity_panel", "opacity_overlay",
    "font_text", "font_strong",
)

# Convenience groupings for editor rendering.
TOKEN_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Surface", ("bg", "bg_alt", "bg_elev", "panel", "panel_alt",
                  "border", "border_strong", "divider", "scrim")),
    ("Text", ("text", "text_muted", "text_strong", "text_on_accent",
              "text_disabled", "link")),
    ("Accents", ("accent", "accent_alt", "accent_soft",
                  "success", "warning", "danger", "info")),
    ("State", ("state_active", "state_hover", "state_pressed", "state_disabled")),
    ("Header", ("header_bg", "header_text", "header_accent")),
    ("Charts", ("chart_1", "chart_2", "chart_3", "chart_4", "chart_5", "chart_6")),
    ("Effects", ("shadow", "glow", "opacity_panel", "opacity_overlay",
                  "font_text", "font_strong")),
)

DEFAULT_THEME: str = "dark"


def _theme(**kwargs: object) -> dict[str, object]:
    """Theme constructor — fills in any missing token with a sane fallback."""
    fallback = {
        "bg": "111111ee",  "bg_alt": "1a1a1aee", "bg_elev": "222222ee",
        "panel": "111c",   "panel_alt": "1a1a1add",
        "border": "ffffff22", "border_strong": "ffffff55", "divider": "ffffff11",
        "scrim": "000000aa",
        "text": "ffffff", "text_muted": "aaaaaa", "text_strong": "ffffff",
        "text_on_accent": "111111", "text_disabled": "555555", "link": "15dfa",
        "accent": "15dfa", "accent_alt": "0d8fcc", "accent_soft": "15dfa33",
        "success": "2dd47b", "warning": "f5c542", "danger": "e2484a", "info": "4ab8ff",
        "state_active": "15dfa", "state_hover": "ffffff33",
        "state_pressed": "ffffff55", "state_disabled": "ffffff11",
        "header_bg": "0a0a0aee", "header_text": "ffffff", "header_accent": "15dfa",
        "chart_1": "15dfa", "chart_2": "f5c542", "chart_3": "2dd47b",
        "chart_4": "e2484a", "chart_5": "b566ff", "chart_6": "4ab8ff",
        "shadow": "000000aa", "glow": "15dfa55",
        "opacity_panel": 0.93, "opacity_overlay": 0.65,
        "font_text": "GameFont", "font_strong": "GameFontBlack",
    }
    fallback.update(kwargs)
    return fallback


THEMES: dict[str, dict[str, object]] = {
    "dark": _theme(
        bg="111111ee", bg_alt="1a1a1aee", bg_elev="222222ee",
        panel="111c", panel_alt="1a1a1add",
        accent="15dfa", accent_alt="0d8fcc", accent_soft="15dfa33",
        header_bg="0a0a0aee", header_accent="15dfa",
    ),
    "light": _theme(
        bg="f0f0f0ee", bg_alt="e6e6e6ee", bg_elev="fafafaee",
        panel="f0f0f0ee", panel_alt="e6e6e6dd",
        border="00000022", border_strong="00000055", divider="00000011",
        scrim="ffffffaa",
        text="111111", text_muted="555555", text_strong="000000",
        text_on_accent="ffffff", text_disabled="aaaaaa", link="0d8fcc",
        accent="0d8fcc", accent_alt="15dfa", accent_soft="0d8fcc33",
        state_active="0d8fcc", state_hover="00000022",
        state_pressed="00000044", state_disabled="00000011",
        header_bg="ffffffee", header_text="111111", header_accent="0d8fcc",
        shadow="000000aa", glow="0d8fcc55",
    ),
    "nord": _theme(
        bg="2e3440ee", bg_alt="3b4252ee", bg_elev="434c5eee",
        panel="2e3440ee", panel_alt="3b4252dd",
        border="4c566a55", border_strong="4c566aaa", divider="4c566a33",
        text="eceff4", text_muted="d8dee9", text_strong="ffffff",
        text_on_accent="2e3440", text_disabled="4c566a", link="88c0d0",
        accent="88c0d0", accent_alt="5e81ac", accent_soft="88c0d033",
        success="a3be8c", warning="ebcb8b", danger="bf616a", info="81a1c1",
        state_active="88c0d0", state_hover="4c566a55",
        state_pressed="4c566aaa", state_disabled="4c566a22",
        header_bg="3b4252ee", header_accent="88c0d0",
        chart_1="88c0d0", chart_2="ebcb8b", chart_3="a3be8c",
        chart_4="bf616a", chart_5="b48ead", chart_6="81a1c1",
        glow="88c0d055",
    ),
    "dracula": _theme(
        bg="282a36ee", bg_alt="383a46ee", bg_elev="44475aee",
        panel="282a36ee", panel_alt="383a46dd",
        border="6272a455", border_strong="6272a4aa", divider="6272a433",
        text="f8f8f2", text_muted="bdbdbd", text_strong="ffffff",
        text_on_accent="282a36", text_disabled="6272a4", link="8be9fd",
        accent="bd93f9", accent_alt="ff79c6", accent_soft="bd93f933",
        success="50fa7b", warning="f1fa8c", danger="ff5555", info="8be9fd",
        state_active="bd93f9", state_hover="6272a455",
        state_pressed="6272a4aa", state_disabled="6272a422",
        header_bg="44475aee", header_accent="bd93f9",
        chart_1="bd93f9", chart_2="f1fa8c", chart_3="50fa7b",
        chart_4="ff5555", chart_5="ff79c6", chart_6="8be9fd",
        glow="bd93f955",
    ),
    "neon": _theme(
        bg="0a0014ee", bg_alt="14001eee", bg_elev="1e0028ee",
        panel="0a0014ee", panel_alt="14001edd",
        border="ff00ff55", border_strong="ff00ffaa", divider="ff00ff22",
        text="ffffff", text_muted="cccccc", text_strong="ffffff",
        text_on_accent="0a0014", text_disabled="555555", link="00ffff",
        accent="ff00ff", accent_alt="00ffff", accent_soft="ff00ff33",
        success="00ff88", warning="ffff00", danger="ff0044", info="00ffff",
        state_active="ff00ff", state_hover="ff00ff33",
        state_pressed="ff00ff66", state_disabled="ffffff11",
        header_bg="14001eee", header_accent="ff00ff",
        chart_1="ff00ff", chart_2="ffff00", chart_3="00ff88",
        chart_4="ff0044", chart_5="00ffff", chart_6="ff8800",
        glow="ff00ff88",
    ),
    "tm_classic": _theme(
        bg="1a0000ee", bg_alt="2a0000ee", bg_elev="3a0000ee",
        panel="1a0000ee", panel_alt="2a0000dd",
        border="ffffff33", border_strong="ffffff77", divider="ffffff22",
        text="ffffff", text_muted="dddddd", text_strong="ffffff",
        text_on_accent="1a0000", text_disabled="888888", link="ffd700",
        accent="e2484a", accent_alt="ffd700", accent_soft="e2484a33",
        success="2dd47b", warning="ffd700", danger="ff0000", info="ffffff",
        state_active="e2484a", state_hover="ffffff22",
        state_pressed="ffffff44", state_disabled="ffffff11",
        header_bg="0a0000ee", header_accent="e2484a",
        chart_1="e2484a", chart_2="ffd700", chart_3="ffffff",
        chart_4="2dd47b", chart_5="ff8800", chart_6="4ab8ff",
        glow="e2484a55",
    ),
}


def theme_keys() -> list[str]:
    """Stable order for UI rendering."""
    return list(THEMES.keys())


def resolve_theme(theme_key: str, overrides: dict[str, str] | None = None
                  ) -> dict[str, object]:
    """Return a fully-populated token dict for ``theme_key`` with any
    master-admin overrides merged on top. Unknown keys fall back to the
    default theme; unknown tokens are filled from the dark theme.
    """
    base = THEMES.get(theme_key) or THEMES[DEFAULT_THEME]
    out = dict(THEMES[DEFAULT_THEME])
    out.update(base)
    if overrides:
        out.update({k: v for k, v in overrides.items() if k in TOKENS})
    return out
