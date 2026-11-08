from datetime import UTC, datetime
from typing import Literal

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Query
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.modules.calendar.repository import CalendarRepository
from predatory_beavers.modules.calendar.schemas import CalendarPage

router = APIRouter(prefix="/calendar", route_class=DishkaRoute, tags=["calendar"])


@router.get("", response_model=CalendarPage)
async def calendar(
    session: FromDishka[AsyncSession],
    team: Literal["men", "women"] | None = None,
    discipline: Literal["5x5", "3x3", "phygital"] | None = None,
    status: Literal[
        "scheduled", "live", "finished", "postponed", "cancelled", "unpublished", "participated"
    ]
    | None = None,
    season: str | None = Query(None, pattern=r"^(all|[0-9]{4}-[0-9]{4})$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=24),
    at: AwareDatetime | None = None,
) -> CalendarPage:
    return await CalendarRepository(session).page(
        at=(at or datetime.now(UTC)).astimezone(UTC),
        team=team,
        discipline=discipline,
        status=status,
        season=season,
        page=page,
        page_size=page_size,
    )
