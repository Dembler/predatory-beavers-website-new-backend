from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.modules.audit.repository import AuditRepository
from predatory_beavers.modules.audit.service import AuditService


class AuditProvider(Provider):
    scope = Scope.REQUEST

    @provide
    def repository(self, session: AsyncSession) -> AuditRepository:
        return AuditRepository(session)

    @provide
    def service(self, repository: AuditRepository) -> AuditService:
        return AuditService(repository)
