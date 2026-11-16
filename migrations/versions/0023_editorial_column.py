"""Editor-controlled weekly columns and a durable generation lane.

Revision ID: 0023
Revises: 0022
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "articles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slug", sa.String(160), nullable=False, unique=True),
        sa.Column("scope_key", sa.String(160), nullable=False, unique=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("draft_head_revision_id", sa.Uuid(), nullable=True),
        sa.Column("approved_revision_id", sa.Uuid(), nullable=True),
        sa.Column("approved_check_id", sa.Uuid(), nullable=True),
        sa.Column("published_revision_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "article_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("article_id", sa.Uuid(), sa.ForeignKey("articles.id"), nullable=False),
        sa.Column("origin_job_id", sa.Uuid(), nullable=True, unique=True),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("fact_sheet", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_article_revisions_article_id", "article_revisions", ["article_id"])
    op.create_table(
        "article_checks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("revision_id", sa.Uuid(), sa.ForeignKey("article_revisions.id"), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_article_checks_revision_id", "article_checks", ["revision_id"])
    op.create_table(
        "editorial_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("article_id", sa.Uuid(), sa.ForeignKey("articles.id"), nullable=False),
        sa.Column("generation_key", sa.String(64), nullable=False),
        sa.Column("base_head_revision_id", sa.Uuid(), nullable=True),
        sa.Column("fact_sheet", sa.JSON(), nullable=False),
        sa.Column("brief", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result_revision_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_attempt_state", sa.String(16), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("article_id", "generation_key"),
    )
    op.create_index("ix_editorial_jobs_article_id", "editorial_jobs", ["article_id"])
    op.create_index("ix_editorial_jobs_status", "editorial_jobs", ["status"])
    op.create_table(
        "article_outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("article_id", sa.Uuid(), sa.ForeignKey("articles.id"), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=True),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("article_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "editorial_lease",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("editorial_lease")
    op.drop_table("article_outbox")
    op.drop_index("ix_editorial_jobs_status", table_name="editorial_jobs")
    op.drop_index("ix_editorial_jobs_article_id", table_name="editorial_jobs")
    op.drop_table("editorial_jobs")
    op.drop_index("ix_article_checks_revision_id", table_name="article_checks")
    op.drop_table("article_checks")
    op.drop_index("ix_article_revisions_article_id", table_name="article_revisions")
    op.drop_table("article_revisions")
    op.drop_table("articles")
