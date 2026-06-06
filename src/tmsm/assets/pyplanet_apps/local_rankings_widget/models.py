"""DB model for local_rankings_widget self-contained records."""
from __future__ import annotations

from peewee import CharField, DateTimeField, IntegerField

from pyplanet.core.db import Model


class LocalRankingRecord(Model):
    map_uid = CharField(max_length=64, index=True)
    login = CharField(max_length=64)
    nickname = CharField(max_length=255, null=True)
    score = IntegerField(default=0)
    created_at = DateTimeField(null=True)
    updated_at = DateTimeField(null=True)

    class Meta:
        db_table = "tmsm_local_rankings_record"
        indexes = ((('map_uid', 'login'), True),)
