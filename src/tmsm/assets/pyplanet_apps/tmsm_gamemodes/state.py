"""Persistent state for the gamemodes orchestrator.

Single JSON file at ``<cwd>/tmsm_gamemodes.json``:

* ``active``        - key of the currently activated mode, or ``None``
* ``configs``       - {mode_key: config_dict} per-mode operator overrides
* ``mode_states``   - {mode_key: arbitrary_dict} mode-private runtime that
                       must survive a PyPlanet restart mid-rotation.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "active": None,
        "configs": {},
        "mode_states": {},
    }


def state_path() -> Path:
    return Path(os.getcwd()).resolve() / "tmsm_gamemodes.json"


def load() -> dict[str, Any]:
    out = default_state()
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(raw, dict):
        return out
    for k in ("active", "configs", "mode_states"):
        if k in raw:
            out[k] = raw[k]
    out["configs"] = dict(out.get("configs") or {})
    out["mode_states"] = dict(out.get("mode_states") or {})
    return out


def save(state: dict[str, Any]) -> None:
    try:
        state_path().write_text(
            json.dumps(state, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        logger.exception("tmsm_gamemodes: state save failed")
