"""Add editable captain portraits and multiple contact methods."""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("content_blocks") as batch:
        batch.add_column(sa.Column("photo_asset_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("contacts", sa.JSON(), nullable=False, server_default="[]"))
        batch.create_foreign_key(
            "fk_content_blocks_photo_asset_id_media_assets",
            "media_assets",
            ["photo_asset_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    blocks = sa.table(
        "content_blocks", sa.column("photo_asset_id", sa.Uuid()), sa.column("contacts", sa.JSON())
    )
    if op.get_bind().scalar(
        sa.select(sa.func.count())
        .select_from(blocks)
        .where(
            sa.or_(
                blocks.c.photo_asset_id.is_not(None), sa.cast(blocks.c.contacts, sa.String) != "[]"
            )
        )
    ):
        raise RuntimeError("Export captain photos and contacts before downgrading")
    with op.batch_alter_table("content_blocks") as batch:
        batch.drop_constraint("fk_content_blocks_photo_asset_id_media_assets", type_="foreignkey")
        batch.drop_column("contacts")
        batch.drop_column("photo_asset_id")
