"""Durable editorial records; content revisions are never updated in place."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from predatory_beavers.db.base import Base
from predatory_beavers.db.types import UTCDateTime


def utc_now() -> datetime:
    return datetime.now(UTC)


class Article(Base):
    __tablename__ = "articles"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    scope_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="weekly_column", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    draft_head_revision_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    approved_revision_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    approved_check_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    published_revision_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class ArticleRevision(Base):
    __tablename__ = "article_revisions"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    article_id: Mapped[UUID] = mapped_column(ForeignKey("articles.id"), index=True)
    origin_job_id: Mapped[UUID | None] = mapped_column(Uuid, unique=True, nullable=True)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    fact_sheet: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    usage: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class ArticleCheck(Base):
    __tablename__ = "article_checks"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    revision_id: Mapped[UUID] = mapped_column(ForeignKey("article_revisions.id"), index=True)
    report: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class EditorialJob(Base):
    __tablename__ = "editorial_jobs"
    __table_args__ = (UniqueConstraint("article_id", "generation_key"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    article_id: Mapped[UUID] = mapped_column(ForeignKey("articles.id"), index=True)
    generation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    base_head_revision_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    fact_sheet: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    brief: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
    result_revision_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deadline_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    claim_token: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    provider_attempt_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    usage: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class ArticleOutbox(Base):
    __tablename__ = "article_outbox"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    article_id: Mapped[UUID] = mapped_column(ForeignKey("articles.id"), nullable=False)
    revision_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    article_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class EditorialLease(Base):
    """One database-wide generation lane, including multiple worker processes."""

    __tablename__ = "editorial_lease"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    lease_until: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
