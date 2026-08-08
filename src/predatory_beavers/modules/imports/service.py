import logging
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from predatory_beavers.api.errors import AppError, ConflictError, NotFoundError
from predatory_beavers.db.uow import UnitOfWork
from predatory_beavers.integrations.asb.client import AsbClient
from predatory_beavers.integrations.asb.errors import AsbInvalidResponseError
from predatory_beavers.modules.audit.context import AuditActor, get_audit_actor
from predatory_beavers.modules.audit.models import AuditAction
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.club.repository import TeamRepository
from predatory_beavers.modules.imports.asb_applier import AsbImportApplier
from predatory_beavers.modules.imports.models import ImportJob, ImportStatus
from predatory_beavers.modules.imports.repository import ImportJobRepository
from predatory_beavers.modules.imports.schemas import AsbImportRequest
from predatory_beavers.modules.matches.repository import (
    CompetitionRepository,
    MatchRepository,
    VenueRepository,
)
from predatory_beavers.modules.standings.repository import StandingsRepository
from predatory_beavers.observability.logging import get_request_id

logger = logging.getLogger(__name__)


class ImportService:
    """Coordinates import-job state, remote fetching and one atomic data application."""

    def __init__(
        self,
        job_repository: ImportJobRepository,
        team_repository: TeamRepository,
        competition_repository: CompetitionRepository,
        venue_repository: VenueRepository,
        match_repository: MatchRepository,
        standings_repository: StandingsRepository,
        asb_client: AsbClient,
        audit_service: AuditService,
        unit_of_work: UnitOfWork,
    ) -> None:
        self._job_repository = job_repository
        self._asb_client = asb_client
        self._audit_service = audit_service
        self._unit_of_work = unit_of_work
        self._applier = AsbImportApplier(
            team_repository=team_repository,
            competition_repository=competition_repository,
            venue_repository=venue_repository,
            match_repository=match_repository,
            standings_repository=standings_repository,
        )

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        status: ImportStatus | None = None,
        team: str | None = None,
    ) -> tuple[list[ImportJob], int]:
        return await self._job_repository.list(
            page=page,
            page_size=page_size,
            status=status,
            team=team,
        )

    async def get(self, job_id: UUID) -> ImportJob:
        job = await self._job_repository.get(job_id)
        if job is None:
            raise NotFoundError("Import job not found")
        return job

    async def import_asb(self, payload: AsbImportRequest) -> ImportJob:
        job = await self._create_job(payload)
        try:
            async with self._unit_of_work:
                job.status = ImportStatus.RUNNING
                job.started_at = datetime.now(UTC)
                await self._job_repository.save(job)
                await self._unit_of_work.commit()

            bundle = await self._asb_client.fetch_import(
                competition_id=payload.competition_id,
                standings_competition_id=payload.resolved_standings_competition_id,
                external_team_id=payload.external_team_id,
            )

            async with self._unit_of_work:
                result = await self._applier.apply(payload, bundle)
                job = await self.get(job.id)
                job.status = ImportStatus.COMPLETED
                job.result = result
                job.completed_at = datetime.now(UTC)
                await self._job_repository.save(job)
                await self._audit_service.record(
                    action=AuditAction.IMPORT,
                    entity_type="import_job",
                    entity_id=job.id,
                    after={
                        "provider": job.provider,
                        "team": job.team_slug,
                        "competition_id": job.competition_external_id,
                        "standings_competition_id": job.standings_external_id,
                        "external_team_id": job.external_team_id,
                        "season": job.season,
                        "result": result,
                    },
                )
                await self._unit_of_work.commit()
                return job
        except AppError as exc:
            await self._mark_failed(job.id, code=exc.code, detail=exc.detail)
            raise
        except ValidationError as exc:
            error = AsbInvalidResponseError
            await self._mark_failed(job.id, code=error.code, detail=error.detail)
            raise error from exc
        except Exception as exc:
            logger.exception("Unexpected ASB import failure", exc_info=exc)
            await self._mark_failed(
                job.id,
                code="import_internal_error",
                detail="Unexpected import failure",
            )
            raise

    async def _create_job(self, payload: AsbImportRequest) -> ImportJob:
        actor = get_audit_actor() or AuditActor(id=None, username="system", role=None)
        job = ImportJob(
            provider="asb",
            status=ImportStatus.PENDING,
            team_slug=payload.team,
            competition_external_id=payload.competition_id,
            standings_external_id=payload.resolved_standings_competition_id,
            external_team_id=payload.external_team_id,
            season=payload.season,
            request_data=payload.model_dump(mode="json"),
            actor_id=actor.id,
            actor_username=actor.username,
            request_id=get_request_id(),
        )
        try:
            async with self._unit_of_work:
                created = await self._job_repository.add(job)
                await self._unit_of_work.commit()
                return created
        except IntegrityError as exc:
            raise ConflictError("An ASB import for this target is already running") from exc

    async def _mark_failed(self, job_id: UUID, *, code: str, detail: str) -> None:
        async with self._unit_of_work:
            job = await self._job_repository.get(job_id)
            if job is None:
                logger.error("Import job disappeared before failure could be recorded")
                return
            job.status = ImportStatus.FAILED
            job.error_code = code[:64]
            job.error_detail = detail[:2000]
            job.completed_at = datetime.now(UTC)
            await self._job_repository.save(job)
            await self._unit_of_work.commit()
