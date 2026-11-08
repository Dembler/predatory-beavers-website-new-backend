"""Durable replay keys for editor POST requests."""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_creation_keys",
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("resource", sa.String(32), nullable=False),
        sa.Column("key", sa.Uuid(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("actor_id", "resource", "key", name="pk_admin_creation_keys"),
    )


def downgrade() -> None:
    op.drop_table("admin_creation_keys")
