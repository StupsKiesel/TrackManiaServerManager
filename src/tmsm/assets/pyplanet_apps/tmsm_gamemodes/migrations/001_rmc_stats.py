"""Create RMC challenge history + per-player contribution tables."""
from playhouse.migrate import SchemaMigrator

from ..models import RmcPlayerTotals, RmcRun, RmcRunPlayer


def upgrade(migrator: SchemaMigrator):
    db = RmcRun._meta.database
    db.create_tables([RmcRun, RmcRunPlayer, RmcPlayerTotals], safe=True)


def downgrade(migrator: SchemaMigrator):
    db = RmcRun._meta.database
    db.drop_tables([RmcRunPlayer, RmcPlayerTotals, RmcRun], safe=True)
