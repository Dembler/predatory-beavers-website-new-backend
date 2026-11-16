"""Stateless mascot domain helpers operating directly on an ``AsyncSession``.

The public router resolves ``AsyncSession`` and ``Settings`` via Dishka and
calls these helpers; no provider registration is required because nothing here
holds state.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.modules.mascot.config import MascotConfig
from predatory_beavers.modules.mascot.errors import (
    InvalidMascotCsrfError,
    InvalidMascotSessionError,
    MascotActiveRunError,
    MascotPayloadMismatchError,
    MascotRateLimitedError,
    MascotSessionOwnershipError,
)
from predatory_beavers.modules.mascot.models import (
    RUN_ACTIVE_STATUSES,
    MascotRun,
    MascotRunEvent,
    MascotSession,
)
from predatory_beavers.modules.mascot.schemas import MascotContext
from predatory_beavers.modules.mascot.security import (
    hash_token,
    mascot_csrf_token,
    new_opaque_token,
)


def payload_hash(message: str, context: MascotContext | None) -> str:
    data = {
        "message": message,
        "context": context.model_dump(exclude_none=True) if context is not None else None,
    }
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


async def authenticate_session(
    session: AsyncSession,
    *,
    cookie_token: str | None,
    csrf_header: str | None = None,
    expected_session_id: object | None = None,
) -> MascotSession:
    """Resolve and validate the anonymous cookie session.

    ``csrf_header is None`` means the caller already rejected a missing CSRF
    token for mutating requests; this helper only verifies it when provided.
    """
    if not cookie_token:
        raise InvalidMascotSessionError
    row = await session.scalar(
        select(MascotSession).where(MascotSession.token_hash == hash_token(cookie_token))
    )
    now = datetime.now(UTC)
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        raise InvalidMascotSessionError
    if csrf_header is not None and not hmac.compare_digest(
        row.csrf_token_hash, hash_token(csrf_header)
    ):
        raise InvalidMascotCsrfError
    if expected_session_id is not None and row.id != expected_session_id:
        raise MascotSessionOwnershipError
    return row


async def create_or_reuse_session(
    session: AsyncSession, config: MascotConfig, cookie_token: str | None
) -> tuple[MascotSession, str, str, bool]:
    """Reuse a valid existing cookie session, otherwise create one.

    Returns ``(record, csrf_token, opaque_token, created)``. The opaque token is
    what the router writes into the HttpOnly cookie. The CSRF token is stable
    across restores of the same cookie (like the auth module), never changing
    ``session_id``.
    """
    now = datetime.now(UTC)
    if cookie_token:
        existing = await session.scalar(
            select(MascotSession).where(MascotSession.token_hash == hash_token(cookie_token))
        )
        if existing is not None and existing.revoked_at is None and existing.expires_at > now:
            csrf = mascot_csrf_token(cookie_token)
            if not hmac.compare_digest(existing.csrf_token_hash, hash_token(csrf)):
                existing.csrf_token_hash = hash_token(csrf)
            existing.last_seen_at = now
            await session.commit()
            return existing, csrf, cookie_token, False
    token = new_opaque_token()
    csrf = mascot_csrf_token(token)
    record = MascotSession(
        token_hash=hash_token(token),
        csrf_token_hash=hash_token(csrf),
        expires_at=now + timedelta(seconds=config.session_ttl_seconds),
    )
    session.add(record)
    await session.commit()
    return record, csrf, token, True


async def append_event(
    session: AsyncSession, run_id: object, event_type: str, payload: dict[str, object]
) -> int:
    # Persist public progress timing with the event, so replay never invents a
    # new start time after the visitor reconnects. These are workflow events,
    # not the provider's private reasoning or raw response.
    if event_type in {"status", "done", "error", "cancelled"}:
        now = datetime.now(UTC)
        payload = {**payload, "at": now.isoformat()}
        if event_type != "status":
            created_at = await session.scalar(
                select(MascotRun.created_at).where(MascotRun.id == run_id)
            )
            if created_at is not None:
                payload["duration_ms"] = max(0, round((now - created_at).total_seconds() * 1000))
    max_seq = await session.scalar(
        select(func.max(MascotRunEvent.seq)).where(MascotRunEvent.run_id == run_id)
    )
    seq = int(max_seq or 0) + 1
    session.add(MascotRunEvent(run_id=run_id, seq=seq, event_type=event_type, payload=payload))
    await session.flush()
    return seq


async def get_run(session: AsyncSession, run_id: object) -> MascotRun | None:
    result: MascotRun | None = await session.scalar(select(MascotRun).where(MascotRun.id == run_id))
    return result


async def get_events(session: AsyncSession, run_id: object, after_seq: int) -> list[MascotRunEvent]:
    rows = await session.scalars(
        select(MascotRunEvent)
        .where(MascotRunEvent.run_id == run_id, MascotRunEvent.seq > after_seq)
        .order_by(MascotRunEvent.seq)
    )
    return list(rows)


async def last_seq(session: AsyncSession, run_id: object) -> int:
    value = await session.scalar(
        select(func.max(MascotRunEvent.seq)).where(MascotRunEvent.run_id == run_id)
    )
    return int(value or 0)


async def create_run(
    session: AsyncSession,
    config: MascotConfig,
    *,
    session_id: object,
    client_request_id: object,
    message: str,
    context: MascotContext | None,
) -> tuple[MascotRun, bool]:
    """Idempotent run creation with payload-hash and one-active-run checks."""
    request_id = str(client_request_id)
    digest = payload_hash(message, context)
    existing = await session.scalar(
        select(MascotRun).where(
            MascotRun.session_id == session_id,
            MascotRun.client_request_id == request_id,
        )
    )
    if existing is not None:
        if existing.request_payload_hash != digest:
            raise MascotPayloadMismatchError
        return existing, False

    active = await session.scalar(
        select(MascotRun).where(
            MascotRun.session_id == session_id,
            MascotRun.status.in_(RUN_ACTIVE_STATUSES),
        )
    )
    if active is not None:
        raise MascotActiveRunError(active.id)

    now = datetime.now(UTC)
    recent_count = await session.scalar(
        select(func.count())
        .select_from(MascotRun)
        .where(
            MascotRun.session_id == session_id,
            MascotRun.created_at > now - timedelta(hours=1),
        )
    )
    if recent_count is not None and recent_count >= config.rate_limit_hour:
        raise MascotRateLimitedError
    run = MascotRun(
        session_id=session_id,
        client_request_id=request_id,
        request_payload_hash=digest,
        message=message,
        context=context.model_dump(exclude_none=True) if context is not None else None,
        status="queued",
        deadline_at=now + timedelta(seconds=config.deadline_seconds),
    )
    session.add(run)
    try:
        await session.flush()
    except IntegrityError:
        # A concurrent request won the (session_id, client_request_id) or the
        # active-run partial unique index. Re-read the committed state.
        await session.rollback()
        existing = await session.scalar(
            select(MascotRun).where(
                MascotRun.session_id == session_id,
                MascotRun.client_request_id == request_id,
            )
        )
        if existing is not None:
            if existing.request_payload_hash != digest:
                raise MascotPayloadMismatchError from None
            return existing, False
        active = await session.scalar(
            select(MascotRun).where(
                MascotRun.session_id == session_id,
                MascotRun.status.in_(RUN_ACTIVE_STATUSES),
            )
        )
        if active is not None:
            raise MascotActiveRunError(active.id) from None
        raise
    await append_event(
        session,
        run.id,
        "status",
        {"stage": "queued", "message": "Готовлюсь ответить…"},
    )
    await session.commit()
    return run, True


async def cancel_run(session: AsyncSession, run: MascotRun) -> MascotRun:
    """Idempotent explicit cancellation (plan §8.5)."""
    now = datetime.now(UTC)
    run_id = run.id
    cancelled = await session.scalar(
        update(MascotRun)
        .where(
            MascotRun.id == run_id,
            MascotRun.status == "queued",
        )
        .values(status="cancelled", finished_at=now)
        .returning(MascotRun.id)
    )
    if cancelled is not None:
        await append_event(session, run.id, "cancelled", {})
    else:
        await session.execute(
            update(MascotRun)
            .where(
                MascotRun.id == run_id,
                MascotRun.status.in_(("running", "cancelling")),
            )
            .values(status="cancelling", cancel_requested_at=now)
        )
    await session.commit()
    await session.refresh(run)
    return run
