# TMX Browser App

TMX Browser is an operator-facing in-game browser for ManiaExchange maps.

It lets operators search Trackmania Exchange / Mania-Exchange, inspect map
details, and add selected maps directly to the server playlist.

## App Identity

- Module: pyplanet.apps.tmsm.tmx_browser
- Label: tmsm_tmx_browser

## Dependencies

Required:
- core.maniaplanet

Optional integrations:
- tmsm_hub (hub tile and command entry)
- notification_engine or tmsm_status (toast notifications)

## Supported Games and Sites

Site is selected automatically from the running game:
- tmnext -> https://trackmania.exchange
- tm -> https://tm.mania.exchange
- sm -> https://sm.mania.exchange

## Main Features

- Search maps by name and granular filters.
- Open details sub-window for selected map.
- Add selected map to server (`MapManager.upload_map`).
- Optional queue-after-add behavior (`juke_after` per login state).
- Operator filter UI with admin-enforced policy constraints.
- Persistent local metadata cache table for TMX tracks.

## Operator Workflow

1. Open from hub tile `Trackmania Exchange` (operator role), or hub command `tmx`.
2. Search in the main window and page through cursor-based results.
3. Optionally open filter window and apply granular filters.
4. Select a row and use:
   - Info: open details window
   - Add: download and upload map to server
5. Use breadcrumb/back to return between list/details/filters.

## Windows and Views

The app uses four views:
- Main list: search box, filter/policy entry points, result table, add/info controls
- Details: enriched metadata view for selected map
- Filters: granular search controls (draft -> apply)
- Policy: admin-only server-wide search policy editor

View classes are in [src/tmsm/assets/pyplanet_apps/tmx_browser/views.py](src/tmsm/assets/pyplanet_apps/tmx_browser/views.py).

## Search API Layer

HTTP client helpers live in [src/tmsm/assets/pyplanet_apps/tmx_browser/tmx.py](src/tmsm/assets/pyplanet_apps/tmx_browser/tmx.py).

Primary operations:
- `search(...)` -> normalized results + pagination info
- `download(game, track_id)` -> raw GBX bytes
- `thumbnail_url(game, track_id)` -> thumbnail endpoint
- `tags(game)` -> tag list for filter/policy chips

Search uses the v2 `/api/maps` endpoint with cursor paging (`after=<MapId>`).

## Filter Model

Default filter draft fields include:
- author
- environment
- vehicle
- maptype
- mood
- difficulty
- routes
- tags
- collection
- length_min_s / length_max_s
- order1 / order2

Length fields are entered as human text and parsed at apply time.
Accepted examples:
- `90`
- `1:30`
- `5m`
- `1h 2m 30s`

## Admin Policy (Server-Wide)

Policy file:
- `<pool cwd>/tmx_policy.json`

Implementation:
- [src/tmsm/assets/pyplanet_apps/tmx_browser/policy.py](src/tmsm/assets/pyplanet_apps/tmx_browser/policy.py)

Policy capabilities:
- `locked`: force specific filter values
- `hidden`: hide selected filter rows from operators
- `length_min_s_floor` / `length_max_s_cap`: enforce length clamps
- `tags_required_any`: require at least one of these tags
- `tags_blocked`: remove blocked tag ids

Permission gate:
- `tmsm_tmx_browser:policy` (registered with min level admin)

Behavior:
- Operator input is always passed through `apply_to_filters(...)` before search.
- Operator filter UI is masked with `visible_to_operator(...)`.

## Metadata Cache Table

Model:
- [src/tmsm/assets/pyplanet_apps/tmx_browser/models.py](src/tmsm/assets/pyplanet_apps/tmx_browser/models.py)

Table name:
- `tmx_map_meta`

Purpose:
- Keep normalized TMX metadata snapshots locally.
- Optionally bind TMX track id to core server map id once added.

Lifecycle:
- Table is ensured on app start.
- Search results are persisted in batch.
- Add flow binds metadata row to uploaded server map id when possible.

## Add-to-Server Flow

When operator clicks Add:
1. Resolve selected TMX track id from current result state.
2. Download GBX bytes via TMX client.
3. Build safe map filename under `tmx/`.
4. Upload through `MapManager.upload_map(...)`.
5. Optionally queue/play according to per-login `juke_after` state.
6. Persist/bind metadata cache row.

## Notifications

The app sends toast notifications through first available signal:
- `notification_engine:notify`
- fallback: `tmsm_status:notify`

If neither exists, notifications are effectively no-op.

## Developer Notes

- Per-login state is isolated in `_state[login]`, including filter drafts.
- View visibility is tracked per sub-window in `_visible_logins`.
- Main result table supports horizontal column paging with sticky first column.
- Unknown/unmatched UI actions are logged via catch-all router.

## Minimal Integration Example

Using TMX client helper directly from another addon:

```python
from pyplanet.apps.tmsm.tmx_browser.tmx import search as tmx_search

rows = await tmx_search(
    game="tmnext",
    query="grass",
    limit=12,
    order=0,
    difficulty=2,
)

results = rows.get("results", [])
```

## Troubleshooting

No results / request issues:
- Verify outbound access to the selected MX host.
- Check server logs for `tmx_browser` exceptions.
- Confirm policy is not over-constraining filters.

Cannot open policy editor:
- Ensure player has `tmsm_tmx_browser:policy` permission.

Add fails:
- Check map download errors from TMX API.
- Check `MapManager.upload_map` exceptions.
- Verify filesystem permissions in server map directories.
