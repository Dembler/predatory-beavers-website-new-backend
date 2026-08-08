from datetime import date
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from predatory_beavers.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class TeamCategory(StrEnum):
    MEN = "men"
    WOMEN = "women"


class MediaAsset(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "media_assets"
    __table_args__ = (
        CheckConstraint("size >= 0", name="media_asset_size_non_negative"),
        CheckConstraint("width IS NULL OR width > 0", name="media_asset_width_positive"),
        CheckConstraint("height IS NULL OR height > 0", name="media_asset_height_positive"),
    )

    storage_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    mime: Mapped[str] = mapped_column(String(128), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    alt_text: Mapped[str | None] = mapped_column(String(500), nullable=True)


class Team(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "teams"

    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[TeamCategory] = mapped_column(
        Enum(TeamCategory, name="team_category", native_enum=False), nullable=False, index=True
    )
    logo_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="SET NULL"), nullable=True
    )
    active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=true(),
        nullable=False,
        index=True,
    )

    logo: Mapped[MediaAsset | None] = relationship(lazy="raise")
    players: Mapped[list["Player"]] = relationship(back_populates="team", lazy="raise")


class Player(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "players"
    __table_args__ = (
        CheckConstraint("sort_order >= 0", name="player_sort_order_non_negative"),
        Index("ix_players_team_sort_order", "team_id", "sort_order"),
        Index(
            "ix_players_public_order",
            "team_id",
            "active",
            "is_deleted",
            "sort_order",
        ),
    )

    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    age_text: Mapped[str | None] = mapped_column(String(64), nullable=True)
    position: Mapped[str | None] = mapped_column(String(100), nullable=True)
    fact: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="SET NULL"), nullable=True
    )
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

    team: Mapped[Team] = relationship(back_populates="players", lazy="raise")
    photo: Mapped[MediaAsset | None] = relationship(lazy="raise")
