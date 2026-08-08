from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import JSON, Enum, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from predatory_beavers.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from predatory_beavers.db.types import UTCDateTime


class ImportStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ImportJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "import_jobs"
    __table_args__ = (
        Index(
            "uq_import_jobs_active_target",
            "provider",
            "team_slug",
            "competition_external_id",
            unique=True,
            sqlite_where=text("status IN ('PENDING', 'RUNNING')"),
        ),
    )

    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[ImportStatus] = mapped_column(
        Enum(ImportStatus, name="import_status", native_enum=False, length=16),
        default=ImportStatus.PENDING,
        nullable=False,
        index=True,
    )
    team_slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    competition_external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    standings_external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    external_team_id: Mapped[str] = mapped_column(String(128), nullable=False)
    season: Mapped[str] = mapped_column(String(32), nullable=False)
    request_data: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    result: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actor_username: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
