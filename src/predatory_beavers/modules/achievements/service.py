from uuid import UUID

from predatory_beavers.api.errors import NotFoundError
from predatory_beavers.db.uow import UnitOfWork
from predatory_beavers.modules.achievements.models import Achievement
from predatory_beavers.modules.achievements.repository import AchievementRepository
from predatory_beavers.modules.achievements.schemas import (
    AchievementCreate,
    AchievementRead,
    AchievementUpdate,
)
from predatory_beavers.modules.audit.models import AuditAction
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.club.models import TeamCategory
from predatory_beavers.modules.club.repository import TeamRepository
from predatory_beavers.modules.media.repository import MediaRepository


class AchievementService:
    def __init__(
        self,
        repository: AchievementRepository,
        team_repository: TeamRepository,
        media_repository: MediaRepository,
        unit_of_work: UnitOfWork,
        audit_service: AuditService | None = None,
    ) -> None:
        self._repository = repository
        self._team_repository = team_repository
        self._media_repository = media_repository
        self._unit_of_work = unit_of_work
        self._audit_service = audit_service

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        team: str | None = None,
        category: TeamCategory | None = None,
        active: bool | None = True,
        public_only: bool = True,
    ) -> tuple[list[Achievement], int]:
        return await self._repository.list(
            page=page,
            page_size=page_size,
            team=team,
            category=category,
            active=active,
            public_only=public_only,
        )

    async def get(self, achievement_id: UUID) -> Achievement:
        achievement = await self._repository.get(achievement_id)
        if achievement is None:
            raise NotFoundError("Achievement not found")
        return achievement

    async def get_public(self, achievement_id: UUID) -> Achievement:
        achievement = await self._repository.get(achievement_id, public_only=True)
        if achievement is None:
            raise NotFoundError("Achievement not found")
        return achievement

    async def create(self, payload: AchievementCreate) -> Achievement:
        async with self._unit_of_work:
            await self._require_references(payload.team_id, payload.media_asset_id)
            achievement = await self._repository.add(Achievement(**payload.model_dump()))
            await self._record(
                action=AuditAction.CREATE,
                achievement=achievement,
                after=AchievementRead.model_validate(achievement).model_dump(mode="json"),
            )
            await self._unit_of_work.commit()
            return achievement

    async def update(
        self,
        achievement_id: UUID,
        payload: AchievementUpdate,
    ) -> Achievement:
        async with self._unit_of_work:
            achievement = await self.get(achievement_id)
            before = AchievementRead.model_validate(achievement).model_dump(mode="json")
            changes = payload.model_dump(exclude_unset=True)
            team_id = changes.get("team_id", achievement.team_id)
            media_asset_id = changes.get("media_asset_id", achievement.media_asset_id)
            if team_id != achievement.team_id or media_asset_id != achievement.media_asset_id:
                await self._require_references(team_id, media_asset_id)
            for field, value in changes.items():
                setattr(achievement, field, value)
            updated = await self._repository.save(achievement)
            await self._record(
                action=AuditAction.UPDATE,
                achievement=updated,
                before=before,
                after=AchievementRead.model_validate(updated).model_dump(mode="json"),
            )
            await self._unit_of_work.commit()
            return updated

    async def delete(self, achievement_id: UUID) -> None:
        async with self._unit_of_work:
            achievement = await self.get(achievement_id)
            before = AchievementRead.model_validate(achievement).model_dump(mode="json")
            await self._repository.soft_delete(achievement)
            await self._record(
                action=AuditAction.DELETE,
                achievement=achievement,
                before=before,
            )
            await self._unit_of_work.commit()

    async def _require_references(self, team_id: UUID, media_asset_id: UUID) -> None:
        if await self._team_repository.get(team_id) is None:
            raise NotFoundError("Team not found")
        if await self._media_repository.get(media_asset_id) is None:
            raise NotFoundError("Media asset not found")

    async def _record(
        self,
        *,
        action: AuditAction,
        achievement: Achievement,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
    ) -> None:
        if self._audit_service is not None:
            await self._audit_service.record(
                action=action,
                entity_type="achievement",
                entity_id=achievement.id,
                before=before,
                after=after,
            )
