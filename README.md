# tmsm — TrackMania Server Manager

A terminal UI (TUI) for managing TrackMania 2020 and ManiaPlanet dedicated
servers, PyPlanet controllers, and a portable MariaDB instance — all on Linux.

---

## Features

| Area | What tmsm does |
|---|---|
| **Game servers** | Download, install, start/stop/restart TM2020 & ManiaPlanet servers |
| **PyPlanet pools** | Create isolated pools (settings split into `base.py` / `apps.py` / `local.py`), start/stop, edit config |
| **MariaDB** | Portable single-node MariaDB managed as just another instance |
| **DB browser** | Launch `lazysql` against any pool or the MariaDB service |
| **System stats** | Live CPU (per-core + sparkline), memory, disk, network (sparklines), GPU, temperatures |
| **UFW firewall** | View rules, add rules via structured form, delete rules, enable/disable with SSH-safety toggle |
| **systemd services** | Browse all services, start / stop / restart / enable / disable, view journal |
| **File manager** | Open any instance directory in Midnight Commander (`modarin256` skin) |
| **Log viewer** | Tail tmsm capture logs and native server logs |
| **Config editor** | Edit settings files in-TUI |
| **Process model** | Every process runs in a named GNU `screen` session; attach/detach from inside the TUI |

---

## Install

```bash
git clone <repo> tmsm
cd tmsm
./install.sh
tmsm
```

**Requirements:** Linux / WSL, Python ≥ 3.11, `git`, `screen`.  
The first run offers to download a portable MariaDB and a managed Python 3.8
(via pyenv) for the PyPlanet venv.

**Update:**
```bash
git pull && ./install.sh
```

**Uninstall** (data under `~/.tmsm/` is kept):
```bash
./uninstall.sh
# wipe everything including data:
./uninstall.sh --purge
```

---

## Layout

```
~/.tmsm/
  config.toml
  tmsm-venv/                 # tmsm's own virtualenv
  run/                       # PID files
  servers/<name>/            # one folder per game-server instance
    server/                  # dedicated server binaries
    logs/tmsm.log
  pyplanet/
    venv/                    # Python 3.8 venv (managed by pyenv)
    src/                     # PyPlanet source
    pools/<name>/
      settings/
        __init__.py          # imports base + apps + optional local
        base.py              # DB connection, logging, MAP_MATCHSETTINGS
        apps.py              # APPS list (contrib plugins)
        local.py             # optional local overrides (not created by default)
      logs/tmsm.log
      pool.toml
  mariadb/                   # portable MariaDB data dir
  backups/
  logs/
```

---

## Keys

### Main screen

| Key | Action |
|---|---|
| `↑` / `↓` | Move selection |
| `Enter` | Open action menu |
| `n` | Create new instance |
| `R` | Refresh instance list |
| `s` | GNU screen sessions screen |
| `t` | System stats screen |
| `f` | UFW firewall screen |
| `y` | systemd services screen |
| `q` | Quit |

### Action menu (per instance)

| Item | Description |
|---|---|
| ▶ Start | Start the instance |
| ■ Stop | Stop the instance |
| ↻ Restart | Restart the instance |
| ⇆ Attach | Attach to the GNU screen session |
| ≡ View logs | Pick and tail a log file |
| ✎ Edit config | Pick and edit a settings file |
| ⛁ Open DB tool | Launch `lazysql` (pools and MariaDB only) |
| ⤓ Update server | Re-download and update game server binaries |
| 📂 Open location | Open instance directory in `mc` (Midnight Commander) |
| ✗ Delete | Delete the instance (must be stopped) |

### Stats screen (`t`)

Two-column layout: CPU panel on the left (per-core bars + sparkline history),
Memory / Disk / Network / GPU / Temperatures on the right.  
Auto-refreshes every 2 s. Press `R` to force refresh, `Esc`/`q` to go back.

### UFW screen (`f`)

| Key | Action |
|---|---|
| `a` | Add rule — structured form (action / direction / port / protocol / from / to) with live preview |
| `d` / `Del` | Delete selected rule (with confirmation) |
| `t` | Toggle UFW on/off — shows a safety dialog with optional "allow SSH first" switch |
| `R` | Refresh |
| `Esc` / `q` | Back |

If UFW is not installed the screen shows an install hint instead.  
sudo credentials are prompted in-TUI (password never reaches the raw terminal)
and cached for the session.

### systemd screen (`y`)

| Key | Action |
|---|---|
| `s` | Start service |
| `S` | Stop service (with confirmation) |
| `r` | Restart service |
| `e` | Enable service |
| `d` | Disable service (with confirmation) |
| `j` | View journal (`journalctl -n 200`) |
| `/` | Filter by name or description |
| `R` | Refresh |
| `Esc` / `q` | Back |

### Screen sessions screen (`s`)

Lists all GNU `screen` sessions. `Enter`/`a` to attach, `k`/`Del` to kill.

---

## Process model

Every server, pool, and service runs in a detached GNU `screen` session named
`tmsm-<instance>`. From a shell you can attach with:

```bash
screen -r tmsm-<name>
```

Detach with `Ctrl-A d`. The TUI's **Attach** action does the same by suspending
the TUI, running `screen -r`, and resuming when you detach.

Output is also captured to each instance's `logs/tmsm.log`.

---

## sudo / authentication

Screens that need elevated privileges (UFW, systemd service management) prompt
for a sudo password inside the TUI using a dedicated authentication dialog.
The password is verified immediately via `sudo -S -v` and cached in-process for
the session so you only need to enter it once.
