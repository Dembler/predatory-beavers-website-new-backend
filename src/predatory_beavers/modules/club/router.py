from typing import Annotated
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Query, Request, Response, status

from predatory_beavers.api.responses import ApiResponse, PaginatedResponse
from predatory_beavers.modules.club.auth import AdminAuthorizer
from predatory_beavers.modules.club.models import TeamCategory
from predatory_beavers.modules.club.schemas import (
    PlayerCreate,
    PlayerRead,
    PlayerUpdate,
    TeamRead,
)
from predatory_beavers.modules.club.service import PlayerService, TeamService
from predatory_beavers.settings import Settings

public_router = APIRouter(route_class=DishkaRoute, tags=["club"])
# Conventional import name used by feature router composition.
router = public_router
admin_router = APIRouter(
    prefix="/admin/players",
    route_class=DishkaRoute,
    tags=["admin-players"],
)


@public_router.get("/teams", response_model=PaginatedResponse[TeamRead])
async def list_teams(
    service: FromDishka[TeamService],
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    category: Annotated[TeamCategory | None, Query()] = None,
) -> PaginatedResponse[TeamRead]:
    teams, total = await service.list(
        page=page,
        page_size=page_size,
        category=category,
        active=True,
    )
    return PaginatedResponse[TeamRead].create(
        items=[TeamRead.model_validate(team) for team in teams],
        total=total,
        page=page,
        page_size=page_size,
    )


@public_router.get("/players", response_model=PaginatedResponse[PlayerRead])
async def list_players(
    service: FromDishka[PlayerService],
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    team: str | None = Query(None, min_length=1, max_length=64),
    category: Annotated[TeamCategory | None, Query()] = None,
) -> PaginatedResponse[PlayerRead]:
    players, total = await service.list(
        page=page,
        page_size=page_size,
        team=team,
        category=category,
        active=True,
    )
    return PaginatedResponse[PlayerRead].create(
        items=[PlayerRead.model_validate(player) for player in players],
        total=total,
        page=page,
        page_size=page_size,
    )


@public_router.get("/players/{player_id}", response_model=ApiResponse[PlayerRead])
async def get_player(
    player_id: UUID,
    service: FromDishka[PlayerService],
) -> ApiResponse[PlayerRead]:
    player = await service.get_public(player_id)
    return ApiResponse(message="Player retrieved", data=PlayerRead.model_validate(player))


@admin_router.post("", response_model=ApiResponse[PlayerRead], status_code=status.HTTP_201_CREATED)
async def create_player(
    payload: PlayerCreate,
    request: Request,
    service: FromDishka[PlayerService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[PlayerRead]:
    await authorizer.require_editor(
        request.cookies.get(settings.session_cookie_name),
        request.headers.get(settings.csrf_header_name),
    )
    player = await service.create(payload)
    return ApiResponse(message="Player created", data=PlayerRead.model_validate(player))


@admin_router.patch("/{player_id}", response_model=ApiResponse[PlayerRead])
async def update_player(
    player_id: UUID,
    payload: PlayerUpdate,
    request: Request,
    service: FromDishka[PlayerService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[PlayerRead]:
    await authorizer.require_editor(
        request.cookies.get(settings.session_cookie_name),
        request.headers.get(settings.csrf_header_name),
    )
    player = await service.update(player_id, payload)
    return ApiResponse(message="Player updated", data=PlayerRead.model_validate(player))


@admin_router.delete("/{player_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_player(
    player_id: UUID,
    request: Request,
    service: FromDishka[PlayerService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> Response:
    await authorizer.require_editor(
        request.cookies.get(settings.session_cookie_name),
        request.headers.get(settings.csrf_header_name),
    )
    await service.delete(player_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
