# Restart App

Restart is a master-admin tool for controlled restarts.

It supports:
- Manual restart of PyPlanet.
- Manual restart of the dedicated server (with coordinated PyPlanet restart).
- Scheduled restarts (weekly or monthly) with optional pre-notifications.
- Optional file watcher auto-reload for development.

## App Identity

- Module: pyplanet.apps.tmsm.restart
- Label: tmsm_restart

## Dependencies

- core.maniaplanet
- tmsm_ui
- tmsm_hub

Optional integration:
- notification_engine (used for toast notifications when available)

## Access Control

This app is master-admin only.

Checks use:
- `player.level >= Player.LEVEL_MASTER`

## UI Overview

Hub tile opens the main restart window.

Main window sections:
- Manual:
  - Restart PyPlanet
  - Restart Dedicated
- Developer:
  - Toggle file-change auto-reload
  - Configure watch directory path
- Restart schedules:
  - List existing schedule rows
  - Enable/disable each row
  - Delete row
  - Open add-schedule form

Add-schedule form:
- Time (`HH:MM`, 24h)
- Target (`pyplanet` or `dedicated`)
- Frequency (`weekly` or `monthly`)
- Weekly day mask or monthly day-of-month
- Notification rows (lead minutes + optional custom text)

## Manual Restart Behavior

### Restart PyPlanet

Flow:
1. Broadcast restart message.
2. Spawn detached bash respawner that:
   - recreates screen session `tmsm-<pool>`
   - launches `pyplanet start --settings=settings`
3. Current process exits via `os._exit(1)`.

### Restart Dedicated

Flow:
1. Resolve target server from `pool.toml` (`target_server`).
2. Read dedicated metadata from `~/.tmsm/servers/<server>/instance.toml`.
3. Spawn detached bash respawner that:
   - quits screen `tmsm-<server>`
   - relaunches dedicated binary with reconstructed argv
4. Spawn PyPlanet respawner with XML-RPC port wait.
5. Current process exits.

Important: dedicated restart always triggers a coordinated PyPlanet restart so
PyPlanet reconnects to a fresh server process, not a stale ghost.

## Scheduled Restart Behavior

Scheduler loop:
- Tick interval: 20s
- Trigger key dedupe per minute and schedule id

Frequencies:
- Weekly: bitmask over Mon..Sun
- Monthly: specific day of month (1..31)

When a schedule fires:
- Broadcast immediate restart warning.
- If target is `dedicated`, restart dedicated first.
- Always restart PyPlanet after (optionally waiting for dedicated XML-RPC).

## Pre-Notifications

Each schedule can include multiple pre-fire notifications.

Notification row schema:
- `min`: lead time in minutes (>0, <=1440)
- `text`: optional custom message

Behavior:
- Rows are normalized, de-duplicated by minute, sorted descending.
- At matching lead time, app emits warning once per minute/schedule/lead key.
- If `text` is empty, default message is used.

## File Watcher Auto-Reload (Developer)

When enabled:
- Poll interval: 2s
- Watches suffixes: `.py`, `.xml`, `.json`
- Walks directory recursively with symlink-following and loop guard.
- On first detected change:
  - notify ops
  - spawn PyPlanet respawner
  - exit current process

Default watch directory:
- `~/.tmsm/pyplanet/src/pyplanet/apps/tmsm`

## Persistence

State file location:
- `<pool cwd>/restart.state.json`

Stored fields:
- `schedules`: list of schedule dicts
- `watch_active`: bool
- `watch_dir`: string

Schedule dict fields (effective):
- `id`: short id
- `time`: `HH:MM`
- `target`: `pyplanet` or `dedicated`
- `freq`: `weekly` or `monthly`
- `enabled`: bool
- `last_run`: timestamp string
- `notifications`: list of `{min, text}`
- `days`: weekly bitmask (for weekly)
- `dom`: day-of-month 1..31 (for monthly)

## Runtime Paths and Sessions

Environment/path assumptions:
- `TMSM_HOME` defaults to `~/.tmsm`
- `SCREENDIR` defaults to `<TMSM_HOME>/screen`
- server roots under `<TMSM_HOME>/servers`

Screen naming:
- pool session: `tmsm-<pool_name>`
- dedicated session: `tmsm-<server_name>`

Respawner traces:
- `<pool>/logs/restart_pp.log`
- `<server>/logs/restart_dedi.log`

## Operational Notes

- Dedicated-only restart is intentionally not offered as isolated runtime state;
  app performs coordinated restart for reliability.
- Notification_engine is optional; chat fallback is used when unavailable.
- For monthly schedules, days beyond month length are naturally skipped.
- Use this app on Linux/screen-based deployments as intended by tmsm runtime.

## Troubleshooting

Restart button does nothing:
- Verify user is master admin.
- Check pool/server logs for respawner errors.
- Verify `screen` and `bash` are installed and callable.

Dedicated restart fails:
- Confirm `pool.toml` has valid `target_server`.
- Confirm `~/.tmsm/servers/<target_server>/instance.toml` exists.
- Confirm dedicated binary exists in server root.

Watcher not detecting changes:
- Confirm watch directory path is correct and saved.
- Ensure file extension is one of `.py`, `.xml`, `.json`.
- Check symlinked project tree permissions.
