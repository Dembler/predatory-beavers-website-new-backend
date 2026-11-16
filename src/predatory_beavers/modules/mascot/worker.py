"""Durable local chat worker. A claimed run makes at most one provider attempt."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from predatory_beavers.modules.mascot.agent import (
    GREETING_REPLY,
    SAFE_FALLBACK_CITATIONS,
    SAFE_FALLBACK_REPLY,
    SYSTEM_PROMPT,
    build_user_prompt,
    is_greeting,
    parse_reply,
)
from predatory_beavers.modules.mascot.config import MascotConfig
from predatory_beavers.modules.mascot.gateway import (
    CancelCheck,
    CompletionResult,
    GatewayCancelled,
    GatewayDeadlineExceeded,
    GatewayError,
    LLMGateway,
)
from predatory_beavers.modules.mascot.knowledge import (
    KnowledgeDoc,
    build_corpus,
    corpus_hash,
    retrieve,
)
from predatory_beavers.modules.mascot.models import RUN_ACTIVE_STATUSES, MascotRun
from predatory_beavers.modules.mascot.service import append_event, get_run
from predatory_beavers.modules.mascot.validation import reply_grounding_issues, safety_fallback

logger = logging.getLogger(__name__)


class MascotWorker:
    def __init__(
        self,
        config: MascotConfig,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: LLMGateway,
        *,
        lease_seconds: float = 60.0,
        poll_interval: float = 0.25,
    ) -> None:
        self._config = config
        self._session_factory = session_factory
        self._gateway = gateway
        self._lease_seconds = max(lease_seconds, config.deadline_seconds + 5)
        self._poll_interval = poll_interval

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        stop = stop_event or asyncio.Event()
        while not stop.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Mascot worker iteration failed")
                processed = False
            if not processed:
                try:
                    await asyncio.wait_for(stop.wait(), self._poll_interval)
                except TimeoutError:
                    pass

    async def run_once(self) -> bool:
        async with self._session_factory() as session:
            await self.recover_stale(session)
            run = await self.claim_next(session)
        if run is None:
            return False
        await self.process_run(run.id, run.claim_token)
        return True

    async def recover_stale(self, session: AsyncSession) -> None:
        now = datetime.now(UTC)
        expired = or_(MascotRun.deadline_at <= now, MascotRun.lease_until <= now)
        ids = list(
            await session.scalars(
                select(MascotRun.id).where(
                    MascotRun.status.in_(RUN_ACTIVE_STATUSES),
                    expired,
                )
            )
        )
        await session.rollback()
        for run_id in ids:
            run = await session.scalar(
                update(MascotRun)
                .where(
                    MascotRun.id == run_id,
                    MascotRun.status.in_(RUN_ACTIVE_STATUSES),
                    expired,
                )
                .values(finished_at=now)
                .returning(MascotRun)
                .execution_options(populate_existing=True)
            )
            if run is None:
                await session.rollback()
                continue
            cancelled = run.cancel_requested_at is not None
            run.status = "cancelled" if cancelled else "interrupted"
            if run.provider_attempt_state == "started":
                run.provider_attempt_state = "unknown"
            await append_event(
                session,
                run.id,
                "cancelled" if cancelled else "error",
                {}
                if cancelled
                else {
                    "code": "deadline_exceeded",
                    "message": "Время ожидания истекло",
                    "retryable": True,
                },
            )
            await session.commit()

    async def claim_next(self, session: AsyncSession) -> MascotRun | None:
        now = datetime.now(UTC)
        candidate = await session.scalar(
            select(MascotRun.id)
            .where(
                MascotRun.status == "queued",
                MascotRun.deadline_at > now,
            )
            .order_by(MascotRun.created_at, MascotRun.id)
            .limit(1)
        )
        await session.rollback()
        if candidate is None:
            return None
        # CAS works on SQLite too: two workers cannot both claim the same row.
        run = await session.scalar(
            update(MascotRun)
            .where(
                MascotRun.id == candidate,
                MascotRun.status == "queued",
                MascotRun.deadline_at > now,
            )
            .values(
                status="running",
                claim_token=uuid4(),
                started_at=now,
                lease_until=now + timedelta(seconds=self._lease_seconds),
            )
            .returning(MascotRun)
        )
        await session.commit()
        return run

    async def _lock_owned(self, session: AsyncSession, run: MascotRun) -> MascotRun | None:
        run_id, token = run.id, run.claim_token
        await session.rollback()
        current = await session.scalar(
            update(MascotRun)
            .where(
                MascotRun.id == run_id,
                MascotRun.claim_token == token,
                MascotRun.status.in_(("running", "cancelling")),
            )
            .values(claim_token=token)
            .returning(MascotRun)
            .execution_options(populate_existing=True)
        )
        return current

    async def process_run(self, run_id: UUID, claim_token: UUID | None = None) -> None:
        async with self._session_factory() as session:
            run = await get_run(session, run_id)
            if run is None or run.status != "running" or run.claim_token != claim_token:
                return
            clock = datetime.now(UTC)
            documents = await build_corpus(session, now=clock)
            message, owner_id, deadline = run.message, run.session_id, run.deadline_at
            context_team = (run.context or {}).get("team")
            run = await self._lock_owned(session, run)
            if run is None:
                return
            if run.cancel_requested_at is not None:
                await self._finish(session, run, "cancelled", "cancelled", {})
                return
            if datetime.now(UTC) >= deadline:
                await self._finish(
                    session,
                    run,
                    "interrupted",
                    "error",
                    {
                        "code": "deadline_exceeded",
                        "message": "Время ожидания истекло",
                        "retryable": True,
                    },
                )
                return
            run.data_revision = corpus_hash(documents)
            await append_event(
                session,
                run.id,
                "status",
                {
                    "stage": "reading_data",
                    "message": "Ныряю в клубную запруду за нужной историей.",
                },
            )
            if is_greeting(message):
                await self._answer(session, run, GREETING_REPLY, [], None)
                return
            if run.provider_attempt_state is not None:
                return
            passages = retrieve(
                message,
                documents,
                now=clock,
                team_category=context_team if isinstance(context_team, str) else None,
            )
            run.provider_attempt_state = "started"
            # Titles come only from the filtered public corpus. Do not expose
            # passages, prompts, provider reasoning or unpublished material.
            source_titles = list(dict.fromkeys(doc.title[:160] for doc in passages))[:6]
            await append_event(
                session,
                run.id,
                "status",
                {
                    "stage": "thinking",
                    "message": (
                        "Укладываю веточку к веточке, чтобы ответ держался крепко."
                        if source_titles
                        else "В этой запруде нужной веточки пока не нашёл."
                    ),
                    "source_count": len(source_titles),
                    "source_titles": source_titles,
                },
            )
            await session.commit()
            try:
                result = await self._gateway.complete(
                    system=SYSTEM_PROMPT,
                    user=build_user_prompt(message, passages, now=clock),
                    max_tokens=self._config.max_output_tokens,
                    temperature=0.2,
                    deadline=deadline,
                    cancel_check=self._cancel_check(run_id, claim_token),
                    session_id=str(owner_id),
                )
            except GatewayError as exc:
                run = await self._lock_owned(session, run)
                if run is None:
                    return
                run.provider_attempt_state = "unknown"
                cancelled = isinstance(exc, GatewayCancelled) or run.cancel_requested_at is not None
                await self._finish(
                    session,
                    run,
                    "cancelled"
                    if cancelled
                    else "interrupted"
                    if isinstance(exc, GatewayDeadlineExceeded)
                    else "failed",
                    "cancelled" if cancelled else "error",
                    {}
                    if cancelled
                    else {"code": exc.code, "message": exc.message, "retryable": exc.retryable},
                )
                return
            run = await self._lock_owned(session, run)
            if run is None:
                await session.execute(
                    update(MascotRun)
                    .where(
                        MascotRun.id == run_id,
                        MascotRun.claim_token == claim_token,
                    )
                    .values(
                        provider_attempt_state="settled",
                        model=result.model,
                        provider_input_tokens=result.usage.input_tokens,
                        provider_output_tokens=result.usage.output_tokens,
                        provider_usage=result.usage.details,
                    )
                )
                await session.commit()
                return
            self._usage(run, result)
            if run.cancel_requested_at is not None:
                await self._finish(session, run, "cancelled", "cancelled", {})
                return
            if datetime.now(UTC) >= deadline:
                await self._finish(
                    session,
                    run,
                    "interrupted",
                    "error",
                    {
                        "code": "deadline_exceeded",
                        "message": "Время ожидания истекло",
                        "retryable": True,
                    },
                )
                return
            await append_event(
                session,
                run.id,
                "status",
                {
                    "stage": "validating",
                    "message": "Последний обход плотины — всё ли на своих местах?",
                },
            )
            reply = parse_reply(result.content)
            cited, valid = self._resolve_citations(
                [] if reply is None else reply.source_ids, passages
            )
            if reply is None or not valid:
                support = safety_fallback(message)
                if support is not None:
                    await self._answer(session, run, support, [], result)
                    return
                await self._finish(
                    session,
                    run,
                    "failed",
                    "error",
                    {
                        "code": "invalid_model_response",
                        "message": "Не удалось проверить ответ",
                        "retryable": False,
                    },
                )
                return
            if reply_grounding_issues(reply.text, cited, question=message, now=clock):
                support = safety_fallback(message)
                await self._answer(
                    session,
                    run,
                    support or SAFE_FALLBACK_REPLY,
                    [] if support is not None else SAFE_FALLBACK_CITATIONS,
                    result,
                )
            else:
                await self._answer(
                    session,
                    run,
                    reply.text,
                    [{"title": doc.title, "url": doc.url} for doc in cited],
                    result,
                )

    @staticmethod
    def _resolve_citations(
        source_ids: list[int], passages: list[KnowledgeDoc]
    ) -> tuple[list[KnowledgeDoc], bool]:
        if any(source_id < 1 or source_id > len(passages) for source_id in source_ids):
            return [], False
        return [passages[source_id - 1] for source_id in source_ids], True

    def _cancel_check(self, run_id: UUID, token: UUID | None) -> CancelCheck:
        async def check() -> bool:
            async with self._session_factory() as session:
                row = await get_run(session, run_id)
                return (
                    row is None
                    or row.claim_token != token
                    or row.status != "running"
                    or row.cancel_requested_at is not None
                )

        return check

    @staticmethod
    def _usage(run: MascotRun, result: CompletionResult) -> None:
        run.model = result.model
        run.provider_attempt_state = "settled"
        run.provider_input_tokens = result.usage.input_tokens
        run.provider_output_tokens = result.usage.output_tokens
        run.provider_usage = result.usage.details

    async def _answer(
        self,
        session: AsyncSession,
        run: MascotRun,
        text: str,
        citations: list[dict[str, str]],
        result: CompletionResult | None,
    ) -> None:
        if result is not None:
            self._usage(run, result)
        await append_event(
            session,
            run.id,
            "answer",
            {
                "text": text,
                "citations": citations,
                "claims_version": "prototype-v1",
            },
        )
        await self._finish(
            session,
            run,
            "completed",
            "done",
            {
                "run_id": str(run.id),
                "usage": {
                    "input_tokens": result.usage.input_tokens if result else 0,
                    "output_tokens": result.usage.output_tokens if result else 0,
                },
            },
        )

    @staticmethod
    async def _finish(
        session: AsyncSession, run: MascotRun, status: str, event: str, payload: dict[str, object]
    ) -> None:
        run.status = status
        run.finished_at = datetime.now(UTC)
        await append_event(session, run.id, event, payload)
        await session.commit()


async def _run_main() -> None:
    from predatory_beavers.db.session import create_engine, create_session_factory
    from predatory_beavers.modules.mascot.gateway import HttpLLMGateway
    from predatory_beavers.observability import configure_logging
    from predatory_beavers.settings import get_settings

    settings = get_settings()
    config = MascotConfig.from_settings(settings)
    if not config.enabled or config.env not in ("dev", "test"):
        logger.info("Mascot worker disabled")
        return
    configure_logging(level=settings.log_level, json_logs=settings.log_json)
    engine = create_engine(settings)
    worker = MascotWorker(config, create_session_factory(engine), HttpLLMGateway(config))
    try:
        await worker.run()
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    del argv
    asyncio.run(_run_main())


if __name__ == "__main__":
    main()
