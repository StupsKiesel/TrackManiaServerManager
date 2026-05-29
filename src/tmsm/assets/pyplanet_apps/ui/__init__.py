"""tmsm UI framework — PySide6-style widgets for PyPlanet manialinks.

Public surface:

    from pyplanet.apps.tmsm.ui import BaseView, FormView, Audience, Z, theme

Templates are imported with:

    {% import 'tmsm_ui/widgets.xml' as ui %}
"""
from .app import UiApp  # noqa: F401
from .audience import Audience  # noqa: F401
from .tokens import Z, Color, Size, theme  # noqa: F401
from .views import BaseView, FormView  # noqa: F401

__all__ = [
    "UiApp",
    "Audience",
    "Z",
    "Color",
    "Size",
    "theme",
    "BaseView",
    "FormView",
]
