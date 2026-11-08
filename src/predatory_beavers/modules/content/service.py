from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from predatory_beavers.api.concurrency import require_version
from predatory_beavers.api.errors import NotFoundError
from predatory_beavers.api.idempotency import CreationLedger
from predatory_beavers.api.search import text_search
from predatory_beavers.db.uow import UnitOfWork
from predatory_beavers.modules.audit.models import AuditAction
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.content.models import ContentBlock, ContentSection
from predatory_beavers.modules.content.schemas import ContentCreate, ContentRead, ContentUpdate


class ContentService:
    def __init__(self, session: AsyncSession, uow: UnitOfWork, audit: AuditService) -> None:
        self._session = session
        self._creations = CreationLedger(session)
        self._uow = uow
        self._audit = audit

    async def list(
        self,
        *,
        section: ContentSection | None,
        public_only: bool,
        page: int = 1,
        page_size: int | None = None,
        q: str | None = None,
        active: bool | None = None,
    ) -> tuple[list[ContentBlock], int]:
        filters: list[ColumnElement[bool]] = [ContentBlock.is_deleted.is_(False)]
        if section is not None:
            filters.append(ContentBlock.section == section.value)
        if public_only:
            filters.append(ContentBlock.active.is_(True))
        if q and q.strip():
            filters.append(text_search(q, ContentBlock.title, ContentBlock.body))
        if not public_only and active is not None:
            filters.append(ContentBlock.active.is_(active))
        total = await self._session.scalar(select(func.count(ContentBlock.id)).where(*filters))
        statement = (
            select(ContentBlock)
            .where(*filters)
            .order_by(
                ContentBlock.sort_order,
                ContentBlock.created_at,
                ContentBlock.id,
            )
        )
        if page_size is not None:
            statement = statement.offset((page - 1) * page_size).limit(page_size)
        return list(await self._session.scalars(statement)), int(total or 0)

    async def get(self, block_id: UUID) -> ContentBlock:
        block = await self._session.scalar(
            select(ContentBlock).where(
                ContentBlock.id == block_id,
                ContentBlock.is_deleted.is_(False),
            )
        )
        if block is None:
            raise NotFoundError("Content block not found")
        return block

    async def create(self, payload: ContentCreate) -> ContentBlock:
        async with self._uow:
            previous_id = await self._creations.reserve("content", payload)
            if previous_id is not None:
                previous = await self.get(previous_id)
                await self._uow.commit()
                return previous
            block = ContentBlock(**payload.model_dump(mode="json"))
            self._session.add(block)
            await self._session.flush()
            await self._record(AuditAction.CREATE, block, after=self._serialize(block))
            await self._creations.finish("content", payload, block.id)
            await self._uow.commit()
            return block

    async def update(self, block_id: UUID, payload: ContentUpdate) -> ContentBlock:
        async with self._uow:
            block = await self.get(block_id)
            require_version(block.updated_at, payload.expected_updated_at)
            before = self._serialize(block)
            for field, value in payload.model_dump(mode="json", exclude_unset=True).items():
                setattr(block, field, value)
            await self._session.flush()
            await self._record(AuditAction.UPDATE, block, before, self._serialize(block))
            await self._uow.commit()
            return block

    async def delete(self, block_id: UUID, expected_updated_at: datetime | None = None) -> None:
        async with self._uow:
            block = await self.get(block_id)
            require_version(block.updated_at, expected_updated_at)
            before = self._serialize(block)
            block.is_deleted = True
            block.deleted_at = datetime.now(UTC)
            block.active = False
            await self._session.flush()
            await self._record(AuditAction.DELETE, block, before)
            await self._uow.commit()

    @staticmethod
    def _serialize(block: ContentBlock) -> dict[str, object]:
        return ContentRead.model_validate(block).model_dump(mode="json")

    async def _record(
        self,
        action: AuditAction,
        block: ContentBlock,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
    ) -> None:
        await self._audit.record(
            action=action,
            entity_type="content_block",
            entity_id=block.id,
            before=before,
            after=after,
        )
