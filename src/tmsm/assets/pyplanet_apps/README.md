# tmsm-shipped PyPlanet addons

This folder contains PyPlanet apps that ship as part of tmsm. They are
installed automatically when the user installs an addon by name from the
tmsm Addons screen, by symlinking the addon directory into
`<pyplanet-src>/pyplanet/apps/tmsm/<addon-name>/`.

## License

Covered by tmsm's project-wide GPL-3.0-or-later license — see the top-level
`LICENSE` file.

## Adding a new addon

1. Create a subdirectory here, e.g. `my_widget/`.
2. Put a normal PyPlanet app inside (`__init__.py` exporting an `AppConfig`).
3. Optionally add a `tmsm-addon.json` with metadata:
   ```json
   { "description": "Adds a fancy widget.", "author": "you" }
   ```
4. The addon will appear in the tmsm Addons screen on next launch.

On install, the user's pool's `settings/apps.py` will gain (commented out):

```python
# "pyplanet.apps.tmsm.my_widget",
```

The user activates it by removing the leading `#`.
