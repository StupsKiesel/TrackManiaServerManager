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
    drive_mode = CharField(max_length=32, null=True)
    state_all = BooleanField(null=True)
    state_loading_map = BooleanField(null=True)
    state_warmup = BooleanField(null=True)
    state_pre_race = BooleanField(null=True)
    state_in_race = BooleanField(null=True)
    state_in_podium = BooleanField(null=True)
    state_post_race = BooleanField(null=True)
    group_key = CharField(max_length=64, null=True)
    group_member_enabled = BooleanField(null=True)
    group_priority = IntegerField(null=True)
    group_order = IntegerField(null=True)
    anim_dir = CharField(max_length=16, null=True)
    anim_duration_ms = IntegerField(null=True)
    anim_delay_ms = IntegerField(null=True)
    # Per-widget admin toggle: when False, the editor grays out the
    # Personal scope option and the app rejects per-player overrides.
    # NULL falls back to the widget class default (WIDGET_ALLOW_PERSONAL).
    allow_personal = BooleanField(null=True)
    # Per-widget master-admin override for the colored strip edge on
    # horizontal slides. NULL = use widget class default WIDGET_STRIP_PREFER_TOP.
    strip_prefer_top = BooleanField(null=True)
    # Master-admin kill-switch. True = widget never renders / never popups /
    # never counted in groups, even though its providing app is installed and
    # registered. NULL/False = enabled (default).
    widget_disabled = BooleanField(null=True)
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


class WidgetGroupConfig(Model):
    group_key = CharField(max_length=64, primary_key=True)
    label = CharField(max_length=64, null=True)
    description = CharField(max_length=255, null=True)
    order = IntegerField(null=True)
    anchor_x = FloatField(null=True)
    anchor_y = FloatField(null=True)
    anchor_w = FloatField(null=True)
    anchor_h = FloatField(null=True)
    mode = CharField(max_length=32, null=True)
    max_visible = IntegerField(null=True)
    runtime_prev_enabled = BooleanField(null=True)
    runtime_next_enabled = BooleanField(null=True)
    runtime_auto_enabled = BooleanField(null=True)
    runtime_pin_enabled = BooleanField(null=True)
    fixed_widget_key = CharField(max_length=64, null=True)
    updated_at = DateTimeField(null=True)

    class Meta:
        db_table = "tmsm_widget_group_config"


class WidgetThemeOverride(Model):
    theme_key = CharField(max_length=32)
    token = CharField(max_length=48)
    value = CharField(max_length=32)
    updated_at = DateTimeField(null=True)

    class Meta:
        db_table = "tmsm_widget_theme_overrides"
        primary_key = CompositeKey("theme_key", "token")
