"""Keep existing public assets compatible; protect new unpublished uploads."""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Mark existing rows explicitly; future inserts default to private.
    op.add_column(
        "media_assets",
        sa.Column("legacy_public", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(sa.text("UPDATE media_assets SET legacy_public = 1"))


def downgrade() -> None:
    op.drop_column("media_assets", "legacy_public")
