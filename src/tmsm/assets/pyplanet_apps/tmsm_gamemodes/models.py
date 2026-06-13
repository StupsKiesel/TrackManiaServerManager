"""Peewee models for tmsm_gamemodes.

`gm_widget_config` — per-mode widget layout overrides. One row per
(mode_key, widget_key); when the mode is activated the orchestrator
pushes every matching row into `widget_engine`'s runtime layout overlay.

`rmc_run` / `rmc_run_player` / `rmc_player_totals` — RMC challenge history
and per-player contribution stats. Written at the end of each run; the
totals table is a rebuildable cache for the future player-stats app.
"""
from __future__ import annotations

from peewee import (
    BooleanField,
    CharField,
    CompositeKey,
    DateTimeField,
    FloatField,
    ForeignKeyField,
    IntegerField,
)

from pyplanet.core.db import Model


class GmWidgetConfig(Model):
    mode_key = CharField(max_length=64)
    widget_key = CharField(max_length=64)
    x = FloatField()
    y = FloatField()
    w = FloatField()
    h = FloatField()
    disabled = BooleanField(null=True)
    drive_mode = CharField(max_length=32, null=True)
    anim_dir = CharField(max_length=16, null=True)
    anim_duration_ms = IntegerField(null=True)
    anim_in_delay_ms = IntegerField(null=True)
    anim_out_delay_ms = IntegerField(null=True)
    updated_at = DateTimeField(null=True)

    class Meta:
        db_table = "gm_widget_config"
        primary_key = CompositeKey("mode_key", "widget_key")


class RmcRun(Model):
    """One row per finished RMC challenge (operator-stopped or time-out)."""

    started_at = DateTimeField()
    finished_at = DateTimeField()
    duration_ms = IntegerField()
    goal_medal = CharField(max_length=16)        # bronze|silver|gold|at
    secondary_medal = CharField(max_length=16)   # the medal one notch easier
    reason = CharField(max_length=64)            # "Time is over" / "Stopped by operator" / …
    maps_cleared = IntegerField()                # goal-medal clears in this run
    secondary_cleared = IntegerField()           # secondary-medal clears in this run
    players_count = IntegerField()               # unique participants

    class Meta:
        db_table = "rmc_run"


class RmcRunPlayer(Model):
    """Per-(run, player) contribution row."""

    run = ForeignKeyField(RmcRun, related_name="contributions", on_delete="CASCADE")
    login = CharField(max_length=100, index=True)
    nickname = CharField(max_length=150)
    goal_clears = IntegerField(default=0)
    secondary_clears = IntegerField(default=0)
    finishes = IntegerField(default=0)
    best_delta_ms = IntegerField(null=True)      # min(score - goal_ms) across goal clears, NULL if none
    total_clear_time_ms = IntegerField(default=0)  # sum of clearing times (goal clears only)

    class Meta:
        db_table = "rmc_run_player"
        indexes = ((("run", "login"), True),)


class RmcPlayerTotals(Model):
    """Lifetime roll-up; rebuildable from `rmc_run_player`."""

    login = CharField(max_length=100, primary_key=True)
    nickname = CharField(max_length=150)
    runs_played = IntegerField(default=0)
    goal_clears = IntegerField(default=0)
    secondary_clears = IntegerField(default=0)
    finishes = IntegerField(default=0)
    last_played_at = DateTimeField(null=True)

    class Meta:
        db_table = "rmc_player_totals"
