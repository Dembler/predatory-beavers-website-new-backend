import json
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Query, Request, status
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.api.responses import ApiResponse, PaginatedResponse
from predatory_beavers.modules.club.auth import AdminAuthorizer
from predatory_beavers.modules.imports.models import CloudSportsState, ImportStatus
from predatory_beavers.modules.imports.schemas import AsbImportRequest, ImportJobRead
from predatory_beavers.modules.imports.service import ImportService
from predatory_beavers.settings import Settings

router = APIRouter(
    prefix="/admin/imports",
    route_class=DishkaRoute,
    tags=["admin-imports"],
)


@router.get("", response_model=PaginatedResponse[ImportJobRead])
async def list_import_jobs(
    request: Request,
    service: FromDishka[ImportService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    import_status: Annotated[ImportStatus | None, Query(alias="status")] = None,
    team: str | None = Query(None, min_length=1, max_length=64),
) -> PaginatedResponse[ImportJobRead]:
    await authorizer.require_admin_session(
        request.cookies.get(settings.session_cookie_name),
    )
    jobs, total = await service.list(
        page=page,
        page_size=page_size,
        status=import_status,
        team=team,
    )
    return PaginatedResponse[ImportJobRead].create(
        items=[ImportJobRead.model_validate(job) for job in jobs],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/asb",
    response_model=ApiResponse[ImportJobRead],
    status_code=status.HTTP_201_CREATED,
)
async def import_asb(
    payload: AsbImportRequest,
    request: Request,
    service: FromDishka[ImportService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[ImportJobRead]:
    await authorizer.require_admin(
        request.cookies.get(settings.session_cookie_name),
        request.headers.get(settings.csrf_header_name),
    )
    job = await service.import_asb(payload)
    return ApiResponse(
        message="ASB import completed",
        data=ImportJobRead.model_validate(job),
    )


@router.get("/sports-sync", response_model=ApiResponse[dict[str, Any]])
async def sports_sync_status(
    request: Request,
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
    session: FromDishka[AsyncSession],
) -> ApiResponse[dict[str, Any]]:
    await authorizer.require_admin_session(request.cookies.get(settings.session_cookie_name))
    state: dict[str, Any] = {}
    url = make_url(settings.database_url)
    if url.get_backend_name() == "postgresql":
        state = (
            await session.scalar(
                select(CloudSportsState.value).where(CloudSportsState.key == "status")
            )
            or {}
        )
    else:
        database = Path(url.database or "data/local.db")
        if not database.is_absolute():
            database = settings.sports_sync_backend_root / database
        state_path = database.parent / "sports-sync" / "status.json"
        try:
            if state_path.stat().st_size <= 256 * 1024:
                parsed = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    state = parsed
        except (OSError, ValueError):
            pass
    fields = [
        "state",
        "attempt_started_at",
        "attempt_finished_at",
        "next_attempt_at",
        "last_success_at",
        "last_error",
        "sources",
    ]
    data = {key: state.get(key) for key in fields}
    data.update(
        enabled=settings.sports_sync_enabled or settings.sports_sync_external_enabled,
        interval_seconds=settings.sports_sync_interval_seconds,
        fiba_interval_seconds=settings.sports_sync_fiba_interval_seconds,
        timeout_seconds=settings.sports_sync_timeout_seconds,
        state=state.get("state", "idle"),
        sources=state.get("sources") or {},
    )
    return ApiResponse(message="Sports sync status retrieved", data=data)


@router.get("/{job_id}", response_model=ApiResponse[ImportJobRead])
async def get_import_job(
    job_id: UUID,
    request: Request,
    service: FromDishka[ImportService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[ImportJobRead]:
    await authorizer.require_admin_session(
        request.cookies.get(settings.session_cookie_name),
    )
    job = await service.get(job_id)
    return ApiResponse(
        message="Import job retrieved",
        data=ImportJobRead.model_validate(job),
    )
