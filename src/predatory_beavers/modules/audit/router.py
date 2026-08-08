from datetime import datetime
from typing import Annotated
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Query, Request

from predatory_beavers.api.responses import PaginatedResponse
from predatory_beavers.modules.audit.models import AuditAction
from predatory_beavers.modules.audit.schemas import AuditLogRead
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.club.auth import AdminAuthorizer
from predatory_beavers.settings import Settings

router = APIRouter(
    prefix="/admin/audit-log",
    route_class=DishkaRoute,
    tags=["admin-audit"],
)


@router.get("", response_model=PaginatedResponse[AuditLogRead])
async def list_audit_log(
    request: Request,
    service: FromDishka[AuditService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    actor_id: UUID | None = None,
    action: Annotated[AuditAction | None, Query()] = None,
    entity_type: str | None = Query(None, min_length=1, max_length=64),
    entity_id: str | None = Query(None, min_length=1, max_length=128),
    created_from: Annotated[datetime | None, Query(alias="from")] = None,
    created_to: Annotated[datetime | None, Query(alias="to")] = None,
) -> PaginatedResponse[AuditLogRead]:
    await authorizer.require_admin_session(
        request.cookies.get(settings.session_cookie_name),
    )
    entries, total = await service.list(
        page=page,
        page_size=page_size,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        created_from=created_from,
        created_to=created_to,
    )
    return PaginatedResponse[AuditLogRead].create(
        items=[AuditLogRead.model_validate(entry) for entry in entries],
        total=total,
        page=page,
        page_size=page_size,
    )
