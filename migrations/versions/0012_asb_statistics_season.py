"""Explicit season and official source for each current member's statistics."""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "asb_roster_members", sa.Column("statistics_season", sa.String(32), nullable=True)
    )
    op.add_column(
        "asb_roster_members", sa.Column("statistics_source_url", sa.String(2048), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("asb_roster_members", "statistics_source_url")
    op.drop_column("asb_roster_members", "statistics_season")
