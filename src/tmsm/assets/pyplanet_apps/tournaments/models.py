"""DB models for the tournaments app (v1: single-stage solo cup)."""
from __future__ import annotations

from peewee import (
    BooleanField,
    CharField,
    DateTimeField,
    IntegerField,
    SQL,
)

from pyplanet.core.db import Model


class Tournament(Model):
    name = CharField(max_length=128)
    # draft | registration | running | finished
    status = CharField(max_length=24, default="draft",
                       constraints=[SQL("DEFAULT 'draft'")])
    # Nadeo/script path relative to Scripts/Modes/ used for each match.
    match_mode = CharField(max_length=255, null=True)
    match_mode_label = CharField(max_length=64, null=True)
    lock_to_participants = BooleanField(default=True, constraints=[SQL("DEFAULT 1")])
    self_signup = BooleanField(default=True, constraints=[SQL("DEFAULT 1")])
    current_map_index = IntegerField(default=0, constraints=[SQL("DEFAULT 0")])
    # When set, the controller auto-advances through the map pool and
    # auto-finishes once the pool is exhausted.
    auto_advance = BooleanField(default=True, constraints=[SQL("DEFAULT 1")])
    # When > 0, the tournament auto-starts once this many participants have
    # joined (registration only). 0 = manual start only.
    auto_start_threshold = IntegerField(default=0, constraints=[SQL("DEFAULT 0")])
    winner_login = CharField(max_length=64, null=True)
    created_at = DateTimeField(null=True)

    class Meta:
        db_table = "tmsm_tournament"


class TournamentParticipant(Model):
    tournament_id = IntegerField(index=True)
    login = CharField(max_length=64)
    nickname = CharField(max_length=255, null=True)
    seed = IntegerField(default=0, constraints=[SQL("DEFAULT 0")])
    points = IntegerField(default=0, constraints=[SQL("DEFAULT 0")])
    joined_at = DateTimeField(null=True)

    class Meta:
        db_table = "tmsm_tournament_participant"
        indexes = ((("tournament_id", "login"), True),)


class TournamentMap(Model):
    tournament_id = IntegerField(index=True)
    order_index = IntegerField(default=0, constraints=[SQL("DEFAULT 0")])
    map_uid = CharField(max_length=64)
    name = CharField(max_length=255, null=True)
    # pending | played | skipped
    status = CharField(max_length=24, default="pending",
                       constraints=[SQL("DEFAULT 'pending'")])
    played_at = DateTimeField(null=True)

    class Meta:
        db_table = "tmsm_tournament_map"


class TournamentResult(Model):
    tournament_id = IntegerField(index=True)
    map_id = IntegerField(index=True)
    login = CharField(max_length=64)
    nickname = CharField(max_length=255, null=True)
    position = IntegerField(default=0, constraints=[SQL("DEFAULT 0")])
    points = IntegerField(default=0, constraints=[SQL("DEFAULT 0")])
    score = IntegerField(default=0, constraints=[SQL("DEFAULT 0")])
    created_at = DateTimeField(null=True)

    class Meta:
        db_table = "tmsm_tournament_result"
        indexes = ((("map_id", "login"), True),)
