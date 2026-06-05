"""notification_engine — toast notification framework (widget_engine-backed).

Any app can fire a transient status message via::

    await self.instance.signals.get_signal('notification_engine:notify').send_robust({
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

The notification stack anchor (position + width) is configurable through
the widget_engine UI as the ``notifications`` widget.
"""
from .app import NotificationEngineApp  # noqa: F401
