from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, Boolean, ForeignKey, Index, String, text, true
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from predatory_beavers.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from predatory_beavers.db.types import UTCDateTime
from predatory_beavers.modules.club.models import Team
from predatory_beavers.modules.matches.models import Competition


class StandingsSnapshot(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "standings_snapshots"
    __table_args__ = (
        Index(
            "uq_standings_current_team_competition",
            "team_id",
            "competition_id",
            unique=True,
            sqlite_where=text("is_current = 1 AND is_deleted = 0"),
        ),
        Index(
            "ix_standings_team_fetched_at",
            "team_id",
            "fetched_at",
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
    rows: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    source: Mapped[str] = mapped_column(
        String(32),
        default="manual",
        nullable=False,
        index=True,
    )
    source_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    is_current: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=true(),
        nullable=False,
        index=True,
    )

    team: Mapped[Team] = relationship(lazy="raise")
    competition: Mapped[Competition] = relationship(lazy="raise")
