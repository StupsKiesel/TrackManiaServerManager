"""Admin policy for the TMX browser.

A single JSON file at ``<cwd>/tmx_policy.json`` defines server-wide
restrictions that operators cannot override:

* ``locked``  - {filter_key: forced_value} pinned filters
* ``hidden``  - filter keys removed from the operator's filter screen
* ``length_min_s_floor`` / ``length_max_s_cap`` - numeric clamps in seconds
* ``tags_required_any`` - operator query must include at least one of these
* ``tags_blocked``      - blocked tag ids (stripped from operator selection)

``apply_to_filters`` enforces the policy at search time (always wins over
operator input). ``visible_to_operator`` strips hidden slots from a filter
dict so the operator UI never shows them.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Filter keys the admin may lock to a fixed value.
LOCKABLE_KEYS: tuple[str, ...] = (
    "author", "environment", "vehicle", "maptype", "mood",
    "difficulty", "routes", "collection",
)
# Filter rows the admin may remove from the operator's filter UI.
HIDEABLE_KEYS: tuple[str, ...] = LOCKABLE_KEYS + ("length", "tags")

_EMPTY_STR_KEYS = {"author", "maptype"}


def default_policy() -> dict[str, Any]:
    return {
        "version":            1,
        "locked":             {},
        "hidden":             [],
        "length_min_s_floor": None,
        "length_max_s_cap":   None,
        "tags_required_any":  [],
        "tags_blocked":       [],
    }


def policy_path() -> Path:
    return Path(os.getcwd()).resolve() / "tmx_policy.json"


def load() -> dict[str, Any]:
    out = default_policy()
    try:
        data = json.loads(policy_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    out.update({k: v for k, v in data.items() if k in out})
    out["locked"] = dict(data.get("locked") or {})
    out["hidden"] = [k for k in (data.get("hidden") or []) if k in HIDEABLE_KEYS]
    out["tags_required_any"] = _coerce_int_list(data.get("tags_required_any"))
    out["tags_blocked"]      = _coerce_int_list(data.get("tags_blocked"))
    return out


def save(policy: dict[str, Any]) -> None:
    try:
        policy_path().write_text(
            json.dumps(policy, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        logger.exception("tmx_policy: save failed")


def _coerce_int_list(raw: Any) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def apply_to_filters(filters: dict[str, Any],
                     policy: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``filters`` with the policy enforced."""
    out = dict(filters)

    # Locked values always override whatever the operator chose.
    for k, v in (policy.get("locked") or {}).items():
        out[k] = v

    # Length clamps.
    floor = policy.get("length_min_s_floor")
    if floor is not None:
        cur = out.get("length_min_s") or 0
        out["length_min_s"] = max(int(floor), int(cur))
    cap = policy.get("length_max_s_cap")
    if cap is not None:
        cur = out.get("length_max_s")
        out["length_max_s"] = int(cap) if not cur else min(int(cap), int(cur))

    # Tag whitelist / blocklist.
    blocked = set(policy.get("tags_blocked") or [])
    req_any = list(policy.get("tags_required_any") or [])
    tags = [int(t) for t in (out.get("tags") or []) if int(t) not in blocked]
    if req_any and not any(t in req_any for t in tags):
        tags = sorted(set(tags) | set(req_any))
    out["tags"] = tags
    return out


def visible_to_operator(filters: dict[str, Any],
                        policy: dict[str, Any]) -> dict[str, Any]:
    """Blank out filter slots the admin has hidden from operators."""
    hidden = set(policy.get("hidden") or [])
    out = dict(filters)
    for k in hidden:
        if k == "length":
            out["length_min_s"] = None
            out["length_max_s"] = None
        elif k == "tags":
            out["tags"] = []
        elif k in _EMPTY_STR_KEYS:
            out[k] = ""
        else:
            out[k] = None
    return out
