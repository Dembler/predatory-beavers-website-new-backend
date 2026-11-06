"""Season-verified public ASB roster and permitted team-season totals."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asb_roster_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("team", sa.String(16), nullable=False),
        sa.Column("season", sa.String(32), nullable=False),
        sa.Column("external_team_id", sa.String(128), nullable=False),
        sa.Column("competition_id", sa.String(128), nullable=False),
        sa.Column("is_published", sa.Boolean(), nullable=False),
        sa.Column("source_url", sa.String(2048), nullable=False),
        sa.Column("stats_source_url", sa.String(2048), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("team", "season"),
        sa.CheckConstraint("team IN ('men','women')", name="asb_roster_team"),
    )
    op.create_index("ix_asb_roster_snapshots_team", "asb_roster_snapshots", ["team"])
    op.create_index("ix_asb_roster_snapshots_season", "asb_roster_snapshots", ["season"])
    op.create_table(
        "asb_roster_members",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "snapshot_id",
            sa.Uuid(),
            sa.ForeignKey("asb_roster_snapshots.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("external_player_id", sa.String(128), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("photo_url", sa.String(2048), nullable=True),
        sa.Column("games_played", sa.Integer(), nullable=True),
        sa.Column("points_total", sa.Integer(), nullable=True),
        sa.Column("assists_total", sa.Integer(), nullable=True),
        sa.Column("rebounds_total", sa.Integer(), nullable=True),
        sa.Column("seconds_total", sa.Integer(), nullable=True),
        sa.Column("is_listed", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("snapshot_id", "external_player_id"),
    )
    op.create_index("ix_asb_roster_members_snapshot_id", "asb_roster_members", ["snapshot_id"])


def downgrade() -> None:
    op.drop_table("asb_roster_members")
    op.drop_table("asb_roster_snapshots")
