# Notification Engine

Notification Engine is a shared toast system for tmsm PyPlanet apps.

It exposes a signal-based API so any addon can show transient, themed
notifications to one player, many players, or everyone.

## App Identity

- App module: pyplanet.apps.tmsm.notification_engine
- App label: notification_engine

## What It Does

- Renders stacked toast cards in the top-right of the screen.
- Supports severities: info, success, warning, error.
- Supports optional action buttons per notification.
- Auto-dismisses notifications without actions after a timeout.
- Provides explicit dismiss by id.
- Registers a movable anchor in widget_engine as widget key notifications.

## Signals

The app registers these signals in on_init:
- notification_engine:notify
- notification_engine:dismiss

### notification_engine:notify

Required payload:
- message: string

Optional payload:
- severity: info | success | warning | error (default info)
- audience: global | players | login (default global)
- login: string or list of strings (used with audience/login targeting)
- duration_ms: int (default 4000)
- button: false | true | string | list[dict]
- id: stable id for dedup/replace behavior (auto-generated if omitted)
- source: freeform source tag
- icon: icon override
- color: color override

Behavior:
- If id already exists for a player, it is replaced in-place.
- Maximum 4 visible notifications per player; oldest is dropped on overflow.
- With no actions, it auto-dismisses after duration_ms.
- With actions, it stays until dismissed.

### notification_engine:dismiss

Payload:
- id: required notification id
- login: optional specific login; if omitted, dismisses that id for all players

## Button/Action Encoding

button accepts several forms:
- false or null: no buttons
- true: one default OK button
- "Label": one button with that label
- list of dicts: custom actions

Each action dict fields:
- label: button text
- action: logical action id (default dismiss)
- variant: primary | ghost | danger | success | warning

The UI emits callback actions in this format:
- act__<notification_id>__<action>

## Severity Theme Defaults

From registry.py:
- info: color 15f, icon info
- success: color 0a4, icon check
- warning: color f80, icon warning
- error: color f44, icon error

icon or color payload values override these defaults.

## Widget Engine Integration

Notification Engine registers itself as a persistent widget anchor:
- key: notifications
- name: Notifications
- icon: info
- default x/y/w/h: 79.0 / 89.0 / 80.0 / 50.0

This lets admins reposition the toast stack from widget_engine manager.

Notes:
- It sends entry + app during register so widget_engine can trigger refresh paths.
- It listens for widget_engine:request_register and re-announces itself.

## Minimal Usage Example

From another app, send robust payloads via signal manager.

```python
sig = self.context.signals.get_signal("notification_engine:notify")
await sig.send_robust(
    {
        "message": "Map metadata saved",
        "severity": "success",
        "login": player.login,
        "audience": "login",
        "duration_ms": 2500,
    },
    raw=True,
)
```

Dismiss later by id:

```python
sig = self.context.signals.get_signal("notification_engine:dismiss")
await sig.send_robust({"id": "my-stable-id"}, raw=True)
```

## Lifecycle Summary

Per notification and player:
1. enter: card slides in
2. idle: card remains visible
3. leave: card slides out and is removed

Animation constants in app.py:
- ANIM_MS = 250
- MAX_VISIBLE = 4

## Troubleshooting

No notifications visible:
- Ensure app is loaded in pool settings/apps.py.
- Ensure payload includes message.
- Check target resolution (audience/login combination).
- Confirm widget_engine registration did not fail at startup.

Notifications vanish too quickly:
- Increase duration_ms.
- Add actions (cards with actions do not auto-dismiss).

Reusing ids replaces old cards:
- This is expected dedup behavior by design.
