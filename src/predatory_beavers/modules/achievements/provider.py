from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.db.uow import UnitOfWork
from predatory_beavers.modules.achievements.repository import AchievementRepository
from predatory_beavers.modules.achievements.service import AchievementService
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.club.repository import TeamRepository
from predatory_beavers.modules.media.repository import MediaRepository


class AchievementsProvider(Provider):
    scope = Scope.REQUEST

    @provide
    def repository(self, session: AsyncSession) -> AchievementRepository:
        return AchievementRepository(session)

    @provide
    def service(
        self,
        repository: AchievementRepository,
        team_repository: TeamRepository,
        media_repository: MediaRepository,
        unit_of_work: UnitOfWork,
        audit_service: AuditService,
    ) -> AchievementService:
        return AchievementService(
            repository,
            team_repository,
            media_repository,
            unit_of_work,
            audit_service,
        )
