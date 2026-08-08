from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from predatory_beavers.modules.audit.models import AdminAuditLog, AuditAction


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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
        filters: list[ColumnElement[bool]] = []
        if actor_id is not None:
            filters.append(AdminAuditLog.actor_id == actor_id)
        if action is not None:
            filters.append(AdminAuditLog.action == action)
        if entity_type is not None:
            filters.append(AdminAuditLog.entity_type == entity_type)
        if entity_id is not None:
            filters.append(AdminAuditLog.entity_id == entity_id)
        if created_from is not None:
            filters.append(AdminAuditLog.created_at >= created_from)
        if created_to is not None:
            filters.append(AdminAuditLog.created_at <= created_to)

        total = await self._session.scalar(select(func.count(AdminAuditLog.id)).where(*filters))
        statement = (
            select(AdminAuditLog)
            .where(*filters)
            .order_by(AdminAuditLog.created_at.desc(), AdminAuditLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(await self._session.scalars(statement)), int(total or 0)

    async def add(self, entry: AdminAuditLog) -> AdminAuditLog:
        self._session.add(entry)
        await self._session.flush()
        return entry
