from datetime import UTC, datetime

from predatory_beavers.modules.club.repository import TeamRepository
from predatory_beavers.modules.club.schemas import TeamRead
from predatory_beavers.modules.home.schemas import HomeData
from predatory_beavers.modules.matches.repository import MatchRepository
from predatory_beavers.modules.matches.schemas import MatchRead


class HomeService:
    def __init__(
        self,
        team_repository: TeamRepository,
        match_repository: MatchRepository,
    ) -> None:
        self._team_repository = team_repository
        self._match_repository = match_repository

    async def get(self) -> HomeData:
        now = datetime.now(UTC)
        teams, _ = await self._team_repository.list(page=1, page_size=10, active=True)
        next_match = await self._match_repository.next_public_match(now)
        recent_results = await self._match_repository.recent_public_results(now, limit=3)
        return HomeData(
            generated_at=now,
            next_match=MatchRead.model_validate(next_match) if next_match is not None else None,
            recent_results=[MatchRead.model_validate(item) for item in recent_results],
            teams=[TeamRead.model_validate(item) for item in teams],
        )
