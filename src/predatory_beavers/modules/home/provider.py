from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.modules.club.repository import TeamRepository
from predatory_beavers.modules.home.service import HomeService
from predatory_beavers.modules.matches.repository import MatchRepository


class HomeProvider(Provider):
    @provide(scope=Scope.REQUEST)
    def service(
        self,
        team_repository: TeamRepository,
        match_repository: MatchRepository,
        session: AsyncSession,
    ) -> HomeService:
        return HomeService(team_repository, match_repository, session)
