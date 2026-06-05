"""Peewee models for TMX browser metadata cache.

Auto-discovered by PyPlanet when the app is loaded.
"""
from __future__ import annotations

from peewee import (
    BooleanField,
    CharField,
    DateTimeField,
    IntegerField,
    TextField,
)

from pyplanet.core.db import Model


class TmxMapMeta(Model):
    """Per-TMX-track metadata snapshot.

    The row is keyed by TMX track id and stores the normalized values used
    by the browser list/detail views.
    """

    track_id = IntegerField(primary_key=True)
    uid = CharField(max_length=64, null=True, index=True)
    name = CharField(max_length=255, null=True)
    author = CharField(max_length=150, null=True)
    length = CharField(max_length=32, null=True)
    difficulty = CharField(max_length=64, null=True)
    awards = IntegerField(default=0)
    style = CharField(max_length=64, null=True)
    uploaded = CharField(max_length=64, null=True)
    filename = CharField(max_length=255, null=True)

    map_type = CharField(max_length=96, null=True)
    title_pack = CharField(max_length=128, null=True)
    environment = CharField(max_length=64, null=True)
    vehicle = CharField(max_length=64, null=True)
    mood = CharField(max_length=64, null=True)
    route = CharField(max_length=64, null=True)
    tags_csv = TextField(null=True)
    comment_count = IntegerField(default=0)
    replay_count = IntegerField(default=0)
    track_value = IntegerField(default=0)
    display_cost = IntegerField(default=0)
    laps = IntegerField(default=0)
    has_thumbnail = BooleanField(default=False)
    downloadable = BooleanField(default=True)
    author_time = IntegerField(default=0)
    comments = TextField(null=True)

    # Optional link to the server-side map table (core map id) once the track
    # has been added to this server.
    server_map_id = IntegerField(null=True, index=True)

    updated_at = DateTimeField(null=True)

    class Meta:
        db_table = "tmx_map_meta"
