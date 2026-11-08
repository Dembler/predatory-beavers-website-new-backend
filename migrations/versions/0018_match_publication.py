"""Allow recovered matches to remain unpublished until editorial review."""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "matches", sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False)
    )
    op.create_index("ix_matches_active", "matches", ["active"])
    op.execute(sa.text("UPDATE matches SET active = 0 WHERE is_deleted = 1"))


def downgrade() -> None:
    op.drop_index("ix_matches_active", table_name="matches")
    op.drop_column("matches", "active")
