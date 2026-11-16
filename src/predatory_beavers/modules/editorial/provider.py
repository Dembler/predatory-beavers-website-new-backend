from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.editorial.service import EditorialService


class EditorialProvider(Provider):
    @provide(scope=Scope.REQUEST)
    def service(self, session: AsyncSession, audit: AuditService) -> EditorialService:
        return EditorialService(session, audit)
