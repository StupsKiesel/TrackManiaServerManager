"""tmsm widgets — global on-screen widget framework.

Apps register a widget by subclassing ``WidgetAppBase``. The widgets app
tracks positions (global + per-player overrides), supports an in-game
position editor, and provides a shared frame macro that handles client
side hide rules with configurable animations.
"""
from .registry import WidgetEntry, WidgetKind, HideRule, Animation  # noqa: F401
from .app import WidgetsApp  # noqa: F401

# NOTE: WidgetAppBase is intentionally NOT imported here. PyPlanet's app
# loader scans the package module via inspect.getmembers (alphabetical),
# and an exposed AppConfig subclass that sorts before WidgetsApp would be
# picked instead — registering this package under the wrong label.
# Widget addons should import WidgetAppBase from the submodule:
#     from pyplanet.apps.tmsm.widgets.widget_base import WidgetAppBase
