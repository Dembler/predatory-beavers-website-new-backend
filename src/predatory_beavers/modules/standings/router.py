from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Query, Request, Response, status

from predatory_beavers.api.responses import ApiResponse, PaginatedResponse
from predatory_beavers.modules.club.auth import AdminAuthorizer
from predatory_beavers.modules.standings.schemas import (
    StandingsPublish,
    StandingsSnapshotRead,
)
from predatory_beavers.modules.standings.service import StandingsService
from predatory_beavers.settings import Settings

public_router = APIRouter(route_class=DishkaRoute, tags=["standings"])
admin_router = APIRouter(
    prefix="/admin/standings",
    route_class=DishkaRoute,
    tags=["admin-standings"],
)


@public_router.get("/standings", response_model=ApiResponse[StandingsSnapshotRead])
async def get_standings(
    service: FromDishka[StandingsService],
    team: str = Query(min_length=1, max_length=64),
    season: str | None = Query(None, min_length=1, max_length=32),
    competition_id: UUID | None = None,
) -> ApiResponse[StandingsSnapshotRead]:
    snapshot = await service.get_public(
        team=team,
        season=season,
        competition_id=competition_id,
    )
    return ApiResponse(
        message="Standings retrieved",
        data=StandingsSnapshotRead.model_validate(snapshot),
    )


@admin_router.get("", response_model=PaginatedResponse[StandingsSnapshotRead])
async def list_admin_standings(
    request: Request,
    service: FromDishka[StandingsService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    team: str | None = Query(None, min_length=1, max_length=64),
    season: str | None = Query(None, min_length=1, max_length=32),
    current: bool | None = Query(None),
) -> PaginatedResponse[StandingsSnapshotRead]:
    await authorizer.require_editor_session(
        request.cookies.get(settings.session_cookie_name),
    )
    snapshots, total = await service.list(
        page=page,
        page_size=page_size,
        team=team,
        season=season,
        is_current=current,
    )
    return PaginatedResponse[StandingsSnapshotRead].create(
        items=[StandingsSnapshotRead.model_validate(item) for item in snapshots],
        total=total,
        page=page,
        page_size=page_size,
    )


@admin_router.get("/{snapshot_id}", response_model=ApiResponse[StandingsSnapshotRead])
async def get_admin_standings(
    snapshot_id: UUID,
    request: Request,
    service: FromDishka[StandingsService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[StandingsSnapshotRead]:
    await authorizer.require_editor_session(
        request.cookies.get(settings.session_cookie_name),
    )
    snapshot = await service.get(snapshot_id)
    return ApiResponse(
        message="Standings snapshot retrieved",
        data=StandingsSnapshotRead.model_validate(snapshot),
    )


@admin_router.post(
    "",
    response_model=ApiResponse[StandingsSnapshotRead],
    status_code=status.HTTP_201_CREATED,
)
async def publish_standings(
    payload: StandingsPublish,
    request: Request,
    service: FromDishka[StandingsService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[StandingsSnapshotRead]:
    await authorizer.require_editor(
        request.cookies.get(settings.session_cookie_name),
        request.headers.get(settings.csrf_header_name),
    )
    snapshot = await service.publish(payload)
    return ApiResponse(
        message="Standings published",
        data=StandingsSnapshotRead.model_validate(snapshot),
    )


@admin_router.delete("/{snapshot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_standings(
    snapshot_id: UUID,
    request: Request,
    service: FromDishka[StandingsService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> Response:
    await authorizer.require_editor(
        request.cookies.get(settings.session_cookie_name),
        request.headers.get(settings.csrf_header_name),
    )
    await service.delete(snapshot_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
