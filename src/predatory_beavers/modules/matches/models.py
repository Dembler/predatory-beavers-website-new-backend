from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    false,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from predatory_beavers.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from predatory_beavers.db.types import UTCDateTime
from predatory_beavers.modules.club.models import MediaAsset, Team


class MatchStatus(StrEnum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    FINISHED = "finished"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"


class ClubSide(StrEnum):
    HOME = "home"
    AWAY = "away"


class Competition(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "competitions"
    __table_args__ = (
        Index(
            "uq_competitions_source_external_id",
            "source",
            "external_id",
            unique=True,
            sqlite_where=text("external_id IS NOT NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    season: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), default="manual", nullable=False, index=True)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=true(),
        nullable=False,
        index=True,
    )


class Venue(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "venues"
    __table_args__ = (
        CheckConstraint(
            "latitude IS NULL OR (latitude >= -90 AND latitude <= 90)",
            name="venue_latitude_range",
        ),
        CheckConstraint(
            "longitude IS NULL OR (longitude >= -180 AND longitude <= 180)",
            name="venue_longitude_range",
        ),
        CheckConstraint(
            "(latitude IS NULL AND longitude IS NULL) OR "
            "(latitude IS NOT NULL AND longitude IS NOT NULL)",
            name="venue_coordinates_pair",
        ),
        Index(
            "uq_venues_source_external_id",
            "source",
            "external_id",
            unique=True,
            sqlite_where=text("external_id IS NOT NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str] = mapped_column(String(500), nullable=False)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(
        String(32),
        default="manual",
        server_default=text("'manual'"),
        nullable=False,
        index=True,
    )
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=true(),
        nullable=False,
        index=True,
    )


class Match(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "matches"
    __table_args__ = (
        CheckConstraint(
            "home_score IS NULL OR home_score >= 0", name="match_home_score_non_negative"
        ),
        CheckConstraint(
            "away_score IS NULL OR away_score >= 0", name="match_away_score_non_negative"
        ),
        CheckConstraint(
            "(home_score IS NULL AND away_score IS NULL) OR "
            "(home_score IS NOT NULL AND away_score IS NOT NULL)",
            name="match_score_pair",
        ),
        Index("ix_matches_team_starts_at", "team_id", "starts_at"),
        Index("ix_matches_status_starts_at", "status", "starts_at"),
        Index(
            "uq_matches_source_external_id",
            "source",
            "external_id",
            unique=True,
            sqlite_where=text("external_id IS NOT NULL"),
        ),
    )

    team_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    competition_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("competitions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    venue_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("venues.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    club_side: Mapped[ClubSide] = mapped_column(
        Enum(ClubSide, name="club_side", native_enum=False, length=8),
        nullable=False,
    )
    home_team_name: Mapped[str] = mapped_column(String(200), nullable=False)
    away_team_name: Mapped[str] = mapped_column(String(200), nullable=False)
    home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[MatchStatus] = mapped_column(
        Enum(MatchStatus, name="match_status", native_enum=False, length=16),
        default=MatchStatus.SCHEDULED,
        nullable=False,
        index=True,
    )
    source: Mapped[str] = mapped_column(String(32), default="manual", nullable=False, index=True)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    home_logo_asset_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("media_assets.id", ondelete="SET NULL"),
        nullable=True,
    )
    away_logo_asset_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("media_assets.id", ondelete="SET NULL"),
        nullable=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    featured: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
        nullable=False,
        index=True,
    )

    team: Mapped[Team] = relationship(lazy="raise")
    competition: Mapped[Competition] = relationship(lazy="raise")
    venue: Mapped[Venue | None] = relationship(lazy="raise")
    home_logo: Mapped[MediaAsset | None] = relationship(
        foreign_keys=[home_logo_asset_id],
        lazy="raise",
    )
    away_logo: Mapped[MediaAsset | None] = relationship(
        foreign_keys=[away_logo_asset_id],
        lazy="raise",
    )
