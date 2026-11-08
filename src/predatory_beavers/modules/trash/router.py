from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.api.responses import ApiResponse, PaginatedResponse
from predatory_beavers.db.uow import UnitOfWork
from predatory_beavers.integrations.object_storage.client import ObjectStorage
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.club.auth import AdminAuthorizer
from predatory_beavers.modules.content.models import ContentSection
from predatory_beavers.modules.trash.schemas import (
    RestoreRequest,
    RestoreResult,
    TrashEntity,
    TrashItem,
)
from predatory_beavers.modules.trash.service import TrashService
from predatory_beavers.settings import Settings

router = APIRouter(prefix="/admin/trash", route_class=DishkaRoute, tags=["admin-trash"])


@router.get("/{entity}", response_model=PaginatedResponse[TrashItem])
async def list_deleted(
    entity: TrashEntity,
    request: Request,
    session: FromDishka[AsyncSession],
    unit_of_work: FromDishka[UnitOfWork],
    audit: FromDishka[AuditService],
    storage: FromDishka[ObjectStorage],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
    q: str | None = Query(None, max_length=200),
    section: ContentSection | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
) -> PaginatedResponse[TrashItem]:
    await authorizer.require_editor_session(request.cookies.get(settings.session_cookie_name))
    items, total = await TrashService(session, unit_of_work, audit, storage).list_deleted(
        entity, page=page, page_size=page_size, q=q, section=section
    )
    return PaginatedResponse[TrashItem].create(
        items=items, total=total, page=page, page_size=page_size
    )


@router.post("/{entity}/{record_id}/restore", response_model=ApiResponse[RestoreResult])
async def restore_deleted(
    entity: TrashEntity,
    record_id: UUID,
    payload: RestoreRequest,
    request: Request,
    session: FromDishka[AsyncSession],
    unit_of_work: FromDishka[UnitOfWork],
    audit: FromDishka[AuditService],
    storage: FromDishka[ObjectStorage],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[RestoreResult]:
    await authorizer.require_editor(
        request.cookies.get(settings.session_cookie_name),
        request.headers.get(settings.csrf_header_name),
    )
    result = await TrashService(session, unit_of_work, audit, storage).restore(
        entity, record_id, payload
    )
    return ApiResponse(message="Record restored as a draft", data=result)
