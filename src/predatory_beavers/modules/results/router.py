from datetime import date
from typing import Literal
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.api.responses import ApiResponse, PaginatedResponse
from predatory_beavers.modules.results.honours import honours_summary
from predatory_beavers.modules.results.models import ArchiveCoverage, ArchiveResult

router = APIRouter(prefix="/results-archive", route_class=DishkaRoute, tags=["results-archive"])


class ArchiveResultRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    external_id: str
    discipline: str
    team: str
    season: str
    year: int
    tournament: str
    event_date: date | None
    date_precision: str
    location: str | None
    team_name: str
    opponent_name: str | None
    club_score: int | None
    opponent_score: int | None
    place: int | None
    status: str
    source: str
    source_url: str | None
    source_label: str
    emblem_url: str | None
    opponent_emblem_url: str | None
    notes: str | None


@router.get("/honours", response_model=ApiResponse[dict[str, object]])
async def club_honours(
    session: FromDishka[AsyncSession],
    team: Literal["men", "women"] | None = None,
) -> ApiResponse[dict[str, object]]:
    query = select(ArchiveResult).where(
        ArchiveResult.status.in_(["finished", "participated"]),
        ArchiveResult.opponent_name.is_(None),
    )
    if team:
        query = query.where(ArchiveResult.team == team)
    rows = (await session.scalars(query)).all()
    return ApiResponse(message="Documented club honours retrieved", data=honours_summary(rows))


@router.get("/summary", response_model=ApiResponse[dict[str, object]])
async def archive_summary(session: FromDishka[AsyncSession]) -> ApiResponse[dict[str, object]]:
    groups = (
        await session.execute(
            select(
                ArchiveResult.discipline,
                ArchiveResult.team,
                ArchiveResult.season,
                func.count().label("records"),
            ).group_by(ArchiveResult.discipline, ArchiveResult.team, ArchiveResult.season)
        )
    ).all()
    total = sum(row.records for row in groups)
    matches = (
        await session.scalar(
            select(func.count())
            .select_from(ArchiveResult)
            .where(ArchiveResult.opponent_name.is_not(None))
        )
        or 0
    )
    coverage = (
        await session.scalars(
            select(ArchiveCoverage).order_by(
                ArchiveCoverage.provider, ArchiveCoverage.team, ArchiveCoverage.season.desc()
            )
        )
    ).all()
    sources = (
        await session.execute(
            select(ArchiveResult.source, ArchiveResult.source_label)
            .distinct()
            .order_by(ArchiveResult.source)
        )
    ).all()
    return ApiResponse(
        message="Archive coverage retrieved",
        data={
            "total": total,
            "matches": matches,
            "tournaments": total - matches,
            "seasons": sorted({row.season for row in groups}, reverse=True),
            "disciplines": sorted({row.discipline for row in groups}),
            "teams": sorted({row.team for row in groups}),
            "groups": [
                {
                    "discipline": row.discipline,
                    "team": row.team,
                    "season": row.season,
                    "count": row.records,
                }
                for row in groups
            ],
            "coverage": [
                {
                    "provider": row.provider,
                    "team": row.team,
                    "season": row.season,
                    "records": row.records,
                    "status": row.status,
                    "source_url": row.source_url,
                    "note": row.note,
                }
                for row in coverage
            ],
            "sources": [{"source": row.source, "label": row.source_label} for row in sources],
        },
    )


@router.get("", response_model=PaginatedResponse[ArchiveResultRead])
async def list_archive(
    session: FromDishka[AsyncSession],
    discipline: Literal["5x5", "3x3", "phygital"] | None = None,
    team: Literal["men", "women"] | None = None,
    season: str | None = Query(None, max_length=32),
    year: int | None = Query(None, ge=2000, le=2200),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
) -> PaginatedResponse[ArchiveResultRead]:
    conditions = []
    for field, value in [
        (ArchiveResult.discipline, discipline),
        (ArchiveResult.team, team),
        (ArchiveResult.season, season),
        (ArchiveResult.year, year),
    ]:
        if value is not None:
            conditions.append(field == value)
    total = await session.scalar(select(func.count()).select_from(ArchiveResult).where(*conditions))
    results = (
        await session.scalars(
            select(ArchiveResult)
            .where(*conditions)
            .order_by(
                ArchiveResult.year.desc(),
                ArchiveResult.event_date.desc(),
                ArchiveResult.tournament,
                ArchiveResult.team,
                ArchiveResult.id,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return PaginatedResponse.create(
        items=[ArchiveResultRead.model_validate(row) for row in results],
        total=total or 0,
        page=page,
        page_size=page_size,
    )
