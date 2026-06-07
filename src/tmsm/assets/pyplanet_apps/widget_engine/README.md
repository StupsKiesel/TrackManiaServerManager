# Widget Engine

Widget Engine is the host app for all tmsm widgets.

It provides:
- Widget registration and discovery.
- Persistent storage for position, size, behavior, and phase visibility.
- Runtime refresh and per-owner runtime layout overrides.
- Optional GBX manialink replacement support.
- Editor/manager UI integration and command entrypoints.

## App Identity

- App module: pyplanet.apps.tmsm.widget_engine
- App label: widget_engine

Main implementation files:
- app.py: host lifecycle, signal contract, command integration, registration bridge.
- engine.py: resolve/compose runtime widget state.
- storage.py: DB-backed persistence and settings.
- registry.py: shared enums and dataclasses for widget declarations.
- widget_base.py: base class for widget addons.

## Registration Flow

A widget addon typically subclasses WidgetAppBase and sends one WidgetEntry through the register signal. The host tracks two sets:
- available: any widget that announced itself.
- installed: widgets with a storage row and not tombstoned.

Install behavior:
- First registration with no row and no tombstone auto-installs.
- If a tombstone exists, the widget stays available-only until explicitly re-installed.

## Signals

Widget Engine registers both modern and legacy namespaces for compatibility.

Modern namespace:
- widget_engine:register
- widget_engine:request_register
- widget_engine:refresh

Legacy namespace still accepted:
- tmsm_widgets:register
- tmsm_widgets:request_register
- tmsm_widgets:refresh
- tmsm_widgets:position_changed
- tmsm_widgets:runtime_override_set
- tmsm_widgets:runtime_override_clear
- tmsm_widgets:runtime_override_clear_owner
- tmsm_widgets:runtime_layout_apply
- tmsm_widgets:runtime_layout_clear_owner
- tmsm_widgets:runtime_layout_clear_all

### Payloads

Register:
- entry: WidgetEntry
- app: widget AppConfig instance (optional, used for rendering callbacks)

Refresh:
- key: optional widget key. If omitted, refresh all widgets.

Runtime override set (transient per-login or owner-wide layout):
- owner: required owner id string.
- widget_key or key: required widget key.
- login: optional. If present, applies transient override for that login.
- Optional patch fields: x, y, w, h, drive_mode, anim_dir, anim_duration_ms, anim_delay_ms, enabled.
- Optional pos object: {x, y, w, h}.

Runtime override clear:
- owner: required.
- widget_key or key: required.
- login: optional. If present, clears transient override for this login only.

Runtime override clear owner:
- owner: required. Clears all transients and owner runtime layout.

Runtime layout apply:
- owner: required.
- widgets or entries: list of objects, each with widget_key or key plus patch fields.

Runtime layout clear owner:
- owner: required.

Runtime layout clear all:
- no payload required.

## Building a Widget Addon

Use WidgetAppBase from widget_engine.widget_base.

Minimum requirements:
- Define WIDGET_KEY and WIDGET_NAME.
- Set WIDGET_TEMPLATE to a valid template path.
- Keep app name and module path lowercase for tmsm addons.

Minimal skeleton:

```python
from pyplanet.apps.tmsm.widget_engine.widget_base import WidgetAppBase


class MyWidget(WidgetAppBase):
    name = "pyplanet.apps.tmsm.my_widget"
    label = "my_widget"

    WIDGET_KEY = "my_widget"
    WIDGET_NAME = "My Widget"
    WIDGET_TEMPLATE = "my_widget/widget.xml"
```

Optional hooks:
- async get_widget_data(login): return extra per-player context.
- Override class attributes for size, animation, hide rules, and phase visibility.

## GBX Manialink Replacement

A widget can claim and re-own an existing manialink id using GbxReplacement in its WidgetEntry.

Notes:
- The addon should implement build_replacement_xml(login).
- The engine rewrites/wraps XML to the claimed id and re-pushes on startup, phase change, and player connect.
- UI module hiding can be configured for replacement widgets when default title-pack UI overlaps.

## Commands and Manager

When tmsm_ui is available, Widget Engine exposes:
- //widget command for admin operations.
- Manager and edit overlay views.
- Optional Hub tile registration if tmsm_hub is loaded.

Without tmsm_ui, engine stays in render-only mode.

## Troubleshooting

Widget does not appear:
- Verify addon module path and app name are lowercase in pyplanet.apps.tmsm.*.
- Confirm widget sent register signal and appears as available.
- Check if tombstoned in storage (available but not installed).
- Force refresh with widget_engine:refresh.

Import errors mentioning old namespace:
- Use imports from pyplanet.apps.tmsm.widget_engine.*
- Do not import from pyplanet.apps.tmsm.widgets.* (removed namespace).
