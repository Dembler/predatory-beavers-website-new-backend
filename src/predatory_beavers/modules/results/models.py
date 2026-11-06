"""Public team results only: this archive deliberately has no player fields."""

from datetime import date

from sqlalchemy import CheckConstraint, Date, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from predatory_beavers.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ArchiveResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "archive_results"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        CheckConstraint("team IN ('men', 'women')", name="archive_team"),
        CheckConstraint("discipline IN ('5x5', '3x3', 'phygital')", name="archive_discipline"),
        CheckConstraint("place IS NULL OR place > 0", name="archive_place"),
        CheckConstraint("club_score IS NULL OR club_score >= 0", name="archive_club_score"),
        CheckConstraint(
            "opponent_score IS NULL OR opponent_score >= 0", name="archive_opponent_score"
        ),
        CheckConstraint(
            "(club_score IS NULL AND opponent_score IS NULL) OR "
            "(club_score IS NOT NULL AND opponent_score IS NOT NULL)",
            name="archive_score_pair",
        ),
    )

    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    discipline: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    team: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    season: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    tournament: Mapped[str] = mapped_column(String(500), nullable=False)
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    date_precision: Mapped[str] = mapped_column(String(16), nullable=False)
    location: Mapped[str | None] = mapped_column(String(500), nullable=True)
    team_name: Mapped[str] = mapped_column(String(200), nullable=False)
    opponent_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    club_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    opponent_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    place: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    source_label: Mapped[str] = mapped_column(String(200), nullable=False)
    emblem_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    opponent_emblem_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class ArchiveCoverage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "archive_coverage"
    __table_args__ = (UniqueConstraint("provider", "team", "season"),)

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    team: Mapped[str] = mapped_column(String(16), nullable=False)
    season: Mapped[str] = mapped_column(String(32), nullable=False)
    records: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)
