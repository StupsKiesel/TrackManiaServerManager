"""Peewee models for widget configuration persistence.

Exactly two tables:

* ``tmsm_widget_config_global`` — admin-set defaults that apply to every
  player. Holds position (x/y/w/h) AND behaviour settings
  (hide-while-driving, slide direction, animation timings) per widget.
  Seeded once from ``defaults.json`` on first boot when empty.
* ``tmsm_widget_config_personal`` — per-player overrides. Always holds
  a position (x/y/w/h) and may also carry per-player animation
  overrides (anim_dir / anim_duration_ms / anim_delay_ms). Wins over
  the global row when present. Master-only settings
  (hide_while_driving, allow_personal) live only on the global row.

Auto-discovered by PyPlanet on app load (``apps.discover()`` imports
every ``models.py`` and runs migrations via ``db.initiate()``).
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


class WidgetConfigGlobal(Model):
    widget_key = CharField(max_length=64, primary_key=True)
    x = FloatField()
    y = FloatField()
    w = FloatField()
    h = FloatField()
    # Behaviour (server-wide). Nullable so partially-seeded rows still
    # load; the storage layer substitutes class defaults when NULL.
    hide_while_driving = BooleanField(null=True)
    anim_dir = CharField(max_length=16, null=True)
    anim_duration_ms = IntegerField(null=True)
    anim_delay_ms = IntegerField(null=True)
    # Per-widget admin toggle: when False, the editor grays out the
    # Personal scope option and the app rejects per-player overrides.
    # NULL falls back to the widget class default (WIDGET_ALLOW_PERSONAL).
    allow_personal = BooleanField(null=True)
    updated_at = DateTimeField(null=True)

    class Meta:
        db_table = "tmsm_widget_config_global"


class WidgetConfigPersonal(Model):
    widget_key = CharField(max_length=64)
    login = CharField(max_length=64)
    x = FloatField()
    y = FloatField()
    w = FloatField()
    h = FloatField()
    # Per-player animation overrides. Nullable — when NULL the global
    # row's value (or class default) applies.
    anim_dir = CharField(max_length=16, null=True)
    anim_duration_ms = IntegerField(null=True)
    anim_delay_ms = IntegerField(null=True)
    updated_at = DateTimeField(null=True)

    class Meta:
        db_table = "tmsm_widget_config_personal"
        primary_key = CompositeKey("widget_key", "login")
