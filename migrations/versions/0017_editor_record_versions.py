"""Atomic editor versions, independent of legacy timestamp serialization."""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

EDITOR_TABLES = (
    "media_assets",
    "teams",
    "players",
    "achievements",
    "content_blocks",
    "competitions",
    "venues",
    "matches",
)


def upgrade() -> None:
    for table in EDITOR_TABLES:
        op.add_column(
            table, sa.Column("version_id", sa.Integer(), nullable=False, server_default="1")
        )


def downgrade() -> None:
    for table in reversed(EDITOR_TABLES):
        op.drop_column(table, "version_id")
