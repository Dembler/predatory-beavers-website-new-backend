"""Add the append-only administrative audit log.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_audit_log",
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("actor_username", sa.String(64), nullable=False),
        sa.Column("actor_role", sa.String(16), nullable=True),
        sa.Column(
            "action",
            sa.Enum(
                "CREATE",
                "UPDATE",
                "DELETE",
                "UPLOAD",
                "PUBLISH",
                "IMPORT",
                name="audit_action",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=False),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_admin_audit_log_actor_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_audit_log")),
    )
    op.create_index(
        op.f("ix_admin_audit_log_action"),
        "admin_audit_log",
        ["action"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admin_audit_log_actor_id"),
        "admin_audit_log",
        ["actor_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admin_audit_log_actor_username"),
        "admin_audit_log",
        ["actor_username"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admin_audit_log_created_at"),
        "admin_audit_log",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admin_audit_log_entity_type"),
        "admin_audit_log",
        ["entity_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_admin_audit_log_request_id"),
        "admin_audit_log",
        ["request_id"],
        unique=False,
    )
    op.create_index(
        "ix_admin_audit_created_id",
        "admin_audit_log",
        ["created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_admin_audit_entity",
        "admin_audit_log",
        ["entity_type", "entity_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_admin_audit_entity", table_name="admin_audit_log")
    op.drop_index("ix_admin_audit_created_id", table_name="admin_audit_log")
    op.drop_index(
        op.f("ix_admin_audit_log_request_id"),
        table_name="admin_audit_log",
    )
    op.drop_index(
        op.f("ix_admin_audit_log_entity_type"),
        table_name="admin_audit_log",
    )
    op.drop_index(
        op.f("ix_admin_audit_log_created_at"),
        table_name="admin_audit_log",
    )
    op.drop_index(
        op.f("ix_admin_audit_log_actor_username"),
        table_name="admin_audit_log",
    )
    op.drop_index(
        op.f("ix_admin_audit_log_actor_id"),
        table_name="admin_audit_log",
    )
    op.drop_index(
        op.f("ix_admin_audit_log_action"),
        table_name="admin_audit_log",
    )
    op.drop_table("admin_audit_log")
