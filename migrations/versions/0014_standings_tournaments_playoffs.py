"""Keep ASB championships, group stages and official playoff fixtures distinct."""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("standings_snapshots", sa.Column("tournament_id", sa.String(128), nullable=True))
    op.add_column(
        "standings_snapshots", sa.Column("tournament_name", sa.String(200), nullable=True)
    )
    op.add_column("standings_snapshots", sa.Column("stage_name", sa.String(200), nullable=True))
    op.add_column(
        "standings_snapshots",
        sa.Column("stage_kind", sa.String(32), nullable=False, server_default="other"),
    )
    op.add_column(
        "standings_snapshots", sa.Column("games", sa.JSON(), nullable=False, server_default="[]")
    )


def downgrade() -> None:
    for column in ("games", "stage_kind", "stage_name", "tournament_name", "tournament_id"):
        op.drop_column("standings_snapshots", column)
