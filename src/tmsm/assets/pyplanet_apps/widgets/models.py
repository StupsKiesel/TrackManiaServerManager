"""Peewee models for widget position persistence.

Two tables:

* ``tmsm_widget_position_global`` — admin-set defaults that apply to every
  player. Seeded once from ``defaults.json`` on first boot when the table
  is empty.
* ``tmsm_widget_position_player`` — per-player overrides. Win over the
  global row when present.

Auto-discovered by PyPlanet on app load (``apps.discover()`` imports every
``models.py`` and runs migrations via ``db.initiate()``).
"""
from __future__ import annotations

from peewee import CharField, CompositeKey, DateTimeField, FloatField

from pyplanet.core.db import Model


class WidgetPositionGlobal(Model):
    widget_key = CharField(max_length=64, primary_key=True)
    x = FloatField()
    y = FloatField()
    w = FloatField()
    h = FloatField()
    updated_at = DateTimeField(null=True)

    class Meta:
        db_table = "tmsm_widget_position_global"


class WidgetPositionPlayer(Model):
    widget_key = CharField(max_length=64)
    login = CharField(max_length=64)
    x = FloatField()
    y = FloatField()
    w = FloatField()
    h = FloatField()
    updated_at = DateTimeField(null=True)

    class Meta:
        db_table = "tmsm_widget_position_player"
        primary_key = CompositeKey("widget_key", "login")
