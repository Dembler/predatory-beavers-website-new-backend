from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Query, Request, Response, status

from predatory_beavers.api.responses import ApiResponse, PaginatedResponse
from predatory_beavers.modules.club.auth import AdminAuthorizer
from predatory_beavers.modules.content.models import ContentSection
from predatory_beavers.modules.content.schemas import (
    ContentCreate,
    ContentList,
    ContentRead,
    ContentUpdate,
)
from predatory_beavers.modules.content.service import ContentService
from predatory_beavers.settings import Settings

public_router = APIRouter(prefix="/content", route_class=DishkaRoute, tags=["content"])
admin_router = APIRouter(prefix="/admin/content", route_class=DishkaRoute, tags=["admin-content"])


@public_router.get("/{section}", response_model=ApiResponse[ContentList])
async def list_public_content(
    section: ContentSection,
    service: FromDishka[ContentService],
) -> ApiResponse[ContentList]:
    blocks, _ = await service.list(section=section, public_only=True)
    return ApiResponse(
        message="Content retrieved",
        data=ContentList(
            items=[ContentRead.model_validate(block) for block in blocks],
        ),
    )


@admin_router.get("", response_model=PaginatedResponse[ContentRead])
async def list_admin_content(
    request: Request,
    service: FromDishka[ContentService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
    section: ContentSection | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
) -> PaginatedResponse[ContentRead]:
    await authorizer.require_editor_session(request.cookies.get(settings.session_cookie_name))
    blocks, total = await service.list(
        section=section,
        public_only=False,
        page=page,
        page_size=page_size,
    )
    return PaginatedResponse[ContentRead].create(
        items=[ContentRead.model_validate(block) for block in blocks],
        total=total,
        page=page,
        page_size=page_size,
    )


@admin_router.post("", response_model=ApiResponse[ContentRead], status_code=status.HTTP_201_CREATED)
async def create_content(
    payload: ContentCreate,
    request: Request,
    service: FromDishka[ContentService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[ContentRead]:
    await authorizer.require_editor(
        request.cookies.get(settings.session_cookie_name),
        request.headers.get(settings.csrf_header_name),
    )
    block = await service.create(payload)
    return ApiResponse(message="Content created", data=ContentRead.model_validate(block))


@admin_router.get("/{block_id}", response_model=ApiResponse[ContentRead])
async def get_admin_content(
    block_id: UUID,
    request: Request,
    service: FromDishka[ContentService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[ContentRead]:
    await authorizer.require_editor_session(request.cookies.get(settings.session_cookie_name))
    block = await service.get(block_id)
    return ApiResponse(message="Content retrieved", data=ContentRead.model_validate(block))


@admin_router.patch("/{block_id}", response_model=ApiResponse[ContentRead])
async def update_content(
    block_id: UUID,
    payload: ContentUpdate,
    request: Request,
    service: FromDishka[ContentService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[ContentRead]:
    await authorizer.require_editor(
        request.cookies.get(settings.session_cookie_name),
        request.headers.get(settings.csrf_header_name),
    )
    block = await service.update(block_id, payload)
    return ApiResponse(message="Content updated", data=ContentRead.model_validate(block))


@admin_router.delete("/{block_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_content(
    block_id: UUID,
    request: Request,
    service: FromDishka[ContentService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> Response:
    await authorizer.require_editor(
        request.cookies.get(settings.session_cookie_name),
        request.headers.get(settings.csrf_header_name),
    )
    await service.delete(block_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
