from datetime import datetime
from typing import Annotated
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Query, Request, Response, status

from predatory_beavers.api.responses import ApiResponse, PaginatedResponse
from predatory_beavers.modules.club.auth import AdminAuthorizer
from predatory_beavers.modules.matches.models import MatchStatus
from predatory_beavers.modules.matches.schemas import (
    CompetitionCreate,
    CompetitionRead,
    CompetitionUpdate,
    MatchCreate,
    MatchRead,
    MatchUpdate,
    VenueCreate,
    VenueRead,
    VenueUpdate,
)
from predatory_beavers.modules.matches.service import (
    CompetitionService,
    MatchService,
    VenueService,
)
from predatory_beavers.settings import Settings

public_router = APIRouter(route_class=DishkaRoute, tags=["matches"])
admin_router = APIRouter(prefix="/admin", route_class=DishkaRoute, tags=["admin-matches"])


async def _require_admin_read(
    request: Request,
    authorizer: AdminAuthorizer,
    settings: Settings,
) -> None:
    await authorizer.require_editor_session(
        request.cookies.get(settings.session_cookie_name),
    )


async def _require_admin_write(
    request: Request,
    authorizer: AdminAuthorizer,
    settings: Settings,
) -> None:
    await authorizer.require_editor(
        request.cookies.get(settings.session_cookie_name),
        request.headers.get(settings.csrf_header_name),
    )


@public_router.get("/competitions", response_model=PaginatedResponse[CompetitionRead])
async def list_competitions(
    service: FromDishka[CompetitionService],
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    season: str | None = Query(None, min_length=1, max_length=32),
) -> PaginatedResponse[CompetitionRead]:
    competitions, total = await service.list(
        page=page,
        page_size=page_size,
        season=season,
        active=True,
    )
    return PaginatedResponse[CompetitionRead].create(
        items=[CompetitionRead.model_validate(item) for item in competitions],
        total=total,
        page=page,
        page_size=page_size,
    )


@public_router.get("/venues", response_model=PaginatedResponse[VenueRead])
async def list_venues(
    service: FromDishka[VenueService],
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
) -> PaginatedResponse[VenueRead]:
    venues, total = await service.list(page=page, page_size=page_size, active=True)
    return PaginatedResponse[VenueRead].create(
        items=[VenueRead.model_validate(item) for item in venues],
        total=total,
        page=page,
        page_size=page_size,
    )


@public_router.get("/matches", response_model=PaginatedResponse[MatchRead])
async def list_matches(
    service: FromDishka[MatchService],
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    team: str | None = Query(None, min_length=1, max_length=64),
    match_status: Annotated[MatchStatus | None, Query(alias="status")] = None,
    starts_from: Annotated[datetime | None, Query(alias="from")] = None,
    starts_to: Annotated[datetime | None, Query(alias="to")] = None,
) -> PaginatedResponse[MatchRead]:
    matches, total = await service.list(
        page=page,
        page_size=page_size,
        team=team,
        status=match_status,
        starts_from=starts_from,
        starts_to=starts_to,
        public_only=True,
    )
    return PaginatedResponse[MatchRead].create(
        items=[MatchRead.model_validate(item) for item in matches],
        total=total,
        page=page,
        page_size=page_size,
    )


@public_router.get("/matches/{match_id}", response_model=ApiResponse[MatchRead])
async def get_match(
    match_id: UUID,
    service: FromDishka[MatchService],
) -> ApiResponse[MatchRead]:
    match = await service.get_public(match_id)
    return ApiResponse(message="Match retrieved", data=MatchRead.model_validate(match))


@admin_router.get("/competitions", response_model=PaginatedResponse[CompetitionRead])
async def list_admin_competitions(
    request: Request,
    service: FromDishka[CompetitionService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    season: str | None = Query(None, min_length=1, max_length=32),
    active: bool | None = Query(None),
) -> PaginatedResponse[CompetitionRead]:
    await _require_admin_read(request, authorizer, settings)
    competitions, total = await service.list(
        page=page,
        page_size=page_size,
        season=season,
        active=active,
    )
    return PaginatedResponse[CompetitionRead].create(
        items=[CompetitionRead.model_validate(item) for item in competitions],
        total=total,
        page=page,
        page_size=page_size,
    )


@admin_router.post(
    "/competitions",
    response_model=ApiResponse[CompetitionRead],
    status_code=status.HTTP_201_CREATED,
)
async def create_competition(
    payload: CompetitionCreate,
    request: Request,
    service: FromDishka[CompetitionService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[CompetitionRead]:
    await _require_admin_write(request, authorizer, settings)
    competition = await service.create(payload)
    return ApiResponse(
        message="Competition created",
        data=CompetitionRead.model_validate(competition),
    )


@admin_router.get(
    "/competitions/{competition_id}",
    response_model=ApiResponse[CompetitionRead],
)
async def get_admin_competition(
    competition_id: UUID,
    request: Request,
    service: FromDishka[CompetitionService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[CompetitionRead]:
    await _require_admin_read(request, authorizer, settings)
    competition = await service.get(competition_id)
    return ApiResponse(
        message="Competition retrieved",
        data=CompetitionRead.model_validate(competition),
    )


@admin_router.patch(
    "/competitions/{competition_id}",
    response_model=ApiResponse[CompetitionRead],
)
async def update_competition(
    competition_id: UUID,
    payload: CompetitionUpdate,
    request: Request,
    service: FromDishka[CompetitionService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[CompetitionRead]:
    await _require_admin_write(request, authorizer, settings)
    competition = await service.update(competition_id, payload)
    return ApiResponse(
        message="Competition updated",
        data=CompetitionRead.model_validate(competition),
    )


@admin_router.delete("/competitions/{competition_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_competition(
    competition_id: UUID,
    request: Request,
    service: FromDishka[CompetitionService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> Response:
    await _require_admin_write(request, authorizer, settings)
    await service.delete(competition_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@admin_router.get("/venues", response_model=PaginatedResponse[VenueRead])
async def list_admin_venues(
    request: Request,
    service: FromDishka[VenueService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    active: bool | None = Query(None),
) -> PaginatedResponse[VenueRead]:
    await _require_admin_read(request, authorizer, settings)
    venues, total = await service.list(page=page, page_size=page_size, active=active)
    return PaginatedResponse[VenueRead].create(
        items=[VenueRead.model_validate(item) for item in venues],
        total=total,
        page=page,
        page_size=page_size,
    )


@admin_router.post(
    "/venues",
    response_model=ApiResponse[VenueRead],
    status_code=status.HTTP_201_CREATED,
)
async def create_venue(
    payload: VenueCreate,
    request: Request,
    service: FromDishka[VenueService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[VenueRead]:
    await _require_admin_write(request, authorizer, settings)
    venue = await service.create(payload)
    return ApiResponse(message="Venue created", data=VenueRead.model_validate(venue))


@admin_router.get("/venues/{venue_id}", response_model=ApiResponse[VenueRead])
async def get_admin_venue(
    venue_id: UUID,
    request: Request,
    service: FromDishka[VenueService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[VenueRead]:
    await _require_admin_read(request, authorizer, settings)
    venue = await service.get(venue_id)
    return ApiResponse(message="Venue retrieved", data=VenueRead.model_validate(venue))


@admin_router.patch("/venues/{venue_id}", response_model=ApiResponse[VenueRead])
async def update_venue(
    venue_id: UUID,
    payload: VenueUpdate,
    request: Request,
    service: FromDishka[VenueService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[VenueRead]:
    await _require_admin_write(request, authorizer, settings)
    venue = await service.update(venue_id, payload)
    return ApiResponse(message="Venue updated", data=VenueRead.model_validate(venue))


@admin_router.delete("/venues/{venue_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_venue(
    venue_id: UUID,
    request: Request,
    service: FromDishka[VenueService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> Response:
    await _require_admin_write(request, authorizer, settings)
    await service.delete(venue_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@admin_router.get("/matches", response_model=PaginatedResponse[MatchRead])
async def list_admin_matches(
    request: Request,
    service: FromDishka[MatchService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    team: str | None = Query(None, min_length=1, max_length=64),
    match_status: Annotated[MatchStatus | None, Query(alias="status")] = None,
    starts_from: Annotated[datetime | None, Query(alias="from")] = None,
    starts_to: Annotated[datetime | None, Query(alias="to")] = None,
    featured: bool | None = Query(None),
) -> PaginatedResponse[MatchRead]:
    await _require_admin_read(request, authorizer, settings)
    matches, total = await service.list(
        page=page,
        page_size=page_size,
        team=team,
        status=match_status,
        starts_from=starts_from,
        starts_to=starts_to,
        featured=featured,
        public_only=False,
    )
    return PaginatedResponse[MatchRead].create(
        items=[MatchRead.model_validate(item) for item in matches],
        total=total,
        page=page,
        page_size=page_size,
    )


@admin_router.get("/matches/{match_id}", response_model=ApiResponse[MatchRead])
async def get_admin_match(
    match_id: UUID,
    request: Request,
    service: FromDishka[MatchService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[MatchRead]:
    await _require_admin_read(request, authorizer, settings)
    match = await service.get(match_id)
    return ApiResponse(message="Match retrieved", data=MatchRead.model_validate(match))


@admin_router.post(
    "/matches",
    response_model=ApiResponse[MatchRead],
    status_code=status.HTTP_201_CREATED,
)
async def create_match(
    payload: MatchCreate,
    request: Request,
    service: FromDishka[MatchService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[MatchRead]:
    await _require_admin_write(request, authorizer, settings)
    match = await service.create(payload)
    return ApiResponse(message="Match created", data=MatchRead.model_validate(match))


@admin_router.patch("/matches/{match_id}", response_model=ApiResponse[MatchRead])
async def update_match(
    match_id: UUID,
    payload: MatchUpdate,
    request: Request,
    service: FromDishka[MatchService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[MatchRead]:
    await _require_admin_write(request, authorizer, settings)
    match = await service.update(match_id, payload)
    return ApiResponse(message="Match updated", data=MatchRead.model_validate(match))


@admin_router.delete("/matches/{match_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_match(
    match_id: UUID,
    request: Request,
    service: FromDishka[MatchService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> Response:
    await _require_admin_write(request, authorizer, settings)
    await service.delete(match_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
