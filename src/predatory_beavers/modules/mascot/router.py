"""Public mascot router (internal prototype).

All routes are hard-disabled with ``404`` unless ``mascot_enabled`` is true and
the environment is not production. The router is stateless: it resolves
``AsyncSession`` and ``Settings`` through Dishka and delegates to the helpers
in ``service``. No LLM call ever runs in the request path.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from time import monotonic
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.api.errors import ConflictError, NotFoundError
from predatory_beavers.modules.mascot.config import MascotConfig
from predatory_beavers.modules.mascot.errors import (
    InvalidMascotCsrfError,
    MascotDisabledError,
    MascotInputError,
    MascotOriginError,
)
from predatory_beavers.modules.mascot.models import RUN_TERMINAL_STATUSES
from predatory_beavers.modules.mascot.schemas import (
    CancelResult,
    ChatRunAccepted,
    ChatRunCreate,
    RunStatusRead,
    SessionRead,
)
from predatory_beavers.modules.mascot.security import origin_allowed
from predatory_beavers.modules.mascot.service import (
    authenticate_session,
    cancel_run,
    create_or_reuse_session,
    create_run,
    get_events,
    get_run,
    last_seq,
)
from predatory_beavers.settings import Settings

router = APIRouter(prefix="/mascot", tags=["mascot"], route_class=DishkaRoute)

_HEARTBEAT_SECONDS = 15.0
_POLL_SECONDS = 0.25


def _ensure_enabled(config: MascotConfig) -> None:
    if not config.enabled or config.env not in ("dev", "test"):
        raise MascotDisabledError


def _cookie_token(request: Request, config: MascotConfig) -> str | None:
    return request.cookies.get(config.session_cookie)


def _csrf_header(request: Request, config: MascotConfig) -> str | None:
    value = request.headers.get(config.csrf_header_name)
    return value or None


def _verify_origin(request: Request, config: MascotConfig) -> None:
    if not origin_allowed(request.headers.get("Origin"), config.cors_origins):
        raise MascotOriginError


def _set_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _format_sse(seq: int, event_type: str, payload: dict[str, object]) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"id: {seq}\nevent: {event_type}\ndata: {data}\n\n"


async def _event_stream(session: AsyncSession, run_id: UUID, after_seq: int) -> AsyncIterator[str]:
    cursor = after_seq
    last_heartbeat = monotonic()
    while True:
        await session.rollback()
        events = await get_events(session, run_id, cursor)
        run = await get_run(session, run_id)
        terminal = run is None or run.status in RUN_TERMINAL_STATUSES
        batch = [
            (event.seq, _format_sse(event.seq, event.event_type, event.payload)) for event in events
        ]
        await session.rollback()
        for seq, formatted in batch:
            yield formatted
            cursor = seq
        if terminal:
            break
        if monotonic() - last_heartbeat >= _HEARTBEAT_SECONDS:
            yield ": keep-alive\n\n"
            last_heartbeat = monotonic()
        await asyncio.sleep(_POLL_SECONDS)


@router.post("/session", response_model=SessionRead)
async def create_session(
    request: Request,
    response: Response,
    session: FromDishka[AsyncSession],
    settings: FromDishka[Settings],
) -> SessionRead:
    config = MascotConfig.from_settings(settings)
    _ensure_enabled(config)
    _verify_origin(request, config)
    record, csrf, token, created = await create_or_reuse_session(
        session, config, _cookie_token(request, config)
    )
    if created:
        response.set_cookie(
            key=config.session_cookie,
            value=token,
            max_age=config.session_ttl_seconds,
            path="/",
            secure=config.cookie_secure,
            httponly=True,
            samesite=config.cookie_samesite,
        )
    _set_no_store(response)
    return SessionRead(session_id=record.id, csrf_token=csrf, expires_at=record.expires_at)


@router.post("/chat/runs", response_model=ChatRunAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_chat_run(
    payload: ChatRunCreate,
    request: Request,
    response: Response,
    session: FromDishka[AsyncSession],
    settings: FromDishka[Settings],
) -> ChatRunAccepted:
    config = MascotConfig.from_settings(settings)
    _ensure_enabled(config)
    _verify_origin(request, config)
    csrf = _csrf_header(request, config)
    if not csrf:
        raise InvalidMascotCsrfError
    await authenticate_session(
        session,
        cookie_token=_cookie_token(request, config),
        csrf_header=csrf,
        expected_session_id=payload.session_id,
    )
    if len(payload.message) > config.max_input_chars:
        raise MascotInputError
    run, _ = await create_run(
        session,
        config,
        session_id=payload.session_id,
        client_request_id=payload.client_request_id,
        message=payload.message,
        context=payload.context,
    )
    _set_no_store(response)
    return ChatRunAccepted(
        run_id=run.id,
        session_id=payload.session_id,
        status=run.status,
        events_url=f"{config.api_prefix}/mascot/chat/runs/{run.id}/events",
    )


@router.get("/chat/runs/{run_id}", response_model=RunStatusRead)
async def get_run_status(
    run_id: UUID,
    request: Request,
    response: Response,
    session: FromDishka[AsyncSession],
    settings: FromDishka[Settings],
) -> RunStatusRead:
    config = MascotConfig.from_settings(settings)
    _ensure_enabled(config)
    owner = await authenticate_session(session, cookie_token=_cookie_token(request, config))
    run = await get_run(session, run_id)
    if run is None or run.session_id != owner.id:
        raise NotFoundError("Run not found")
    _set_no_store(response)
    return RunStatusRead(run_id=run.id, status=run.status, last_seq=await last_seq(session, run.id))


@router.get("/chat/runs/{run_id}/events")
async def run_events(
    run_id: UUID,
    request: Request,
    session: FromDishka[AsyncSession],
    settings: FromDishka[Settings],
    after_seq: int = Query(0, ge=0),
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
) -> StreamingResponse:
    config = MascotConfig.from_settings(settings)
    _ensure_enabled(config)
    owner = await authenticate_session(session, cookie_token=_cookie_token(request, config))
    run = await get_run(session, run_id)
    if run is None or run.session_id != owner.id:
        raise NotFoundError("Run not found")
    cursor = after_seq
    if last_event_id is not None:
        try:
            header_seq = int(last_event_id)
        except ValueError:
            header_seq = -1
        if header_seq < 0:
            raise MascotInputError
        if "after_seq" in request.query_params and header_seq != after_seq:
            raise ConflictError("after_seq and Last-Event-ID disagree")
        cursor = header_seq
    return StreamingResponse(
        _event_stream(session, run_id, cursor),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store, no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat/runs/{run_id}/cancel", response_model=CancelResult)
async def cancel_chat_run(
    run_id: UUID,
    request: Request,
    response: Response,
    session: FromDishka[AsyncSession],
    settings: FromDishka[Settings],
) -> CancelResult:
    config = MascotConfig.from_settings(settings)
    _ensure_enabled(config)
    _verify_origin(request, config)
    csrf = _csrf_header(request, config)
    if not csrf:
        raise InvalidMascotCsrfError
    owner = await authenticate_session(
        session, cookie_token=_cookie_token(request, config), csrf_header=csrf
    )
    run = await get_run(session, run_id)
    if run is None or run.session_id != owner.id:
        raise NotFoundError("Run not found")
    await cancel_run(session, run)
    _set_no_store(response)
    return CancelResult(run_id=run.id, status=run.status)
