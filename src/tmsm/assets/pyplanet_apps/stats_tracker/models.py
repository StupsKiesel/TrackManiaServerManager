"""DB models owned by the stats_tracker app.

These two tables are the single source of truth for the podium statistic
widgets. Only stats_tracker writes to them; the widgets read them (directly
through this app's query API).
"""
from __future__ import annotations

from peewee import BigIntegerField, CharField, DateTimeField, IntegerField

from pyplanet.core.db import Model


class StatsPlayer(Model):
    login = CharField(max_length=64, unique=True)
    nickname = CharField(max_length=255, null=True)
    visits = IntegerField(default=0)
    playtime_s = BigIntegerField(default=0)
    spectate_time_s = BigIntegerField(default=0)
    finishes = IntegerField(default=0)
    wins = IntegerField(default=0)
    comp_points = BigIntegerField(default=0)
    first_seen = DateTimeField(null=True)
    last_seen = DateTimeField(null=True)

    class Meta:
        db_table = "tmsm_stats_player"


class StatsMap(Model):
    uid = CharField(max_length=64, unique=True)
    name = CharField(max_length=255, null=True)
    author = CharField(max_length=255, null=True)
    plays = IntegerField(default=0)
    last_played_at = DateTimeField(null=True)

    class Meta:
        db_table = "tmsm_stats_map"
