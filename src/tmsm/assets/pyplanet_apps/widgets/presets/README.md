# Widget config presets

Each `*.csv` file in this folder is a **full snapshot** of
`WidgetConfigGlobal`. One row per widget, every column required.

Header comment lines carry metadata:

```
# preset_key: arcade
# label: Arcade layout
# description: tight HUD for fast modes
```

Followed by a CSV header and one row per widget. Columns mirror the
DB table 1:1.

Presets are eagerly loaded on PyPlanet startup. The widgets editor's
Presets tab lists them and lets a master admin apply one to the global
config (persistent) or as a runtime override (cleared on restart).

To create a preset from the current live config, use **Save preset** on
the Widgets tab — a file `snapshot_YYYYmmdd_HHMMSS.csv` is written.
Rename / edit the file on disk to tidy it up.
