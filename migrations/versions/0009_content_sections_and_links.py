"""Make contact channels, captains, player roles and viewing tips editable.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SECTION_CHECK = "ck_content_blocks_content_section_allowed"


def upgrade() -> None:
    with op.batch_alter_table("content_blocks") as batch:
        batch.add_column(sa.Column("subtitle", sa.String(200), nullable=True))
        batch.add_column(sa.Column("link_url", sa.String(2048), nullable=True))
        batch.add_column(sa.Column("link_label", sa.String(100), nullable=True))
        batch.drop_constraint(op.f(SECTION_CHECK), type_="check")
        batch.create_check_constraint(
            op.f(SECTION_CHECK),
            "section IN ('history', 'rules', 'contact_channels', 'captains', "
            "'player_roles', 'viewing_tips')",
        )


def downgrade() -> None:
    populated_extensions = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM content_blocks WHERE section NOT IN ('history', 'rules') "
            "OR subtitle IS NOT NULL OR link_url IS NOT NULL OR link_label IS NOT NULL"
        )
    )
    if populated_extensions:
        raise RuntimeError(
            "Cannot downgrade content schema without losing data: extended sections or "
            "subtitle/link fields are populated. Export and migrate that content explicitly first."
        )
    with op.batch_alter_table("content_blocks") as batch:
        batch.drop_constraint(op.f(SECTION_CHECK), type_="check")
        batch.create_check_constraint(op.f(SECTION_CHECK), "section IN ('history', 'rules')")
        batch.drop_column("link_label")
        batch.drop_column("link_url")
        batch.drop_column("subtitle")
