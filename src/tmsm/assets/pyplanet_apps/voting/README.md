# Voting App

Voting is the player-facing frontend for server votes.

It provides:
- Hub window UI to start and cast votes.
- In-game widget with quick yes/no buttons.
- Chat command fallback (`/vote`, `/yes`, `/no`).
- Result execution for supported vote actions.

Vote orchestration itself is delegated to `tmsm_voting_engine`.

## App Identity

- App module: pyplanet.apps.tmsm.voting
- App label: voting

## Dependencies

From app dependencies:
- core.maniaplanet
- tmsm_ui
- tmsm_hub
- tmsm_voting_engine
- widget_engine

## Supported Vote Types

The app starts these vote actions:
- Skip current map.
- Extend timelimit by +5, +10, or +15 minutes.
- Replay current map as next map.

All starts use `threshold_yes_no` mode in voting_engine with yes/no options.

## User Interfaces

### Hub Window

- Open from hub tile or `/vote`.
- Shows current vote title, remaining time, options, and current selection.
- Provides start buttons (when no active vote and cooldown expired).
- Provides cancel button (operators only).

### Voting Widget

- Registered in widget_engine as key `voting_widget`.
- Shows active vote title, countdown, and yes/no quick buttons.
- Hidden automatically when no vote is active.
- Position/size/animation come from widget_engine resolved settings.

Default widget anchor:
- x: 50.0
- y: 86.0
- w: 58.0
- h: 12.0

## Chat Commands

Registered commands:
- `/vote [arg1] [arg2]`
- `/yes`
- `/no`

Aliases and behavior:
- `/vote` or `/vote help` opens voting window.
- Cast yes: `/yes`, `/vote yes`, `/vote y`, `/vote +`, `/vote up`.
- Cast no: `/no`, `/vote no`, `/vote n`, `/vote -`, `/vote down`.
- Extend cast shortcuts during active extend vote:
  - `/vote 5`, `/vote +5`, `/vote extend5`
  - `/vote 10`, `/vote +10`, `/vote extend10`
  - `/vote 15`, `/vote +15`, `/vote extend15`
- Start votes:
  - `/vote skip`
  - `/vote extend [5|10|15]`
  - `/vote replay` or `/vote again`
- Cancel active vote (operator only):
  - `/vote cancel`
- Generic fallback:
  - `/vote cast <value>`

## Cooldown and Permissions

- Start cooldown: 20 seconds between vote starts (`_START_COOLDOWN_S = 20`).
- Cancel action requires operator permission.
- Vote starts are denied while cooldown is active.

## Voting Engine Integration

Inbound subscriptions:
- tmsm_voting_engine:started
- tmsm_voting_engine:progress
- tmsm_voting_engine:ended
- tmsm_voting_engine:rejected

Outbound requests:
- tmsm_voting_engine:request_start
- tmsm_voting_engine:request_cast
- tmsm_voting_engine:request_cancel

The app keeps `_active_vote` from engine snapshots and re-renders both window and widget on lifecycle updates.

## Result Execution Logic

On `ended`, the app executes action-specific behavior:

- `skip_map` with winner `yes`:
  - Calls `NextMap`.

- `replay_next` with winner `yes`:
  - Tries `jukebox.insert_map(None, current_map, 0)` when jukebox app exists.
  - Falls back to `map_manager.set_next_map(current_map)`.

- `extend_time`:
  - Extends modescript timelimit via `SetModeScriptSettings`.
  - Handles value units using script metadata hints (seconds/minutes/milliseconds).

If execution fails, error is logged and vote UI still resets safely.

## Signals and Refresh Behavior

Additional listeners:
- widget_engine:request_register (re-register widget entry)
- maniaplanet:player_connect (refresh widget for joining player)

Widget visibility behavior:
- During no active vote, widget first renders hidden state for out animation, then `TemplateView.hide` is scheduled after resolved animation delay.
- During active vote, widget is displayed to online players.

## Notes

- Chat notify/broadcast methods are intentionally silent in in-game chat in current implementation (debug-log only).
- This app is frontend plus executor; canonical vote state and acceptance rules remain in `tmsm_voting_engine`.
