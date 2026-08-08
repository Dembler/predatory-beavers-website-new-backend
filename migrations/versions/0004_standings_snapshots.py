"""Add standings snapshots.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "standings_snapshots",
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("competition_id", sa.Uuid(), nullable=False),
        sa.Column("rows", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_reference", sa.String(128), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["competition_id"],
            ["competitions.id"],
            name=op.f("fk_standings_snapshots_competition_id_competitions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name=op.f("fk_standings_snapshots_team_id_teams"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_standings_snapshots")),
    )
    op.create_index(
        op.f("ix_standings_snapshots_competition_id"),
        "standings_snapshots",
        ["competition_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_standings_snapshots_fetched_at"),
        "standings_snapshots",
        ["fetched_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_standings_snapshots_is_current"),
        "standings_snapshots",
        ["is_current"],
        unique=False,
    )
    op.create_index(
        op.f("ix_standings_snapshots_is_deleted"),
        "standings_snapshots",
        ["is_deleted"],
        unique=False,
    )
    op.create_index(
        op.f("ix_standings_snapshots_source"),
        "standings_snapshots",
        ["source"],
        unique=False,
    )
    op.create_index(
        op.f("ix_standings_snapshots_team_id"),
        "standings_snapshots",
        ["team_id"],
        unique=False,
    )
    op.create_index(
        "ix_standings_team_fetched_at",
        "standings_snapshots",
        ["team_id", "fetched_at"],
        unique=False,
    )
    op.create_index(
        "uq_standings_current_team_competition",
        "standings_snapshots",
        ["team_id", "competition_id"],
        unique=True,
        sqlite_where=sa.text("is_current = 1 AND is_deleted = 0"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_standings_current_team_competition",
        table_name="standings_snapshots",
    )
    op.drop_index("ix_standings_team_fetched_at", table_name="standings_snapshots")
    op.drop_index(
        op.f("ix_standings_snapshots_team_id"),
        table_name="standings_snapshots",
    )
    op.drop_index(
        op.f("ix_standings_snapshots_source"),
        table_name="standings_snapshots",
    )
    op.drop_index(
        op.f("ix_standings_snapshots_is_deleted"),
        table_name="standings_snapshots",
    )
    op.drop_index(
        op.f("ix_standings_snapshots_is_current"),
        table_name="standings_snapshots",
    )
    op.drop_index(
        op.f("ix_standings_snapshots_fetched_at"),
        table_name="standings_snapshots",
    )
    op.drop_index(
        op.f("ix_standings_snapshots_competition_id"),
        table_name="standings_snapshots",
    )
    op.drop_table("standings_snapshots")
