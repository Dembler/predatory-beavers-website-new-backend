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
    matches = sa.table(
        "matches", sa.column("active", sa.Boolean()), sa.column("is_deleted", sa.Boolean())
    )
    op.execute(matches.update().where(matches.c.is_deleted.is_(True)).values(active=False))


def downgrade() -> None:
    op.drop_index("ix_matches_active", table_name="matches")
    op.drop_column("matches", "active")
