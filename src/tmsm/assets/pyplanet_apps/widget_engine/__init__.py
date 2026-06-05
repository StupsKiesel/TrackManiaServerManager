"""widget_engine — minimal phase-aware widget framework.

Public types are exported here. Widget addons should import:

    from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase
    from pyplanet.apps.tmsm.widget_engine import (
        WidgetKind, DriveMode, AnimDir, HideRule, Animation, Phase,
    )

NOTE: `WidgetAppBase` is intentionally NOT re-exported here. PyPlanet's
loader does `inspect.getmembers(package)` and picks the first AppConfig
subclass it sees (alphabetical); if WidgetAppBase were importable from
the package root the host app would be misidentified.
"""
from .registry import (  # noqa: F401
    AnimDir,
    Animation,
    DriveMode,
    HideRule,
    Phase,
    WidgetEntry,
    WidgetKind,
)
from .app import WidgetsApp  # noqa: F401
