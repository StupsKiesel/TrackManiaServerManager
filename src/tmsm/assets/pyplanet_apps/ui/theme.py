"""Process-wide UI theme cache.

The `UiApp` registers PyPlanet `Setting`s for the shared window chrome
colors and pushes their values into the module-level dict below whenever
they change. `BaseView.get_context_data` reads `current()` and exposes it
to every template render as `theme`, so the `ui.window()` macro (and any
other shared chrome) can colour itself without each addon configuring it.

All values are stored as raw manialink color strings (e.g. ``"000a"``,
``"15dfa"``) — no leading ``#``, accepting any of the rgb / rgba /
rrggbb / rrggbbaa lengths the game understands.
"""
from __future__ import annotations

DEFAULT_WINDOW_HEADER_COLOR: str = "000a"
DEFAULT_WINDOW_BODY_COLOR: str = "000a"
DEFAULT_WINDOW_ACCENT_COLOR: str = "15dfa"


_state: dict[str, str] = {
    "window_header_color": DEFAULT_WINDOW_HEADER_COLOR,
    "window_body_color":   DEFAULT_WINDOW_BODY_COLOR,
    "window_accent_color": DEFAULT_WINDOW_ACCENT_COLOR,
}


def current() -> dict[str, str]:
    """Snapshot of the active theme. Safe to embed in template ctx."""
    return dict(_state)


def update(**values: str) -> None:
    """Replace any of the known keys. Unknown keys are ignored.

    Values that are None/empty fall back to the corresponding DEFAULT_*.
    """
    defaults = {
        "window_header_color": DEFAULT_WINDOW_HEADER_COLOR,
        "window_body_color":   DEFAULT_WINDOW_BODY_COLOR,
        "window_accent_color": DEFAULT_WINDOW_ACCENT_COLOR,
    }
    for key, val in values.items():
        if key not in defaults:
            continue
        s = (str(val).strip().lstrip("#") if val is not None else "")
        _state[key] = s if s else defaults[key]
