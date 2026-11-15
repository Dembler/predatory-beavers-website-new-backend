from collections.abc import AsyncIterator

import httpx
from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.db.uow import UnitOfWork
from predatory_beavers.integrations.object_storage.client import ObjectStorage
from predatory_beavers.integrations.object_storage.local import LocalObjectStorage
from predatory_beavers.integrations.object_storage.supabase import SupabaseObjectStorage
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.media.repository import MediaRepository
from predatory_beavers.modules.media.service import MediaService
from predatory_beavers.settings import Settings


class MediaProvider(Provider):
    @provide(scope=Scope.APP)
    async def object_storage(self, settings: Settings) -> AsyncIterator[ObjectStorage]:
        if settings.media_storage_backend == "local":
            yield LocalObjectStorage(settings.media_storage_path)
            return
        assert settings.supabase_url is not None and settings.supabase_secret_key is not None
        async with httpx.AsyncClient(
            timeout=settings.media_storage_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            yield SupabaseObjectStorage(
                settings.supabase_url,
                settings.supabase_secret_key.get_secret_value(),
                settings.supabase_storage_bucket,
                client=client,
            )

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
