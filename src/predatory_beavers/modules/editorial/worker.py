"""Separate durable editorial worker, one paid attempt per logical job."""

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import case, or_, select, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from predatory_beavers.modules.audit.models import AdminAuditLog, AuditAction

# Standalone workers do not import API/main, so register the audit actor FK explicitly.
from predatory_beavers.modules.auth import models as auth_models  # noqa: F401
from predatory_beavers.modules.editorial.facts import FactSheet
from predatory_beavers.modules.editorial.generation import PROMPT_VERSION, generate_column
from predatory_beavers.modules.editorial.models import (
    Article,
    ArticleCheck,
    ArticleRevision,
    EditorialJob,
    EditorialLease,
)
from predatory_beavers.modules.mascot.config import MascotConfig
from predatory_beavers.modules.mascot.gateway import GatewayError, LLMGateway

logger = logging.getLogger(__name__)


class EditorialWorker:
    def __init__(
        self,
        config: MascotConfig,
        sessions: async_sessionmaker[AsyncSession],
        gateway: LLMGateway,
    ) -> None:
        self.config = config
        self.sessions = sessions
        self.gateway = gateway

    async def recover_stale(self, session: AsyncSession) -> None:
        now = datetime.now(UTC)
        rows = list(
            await session.scalars(
                select(EditorialJob).where(
                    EditorialJob.status.in_(("running", "cancelling", "queued")),
                    or_(EditorialJob.deadline_at <= now, EditorialJob.lease_until <= now),
                )
            )
        )
        for job in rows:
            started = job.provider_attempt_state == "started"
            terminal = started or job.deadline_at <= now or job.cancel_requested_at is not None
            await session.execute(
                update(EditorialJob)
                .where(
                    EditorialJob.id == job.id,
                    EditorialJob.status == job.status,
                    EditorialJob.claim_token == job.claim_token,
                )
                .values(
                    status=("cancelled" if job.cancel_requested_at else "interrupted")
                    if terminal
                    else "queued",
                    error_code="provider_outcome_unknown"
                    if started
                    else ("deadline_exceeded" if job.deadline_at <= now else None),
                    provider_attempt_state="unknown" if started else job.provider_attempt_state,
                    finished_at=now if terminal else None,
                    claim_token=None,
                    lease_until=None,
                )
            )
        await session.commit()

    async def claim_next(self, session: AsyncSession) -> EditorialJob | None:
        await self.recover_stale(session)
        now = datetime.now(UTC)
        insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
        await session.execute(
            insert(EditorialLease)
            .values(
                id=1,
                lease_until=now - timedelta(seconds=1),
            )
            .on_conflict_do_nothing()
        )
        token = uuid4()
        locked = await session.scalar(
            update(EditorialLease)
            .where(
                EditorialLease.id == 1,
                EditorialLease.lease_until <= now,
            )
            .values(token=token, lease_until=now + timedelta(seconds=125))
            .returning(EditorialLease.id)
        )
        if locked is None:
            await session.rollback()
            return None
        job_id = await session.scalar(
            select(EditorialJob.id)
            .where(
                EditorialJob.status == "queued",
                EditorialJob.deadline_at > now,
                EditorialJob.provider_attempt_state.is_(None),
            )
            .order_by(EditorialJob.created_at)
            .limit(1)
        )
        if job_id is None:
            await session.rollback()
            return None
        claimed = await session.scalar(
            update(EditorialJob)
            .where(
                EditorialJob.id == job_id,
                EditorialJob.status == "queued",
            )
            .values(
                status="running",
                claim_token=token,
                started_at=now,
                lease_until=now + timedelta(seconds=125),
            )
            .returning(EditorialJob.id)
        )
        if claimed is None:
            await session.rollback()
            return None
        await session.commit()
        return await session.get(EditorialJob, claimed, populate_existing=True)

    async def _cancelled(self, job_id: UUID, token: UUID) -> bool:
        async with self.sessions() as session:
            job = await session.get(EditorialJob, job_id)
            return bool(
                job is None
                or job.claim_token != token
                or job.status != "running"
                or job.cancel_requested_at is not None
                or job.deadline_at <= datetime.now(UTC)
            )

    async def process_job(self, job_id: UUID, token: UUID) -> None:
        async with self.sessions() as session:
            owned = await session.scalar(
                update(EditorialJob)
                .where(
                    EditorialJob.id == job_id,
                    EditorialJob.claim_token == token,
                    EditorialJob.status.in_(("running", "cancelling")),
                    EditorialJob.provider_attempt_state.is_(None),
                )
                .values(claim_token=token)
                .returning(EditorialJob.id)
            )
            if owned is None:
                await session.rollback()
                return
            job = await session.get(EditorialJob, job_id, populate_existing=True)
            if job is None or job.claim_token != token:
                return
            if job.status == "cancelling" or job.cancel_requested_at is not None:
                await session.execute(
                    update(EditorialJob)
                    .where(
                        EditorialJob.id == job_id,
                        EditorialJob.claim_token == token,
                        EditorialJob.status.in_(("running", "cancelling")),
                        EditorialJob.provider_attempt_state.is_(None),
                    )
                    .values(status="cancelled", finished_at=datetime.now(UTC), lease_until=None)
                )
                await self._release(session, token)
                await session.commit()
                return
            if job.status != "running" or job.provider_attempt_state is not None:
                return
            if job.deadline_at <= datetime.now(UTC):
                await session.execute(
                    update(EditorialJob)
                    .where(
                        EditorialJob.id == job_id,
                        EditorialJob.claim_token == token,
                        EditorialJob.provider_attempt_state.is_(None),
                    )
                    .values(
                        status="failed",
                        error_code="deadline_exceeded",
                        finished_at=datetime.now(UTC),
                        lease_until=None,
                    )
                )
                await self._release(session, token)
                await session.commit()
                return
            sheet = FactSheet.model_validate(job.fact_sheet)
            brief, deadline = job.brief, job.deadline_at
            attempted = await session.scalar(
                update(EditorialJob)
                .where(
                    EditorialJob.id == job_id,
                    EditorialJob.claim_token == token,
                    EditorialJob.status == "running",
                    EditorialJob.cancel_requested_at.is_(None),
                    EditorialJob.provider_attempt_state.is_(None),
                )
                .values(provider_attempt_state="started")
                .returning(EditorialJob.id)
            )
            if attempted is None:
                await session.rollback()
                return
            # The provider intent is durable immediately before the one call;
            # cancellation between claim and this point has a known zero calls.
            await session.commit()
        # No database transaction or connection is held while awaiting the provider.
        try:
            generated = await generate_column(
                sheet,
                brief,
                self.gateway,
                deadline=deadline,
                cancel_check=lambda: self._cancelled(job_id, token),
            )
        except GatewayError as exc:
            await self._failed(job_id, token, exc.code)
            return
        except asyncio.CancelledError:
            # Started provider state remains durable. Recovery records unknown,
            # and never repeats the potentially billed call.
            raise
        except Exception:
            logger.error("Editorial generation failed", extra={"job_id": str(job_id)})
            await self._failed(job_id, token, "generation_failed")
            return
        async with self.sessions() as session:
            owned = await session.scalar(
                update(EditorialJob)
                .where(
                    EditorialJob.id == job_id,
                    EditorialJob.claim_token == token,
                    EditorialJob.status.in_(("running", "cancelling")),
                )
                .values(provider_attempt_state="settled")
                .returning(EditorialJob.id)
            )
            if owned is None:
                await session.rollback()
                return
            job = await session.get(EditorialJob, job_id, populate_existing=True)
            assert job is not None
            job.model = generated.model
            job.usage = {
                "input_tokens": generated.usage.input_tokens,
                "output_tokens": generated.usage.output_tokens,
                "details": generated.usage.details,
            }
            if job.cancel_requested_at is not None:
                job.status = "cancelled"
                job.error_code = "cancelled"
            elif job.deadline_at <= datetime.now(UTC):
                job.status, job.error_code = "failed", "deadline_exceeded"
            elif generated.draft is None or generated.checks is None:
                job.status, job.error_code = "failed", "invalid_model_response"
            else:
                revision = await session.scalar(
                    select(ArticleRevision).where(
                        ArticleRevision.origin_job_id == job.id,
                    )
                )
                if revision is None:
                    revision = ArticleRevision(
                        id=uuid4(),
                        article_id=job.article_id,
                        origin_job_id=job.id,
                        origin="model",
                        content=generated.draft.model_dump(mode="json"),
                        fact_sheet=job.fact_sheet,
                        content_hash=generated.checks.content_hash,
                        model=generated.model,
                        prompt_version=PROMPT_VERSION,
                        usage=job.usage,
                    )
                    session.add(revision)
                    await session.flush()
                    session.add(
                        ArticleCheck(
                            revision_id=revision.id,
                            report=generated.checks.model_dump(mode="json"),
                        )
                    )
                # Candidate generations never replace a human draft or live pointer.
                previous = (
                    await session.get(ArticleRevision, job.base_head_revision_id)
                    if job.base_head_revision_id
                    else None
                )
                if previous is None or previous.origin != "human":
                    await session.execute(
                        update(Article)
                        .where(
                            Article.id == job.article_id,
                            Article.draft_head_revision_id == job.base_head_revision_id,
                        )
                        .values(draft_head_revision_id=revision.id, version=Article.version + 1)
                    )
                job.result_revision_id = revision.id
                job.status = "ready_for_review" if generated.checks.passed else "checks_failed"
                job.error_code = None if generated.checks.passed else "checks_failed"
                session.add(
                    AdminAuditLog(
                        actor_username="bobrik-editorial-worker",
                        action=AuditAction.CREATE,
                        entity_type="article_revision",
                        entity_id=str(revision.id),
                        after={"job_id": str(job.id), "article_id": str(job.article_id)},
                    )
                )
            job.finished_at = datetime.now(UTC)
            job.lease_until = None
            await self._release(session, token)
            await session.commit()

    async def _release(self, session: AsyncSession, token: UUID) -> None:
        await session.execute(
            update(EditorialLease)
            .where(
                EditorialLease.id == 1,
                EditorialLease.token == token,
            )
            .values(token=None, lease_until=datetime.now(UTC))
        )

    async def _failed(self, job_id: UUID, token: UUID, code: str) -> None:
        async with self.sessions() as session:
            cancelled = (
                true() if code == "cancelled" else EditorialJob.cancel_requested_at.is_not(None)
            )
            await session.execute(
                update(EditorialJob)
                .where(
                    EditorialJob.id == job_id,
                    EditorialJob.claim_token == token,
                    EditorialJob.status.in_(("running", "cancelling")),
                )
                .values(
                    provider_attempt_state="unknown",
                    status=case((cancelled, "cancelled"), else_="failed"),
                    error_code=code,
                    finished_at=datetime.now(UTC),
                    lease_until=None,
                )
            )
            await self._release(session, token)
            await session.commit()

    async def run_once(self) -> bool:
        async with self.sessions() as session:
            job = await self.claim_next(session)
            if job is None or job.claim_token is None:
                return False
            job_id, token = job.id, job.claim_token
        await self.process_job(job_id, token)
        return True

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        stop = stop_event or asyncio.Event()
        while not stop.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Retain started attempt state for recovery; never repeat a paid
                # call when result persistence failed. No raw brief/model text.
                logger.error("Editorial worker iteration failed")
                processed = False
            if not processed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.5)
                except TimeoutError:
                    pass


async def _run_main() -> None:
    from predatory_beavers.db.session import create_engine, create_session_factory
    from predatory_beavers.modules.mascot.gateway import HttpLLMGateway
    from predatory_beavers.settings import get_settings

    settings = get_settings()
    config = MascotConfig.from_settings(settings)
    if not config.enabled or config.env not in ("dev", "test"):
        return
    # Gateway uses the same paid Go endpoint/key with the configured editor model.
    editor_config = replace(config, llm_chat_model=config.llm_editor_model)
    engine = create_engine(settings)
    worker = EditorialWorker(
        editor_config,
        create_session_factory(engine),
        HttpLLMGateway(editor_config, max_content_chars=36_000),
    )
    try:
        await worker.run()
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(_run_main())


if __name__ == "__main__":
    main()
