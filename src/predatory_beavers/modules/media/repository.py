import builtins
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from predatory_beavers.api.search import text_search
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

    async def list(
        self, *, page: int, page_size: int, q: str | None = None
    ) -> tuple[list[MediaAsset], int]:
        filters: list[ColumnElement[bool]] = [MediaAsset.is_deleted.is_(False)]
        if q and q.strip():
            filters.append(text_search(q, MediaAsset.alt_text))
        total = await self._session.scalar(select(func.count(MediaAsset.id)).where(*filters))
        assets = await self._session.scalars(
            select(MediaAsset)
            .where(*filters)
            .order_by(MediaAsset.created_at.desc(), MediaAsset.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(assets), int(total or 0)

    async def usage(self, asset_id: UUID) -> builtins.list[dict[str, object]]:
        from predatory_beavers.modules.achievements.models import Achievement
        from predatory_beavers.modules.matches.models import Match

        result: builtins.list[dict[str, object]] = []
        references = (
            (Player, Player.photo_asset_id == asset_id, "player", "players", "photo_asset_id"),
            (Team, Team.logo_asset_id == asset_id, "team", "teams", "logo_asset_id"),
            (
                Achievement,
                Achievement.media_asset_id == asset_id,
                "achievement",
                "achievements",
                "media_asset_id",
            ),
            (
                Match,
                or_(Match.home_logo_asset_id == asset_id, Match.away_logo_asset_id == asset_id),
                "match",
                "matches",
                "logo_asset_id",
            ),
        )
        for model, condition, kind, tab, field in references:
            rows = await self._session.scalars(
                select(model).where(condition, model.is_deleted.is_(False))
            )
            for item in rows:
                row = cast(Player | Team | Achievement | Match, item)
                if isinstance(row, Player):
                    title, public_url = row.full_name, f"/players/local/{row.id}"
                elif isinstance(row, Team):
                    title, public_url = row.name, f"/teams/{row.category.value}"
                elif isinstance(row, Achievement):
                    title, public_url = row.title, "/club"
                else:
                    title, public_url = (
                        f"{row.home_team_name} \u2014 {row.away_team_name}",
                        "/matches",
                    )
                result.append(
                    {
                        "entity_type": kind,
                        "entity_id": str(row.id),
                        "title": title,
                        "field": field,
                        "admin_url": f"/admin?tab={tab}&edit={row.id}",
                        "public_url": public_url,
                    }
                )
        return result

    async def is_publicly_referenced(self, asset_id: UUID) -> bool:
        from predatory_beavers.modules.achievements.models import Achievement
        from predatory_beavers.modules.matches.models import Match
        from predatory_beavers.modules.matches.visibility import public_match_conditions

        team_public = (Team.active.is_(True), Team.is_deleted.is_(False))
        return bool(
            await self._session.scalar(
                select(
                    or_(
                        select(Player.id)
                        .join(Player.team)
                        .where(
                            Player.photo_asset_id == asset_id,
                            Player.active.is_(True),
                            Player.is_deleted.is_(False),
                            *team_public,
                        )
                        .exists(),
                        select(Team.id)
                        .where(Team.logo_asset_id == asset_id, *team_public)
                        .exists(),
                        select(Achievement.id)
                        .join(Achievement.team)
                        .where(
                            Achievement.media_asset_id == asset_id,
                            Achievement.active.is_(True),
                            Achievement.is_deleted.is_(False),
                            *team_public,
                        )
                        .exists(),
                        select(Match.id)
                        .join(Match.team)
                        .join(Match.competition)
                        .where(
                            or_(
                                Match.home_logo_asset_id == asset_id,
                                Match.away_logo_asset_id == asset_id,
                            ),
                            *public_match_conditions(),
                        )
                        .exists(),
                    )
                )
            )
        )

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
