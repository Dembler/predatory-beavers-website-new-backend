from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from predatory_beavers.modules.imports.models import ImportJob, ImportStatus


class ImportJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        status: ImportStatus | None = None,
        team: str | None = None,
    ) -> tuple[list[ImportJob], int]:
        filters: list[ColumnElement[bool]] = []
        if status is not None:
            filters.append(ImportJob.status == status)
        if team is not None:
            filters.append(ImportJob.team_slug == team)
        total = await self._session.scalar(select(func.count(ImportJob.id)).where(*filters))
        statement = (
            select(ImportJob)
            .where(*filters)
            .order_by(ImportJob.created_at.desc(), ImportJob.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(await self._session.scalars(statement)), int(total or 0)

    async def get(self, job_id: UUID) -> ImportJob | None:
        return cast(
            ImportJob | None,
            await self._session.scalar(select(ImportJob).where(ImportJob.id == job_id)),
        )

    async def add(self, job: ImportJob) -> ImportJob:
        self._session.add(job)
        await self._session.flush()
        return job

    async def save(self, job: ImportJob) -> ImportJob:
        await self._session.flush()
        return job
