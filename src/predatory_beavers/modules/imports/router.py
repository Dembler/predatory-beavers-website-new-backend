from typing import Annotated
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Query, Request, status

from predatory_beavers.api.responses import ApiResponse, PaginatedResponse
from predatory_beavers.modules.club.auth import AdminAuthorizer
from predatory_beavers.modules.imports.models import ImportStatus
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
