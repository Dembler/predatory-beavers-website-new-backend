from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.modules.club.repository import TeamRepository
from predatory_beavers.modules.club.schemas import TeamRead
from predatory_beavers.modules.home.schemas import HomeData
from predatory_beavers.modules.matches.repository import MatchRepository
from predatory_beavers.modules.matches.schemas import MatchRead
from predatory_beavers.modules.results.models import ArchiveResult
from predatory_beavers.modules.results.router import ArchiveResultRead


class HomeService:
    def __init__(
        self,
        team_repository: TeamRepository,
        match_repository: MatchRepository,
        session: AsyncSession,
    ) -> None:
        self._team_repository = team_repository
        self._match_repository = match_repository
        self._session = session

    async def get(self) -> HomeData:
        now = datetime.now(UTC)
        teams, _ = await self._team_repository.list(page=1, page_size=10, active=True)
        next_match = await self._match_repository.next_public_match(now)
        moscow = timezone(timedelta(hours=3))
        next_result = await self._session.scalar(
            select(ArchiveResult)
            .where(
                ArchiveResult.team.in_([team.slug for team in teams]),
                ArchiveResult.discipline == "5x5",
                ArchiveResult.status == "scheduled",
                ArchiveResult.event_date >= now.astimezone(moscow).date(),
            )
            .order_by(ArchiveResult.event_date, ArchiveResult.id)
            .limit(1)
        )
        if next_match is not None and (
            next_match.status.value == "live"
            or next_result is None
            or next_result.event_date is None
            or next_match.starts_at.astimezone(moscow).date() <= next_result.event_date
        ):
            next_result = None
        elif next_result is not None:
            next_match = None
        recent_results = await self._match_repository.recent_public_results(now, limit=3)
        return HomeData(
            generated_at=now,
            next_match=MatchRead.from_public(next_match) if next_match is not None else None,
            next_result=ArchiveResultRead.model_validate(next_result) if next_result else None,
            recent_results=[MatchRead.from_public(item) for item in recent_results],
            teams=[TeamRead.model_validate(item) for item in teams],
        )
