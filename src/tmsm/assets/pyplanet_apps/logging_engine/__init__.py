"""logging_engine addon: central per-app log level register + master UI.

Discovers every loaded tmsm app and gives each a "Log level" setting (under the
"Logging" category in //settings) so developers can raise/lower verbosity per
app without touching the root logger. Provides the //loglevel admin command and
a master-admins-only panel (hub tile / //logging). Exposes
register_log_level_setting() for apps that want to register their own setting.
"""
from .app import LoggingApp  # noqa: F401
from .loglevel import (  # noqa: F401
    LOG_LEVELS,
    apply_level,
    logger_name_for,
    register_log_level_setting,
    registry,
)
