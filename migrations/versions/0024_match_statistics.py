"""Retain validated official match periods and player box scores."""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "match_statistics",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("team", sa.String(16), nullable=False),
        sa.Column("season", sa.String(32), nullable=False),
        sa.Column("discipline", sa.String(16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_id"),
        sa.CheckConstraint("team IN ('men','women')", name="match_statistics_team"),
        sa.CheckConstraint("discipline = '5x5'", name="match_statistics_discipline"),
    )
    op.create_index("ix_match_statistics_team", "match_statistics", ["team"])
    op.create_index("ix_match_statistics_season", "match_statistics", ["season"])


def downgrade() -> None:
    op.drop_table("match_statistics")
