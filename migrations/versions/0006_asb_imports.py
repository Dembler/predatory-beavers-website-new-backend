"""Add controlled ASB import jobs and venue source identity.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "venues",
        sa.Column("source", sa.String(32), nullable=False, server_default="manual"),
    )
    op.add_column("venues", sa.Column("external_id", sa.String(128), nullable=True))
    op.create_index(op.f("ix_venues_source"), "venues", ["source"], unique=False)
    op.create_index(
        "uq_venues_source_external_id",
        "venues",
        ["source", "external_id"],
        unique=True,
        sqlite_where=sa.text("external_id IS NOT NULL"),
    )

    op.create_table(
        "import_jobs",
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "RUNNING",
                "COMPLETED",
                "FAILED",
                name="import_status",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("team_slug", sa.String(64), nullable=False),
        sa.Column("competition_external_id", sa.String(128), nullable=False),
        sa.Column("standings_external_id", sa.String(128), nullable=False),
        sa.Column("external_team_id", sa.String(128), nullable=False),
        sa.Column("season", sa.String(32), nullable=False),
        sa.Column("request_data", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("actor_username", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_import_jobs_actor_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_jobs")),
    )
    op.create_index(op.f("ix_import_jobs_actor_id"), "import_jobs", ["actor_id"], unique=False)
    op.create_index(op.f("ix_import_jobs_provider"), "import_jobs", ["provider"], unique=False)
    op.create_index(
        op.f("ix_import_jobs_request_id"),
        "import_jobs",
        ["request_id"],
        unique=False,
    )
    op.create_index(op.f("ix_import_jobs_status"), "import_jobs", ["status"], unique=False)
    op.create_index(
        op.f("ix_import_jobs_team_slug"),
        "import_jobs",
        ["team_slug"],
        unique=False,
    )
    op.create_index(
        "uq_import_jobs_active_target",
        "import_jobs",
        ["provider", "team_slug", "competition_external_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('PENDING', 'RUNNING')"),
    )


def downgrade() -> None:
    op.drop_index("uq_import_jobs_active_target", table_name="import_jobs")
    op.drop_index(op.f("ix_import_jobs_team_slug"), table_name="import_jobs")
    op.drop_index(op.f("ix_import_jobs_status"), table_name="import_jobs")
    op.drop_index(op.f("ix_import_jobs_request_id"), table_name="import_jobs")
    op.drop_index(op.f("ix_import_jobs_provider"), table_name="import_jobs")
    op.drop_index(op.f("ix_import_jobs_actor_id"), table_name="import_jobs")
    op.drop_table("import_jobs")

    op.drop_index("uq_venues_source_external_id", table_name="venues")
    op.drop_index(op.f("ix_venues_source"), table_name="venues")
    with op.batch_alter_table("venues") as batch_op:
        batch_op.drop_column("external_id")
        batch_op.drop_column("source")
