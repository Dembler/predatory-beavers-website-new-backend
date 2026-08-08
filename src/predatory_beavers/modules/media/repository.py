from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.modules.club.models import MediaAsset


class MediaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, asset_id: UUID) -> MediaAsset | None:
        statement = select(MediaAsset).where(
            MediaAsset.id == asset_id,
            MediaAsset.is_deleted.is_(False),
        )
        return cast(MediaAsset | None, await self._session.scalar(statement))

    async def get_by_checksum(self, checksum: str) -> MediaAsset | None:
        statement = select(MediaAsset).where(
            MediaAsset.checksum == checksum,
            MediaAsset.is_deleted.is_(False),
        )
        return cast(MediaAsset | None, await self._session.scalar(statement))

    async def add(self, asset: MediaAsset) -> MediaAsset:
        self._session.add(asset)
        await self._session.flush()
        return asset
