"""Reusable per-app log-level :class:`Setting` helper + process-wide registry.

Each tmsm app logs under its own dotted package logger (``app.name`` ==
``pyplanet.apps.tmsm.<pkg>``), and every submodule uses
``logging.getLogger(__name__)`` — a child of that package logger. Setting the
package logger's level therefore controls the verbosity of the whole app
without touching the root logger (which would affect every app at once).

This module exposes :func:`register_log_level_setting`, which registers a
``Log level`` setting on a single app and wires a ``change_target`` callback so
changes made from PyPlanet's ``//settings`` apply live. The registrations are
tracked in a module-level registry so the central ``logging_engine`` app (its
``//loglevel`` command and master UI) can enumerate and drive them.
"""
from __future__ import annotations

import logging
from typing import Optional

from pyplanet.contrib.setting import Setting

logger = logging.getLogger(__name__)

# Ordered for the //settings dropdown / command help. DEFAULT == inherit the
# pool's root level (logging.NOTSET on the app logger).
LOG_LEVELS = ("DEFAULT", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# PyPlanet only accepts categories from its Categories static class
# (Setting.CAT_*), so reuse the standard "Behaviour" bucket.
SETTING_KEY = "log_level"
SETTING_CATEGORY = Setting.CAT_BEHAVIOUR

# label -> (logger_name, Setting). Module-level so it survives for the life of
# the process and lets the central app / //loglevel command enumerate apps.
_REGISTRY: dict[str, tuple[str, Setting]] = {}


def logger_name_for(app) -> str:
    """The dotted logger name an app and its submodules log under."""
    name = getattr(app, "name", "") or ""
    if name:
        return name
    return app.__class__.__module__.rsplit(".", 1)[0]


def _coerce_level(value) -> Optional[int]:
    """Map a stored setting value to a logging level int, or ``None`` to inherit."""
    v = str(value or "DEFAULT").strip().upper()
    if v in ("", "DEFAULT", "NOTSET", "INHERIT"):
        return None
    return getattr(logging, v, None)


def apply_level(logger_name: str, value) -> None:
    """Apply a stored value to ``logger_name``; ``DEFAULT``/unknown -> inherit."""
    lvl = _coerce_level(value)
    logging.getLogger(logger_name).setLevel(logging.NOTSET if lvl is None else lvl)


async def register_log_level_setting(app) -> Optional[Setting]:
    """Register a per-app ``Log level`` setting on *app* and apply its value.

    Idempotent per process: a second call for the same app returns the already
    registered :class:`Setting` without registering it again.
    """
    label = getattr(app, "label", None) or getattr(app, "name", "") or repr(app)
    existing = _REGISTRY.get(label)
    if existing is not None:
        return existing[1]

    log_name = logger_name_for(app)
    holder: dict[str, Setting] = {}

    async def _on_change(*_args) -> None:
        setting = holder.get("setting")
        if setting is None:
            return
        try:
            apply_level(log_name, await setting.get_value())
        except Exception:
            logger.exception("logging_engine: applying level for %s failed", log_name)

    description = (
        "Verbosity for this app's logger (" + log_name + "). "
        "DEFAULT inherits the pool's root level; pick DEBUG to debug just "
        "this app without flooding the console. Applies live. "
        "Allowed: " + ", ".join(LOG_LEVELS) + "."
    )
    try:
        setting = Setting(
            SETTING_KEY, "Log level", SETTING_CATEGORY, type=str,
            default="DEFAULT", description=description,
            change_target=_on_change,
        )
    except TypeError:
        # Older PyPlanet without a change_target kwarg: live //settings edits
        # won't auto-apply (the //loglevel command applies explicitly anyway).
        setting = Setting(
            SETTING_KEY, "Log level", SETTING_CATEGORY, type=str,
            default="DEFAULT", description=description,
        )
    holder["setting"] = setting

    try:
        await app.context.setting.register(setting)
    except Exception:
        logger.exception("logging_engine: register log_level for %s failed", label)
        return None

    _REGISTRY[label] = (log_name, setting)
    try:
        apply_level(log_name, await setting.get_value())
    except Exception:
        logger.exception("logging_engine: initial apply for %s failed", log_name)
    return setting


def registry() -> dict[str, tuple[str, Setting]]:
    """The live ``label -> (logger_name, Setting)`` map of registered apps."""
    return _REGISTRY
