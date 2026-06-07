# tmsm UI Framework

The `ui` addon is a lightweight UI framework for PyPlanet manialinks.

It gives you:
- Shared Jinja widget macros (`tmsm_ui/widgets.xml`)
- Base view classes with signal-style action binding
- Audience targeting helpers
- Permission helpers with impersonation-aware effective levels
- Design tokens (colors, sizes, z-index lanes)

## App Identity

- Module: `pyplanet.apps.tmsm.ui`
- Label: `tmsm_ui`

The app itself is intentionally small. Its primary job is to expose template
macros and helper modules used by other addons.

## Quick Start

### 1. Add dependency in your app

```python
class MyApp(AppConfig):
    app_dependencies = ["core.maniaplanet", "tmsm_ui"]
```

### 2. Build a view

```python
from pyplanet.apps.tmsm.ui.audience import Audience
from pyplanet.apps.tmsm.ui.views import BaseView


class MyPanelView(BaseView):
    template_name = "my_app/panel.xml"
    audience = Audience.everyone()
    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    async def get_context_data(self):
        ctx = await super().get_context_data() or {}
        ctx.update({
            "title": "My Panel",
            "enabled": True,
            "name": "Server A",
        })
        return ctx
```

### 3. Bind actions

```python
self.view = MyPanelView(self)
self.view.connect("save", self._on_save)
self.view.connect("toggle_enabled", self._on_toggle_enabled)

async def _on_save(self, player):
    ...

async def _on_toggle_enabled(self, player):
    ...
```

### 4. Show/hide/refresh

```python
await self.view.show()      # display according to audience
await self.view.refresh()   # redraw only visible logins
await self.view.hide()      # hide/destroy
```

## Template Macros

Import macros with context (required):

```jinja
{% import 'tmsm_ui/widgets.xml' as ui with context %}
```

If you omit `with context`, action routing can silently break because view id
and surrounding variables are not in macro scope.

Common macros:
- `ui.window(...)`
- `ui.push_button(...)`
- `ui.tool_button(...)`
- `ui.check_box(...)`
- `ui.radio_group(...)`
- `ui.line_edit(...)`
- `ui.search_input(...)`
- `ui.banner(...)`
- `ui.breadcrumbs(...)`

### Minimal template example

```jinja
{% import 'tmsm_ui/widgets.xml' as ui with context %}

{% block content %}
{{ ui.window(
    title=title,
    width=90,
    height=45,
    close_action='_close'
) }}

{{ ui.label('Name', x=8, y=-14) }}
{{ ui.line_edit('name', value=name, x=8, y=-20, width=45) }}

{{ ui.check_box('toggle_enabled', label='Enabled', checked=enabled, x=8, y=-28) }}
{{ ui.push_button('save', 'Save', x=8, y=-36, variant='success') }}
{% endblock %}
```

### Action naming model

For standard controls, macros emit action ids as:
- `<view_id>__<name>`

You subscribe using only the short `name` with `view.connect(name, handler)`.

Some controls intentionally emit structured actions, e.g.:
- radio: `<view_id>__<group>__set__<value>`

Handle those in `handle_catch_all(...)` when needed.

## Form Values and FormView

`FormView` is currently an alias of `BaseView`, but it documents intent for
entry-heavy UIs.

`line_edit` values are posted under keys like:
- `entry_<view_id>__<field_name>`

If you use low-level subscribe handlers, read values from that map. If you use
`connect(...)`, the default adapter can pass `values` when your handler accepts
it.

Example:

```python
class MyForm(FormView):
    ...

self.view.connect("submit", self._on_submit)

async def _on_submit(self, player, values=None):
    values = values or {}
    key = f"entry_{self.view.id}__name"
    name = values.get(key, "").strip()
```

## Audience Targeting

Use `Audience` on the view class:

```python
from pyplanet.apps.tmsm.ui.audience import Audience

class AdminView(BaseView):
    audience = Audience.admins()

class OpsView(BaseView):
    audience = Audience.operators()

class Team0View(BaseView):
    audience = Audience.matching(lambda p: getattr(getattr(p, 'flow', None), 'team_id', -1) == 0)
```

Built-ins:
- `everyone()`
- `operators()`
- `admins()`
- `master_admins()`
- `minimum_level(level)`
- `matching(predicate)`

The level checks are impersonation-aware via `ui.perms.effective_level(...)`.

## Permission Helpers

Use these helpers instead of raw `player.level`:

```python
from pyplanet.apps.tmsm.ui import perms

if perms.is_operator(player):
    ...

level = perms.effective_level(player)
label = perms.level_label(player)
```

Available helpers include:
- `effective_level(...)`
- `get_real_level(...)`
- `get_override(login)`
- `is_player/is_operator/is_admin/is_master`
- `set_override(login, level)`
- `clear_override(login)`
- `reset_all()`
- `subscribe_changed(callback)`

Subscriber callback signature:

```python
async def on_perms_changed(login: str, new_level: int, real_level: int) -> None:
    ...
```

## Design Tokens

Import tokens from `pyplanet.apps.tmsm.ui.tokens`:

```python
from pyplanet.apps.tmsm.ui.tokens import Z, theme

z_modal = Z.MODAL
btn_primary = theme.color.primary
font_md = theme.size.font_md
```

Z lanes:
- `BACKGROUND` (100)
- `CONTENT` (200)
- `OVERLAY` (400)
- `MODAL` (500)
- `TOAST` (900)

## BaseView Behavior Notes

- Keeps per-login visibility bookkeeping, so refresh updates only players who
  currently have the view open.
- Auto re-displays for matching players on connect, but only if the view is
  currently visible.
- Wires framework close signal (`_close`) by default.
- Supports default hub breadcrumb handler (`_crumb__hub`) that hides current
  view and emits `tmsm_hub:show`.

## Practical End-to-End Example

```python
# app.py
from pyplanet.apps.config import AppConfig
from .views import SettingsView


class SettingsApp(AppConfig):
    name = "pyplanet.apps.tmsm.settings_demo"
    label = "settings_demo"
    app_dependencies = ["core.maniaplanet", "tmsm_ui"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.enabled = True
        self.server_name = "My Server"
        self.view = None

    async def on_start(self):
        self.view = SettingsView(self)
        self.view.connect("toggle_enabled", self._toggle)
        self.view.connect("save", self._save)

    async def open_for(self, player):
        self.view._visible = True
        self.view._visible_logins.add(player.login)
        await self.view.display(player_logins=[player.login])

    async def _toggle(self, player):
        self.enabled = not self.enabled
        await self.view.refresh()

    async def _save(self, player, values=None):
        values = values or {}
        key = f"entry_{self.view.id}__server_name"
        self.server_name = values.get(key, self.server_name).strip() or self.server_name
        await self.view.refresh()
```

```python
# views.py
from pyplanet.apps.tmsm.ui.audience import Audience
from pyplanet.apps.tmsm.ui.views import FormView


class SettingsView(FormView):
    template_name = "settings_demo/window.xml"
    audience = Audience.operators()
    breadcrumbs = [{"key": "hub", "label": "Hub"}]

    async def get_context_data(self):
        ctx = await super().get_context_data() or {}
        ctx.update({
            "title": "Demo Settings",
            "enabled": self.app.enabled,
            "server_name": self.app.server_name,
        })
        return ctx
```

```jinja
{# templates/settings_demo/window.xml #}
{% import 'tmsm_ui/widgets.xml' as ui with context %}

{{ ui.window(title=title, width=90, height=45, close_action='_close') }}
{{ ui.check_box('toggle_enabled', label='Enabled', checked=enabled, x=8, y=-14) }}
{{ ui.line_edit('server_name', value=server_name, x=8, y=-24, width=50) }}
{{ ui.push_button('save', 'Save', x=8, y=-34, variant='success') }}
```

## Troubleshooting

Buttons do nothing:
- Ensure template imports macros with context.
- Ensure action name in template matches `connect(...)` name.
- Ensure each button `name` is unique inside the same view.

View keeps popping up unexpectedly:
- Use `BaseView` visibility model (`show/hide/refresh`) and avoid unconditional
  `display(...)` calls for all players.

Permission-gated UI wrong during impersonation:
- Use `ui.perms` helpers and `Audience.minimum_level(...)` instead of raw
  player level checks.
