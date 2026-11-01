"""Add editable club history and basketball rules.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    table = op.create_table(
        "content_blocks",
        sa.Column("section", sa.String(32), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "sort_order >= 0", name=op.f("ck_content_blocks_content_sort_order_non_negative")
        ),
        sa.CheckConstraint(
            "section IN ('history', 'rules')",
            name=op.f("ck_content_blocks_content_section_allowed"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_blocks")),
    )
    op.create_index("ix_content_blocks_is_deleted", "content_blocks", ["is_deleted"])
    op.create_index(
        "ix_content_blocks_public_order",
        "content_blocks",
        ["section", "active", "is_deleted", "sort_order"],
    )
    now = datetime.now(UTC)
    initial = [
        (
            "history",
            "Команда, университет, характер",
            "«Хищные Бобры» — мужская и женская баскетбольные команды "
            "Воронежского государственного университета.",
        ),
        ("rules", "Счёт", "Штрафной бросок приносит одно очко, бросок с игры — два или три."),
        ("rules", "Время", "Матч разделён на четверти, а владение команды ограничено по времени."),
        (
            "rules",
            "Нарушения",
            "Пробежка, двойное ведение и персональные фолы дают преимущество сопернику.",
        ),
    ]
    op.bulk_insert(
        table,
        [
            dict(
                id=UUID(int=7000 + index),
                section=section,
                title=title,
                body=body,
                sort_order=index,
                active=True,
                is_deleted=False,
                created_at=now,
                updated_at=now,
            )
            for index, (section, title, body) in enumerate(initial)
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_content_blocks_public_order", table_name="content_blocks")
    op.drop_index("ix_content_blocks_is_deleted", table_name="content_blocks")
    op.drop_table("content_blocks")
