"""Peewee models for the widget engine.

Slice 2 introduces a single table:

    we_widget  — one row per registered widget (base state, no scope, no
                 personal/per-player columns, no phase rows yet).

Future slices will add `we_phase_override`, `we_group`, `we_setting`.
"""
from __future__ import annotations

from peewee import (
    BooleanField,
    CharField,
    CompositeKey,
    DateTimeField,
    FloatField,
    IntegerField,
)

from pyplanet.core.db import Model


class WeWidget(Model):
    widget_key = CharField(max_length=64, primary_key=True)
    # position / size in manialink units
    x = FloatField()
    y = FloatField()
    w = FloatField()
    h = FloatField()
    # behaviour
    drive_mode = CharField(max_length=32, null=True)        # DriveMode enum value
    anim_dir = CharField(max_length=16, null=True)          # AnimDir enum value
    anim_duration_ms = IntegerField(null=True)
    anim_in_delay_ms = IntegerField(null=True)
    anim_out_delay_ms = IntegerField(null=True)
    # admin master kill-switch
    disabled = BooleanField(null=True)
    updated_at = DateTimeField(null=True)

    class Meta:
        db_table = "we_widget"


class WePhaseOverride(Model):
    """Per-phase overlay. Every override column is nullable; only non-NULL
    values overlay the corresponding `we_widget` value when the engine's
    current_phase matches `phase`."""
    widget_key = CharField(max_length=64)
    phase = CharField(max_length=16)
    x = FloatField(null=True)
    y = FloatField(null=True)
    w = FloatField(null=True)
    h = FloatField(null=True)
    drive_mode = CharField(max_length=32, null=True)
    anim_dir = CharField(max_length=16, null=True)
    anim_duration_ms = IntegerField(null=True)
    anim_in_delay_ms = IntegerField(null=True)
    anim_out_delay_ms = IntegerField(null=True)
    disabled = BooleanField(null=True)
    updated_at = DateTimeField(null=True)

    class Meta:
        db_table = "we_phase_override"
        primary_key = CompositeKey("widget_key", "phase")


class WeRemoved(Model):
    """Tombstone: presence of a row means the user uninstalled `widget_key`,
    so the engine must NOT auto-install it again when the addon re-registers
    on the next controller restart. Cleared by `install_widget` via the
    Add picker."""
    widget_key = CharField(max_length=64, primary_key=True)
    removed_at = DateTimeField(null=True)

    class Meta:
        db_table = "we_removed"


class WeSetting(Model):
    """Engine-wide key/value settings (strip placement, debug toggles, …).
    Values are stored as strings; callers parse/cast as needed."""
    key = CharField(max_length=64, primary_key=True)
    value = CharField(max_length=255, null=True)
    updated_at = DateTimeField(null=True)

    class Meta:
        db_table = "we_setting"
