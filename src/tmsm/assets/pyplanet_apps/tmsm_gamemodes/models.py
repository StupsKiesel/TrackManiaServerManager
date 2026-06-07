"""Peewee models for tmsm_gamemodes.

`gm_widget_config` — per-mode widget layout overrides. One row per
(mode_key, widget_key); when the mode is activated the orchestrator
pushes every matching row into `widget_engine`'s runtime layout overlay.
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
