"""tmsm jukebox - server map queue with tmsm.ui UI.

Replaces (shadows) the PyPlanet contrib jukebox under the same `jukebox`
label; keeps the same public API (`.jukebox`, `add_to_jukebox`, ...).
"""
from .app import App_Jukebox  # noqa: F401
