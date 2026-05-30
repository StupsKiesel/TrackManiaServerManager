"""tmsm status — toast notification framework.

Any app can fire a transient status message via::

    await self.instance.signals.get_signal('tmsm_status:notify').send_robust({
        'message': 'Position saved',
        'severity': 'success',          # info | success | warning | error
        'audience': 'global',           # global | admins | ops, OR
        'login': 'somelogin',           # single login or list of logins
        'duration_ms': 3000,            # ignored when button is truthy
        'button': False,                # True / 'Label' / list of {label, action, variant}
        'id': 'widgets:save:tower',     # optional; replaces existing same-id
        'source': 'widgets',            # optional source app tag
        'icon': None,                   # optional override (else from severity)
        'color': None,                  # optional override (else from severity)
    })

The signal returns nothing; fire-and-forget.
"""
from .app import StatusApp  # noqa: F401
from .registry import Severity, Notification, Action  # noqa: F401
