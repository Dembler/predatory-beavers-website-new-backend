from datetime import date
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from predatory_beavers.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from predatory_beavers.modules.club.models import MediaAsset, Team


class Achievement(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "achievements"
    __table_args__ = (
        CheckConstraint("sort_order >= 0", name="achievement_sort_order_non_negative"),
        Index("ix_achievements_team_sort_order", "team_id", "sort_order"),
    )

    team_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    media_asset_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("media_assets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    achieved_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=true(),
        nullable=False,
        index=True,
    )

    team: Mapped[Team] = relationship(lazy="raise")
    media: Mapped[MediaAsset] = relationship(lazy="raise")
