from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.modules.club.repository import PlayerRepository, TeamRepository
from predatory_beavers.modules.club.service import PlayerService, TeamService


class ClubProvider(Provider):
    scope = Scope.REQUEST

    @provide
    def team_repository(self, session: AsyncSession) -> TeamRepository:
        return TeamRepository(session)

    @provide
    def player_repository(self, session: AsyncSession) -> PlayerRepository:
        return PlayerRepository(session)

    @provide
    def team_service(self, repository: TeamRepository) -> TeamService:
        return TeamService(repository)

    @provide
    def player_service(
        self,
        repository: PlayerRepository,
        team_repository: TeamRepository,
    ) -> PlayerService:
        return PlayerService(repository, team_repository)
