"""ORM models for the mascot prototype.

Three durable tables back the whole flow:

- ``mascot_sessions``  — anonymous cookie sessions (hashed opaque token only).
- ``mascot_runs``      — durable chat runs with idempotency, active-run uniqueness,
  an absolute deadline and provider attempt accounting.
- ``mascot_run_events``— the immutable event journal replayed over SSE.

The worker owns the run lifecycle after ``queued``; the API never calls an LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from predatory_beavers.db.base import Base
from predatory_beavers.db.types import UTCDateTime

# Run states shared between API, worker and tests. ``queued|running|cancelling``
# are the only states that participate in the one-active-run-per-session rule.
RUN_ACTIVE_STATUSES = ("queued", "running", "cancelling")
RUN_TERMINAL_STATUSES = ("completed", "failed", "interrupted", "cancelled")

# Provider attempt accounting (see plan §9.6/§15.9). A non-settled attempt is
# never recorded with an invented zero cost.
PROVIDER_ATTEMPT_STATES = ("started", "settled", "unknown")


def utc_now() -> datetime:
    return datetime.now(UTC)


class MascotSession(Base):
    __tablename__ = "mascot_sessions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class MascotRun(Base):
    __tablename__ = "mascot_runs"
    __table_args__ = (
        UniqueConstraint("session_id", "client_request_id"),
        # One active run per session, enforced by the database rather than only
        # by application code (plan §8.2).
        Index(
            "uq_mascot_runs_active_session",
            "session_id",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running', 'cancelling')"),
            postgresql_where=text("status IN ('queued', 'running', 'cancelling')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("mascot_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # Prototype data revision: a deterministic hash of the public corpus that
    # the worker retrieves for this run (plan §6.2 pinning is simplified here).
    data_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deadline_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    claim_token: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    provider_attempt_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    provider_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_usage: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class MascotRunEvent(Base):
    __tablename__ = "mascot_run_events"

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("mascot_runs.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
