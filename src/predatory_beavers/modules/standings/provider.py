from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.db.uow import UnitOfWork
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.club.repository import TeamRepository
from predatory_beavers.modules.matches.repository import CompetitionRepository
from predatory_beavers.modules.standings.repository import StandingsRepository
from predatory_beavers.modules.standings.service import StandingsService


class StandingsProvider(Provider):
    scope = Scope.REQUEST

    @provide
    def repository(self, session: AsyncSession) -> StandingsRepository:
        return StandingsRepository(session)

    @provide
    def service(
        self,
        repository: StandingsRepository,
        team_repository: TeamRepository,
        competition_repository: CompetitionRepository,
        unit_of_work: UnitOfWork,
        audit_service: AuditService,
    ) -> StandingsService:
        return StandingsService(
            repository,
            team_repository,
            competition_repository,
            unit_of_work,
            audit_service,
        )
