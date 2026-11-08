"""Durable editor creation replay, committed atomically with the created entity."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, PrivateAttr
from sqlalchemy import DateTime, String, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from predatory_beavers.api.errors import (
    AppError,
    ConflictError,
    ForbiddenError,
    ServiceUnavailableError,
)
from predatory_beavers.db.base import Base
from predatory_beavers.modules.audit.context import get_audit_actor


class InvalidCreationKeyError(AppError):
    status_code = 400
    code = "invalid_idempotency_key"
    detail = "Idempotency-Key must be a UUID"


class CreationReplayConflictError(ConflictError):
    code = "idempotency_conflict"
    detail = "This Idempotency-Key was already used for different data"


class CreationReplayStateError(ServiceUnavailableError):
    code = "idempotency_incomplete"
    detail = (
        "The original save cannot be confirmed. Keep this draft and contact an administrator "
        "before creating another record."
    )


class IdempotentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    _creation_key: UUID | None = PrivateAttr(default=None)

    def set_creation_key(self, value: str | None) -> None:
        if value is None:
            return
        try:
            self._creation_key = UUID(value)
        except ValueError:
            raise InvalidCreationKeyError from None


class AdminCreationKey(Base):
    __tablename__ = "admin_creation_keys"

    actor_id: Mapped[UUID] = mapped_column(primary_key=True)
    resource: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[UUID] = mapped_column(primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class CreationLedger:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _identity(resource: str, payload: IdempotentCreate) -> dict[str, object] | None:
        if payload._creation_key is None:
            return None
        actor = get_audit_actor()
        if actor is None or actor.id is None:
            raise ForbiddenError("An authenticated editor is required for creation replay")
        return {"actor_id": actor.id, "resource": resource, "key": payload._creation_key}

    async def reserve(self, resource: str, payload: IdempotentCreate) -> UUID | None:
        identity = self._identity(resource, payload)
        if identity is None:
            return None
        encoded = json.dumps(
            payload.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        request_hash = hashlib.sha256(encoded).hexdigest()
        # A write reservation serializes requests even when both arrive before the
        # first response. The losing request observes the committed entity ID.
        inserted = await self._session.scalar(
            insert(AdminCreationKey)
            .values(**identity, request_hash=request_hash)
            .on_conflict_do_nothing()
            .returning(AdminCreationKey.key)
        )
        if inserted is not None:
            return None
        row = await self._session.scalar(select(AdminCreationKey).filter_by(**identity))
        if row is None or row.entity_id is None:
            raise CreationReplayStateError
        if row.request_hash != request_hash:
            raise CreationReplayConflictError
        return row.entity_id

    async def finish(self, resource: str, payload: IdempotentCreate, entity_id: UUID) -> None:
        identity = self._identity(resource, payload)
        if identity is not None:
            await self._session.execute(
                update(AdminCreationKey).filter_by(**identity).values(entity_id=entity_id)
            )
