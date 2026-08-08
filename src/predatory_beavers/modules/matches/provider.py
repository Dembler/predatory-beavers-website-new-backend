from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.db.uow import UnitOfWork
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.club.repository import TeamRepository
from predatory_beavers.modules.matches.repository import (
    CompetitionRepository,
    MatchRepository,
    VenueRepository,
)
from predatory_beavers.modules.matches.service import (
    CompetitionService,
    MatchService,
    VenueService,
)


class MatchesProvider(Provider):
    scope = Scope.REQUEST

    @provide
    def competition_repository(self, session: AsyncSession) -> CompetitionRepository:
        return CompetitionRepository(session)

    @provide
    def venue_repository(self, session: AsyncSession) -> VenueRepository:
        return VenueRepository(session)

    @provide
    def match_repository(self, session: AsyncSession) -> MatchRepository:
        return MatchRepository(session)

    @provide
    def competition_service(
        self,
        repository: CompetitionRepository,
        unit_of_work: UnitOfWork,
        audit_service: AuditService,
    ) -> CompetitionService:
        return CompetitionService(repository, unit_of_work, audit_service)

    @provide
    def venue_service(
        self,
        repository: VenueRepository,
        unit_of_work: UnitOfWork,
        audit_service: AuditService,
    ) -> VenueService:
        return VenueService(repository, unit_of_work, audit_service)

    @provide
    def match_service(
        self,
        repository: MatchRepository,
        team_repository: TeamRepository,
        competition_repository: CompetitionRepository,
        venue_repository: VenueRepository,
        unit_of_work: UnitOfWork,
        audit_service: AuditService,
    ) -> MatchService:
        return MatchService(
            repository,
            team_repository,
            competition_repository,
            venue_repository,
            unit_of_work,
            audit_service,
        )
