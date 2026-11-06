from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from predatory_beavers.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AsbRosterSnapshot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "asb_roster_snapshots"
    __table_args__ = (
        UniqueConstraint("team", "season"),
        CheckConstraint("team IN ('men','women')", name="asb_roster_team"),
    )

    team: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    season: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    external_team_id: Mapped[str] = mapped_column(String(128), nullable=False)
    competition_id: Mapped[str] = mapped_column(String(128), nullable=False)
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    stats_source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AsbRosterMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "asb_roster_members"
    __table_args__ = (UniqueConstraint("snapshot_id", "external_player_id"),)

    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("asb_roster_snapshots.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    external_player_id: Mapped[str] = mapped_column(String(128), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    photo_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    games_played: Mapped[int | None] = mapped_column(Integer, nullable=True)
    points_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assists_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rebounds_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seconds_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    statistics_season: Mapped[str | None] = mapped_column(String(32), nullable=True)
    statistics_source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    is_listed: Mapped[bool] = mapped_column(Boolean, nullable=False)
