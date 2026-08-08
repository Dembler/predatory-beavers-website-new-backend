from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.db.uow import UnitOfWork
from predatory_beavers.integrations.asb.client import AsbClient, HttpAsbClient
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.club.repository import TeamRepository
from predatory_beavers.modules.imports.repository import ImportJobRepository
from predatory_beavers.modules.imports.service import ImportService
from predatory_beavers.modules.matches.repository import (
    CompetitionRepository,
    MatchRepository,
    VenueRepository,
)
from predatory_beavers.modules.standings.repository import StandingsRepository
from predatory_beavers.settings import Settings


class ImportsProvider(Provider):
    @provide(scope=Scope.APP)
    def asb_client(self, settings: Settings) -> AsbClient:
        return HttpAsbClient(settings)

    @provide(scope=Scope.REQUEST)
    def job_repository(self, session: AsyncSession) -> ImportJobRepository:
        return ImportJobRepository(session)

    @provide(scope=Scope.REQUEST)
    def service(
        self,
        job_repository: ImportJobRepository,
        team_repository: TeamRepository,
        competition_repository: CompetitionRepository,
        venue_repository: VenueRepository,
        match_repository: MatchRepository,
        standings_repository: StandingsRepository,
        asb_client: AsbClient,
        audit_service: AuditService,
        unit_of_work: UnitOfWork,
    ) -> ImportService:
        return ImportService(
            job_repository,
            team_repository,
            competition_repository,
            venue_repository,
            match_repository,
            standings_repository,
            asb_client,
            audit_service,
            unit_of_work,
        )
