"""Peewee cache for TMX map lookups, keyed by map UID."""
from peewee import CharField, DateTimeField, IntegerField, TextField

from pyplanet.core.db import Model


class TmxMapCache(Model):
    """One row per map UID we've looked up on TMX."""

    map_uid     = CharField(max_length=64, unique=True, index=True)
    tmx_id      = IntegerField(null=True)
    name        = CharField(max_length=200, null=True)
    author      = CharField(max_length=200, null=True)
    difficulty  = CharField(max_length=32, null=True)   # Beginner / Intermediate / ...
    length_name = CharField(max_length=32, null=True)   # "1m 30s" etc.
    length_ms   = IntegerField(null=True)               # author time, milliseconds
    style       = CharField(max_length=64, null=True)   # primary style tag
    tags        = TextField(null=True)                  # comma-separated tag list
    mood        = CharField(max_length=32, null=True)
    mod_name    = CharField(max_length=200, null=True)  # texture mod name
    mod_url     = TextField(null=True)                  # texture mod download url
    not_on_tmx  = IntegerField(default=0)               # 1 = TMX has no entry; cache the negative

    # Full TMX response as JSON — keeps every field, so new UI features can
    # surface more values without a schema migration.
    raw_json    = TextField(null=True)

    fetched_at  = DateTimeField()

    class Meta:
        db_table = "tmsm_tmx_map_cache"
