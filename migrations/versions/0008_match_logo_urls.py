"""Preserve official ASB team logo URLs on matches.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("matches", sa.Column("home_logo_url", sa.String(2048), nullable=True))
    op.add_column("matches", sa.Column("away_logo_url", sa.String(2048), nullable=True))


def downgrade() -> None:
    op.drop_column("matches", "away_logo_url")
    op.drop_column("matches", "home_logo_url")
