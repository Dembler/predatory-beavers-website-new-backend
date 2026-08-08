"""Add achievements.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "achievements",
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("media_asset_id", sa.Uuid(), nullable=False),
        sa.Column("achieved_at", sa.Date(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "sort_order >= 0",
            name=op.f("ck_achievements_achievement_sort_order_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["media_asset_id"],
            ["media_assets.id"],
            name=op.f("fk_achievements_media_asset_id_media_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name=op.f("fk_achievements_team_id_teams"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_achievements")),
    )
    op.create_index(op.f("ix_achievements_active"), "achievements", ["active"], unique=False)
    op.create_index(
        op.f("ix_achievements_is_deleted"), "achievements", ["is_deleted"], unique=False
    )
    op.create_index(
        op.f("ix_achievements_media_asset_id"),
        "achievements",
        ["media_asset_id"],
        unique=False,
    )
    op.create_index(op.f("ix_achievements_team_id"), "achievements", ["team_id"], unique=False)
    op.create_index(
        "ix_achievements_team_sort_order",
        "achievements",
        ["team_id", "sort_order"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_achievements_team_sort_order", table_name="achievements")
    op.drop_index(op.f("ix_achievements_team_id"), table_name="achievements")
    op.drop_index(op.f("ix_achievements_media_asset_id"), table_name="achievements")
    op.drop_index(op.f("ix_achievements_is_deleted"), table_name="achievements")
    op.drop_index(op.f("ix_achievements_active"), table_name="achievements")
    op.drop_table("achievements")
