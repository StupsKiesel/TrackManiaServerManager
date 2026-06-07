# Voting Engine

Voting Engine is a reusable backend voting service for tmsm addons.

It owns vote lifecycle, eligibility, ballots, timeout handling, and result
calculation. It does not render UI.

## App Identity

- App module: pyplanet.apps.tmsm.voting_engine
- App label: tmsm_voting_engine

Core files:
- app.py: signal-facing wrapper and lifecycle bridge
- engine.py: in-memory vote state and vote logic

## Scope and Behavior

- One active vote at a time.
- Signal-driven API (`request_*` in, lifecycle events out).
- Supports disconnect-safe eligibility updates.
- Emits progress snapshots and final result payload.
- Supports vote modes:
  - plurality
  - threshold_yes_no

## Signals

Namespace: tmsm_voting_engine

Input/request signals:
- request_start
- request_cast
- request_cancel
- request_snapshot

Output/event signals:
- started
- progress
- ended
- rejected

All signals are sent/received with `send_robust(..., raw=True)` style payloads.

## Request Payloads

### request_start

Required:
- options: list of objects with at least `value`

Optional:
- key: vote key (default `vote`)
- title: display title (default `Vote`)
- duration_s: timeout seconds (default 25, min 1)
- mode: `plurality` or `threshold_yes_no` (default `plurality`)
- eligible: list of player logins
- include_spectators: bool (when `eligible` omitted)
- initiator: login/id string
- metadata: dict
- allow_revote: bool (default true)
- pass_ratio: float in [0..1] (default 0.6)

Reject reasons from start path:
- already_active
- invalid_options
- no_eligible_players

### request_cast

Required:
- login: player login
- value: option value

Reject reasons from cast path:
- invalid_cast_payload
- no_active_vote
- not_eligible
- invalid_option
- already_voted

### request_cancel

Optional:
- reason: cancellation reason string (default `cancelled`)

### request_snapshot

Optional:
- request_id: echoed back in progress payload for correlation

## Event Payloads

### started

- vote: full snapshot

### progress

Usually includes:
- vote: full snapshot

May also include:
- login: caster login (cast event path)
- value: cast value (cast event path)
- request_id: echoed from request_snapshot

### ended

- result: final result object

### rejected

- reason: machine-readable reason
- request: original request payload
- vote: optional current snapshot where relevant

## Snapshot Schema

Snapshot fields (engine.snapshot):
- key
- title
- mode
- options
- duration
- remaining
- eligible_count
- eligible
- ballots
- tally
- initiator
- metadata
- pass_ratio

## Result Schema

Result fields (engine.finish result):
- key
- title
- mode
- reason
- cancelled
- winner
- passed
- options
- ballots
- tally
- eligible
- eligible_count
- initiator
- metadata
- duration

Mode behavior:
- plurality:
  - highest tally wins
  - deterministic tie-break by options order
  - passed = winner is not None
- threshold_yes_no:
  - yes option from metadata.yes_value (default `yes`)
  - needed = ceil(eligible_count * pass_ratio)
  - passed when yes_votes >= needed
  - winner is yes_value if passed else no_value (default `no`)

## Eligibility and Disconnects

- If `eligible` is omitted on start, engine uses online players from player manager.
- `include_spectators=false` by default.
- On disconnect, login is removed from eligible set and ballots.
- If all remaining eligible players have voted, vote auto-finishes with reason `all_voted`.

## Minimal Integration Example

Start a vote:

```python
sig = self.context.signals.get_signal("tmsm_voting_engine:request_start")
await sig.send_robust(
    {
        "key": "skip_map",
        "title": "Skip current map?",
        "mode": "threshold_yes_no",
        "options": [
            {"value": "yes", "label": "Yes"},
            {"value": "no", "label": "No"},
        ],
        "duration_s": 20,
        "pass_ratio": 0.6,
        "metadata": {"yes_value": "yes", "no_value": "no"},
    },
    raw=True,
)
```

Cast a vote:

```python
sig = self.context.signals.get_signal("tmsm_voting_engine:request_cast")
await sig.send_robust({"login": player.login, "value": "yes"}, raw=True)
```

Subscribe to lifecycle events:

```python
self.context.signals.listen("tmsm_voting_engine:started", self._on_vote_started)
self.context.signals.listen("tmsm_voting_engine:progress", self._on_vote_progress)
self.context.signals.listen("tmsm_voting_engine:ended", self._on_vote_ended)
self.context.signals.listen("tmsm_voting_engine:rejected", self._on_vote_rejected)
```

## Notes

- The engine intentionally contains no UI logic.
- Frontend apps (for example `voting`) should render HUD/buttons and call the request signals.
