"""Team-only historical results and explicit source coverage.

Revision ID: 0010
Revises: 0009
"""

from alembic import op

from predatory_beavers.modules.results.models import ArchiveCoverage, ArchiveResult

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    ArchiveResult.__table__.create(bind)
    ArchiveCoverage.__table__.create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    ArchiveCoverage.__table__.drop(bind)
    ArchiveResult.__table__.drop(bind)
