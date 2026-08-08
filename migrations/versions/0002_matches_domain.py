"""Add competitions, venues, and matches.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "competitions",
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("season", sa.String(32), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_competitions")),
    )
    op.create_index(op.f("ix_competitions_active"), "competitions", ["active"], unique=False)
    op.create_index(
        op.f("ix_competitions_is_deleted"), "competitions", ["is_deleted"], unique=False
    )
    op.create_index(op.f("ix_competitions_season"), "competitions", ["season"], unique=False)
    op.create_index(op.f("ix_competitions_source"), "competitions", ["source"], unique=False)
    op.create_index(
        "uq_competitions_source_external_id",
        "competitions",
        ["source", "external_id"],
        unique=True,
        sqlite_where=sa.text("external_id IS NOT NULL"),
    )

    op.create_table(
        "venues",
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("address", sa.String(500), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "latitude IS NULL OR (latitude >= -90 AND latitude <= 90)",
            name=op.f("ck_venues_venue_latitude_range"),
        ),
        sa.CheckConstraint(
            "longitude IS NULL OR (longitude >= -180 AND longitude <= 180)",
            name=op.f("ck_venues_venue_longitude_range"),
        ),
        sa.CheckConstraint(
            "(latitude IS NULL AND longitude IS NULL) OR "
            "(latitude IS NOT NULL AND longitude IS NOT NULL)",
            name=op.f("ck_venues_venue_coordinates_pair"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_venues")),
    )
    op.create_index(op.f("ix_venues_active"), "venues", ["active"], unique=False)
    op.create_index(op.f("ix_venues_is_deleted"), "venues", ["is_deleted"], unique=False)

    op.create_table(
        "matches",
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("competition_id", sa.Uuid(), nullable=False),
        sa.Column("venue_id", sa.Uuid(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "club_side",
            sa.Enum("HOME", "AWAY", name="club_side", native_enum=False, length=8),
            nullable=False,
        ),
        sa.Column("home_team_name", sa.String(200), nullable=False),
        sa.Column("away_team_name", sa.String(200), nullable=False),
        sa.Column("home_score", sa.Integer(), nullable=True),
        sa.Column("away_score", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "SCHEDULED",
                "LIVE",
                "FINISHED",
                "POSTPONED",
                "CANCELLED",
                name="match_status",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=True),
        sa.Column("home_logo_asset_id", sa.Uuid(), nullable=True),
        sa.Column("away_logo_asset_id", sa.Uuid(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("featured", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "home_score IS NULL OR home_score >= 0",
            name=op.f("ck_matches_match_home_score_non_negative"),
        ),
        sa.CheckConstraint(
            "away_score IS NULL OR away_score >= 0",
            name=op.f("ck_matches_match_away_score_non_negative"),
        ),
        sa.CheckConstraint(
            "(home_score IS NULL AND away_score IS NULL) OR "
            "(home_score IS NOT NULL AND away_score IS NOT NULL)",
            name=op.f("ck_matches_match_score_pair"),
        ),
        sa.ForeignKeyConstraint(
            ["away_logo_asset_id"],
            ["media_assets.id"],
            name=op.f("fk_matches_away_logo_asset_id_media_assets"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["competition_id"],
            ["competitions.id"],
            name=op.f("fk_matches_competition_id_competitions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["home_logo_asset_id"],
            ["media_assets.id"],
            name=op.f("fk_matches_home_logo_asset_id_media_assets"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name=op.f("fk_matches_team_id_teams"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["venue_id"],
            ["venues.id"],
            name=op.f("fk_matches_venue_id_venues"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_matches")),
    )
    op.create_index(op.f("ix_matches_competition_id"), "matches", ["competition_id"], unique=False)
    op.create_index(op.f("ix_matches_featured"), "matches", ["featured"], unique=False)
    op.create_index(op.f("ix_matches_is_deleted"), "matches", ["is_deleted"], unique=False)
    op.create_index(op.f("ix_matches_source"), "matches", ["source"], unique=False)
    op.create_index(op.f("ix_matches_starts_at"), "matches", ["starts_at"], unique=False)
    op.create_index(op.f("ix_matches_status"), "matches", ["status"], unique=False)
    op.create_index(op.f("ix_matches_team_id"), "matches", ["team_id"], unique=False)
    op.create_index(op.f("ix_matches_venue_id"), "matches", ["venue_id"], unique=False)
    op.create_index("ix_matches_status_starts_at", "matches", ["status", "starts_at"], unique=False)
    op.create_index("ix_matches_team_starts_at", "matches", ["team_id", "starts_at"], unique=False)
    op.create_index(
        "uq_matches_source_external_id",
        "matches",
        ["source", "external_id"],
        unique=True,
        sqlite_where=sa.text("external_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_matches_source_external_id", table_name="matches")
    op.drop_index("ix_matches_team_starts_at", table_name="matches")
    op.drop_index("ix_matches_status_starts_at", table_name="matches")
    op.drop_index(op.f("ix_matches_venue_id"), table_name="matches")
    op.drop_index(op.f("ix_matches_team_id"), table_name="matches")
    op.drop_index(op.f("ix_matches_status"), table_name="matches")
    op.drop_index(op.f("ix_matches_starts_at"), table_name="matches")
    op.drop_index(op.f("ix_matches_source"), table_name="matches")
    op.drop_index(op.f("ix_matches_is_deleted"), table_name="matches")
    op.drop_index(op.f("ix_matches_featured"), table_name="matches")
    op.drop_index(op.f("ix_matches_competition_id"), table_name="matches")
    op.drop_table("matches")
    op.drop_index(op.f("ix_venues_is_deleted"), table_name="venues")
    op.drop_index(op.f("ix_venues_active"), table_name="venues")
    op.drop_table("venues")
    op.drop_index("uq_competitions_source_external_id", table_name="competitions")
    op.drop_index(op.f("ix_competitions_source"), table_name="competitions")
    op.drop_index(op.f("ix_competitions_season"), table_name="competitions")
    op.drop_index(op.f("ix_competitions_is_deleted"), table_name="competitions")
    op.drop_index(op.f("ix_competitions_active"), table_name="competitions")
    op.drop_table("competitions")
