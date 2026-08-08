from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from predatory_beavers.modules.audit.models import AuditAction


class AuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    actor_id: UUID | None
    actor_username: str
    actor_role: str | None
    action: AuditAction
    entity_type: str
    entity_id: str
    before: dict[str, object] | None
    after: dict[str, object] | None
    request_id: str | None
    created_at: datetime
