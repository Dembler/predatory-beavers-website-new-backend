from datetime import datetime
from uuid import UUID

from predatory_beavers.api.errors import ConflictError
from predatory_beavers.modules.audit.context import AuditActor, get_audit_actor
from predatory_beavers.modules.audit.models import AdminAuditLog, AuditAction
from predatory_beavers.modules.audit.repository import AuditRepository
from predatory_beavers.observability.logging import get_request_id


class AuditService:
    def __init__(self, repository: AuditRepository) -> None:
        self._repository = repository

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        actor_id: UUID | None = None,
        action: AuditAction | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> tuple[list[AdminAuditLog], int]:
        for boundary in (created_from, created_to):
            if boundary is not None and boundary.utcoffset() is None:
                raise ConflictError("Audit date filters must include a timezone")
        if created_from is not None and created_to is not None and created_from > created_to:
            raise ConflictError("created_from must not be later than created_to")
        return await self._repository.list(
            page=page,
            page_size=page_size,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            created_from=created_from,
            created_to=created_to,
        )

    async def record(
        self,
        *,
        action: AuditAction,
        entity_type: str,
        entity_id: UUID | str,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
    ) -> AdminAuditLog:
        actor = get_audit_actor() or AuditActor(id=None, username="system", role=None)
        return await self._repository.add(
            AdminAuditLog(
                actor_id=actor.id,
                actor_username=actor.username,
                actor_role=actor.role,
                action=action,
                entity_type=entity_type,
                entity_id=str(entity_id),
                before=before,
                after=after,
                request_id=get_request_id(),
            )
        )
