from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.db.uow import UnitOfWork
from predatory_beavers.integrations.object_storage.client import ObjectStorage
from predatory_beavers.integrations.object_storage.local import LocalObjectStorage
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.media.repository import MediaRepository
from predatory_beavers.modules.media.service import MediaService
from predatory_beavers.settings import Settings


class MediaProvider(Provider):
    @provide(scope=Scope.APP)
    def object_storage(self, settings: Settings) -> ObjectStorage:
        return LocalObjectStorage(settings.media_storage_path)

    @provide(scope=Scope.REQUEST)
    def repository(self, session: AsyncSession) -> MediaRepository:
        return MediaRepository(session)

    @provide(scope=Scope.REQUEST)
    def service(
        self,
        repository: MediaRepository,
        storage: ObjectStorage,
        unit_of_work: UnitOfWork,
        settings: Settings,
        audit_service: AuditService,
    ) -> MediaService:
        return MediaService(repository, storage, unit_of_work, settings, audit_service)
