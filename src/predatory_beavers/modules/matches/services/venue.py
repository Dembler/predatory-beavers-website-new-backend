from uuid import UUID

from predatory_beavers.api.errors import ConflictError, NotFoundError
from predatory_beavers.db.uow import UnitOfWork
from predatory_beavers.modules.audit.models import AuditAction
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.matches.models import Venue
from predatory_beavers.modules.matches.repository import VenueRepository
from predatory_beavers.modules.matches.schemas import VenueCreate, VenueRead, VenueUpdate


class VenueService:
    def __init__(
        self,
        repository: VenueRepository,
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
        active: bool | None = True,
    ) -> tuple[list[Venue], int]:
        return await self._repository.list(page=page, page_size=page_size, active=active)

    async def get(self, venue_id: UUID) -> Venue:
        venue = await self._repository.get(venue_id)
        if venue is None:
            raise NotFoundError("Venue not found")
        return venue

    async def create(self, payload: VenueCreate) -> Venue:
        async with self._unit_of_work:
            await self._ensure_external_identity_available(payload.source, payload.external_id)
            venue = await self._repository.add(Venue(**payload.model_dump()))
            await self._record(
                action=AuditAction.CREATE,
                venue=venue,
                after=VenueRead.model_validate(venue).model_dump(mode="json"),
            )
            await self._unit_of_work.commit()
            return venue

    async def update(self, venue_id: UUID, payload: VenueUpdate) -> Venue:
        async with self._unit_of_work:
            venue = await self.get(venue_id)
            before = VenueRead.model_validate(venue).model_dump(mode="json")
            changes = payload.model_dump(exclude_unset=True)
            latitude = changes.get("latitude", venue.latitude)
            longitude = changes.get("longitude", venue.longitude)
            if (latitude is None) != (longitude is None):
                raise ConflictError("Latitude and longitude must be provided together")
            source = changes.get("source", venue.source)
            external_id = changes.get("external_id", venue.external_id)
            await self._ensure_external_identity_available(
                source,
                external_id,
                exclude_id=venue.id,
            )
            for field, value in changes.items():
                setattr(venue, field, value)
            updated = await self._repository.save(venue)
            await self._record(
                action=AuditAction.UPDATE,
                venue=updated,
                before=before,
                after=VenueRead.model_validate(updated).model_dump(mode="json"),
            )
            await self._unit_of_work.commit()
            return updated

    async def delete(self, venue_id: UUID) -> None:
        async with self._unit_of_work:
            venue = await self.get(venue_id)
            before = VenueRead.model_validate(venue).model_dump(mode="json")
            await self._repository.soft_delete(venue)
            await self._record(action=AuditAction.DELETE, venue=venue, before=before)
            await self._unit_of_work.commit()

    async def _record(
        self,
        *,
        action: AuditAction,
        venue: Venue,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
    ) -> None:
        if self._audit_service is not None:
            await self._audit_service.record(
                action=action,
                entity_type="venue",
                entity_id=venue.id,
                before=before,
                after=after,
            )

    async def _ensure_external_identity_available(
        self,
        source: str,
        external_id: str | None,
        *,
        exclude_id: UUID | None = None,
    ) -> None:
        if source != "manual" and not external_id:
            raise ConflictError("External venues require an external_id")
        if external_id is None:
            return
        existing = await self._repository.get_by_external_identity(source, external_id)
        if existing is not None and existing.id != exclude_id:
            raise ConflictError("Venue external identity already exists")
