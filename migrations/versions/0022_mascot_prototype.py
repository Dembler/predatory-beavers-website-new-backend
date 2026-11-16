"""Mascot prototype tables: anonymous sessions, durable runs and event journal.

Revision ID: 0022
Revises: 0021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_STATUS_WHERE = "status IN ('queued', 'running', 'cancelling')"


def upgrade() -> None:
    op.create_table(
        "mascot_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("csrf_token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mascot_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_mascot_sessions_token_hash")),
    )
    op.create_index(
        op.f("ix_mascot_sessions_expires_at"), "mascot_sessions", ["expires_at"], unique=False
    )

    op.create_table(
        "mascot_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("client_request_id", sa.String(128), nullable=False),
        sa.Column("request_payload_hash", sa.String(64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data_revision", sa.String(64), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_attempt_state", sa.String(16), nullable=True),
        sa.Column("provider_input_tokens", sa.Integer(), nullable=True),
        sa.Column("provider_output_tokens", sa.Integer(), nullable=True),
        sa.Column("provider_usage", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["mascot_sessions.id"],
            name=op.f("fk_mascot_runs_session_id_mascot_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mascot_runs")),
        sa.UniqueConstraint(
            "session_id", "client_request_id", name=op.f("uq_mascot_runs_session_id")
        ),
    )
    op.create_index(op.f("ix_mascot_runs_session_id"), "mascot_runs", ["session_id"], unique=False)
    op.create_index(op.f("ix_mascot_runs_status"), "mascot_runs", ["status"], unique=False)
    op.create_index(
        "uq_mascot_runs_active_session",
        "mascot_runs",
        ["session_id"],
        unique=True,
        sqlite_where=sa.text(_ACTIVE_STATUS_WHERE),
        postgresql_where=sa.text(_ACTIVE_STATUS_WHERE),
    )

    op.create_table(
        "mascot_run_events",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["mascot_runs.id"],
            name=op.f("fk_mascot_run_events_run_id_mascot_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "seq", name=op.f("pk_mascot_run_events")),
    )


def downgrade() -> None:
    op.drop_table("mascot_run_events")
    op.drop_index("uq_mascot_runs_active_session", table_name="mascot_runs")
    op.drop_index(op.f("ix_mascot_runs_status"), table_name="mascot_runs")
    op.drop_index(op.f("ix_mascot_runs_session_id"), table_name="mascot_runs")
    op.drop_table("mascot_runs")
    op.drop_index(op.f("ix_mascot_sessions_expires_at"), table_name="mascot_sessions")
    op.drop_table("mascot_sessions")
