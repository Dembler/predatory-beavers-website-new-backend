from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from predatory_beavers.modules.matches.schemas import MatchRead
from predatory_beavers.modules.results.router import ArchiveResultRead

CalendarSection = Literal["upcoming", "changed", "results", "awaiting"]


class CalendarEntry(BaseModel):
    kind: Literal["calendar", "archive"]
    section: CalendarSection
    season: str
    match: MatchRead | None = None
    result: ArchiveResultRead | None = None


class CalendarCounts(BaseModel):
    upcoming: int = 0
    changed: int = 0
    results: int = 0
    awaiting: int = 0


class CalendarPage(BaseModel):
    items: list[CalendarEntry]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=24)
    total_pages: int = Field(ge=0)
    counts: CalendarCounts
    seasons: list[str]
    default_season: str
    selected_season: str
    at: datetime
