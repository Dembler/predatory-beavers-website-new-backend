from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.modules.club.models import MediaAsset, Player, Team


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
        )
        return cast(MediaAsset | None, await self._session.scalar(statement))

    async def list(self, *, page: int, page_size: int) -> tuple[list[MediaAsset], int]:
        filters = [MediaAsset.is_deleted.is_(False)]
        total = await self._session.scalar(select(func.count(MediaAsset.id)).where(*filters))
        assets = await self._session.scalars(
            select(MediaAsset)
            .where(*filters)
            .order_by(MediaAsset.created_at.desc(), MediaAsset.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(assets), int(total or 0)

    async def is_referenced(self, asset_id: UUID) -> bool:
        # Feature packages re-export providers, so defer cross-feature imports to
        # avoid a provider/service import cycle when media is imported on its own.
        from predatory_beavers.modules.achievements.models import Achievement
        from predatory_beavers.modules.matches.models import Match

        statement = select(
            or_(
                select(Player.id)
                .where(
                    Player.photo_asset_id == asset_id,
                    Player.is_deleted.is_(False),
                )
                .exists(),
                select(Team.id)
                .where(
                    Team.logo_asset_id == asset_id,
                    Team.is_deleted.is_(False),
                )
                .exists(),
                select(Achievement.id)
                .where(
                    Achievement.media_asset_id == asset_id,
                    Achievement.is_deleted.is_(False),
                )
                .exists(),
                select(Match.id)
                .where(
                    or_(Match.home_logo_asset_id == asset_id, Match.away_logo_asset_id == asset_id),
                    Match.is_deleted.is_(False),
                )
                .exists(),
            )
        )
        return bool(await self._session.scalar(statement))

    async def save(self, asset: MediaAsset) -> MediaAsset:
        await self._session.flush()
        return asset

    async def soft_delete(self, asset: MediaAsset) -> None:
        asset.is_deleted = True
        asset.deleted_at = datetime.now(UTC)
        await self._session.flush()

    async def add(self, asset: MediaAsset) -> MediaAsset:
        self._session.add(asset)
        await self._session.flush()
        return asset
