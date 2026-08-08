from typing import Annotated
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Query, Request, Response, status

from predatory_beavers.api.responses import ApiResponse, PaginatedResponse
from predatory_beavers.modules.achievements.schemas import (
    AchievementCreate,
    AchievementRead,
    AchievementUpdate,
)
from predatory_beavers.modules.achievements.service import AchievementService
from predatory_beavers.modules.club.auth import AdminAuthorizer
from predatory_beavers.modules.club.models import TeamCategory
from predatory_beavers.settings import Settings

public_router = APIRouter(route_class=DishkaRoute, tags=["achievements"])
admin_router = APIRouter(
    prefix="/admin/achievements",
    route_class=DishkaRoute,
    tags=["admin-achievements"],
)


@public_router.get("/achievements", response_model=PaginatedResponse[AchievementRead])
async def list_achievements(
    service: FromDishka[AchievementService],
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    team: str | None = Query(None, min_length=1, max_length=64),
    category: Annotated[TeamCategory | None, Query()] = None,
) -> PaginatedResponse[AchievementRead]:
    achievements, total = await service.list(
        page=page,
        page_size=page_size,
        team=team,
        category=category,
        active=True,
        public_only=True,
    )
    return PaginatedResponse[AchievementRead].create(
        items=[AchievementRead.model_validate(item) for item in achievements],
        total=total,
        page=page,
        page_size=page_size,
    )


@public_router.get("/achievements/{achievement_id}", response_model=ApiResponse[AchievementRead])
async def get_achievement(
    achievement_id: UUID,
    service: FromDishka[AchievementService],
) -> ApiResponse[AchievementRead]:
    achievement = await service.get_public(achievement_id)
    return ApiResponse(
        message="Achievement retrieved",
        data=AchievementRead.model_validate(achievement),
    )


@admin_router.get("", response_model=PaginatedResponse[AchievementRead])
async def list_admin_achievements(
    request: Request,
    service: FromDishka[AchievementService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    team: str | None = Query(None, min_length=1, max_length=64),
    category: Annotated[TeamCategory | None, Query()] = None,
    active: bool | None = Query(None),
) -> PaginatedResponse[AchievementRead]:
    await authorizer.require_editor_session(
        request.cookies.get(settings.session_cookie_name),
    )
    achievements, total = await service.list(
        page=page,
        page_size=page_size,
        team=team,
        category=category,
        active=active,
        public_only=False,
    )
    return PaginatedResponse[AchievementRead].create(
        items=[AchievementRead.model_validate(item) for item in achievements],
        total=total,
        page=page,
        page_size=page_size,
    )


@admin_router.get("/{achievement_id}", response_model=ApiResponse[AchievementRead])
async def get_admin_achievement(
    achievement_id: UUID,
    request: Request,
    service: FromDishka[AchievementService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[AchievementRead]:
    await authorizer.require_editor_session(
        request.cookies.get(settings.session_cookie_name),
    )
    achievement = await service.get(achievement_id)
    return ApiResponse(
        message="Achievement retrieved",
        data=AchievementRead.model_validate(achievement),
    )


@admin_router.post(
    "",
    response_model=ApiResponse[AchievementRead],
    status_code=status.HTTP_201_CREATED,
)
async def create_achievement(
    payload: AchievementCreate,
    request: Request,
    service: FromDishka[AchievementService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[AchievementRead]:
    await authorizer.require_editor(
        request.cookies.get(settings.session_cookie_name),
        request.headers.get(settings.csrf_header_name),
    )
    achievement = await service.create(payload)
    return ApiResponse(
        message="Achievement created",
        data=AchievementRead.model_validate(achievement),
    )


@admin_router.patch("/{achievement_id}", response_model=ApiResponse[AchievementRead])
async def update_achievement(
    achievement_id: UUID,
    payload: AchievementUpdate,
    request: Request,
    service: FromDishka[AchievementService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[AchievementRead]:
    await authorizer.require_editor(
        request.cookies.get(settings.session_cookie_name),
        request.headers.get(settings.csrf_header_name),
    )
    achievement = await service.update(achievement_id, payload)
    return ApiResponse(
        message="Achievement updated",
        data=AchievementRead.model_validate(achievement),
    )


@admin_router.delete("/{achievement_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_achievement(
    achievement_id: UUID,
    request: Request,
    service: FromDishka[AchievementService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> Response:
    await authorizer.require_editor(
        request.cookies.get(settings.session_cookie_name),
        request.headers.get(settings.csrf_header_name),
    )
    await service.delete(achievement_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
