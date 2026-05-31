# tmsm - TrackMania Server Manager

A Linux/WSL terminal UI for running and managing:

- TrackMania 2020 and ManiaPlanet dedicated servers
- PyPlanet pools
- a portable MariaDB instance

## Stability Notice

### TUI core status: usable
The terminal manager itself (instance control, logs, diagnostics, stats, services, firewall, etc.) is working and usable.

### tmsm PyPlanet addons status: risky / in heavy construction
The addons under `src/tmsm/assets/pyplanet_apps/` are actively evolving and may change completely between updates.

Do not treat them as stable for production yet.

- Not suited for public servers yet
- Behavior, UI, and data model may change without backward compatibility
- Recommended for local/dev/staging environments only

## Highlights

| Area | What tmsm provides |
|---|---|
| Game servers | Download/install/start/stop/restart TM2020 and ManiaPlanet servers |
| PyPlanet pools | Create isolated pools, manage lifecycle, edit settings |
| MariaDB | Portable single-node MariaDB managed like a normal instance |
| DB tool | Open Harlequin for pools/MariaDB |
| Logs | Tail tmsm capture logs and native server logs |
| Config editing | In-TUI config file picker/editor |
| GNU screen integration | Attach/detach and inspect all sessions |
| System tools | Stats, UFW management, systemd service management |
| Diagnostics | Built-in checks for common install/runtime issues |

## Screenshots (Link-Only)

This README intentionally does not embed images directly. Use the links below to open screenshots.

### Hub

- [Player Hub](docs/images/hub_player.png)
- [Operator Hub](docs/images/hub_operator.png)
- [Admin Hub](docs/images/hub_admin.png)
- [Master Hub](docs/images/hub_master.png)

### Apps

- [Restart](docs/images/app_restart.png)
- [Map List](docs/images/app_maplist.png)
- [Jukebox](docs/images/app_jukebox.png)
- [Console](docs/images/app_console.png)
- [Database](docs/images/app_db.png)
- [System](docs/images/app_system.png)
- [Logs](docs/images/app_logs.png)
- [App Settings](docs/images/app_appsettings.png)
- [App Store](docs/images/app_appstore.png)
- [Game Settings](docs/images/app_gamesettings.png)
- [Trackmania Exchange](docs/images/app_trackmania_exchange.png)
- [Widgets Editor](docs/images/app_widgets_editor.png)

### UI Framework

- [Buttons](docs/images/ui_framework_buttons.png)
- [Inputs](docs/images/ui_framework_inputs.png)
- [More UI](docs/images/ui_framework_more.png)

### Misc

- [tmsm](docs/images/tmsm.png)

## Quick Start

```bash
git clone https://github.com/StupsKiesel/TrackManiaServerManager tmsm
cd tmsm
./install.sh
tmsm
```

Requirements:

- Linux or WSL
- Python 3.11+
- `git`
- `screen`

On first run, tmsm can install:

- portable MariaDB
- managed Python 3.8 (for PyPlanet venv via pyenv)

## Update / Uninstall

Update:

```bash
./install.sh
```

This pulls latest git changes (`--ff-only`) and refreshes the venv.
You can also update from the TUI (`u`) when an update is available.

Uninstall (keep `~/.tmsm` data):

```bash
./uninstall.sh
```

Full purge:

```bash
./uninstall.sh --purge
```

## Key Bindings

### Main screen

| Key | Action |
|---|---|
| `Enter` | Open actions menu |
| `n` | Create new instance |
| `R` | Refresh |
| `s` | Screen sessions |
| `t` | System stats |
| `f` | UFW screen |
| `y` | systemd services |
| `q` | Quit |

### Per-instance action menu

| Action | Description |
|---|---|
| Start / Stop / Restart | Lifecycle control |
| Attach | Attach to GNU screen session |
| View logs | Pick/tail log file |
| Edit config | Pick/edit settings file |
| Open DB tool | Harlequin/lazysql for pools + MariaDB |
| Update server | Re-download/update game server binaries |
| Open location | Open in Midnight Commander (`mc`) |
| Delete | Remove stopped instance |

## Runtime Model

Each managed process runs in a detached GNU `screen` session:

```bash
screen -r tmsm-<name>
```

- Detach: `Ctrl-A d`
- tmsm logs process output to each instance `logs/tmsm.log`

## Directory Layout

```text
~/.tmsm/
  config.toml
  tmsm-venv/
  run/
  servers/<name>/
    server/
    logs/tmsm.log
  pyplanet/
    venv/
    src/
    pools/<name>/
      settings/
        __init__.py
        base.py
        apps.py
        local.py
      logs/tmsm.log
      pool.toml
  mariadb/
  backups/
  logs/
```

## Sudo Handling

For UFW and systemd operations, tmsm prompts in-TUI and validates with `sudo -S -v`.
Credentials are cached in-process for the current session.

## License

tmsm is licensed under GNU GPL v3.0 or later.
See [LICENSE](LICENSE).

Notes:

- Addons in `src/tmsm/assets/pyplanet_apps/` are derivative works of [PyPlanet](https://github.com/PyPlanet/PyPlanet) (GPL-3.0).
- Community addons installed from catalog entries keep their upstream licenses.
