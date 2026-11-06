"""Reviewed tournament context joined to the club's public, deduplicated results."""

import json
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.api.responses import ApiResponse
from predatory_beavers.modules.results.honours import unique_tournament_results
from predatory_beavers.modules.results.models import ArchiveResult
from predatory_beavers.modules.results.router import ArchiveResultRead

router = APIRouter(prefix="/results-archive", route_class=DishkaRoute, tags=["results-archive"])


class ContextSource(BaseModel):
    url: str
    label: str


class TournamentContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    match_terms: list[str] = Field(min_length=1)
    description: str
    organizer: str
    official_url: str | None
    logo_url: str | None
    logo_alt: str
    logo_scope: str
    sources: list[ContextSource]


class ContextCatalogue(BaseModel):
    version: int
    items: list[TournamentContext]


class TournamentRead(TournamentContext):
    results: list[ArchiveResultRead]


class TournamentsRead(BaseModel):
    items: list[TournamentRead]
    result_count: int


@lru_cache(maxsize=1)
def load_contexts() -> list[TournamentContext]:
    path = Path(__file__).parent / "assets" / "tournament-context.json"
    return ContextCatalogue.model_validate(json.loads(path.read_text(encoding="utf-8"))).items


def tournament_catalogue(
    rows: list[ArchiveResult], contexts: list[TournamentContext]
) -> TournamentsRead:
    grouped: dict[str, list[ArchiveResult]] = defaultdict(list)
    chosen: dict[str, TournamentContext] = {}
    for row in unique_tournament_results(rows):
        # A match is a part of a tournament, never a second tournament achievement.
        if row.opponent_name is not None or row.status not in {"finished", "participated"}:
            continue
        candidates = [
            (len(term), context)
            for context in contexts
            for term in context.match_terms
            if term.casefold() in row.tournament.casefold()
        ]
        context = (
            max(candidates, key=lambda candidate: candidate[0])[1]
            if candidates
            else TournamentContext(
                key=f"event-{row.id}",
                name=row.tournament,
                match_terms=[row.tournament],
                description="",
                organizer="",
                official_url=None,
                logo_url=None,
                logo_alt="",
                logo_scope="unavailable",
                sources=[],
            )
        )
        grouped[context.key].append(row)
        chosen[context.key] = context
    items = [
        TournamentRead(
            **chosen[key].model_dump(),
            results=[
                ArchiveResultRead.model_validate(row)
                for row in sorted(
                    results,
                    key=lambda row: (-row.year, row.tournament, row.team, row.discipline),
                )
            ],
        )
        for key, results in grouped.items()
    ]
    items.sort(key=lambda item: (-max(row.year for row in item.results), item.name))
    return TournamentsRead(items=items, result_count=sum(len(item.results) for item in items))


@router.get("/tournaments", response_model=ApiResponse[TournamentsRead])
async def club_tournaments(session: FromDishka[AsyncSession]) -> ApiResponse[TournamentsRead]:
    rows = (
        await session.scalars(
            select(ArchiveResult).where(
                ArchiveResult.opponent_name.is_(None),
                ArchiveResult.status.in_(["finished", "participated"]),
            )
        )
    ).all()
    return ApiResponse(
        message="Club tournament history retrieved",
        data=tournament_catalogue(list(rows), load_contexts()),
    )
