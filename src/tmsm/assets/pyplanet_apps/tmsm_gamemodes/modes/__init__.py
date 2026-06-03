"""Built-in game mode registry.

Importing this package registers every shipped mode (each module uses the
``@register`` decorator from ``..base``). Third-party / out-of-tree modes
can be registered the same way from their own apps; the orchestrator only
reads ``base.REGISTRY``.
"""
from . import evolution   # noqa: F401  side-effect: registers mode
from . import random_challenge  # noqa: F401  side-effect: registers mode
from . import random_challenge_points  # noqa: F401  side-effect: registers mode
