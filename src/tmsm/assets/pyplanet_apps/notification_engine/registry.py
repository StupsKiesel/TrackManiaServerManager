"""Schema types for notification_engine notifications."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Severity(str, Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


# Severity → (theme color hex 'rgb', default icon name from tmsm.ui _ICONS).
SEVERITY_THEME: dict[Severity, tuple[str, str]] = {
    Severity.INFO:    ("15f", "info"),
    Severity.SUCCESS: ("0a4", "check"),
    Severity.WARNING: ("f80", "warning"),
    Severity.ERROR:   ("f44", "error"),
}


@dataclass
class Action:
    label: str = "OK"
    action: str = "dismiss"   # logical id; the manialink fires `act__<notif_id>__<action>`
    variant: str = "primary"  # primary | ghost | danger | success | warning


@dataclass
class Notification:
    nid: str                       # unique id (auto if not provided by caller)
    message: str
    severity: Severity = Severity.INFO
    icon: Optional[str] = None     # overrides severity default
    color: Optional[str] = None    # overrides severity default
    duration_ms: int = 4000
    actions: list[Action] = field(default_factory=list)
    source: str = ""
    # lifecycle state — "enter" while sliding in, "idle" while on screen,
    # "leave" while sliding out. The template uses this to assign frame
    # classes the ManiaScript reads.
    state: str = "enter"
    created_ms: int = 0
