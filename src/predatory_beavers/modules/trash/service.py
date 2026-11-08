from typing import cast
from uuid import UUID

from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, Mapper
from sqlalchemy.sql.elements import ColumnElement

from predatory_beavers.api.concurrency import require_version
from predatory_beavers.api.errors import ConflictError, NotFoundError
from predatory_beavers.api.search import text_search
from predatory_beavers.db.uow import UnitOfWork
from predatory_beavers.integrations.object_storage.client import ObjectStorage
from predatory_beavers.integrations.object_storage.local import ObjectNotFoundError
from predatory_beavers.modules.achievements.models import Achievement
from predatory_beavers.modules.audit.models import AuditAction
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.club.models import MediaAsset, Player, Team
from predatory_beavers.modules.content.models import ContentBlock, ContentSection
from predatory_beavers.modules.matches.models import Competition, Match, Venue
from predatory_beavers.modules.media.repository import MediaRepository
from predatory_beavers.modules.trash.schemas import (
    RestoreRequest,
    RestoreResult,
    TrashEntity,
    TrashItem,
)

type TrashRecord = Player | Achievement | ContentBlock | Competition | Venue | Match | MediaAsset

MODELS: dict[TrashEntity, type[TrashRecord]] = {
    TrashEntity.PLAYERS: Player,
    TrashEntity.ACHIEVEMENTS: Achievement,
    TrashEntity.CONTENT: ContentBlock,
    TrashEntity.COMPETITIONS: Competition,
    TrashEntity.VENUES: Venue,
    TrashEntity.MATCHES: Match,
    TrashEntity.MEDIA: MediaAsset,
}


class RestoreDependencyError(ConflictError):
    code = "restore_dependency"


class TrashService:
    def __init__(
        self,
        session: AsyncSession,
        unit_of_work: UnitOfWork,
        audit: AuditService,
        storage: ObjectStorage,
    ) -> None:
        self._session = session
        self._uow = unit_of_work
        self._audit = audit
        self._storage = storage

    async def list_deleted(
        self,
        entity: TrashEntity,
        *,
        page: int,
        page_size: int,
        q: str | None = None,
        section: ContentSection | None = None,
    ) -> tuple[list[TrashItem], int]:
        model = MODELS[entity]
        filters: list[ColumnElement[bool]] = [model.is_deleted.is_(True)]
        if section is not None:
            if entity != TrashEntity.CONTENT:
                raise ConflictError("section is only valid for content")
            filters.append(ContentBlock.section == section)
        if q and q.strip():
            columns: tuple[
                InstrumentedAttribute[str] | InstrumentedAttribute[str | None] | ColumnElement[str],
                ...,
            ]
            if model is Player:
                columns = (Player.full_name, Player.position, Player.fact)
            elif model is Match:
                columns = (
                    Match.home_team_name,
                    Match.away_team_name,
                    Match.home_team_name + " — " + Match.away_team_name,
                    Match.notes,
                )
            elif model is MediaAsset:
                columns = (MediaAsset.alt_text,)
            elif model is ContentBlock:
                columns = (ContentBlock.title, ContentBlock.body)
            elif model is Achievement:
                columns = (Achievement.title,)
            elif model is Competition:
                columns = (Competition.name, Competition.season)
            else:
                columns = (Venue.name, Venue.address)
            filters.append(text_search(q, *columns))
        total = await self._session.scalar(select(func.count()).select_from(model).where(*filters))
        records = await self._session.scalars(
            select(model)
            .where(*filters)
            .order_by(model.deleted_at.desc(), model.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return [self._item(cast(TrashRecord, record)) for record in records], int(total or 0)

    @staticmethod
    def _item(record: TrashRecord) -> TrashItem:
        detail = None
        if isinstance(record, Player):
            title, detail = record.full_name, record.position
        elif isinstance(record, (Achievement, ContentBlock)):
            title = record.title
            if isinstance(record, ContentBlock):
                detail = record.section
        elif isinstance(record, Match):
            title = f"{record.home_team_name} — {record.away_team_name}"
            detail = record.starts_at.isoformat()
        elif isinstance(record, MediaAsset):
            title = record.alt_text or f"Фото {str(record.id)[:8]}"
            detail = f"{record.width or '?'} × {record.height or '?'} · {record.size} байт"
        else:
            title = record.name
            detail = record.season if isinstance(record, Competition) else record.address
        return TrashItem(
            id=record.id,
            title=title,
            detail=detail,
            deleted_at=record.deleted_at,
            updated_at=record.updated_at,
        )

    async def restore(
        self, entity: TrashEntity, record_id: UUID, payload: RestoreRequest
    ) -> RestoreResult:
        async with self._uow:
            record = cast(TrashRecord | None, await self._session.get(MODELS[entity], record_id))
            if record is None:
                raise NotFoundError("Deleted record not found")
            # A lost successful response can be retried without hiding a record
            # somebody has since published or generating a second audit entry.
            if not record.is_deleted:
                hidden = (
                    not record.legacy_public
                    and not await MediaRepository(self._session).is_publicly_referenced(record.id)
                    if isinstance(record, MediaAsset)
                    else not record.active
                )
                if not hidden:
                    raise ConflictError("Record is already published; refresh the list")
                return self._result(entity, record.id, restored=False)
            require_version(record.updated_at, payload.expected_updated_at)
            await self._require_dependencies(record)
            before = self._snapshot(record)
            record.is_deleted = False
            record.deleted_at = None
            if isinstance(record, MediaAsset):
                record.legacy_public = False
            else:
                record.active = False
            if isinstance(record, Match):
                record.featured = False
            if isinstance(record, Competition):
                # A recovered competition requires an explicit publication
                # decision for dependent matches. A venue only controls the
                # public venue details; its matches stay published throughout.
                children = await self._session.scalars(
                    select(Match).where(Match.competition_id == record.id, Match.active.is_(True))
                )
                for child in children:
                    child.active = False
                    child.featured = False
            await self._session.flush()
            await self._audit.record(
                action=AuditAction.UPDATE,
                entity_type="match"
                if isinstance(record, Match)
                else record.__tablename__.removesuffix("s"),
                entity_id=record.id,
                before=before,
                after={**self._snapshot(record), "operation": "restore"},
            )
            await self._uow.commit()
            return self._result(entity, record.id, restored=True)

    @staticmethod
    def _snapshot(record: TrashRecord) -> dict[str, object]:
        # Only persisted scalar columns: no lazy relationships or binary data.
        return {
            column.key: value.isoformat()
            if hasattr(value, "isoformat")
            else str(value)
            if isinstance(value, UUID)
            else value
            for column in cast(Mapper[TrashRecord], inspect(type(record))).columns
            for value in [getattr(record, column.key)]
        }

    @staticmethod
    def _result(entity: TrashEntity, record_id: UUID, *, restored: bool) -> RestoreResult:
        private = entity == TrashEntity.MEDIA
        return RestoreResult(
            id=record_id,
            entity=entity,
            restored=restored,
            publication_state="private" if private else "hidden",
            message=(
                "Фото восстановлено в библиотеку. "
                "Оно станет публичным после публикации связанной записи."
                if private
                else "Запись восстановлена и скрыта с сайта. "
                "Проверьте данные и включите публикацию отдельно."
            ),
        )

    async def _require_dependencies(self, record: TrashRecord) -> None:
        if isinstance(record, (Player, Achievement, Match)):
            team = await self._session.get(Team, record.team_id)
            if team is None or team.is_deleted:
                raise RestoreDependencyError("Сначала восстановите связанную команду.")
        if isinstance(record, Match):
            competition = await self._session.get(Competition, record.competition_id)
            if competition is None or competition.is_deleted:
                raise RestoreDependencyError("Сначала восстановите связанный турнир.")
            if record.venue_id is not None:
                venue = await self._session.get(Venue, record.venue_id)
                if venue is None or venue.is_deleted:
                    raise RestoreDependencyError(
                        "Сначала восстановите связанный зал или смените его в записи."
                    )
        media_ids: list[UUID | None] = []
        if isinstance(record, Player):
            media_ids = [record.photo_asset_id]
        elif isinstance(record, Achievement):
            media_ids = [record.media_asset_id]
        elif isinstance(record, Match):
            media_ids = [record.home_logo_asset_id, record.away_logo_asset_id]
        elif isinstance(record, MediaAsset):
            media_ids = [record.id]
        for media_id in media_ids:
            if media_id is None:
                continue
            asset = await self._session.get(MediaAsset, media_id)
            if asset is None or (asset.is_deleted and not isinstance(record, MediaAsset)):
                raise RestoreDependencyError(
                    "Сначала восстановите связанную фотографию в разделе «Фото»."
                )
            try:
                await self._storage.read(key=asset.storage_key)
            except ObjectNotFoundError as exc:
                raise RestoreDependencyError(
                    "Файл фотографии отсутствует в хранилище. Восстановите файл из резервной копии."
                ) from exc
