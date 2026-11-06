"""Public official source and chronology for historical standings stages."""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("standings_snapshots", sa.Column("source_url", sa.String(2048), nullable=True))
    op.add_column(
        "standings_snapshots", sa.Column("last_game_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "standings_snapshots",
        sa.Column("stage_order", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("standings_snapshots", "stage_order")
    op.drop_column("standings_snapshots", "last_game_at")
    op.drop_column("standings_snapshots", "source_url")
