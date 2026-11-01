from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.db.uow import UnitOfWork
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.content.service import ContentService


class ContentProvider(Provider):
    @provide(scope=Scope.REQUEST)
    def service(
        self,
        session: AsyncSession,
        unit_of_work: UnitOfWork,
        audit_service: AuditService,
    ) -> ContentService:
        return ContentService(session, unit_of_work, audit_service)
