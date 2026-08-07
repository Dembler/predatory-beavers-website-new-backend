"""Create the initial club and session schema.

Revision ID: 0001
Revises:
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("mime", sa.String(128), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("checksum", sa.String(128), nullable=False),
        sa.Column("alt_text", sa.String(500), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("size >= 0", name=op.f("ck_media_assets_media_asset_size_non_negative")),
        sa.CheckConstraint(
            "width IS NULL OR width > 0",
            name=op.f("ck_media_assets_media_asset_width_positive"),
        ),
        sa.CheckConstraint(
            "height IS NULL OR height > 0",
            name=op.f("ck_media_assets_media_asset_height_positive"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_assets")),
        sa.UniqueConstraint("storage_key", name=op.f("uq_media_assets_storage_key")),
        sa.UniqueConstraint("checksum", name=op.f("uq_media_assets_checksum")),
    )
    op.create_index(
        op.f("ix_media_assets_is_deleted"), "media_assets", ["is_deleted"], unique=False
    )
    op.create_table(
        "teams",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column(
            "category",
            sa.Enum("MEN", "WOMEN", name="team_category", native_enum=False, length=5),
            nullable=False,
        ),
        sa.Column("logo_asset_id", sa.Uuid(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["logo_asset_id"],
            ["media_assets.id"],
            name=op.f("fk_teams_logo_asset_id_media_assets"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_teams")),
        sa.UniqueConstraint("slug", name=op.f("uq_teams_slug")),
    )
    op.create_index(op.f("ix_teams_category"), "teams", ["category"], unique=False)
    op.create_index(op.f("ix_teams_active"), "teams", ["active"], unique=False)
    op.create_index(op.f("ix_teams_is_deleted"), "teams", ["is_deleted"], unique=False)

    op.create_table(
        "players",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("age_text", sa.String(64), nullable=True),
        sa.Column("position", sa.String(100), nullable=True),
        sa.Column("fact", sa.Text(), nullable=True),
        sa.Column("photo_asset_id", sa.Uuid(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["photo_asset_id"],
            ["media_assets.id"],
            name=op.f("fk_players_photo_asset_id_media_assets"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.id"], name=op.f("fk_players_team_id_teams"), ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "sort_order >= 0", name=op.f("ck_players_player_sort_order_non_negative")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_players")),
    )
    op.create_index(op.f("ix_players_team_id"), "players", ["team_id"], unique=False)
    op.create_index(op.f("ix_players_active"), "players", ["active"], unique=False)
    op.create_index(op.f("ix_players_is_deleted"), "players", ["is_deleted"], unique=False)
    op.create_index(
        "ix_players_team_sort_order", "players", ["team_id", "sort_order"], unique=False
    )
    op.create_index(
        "ix_players_public_order",
        "players",
        ["team_id", "active", "is_deleted", "sort_order"],
        unique=False,
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "role",
            sa.Enum("USER", "EDITOR", "ADMIN", name="user_role", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("first_name", sa.String(100), nullable=True),
        sa.Column("last_name", sa.String(100), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index("uq_users_username_ci", "users", [sa.text("lower(username)")], unique=True)
    op.create_index("uq_users_email_ci", "users", [sa.text("lower(email)")], unique=True)
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("csrf_token_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_sessions_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
    )
    op.create_index(op.f("ix_sessions_user_id"), "sessions", ["user_id"], unique=False)
    op.create_index(op.f("ix_sessions_expires_at"), "sessions", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_sessions_expires_at"), table_name="sessions")
    op.drop_index(op.f("ix_sessions_user_id"), table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("uq_users_email_ci", table_name="users")
    op.drop_index("uq_users_username_ci", table_name="users")
    op.drop_table("users")
    op.drop_index("ix_players_public_order", table_name="players")
    op.drop_index("ix_players_team_sort_order", table_name="players")
    op.drop_index(op.f("ix_players_is_deleted"), table_name="players")
    op.drop_index(op.f("ix_players_active"), table_name="players")
    op.drop_index(op.f("ix_players_team_id"), table_name="players")
    op.drop_table("players")
    op.drop_index(op.f("ix_teams_is_deleted"), table_name="teams")
    op.drop_index(op.f("ix_teams_active"), table_name="teams")
    op.drop_index(op.f("ix_teams_category"), table_name="teams")
    op.drop_table("teams")
    op.drop_index(op.f("ix_media_assets_is_deleted"), table_name="media_assets")
    op.drop_table("media_assets")
