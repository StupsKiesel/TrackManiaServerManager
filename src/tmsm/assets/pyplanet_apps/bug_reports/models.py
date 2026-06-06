"""Peewee model for `tmsm_bug_reports`.

Schema is also created/migrated via raw SQL in `storage.py` to play
nicely with peewee_async's Proxy database (same pattern as
`widget_engine`).
"""
from __future__ import annotations

from peewee import (
    BooleanField,
    CharField,
    DateTimeField,
    IntegerField,
    PrimaryKeyField,
    TextField,
)

from pyplanet.core.db import Model


class BugReport(Model):
    id = PrimaryKeyField()
    login = CharField(max_length=64)
    nickname = CharField(max_length=255, null=True)
    map_uid = CharField(max_length=64, null=True)
    map_name = CharField(max_length=255, null=True)
    mode_script = CharField(max_length=128, null=True)
    subject = CharField(max_length=200)
    details = TextField(null=True)
    status = CharField(max_length=16)  # open | fixed | wontfix
    # extended metadata captured at submit time
    auth_level = CharField(max_length=16, null=True)        # player|operator|admin|masteradmin
    game_phase = CharField(max_length=32, null=True)        # pre_race|in_race|in_podium|...
    about_widgets = BooleanField(null=True)
    about_ui = BooleanField(null=True)
    input_device = CharField(max_length=16, null=True)      # keyboard|controller|other
    game_version = CharField(max_length=255, null=True)     # dedicated server version (Name/Version/Build)
    client_version = CharField(max_length=255, null=True)   # reporter's client game version
    uses_openplanet = BooleanField(null=True)               # reporter self-declared
    pyplanet_uptime_s = IntegerField(null=True)
    dedicated_uptime_s = IntegerField(null=True)
    delivered_at = DateTimeField(null=True)  # discord delivery timestamp (NULL = pending)
    created_at = DateTimeField(null=True)
    updated_at = DateTimeField(null=True)

    class Meta:
        db_table = "tmsm_bug_reports"
