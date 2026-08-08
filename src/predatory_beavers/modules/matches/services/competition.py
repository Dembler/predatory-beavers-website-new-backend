from uuid import UUID

from predatory_beavers.api.errors import ConflictError, NotFoundError
from predatory_beavers.db.uow import UnitOfWork
from predatory_beavers.modules.audit.models import AuditAction
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.matches.models import Competition
from predatory_beavers.modules.matches.repository import CompetitionRepository
from predatory_beavers.modules.matches.schemas import (
    CompetitionCreate,
    CompetitionRead,
    CompetitionUpdate,
)


class CompetitionService:
    def __init__(
        self,
        repository: CompetitionRepository,
        unit_of_work: UnitOfWork,
        audit_service: AuditService | None = None,
    ) -> None:
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._audit_service = audit_service

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        season: str | None = None,
        active: bool | None = True,
    ) -> tuple[list[Competition], int]:
        return await self._repository.list(
            page=page,
            page_size=page_size,
            season=season,
            active=active,
        )

    async def get(self, competition_id: UUID) -> Competition:
        competition = await self._repository.get(competition_id)
        if competition is None:
            raise NotFoundError("Competition not found")
        return competition

    async def create(self, payload: CompetitionCreate) -> Competition:
        async with self._unit_of_work:
            await self._ensure_external_identity_available(payload.source, payload.external_id)
            competition = await self._repository.add(Competition(**payload.model_dump()))
            await self._record(
                action=AuditAction.CREATE,
                competition=competition,
                after=CompetitionRead.model_validate(competition).model_dump(mode="json"),
            )
            await self._unit_of_work.commit()
            return competition

    async def update(
        self,
        competition_id: UUID,
        payload: CompetitionUpdate,
    ) -> Competition:
        async with self._unit_of_work:
            competition = await self.get(competition_id)
            before = CompetitionRead.model_validate(competition).model_dump(mode="json")
            changes = payload.model_dump(exclude_unset=True)
            source = changes.get("source", competition.source)
            external_id = changes.get("external_id", competition.external_id)
            self._validate_source_identity(source, external_id)
            await self._ensure_external_identity_available(
                source,
                external_id,
                exclude_id=competition.id,
            )
            for field, value in changes.items():
                setattr(competition, field, value)
            updated = await self._repository.save(competition)
            await self._record(
                action=AuditAction.UPDATE,
                competition=updated,
                before=before,
                after=CompetitionRead.model_validate(updated).model_dump(mode="json"),
            )
            await self._unit_of_work.commit()
            return updated

    async def delete(self, competition_id: UUID) -> None:
        async with self._unit_of_work:
            competition = await self.get(competition_id)
            before = CompetitionRead.model_validate(competition).model_dump(mode="json")
            await self._repository.soft_delete(competition)
            await self._record(
                action=AuditAction.DELETE,
                competition=competition,
                before=before,
            )
            await self._unit_of_work.commit()

    async def _ensure_external_identity_available(
        self,
        source: str,
        external_id: str | None,
        *,
        exclude_id: UUID | None = None,
    ) -> None:
        self._validate_source_identity(source, external_id)
        if external_id is None:
            return
        existing = await self._repository.get_by_external_identity(source, external_id)
        if existing is not None and existing.id != exclude_id:
            raise ConflictError("Competition external identity already exists")

    @staticmethod
    def _validate_source_identity(source: str, external_id: str | None) -> None:
        if source != "manual" and not external_id:
            raise ConflictError("External competitions require an external_id")

    async def _record(
        self,
        *,
        action: AuditAction,
        competition: Competition,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
    ) -> None:
        if self._audit_service is not None:
            await self._audit_service.record(
                action=action,
                entity_type="competition",
                entity_id=competition.id,
                before=before,
                after=after,
            )
