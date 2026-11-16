"""Real transactions and HTTP contracts for editor-controlled publication."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.api.errors import ConflictError
from predatory_beavers.api.main import create_app
from predatory_beavers.db.base import Base
from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.audit.models import AdminAuditLog
from predatory_beavers.modules.audit.repository import AuditRepository
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.auth.models import Session, User, UserRole
from predatory_beavers.modules.auth.service import hash_token, session_csrf_token
from predatory_beavers.modules.editorial.facts import build_fact_sheet
from predatory_beavers.modules.editorial.generation import (
    PROMPT_VERSION,
    ColumnDraft,
    ColumnSection,
    check_draft,
)
from predatory_beavers.modules.editorial.models import (
    Article,
    ArticleCheck,
    ArticleOutbox,
    ArticleRevision,
    EditorialJob,
    EditorialLease,
)
from predatory_beavers.modules.editorial.schemas import EditRequest, RevisionAction
from predatory_beavers.modules.editorial.service import EditorialService
from predatory_beavers.modules.editorial.worker import EditorialWorker
from predatory_beavers.modules.mascot.config import MascotConfig
from predatory_beavers.modules.mascot.gateway import (
    CompletionResult,
    CompletionUsage,
    GatewayUnavailable,
)
from predatory_beavers.modules.mascot.knowledge import KnowledgeDoc
from predatory_beavers.settings import Settings

CLOCK = datetime(2026, 11, 16, 12, tzinfo=UTC)


def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        env="test",
        mascot_enabled=True,
        log_json=False,
        cookie_secure=False,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'editorial.db').as_posix()}",
    )


def draft() -> ColumnDraft:
    # A lengthy but harmless fixture; real language diversity belongs in paid evals.
    words = " ".join("баскетбол" for _ in range(730))
    return ColumnDraft(
        title="Бобровая неделя",
        lead="Привет с берега площадки.",
        sections=[ColumnSection(heading="Взгляд болельщика", body=words, fact_ids=[])],
    )


class FakeGateway:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def complete(self, **kwargs: object) -> CompletionResult:
        self.calls += 1
        if self.fail:
            raise GatewayUnavailable
        return CompletionResult(
            content=draft().model_dump_json(),
            model="fixture-model",
            usage=CompletionUsage(80, 120),
        )


async def schema(config: Settings) -> None:
    engine = create_engine(config)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()


def service(session: AsyncSession) -> EditorialService:
    return EditorialService(session, AuditService(AuditRepository(session)))


async def seed_revision(session: AsyncSession) -> tuple[Article, ArticleRevision]:
    sheet = build_fact_sheet([], date(2026, 11, 16), now=CLOCK)
    content = draft()
    checks = check_draft(content, sheet)
    assert checks.passed
    article = Article(id=uuid4(), slug="test-column", scope_key="test-scope", version=1)
    session.add(article)
    await session.flush()
    revision = ArticleRevision(
        id=uuid4(),
        article_id=article.id,
        origin="model",
        content=content.model_dump(mode="json"),
        fact_sheet=sheet.model_dump(mode="json"),
        content_hash=checks.content_hash,
        model="fixture",
        prompt_version=PROMPT_VERSION,
    )
    session.add(revision)
    await session.flush()
    article.draft_head_revision_id = revision.id
    session.add(
        ArticleCheck(
            revision_id=revision.id,
            report=checks.model_dump(mode="json"),
        )
    )
    await session.commit()
    return article, revision


@pytest.mark.asyncio
async def test_exact_checks_approval_publish_edit_and_unpublish(tmp_path: Path) -> None:
    config = settings(tmp_path)
    await schema(config)
    engine = create_engine(config)
    sessions = create_session_factory(engine)
    async with sessions() as session:
        article, revision = await seed_revision(session)
        article_id, revision_id = article.id, revision.id
        editor = service(session)
        action = RevisionAction(expected_version=1, revision_id=revision_id)
        with pytest.raises(ConflictError):
            await editor.action(article_id, "publish", action)
        await session.rollback()
        approved = await editor.action(article_id, "approve", action)
        assert approved["approved_revision_id"] == str(revision_id)
        assert approved["published_revision_id"] is None
        published = await editor.action(
            article_id,
            "publish",
            action.model_copy(
                update={
                    "expected_version": 2,
                }
            ),
        )
        assert published["published_revision_id"] == str(revision_id)
        edited = await editor.edit(
            article_id,
            EditRequest(
                expected_version=3,
                revision_id=revision_id,
                content=draft(),
            ),
        )
        assert edited["approved_revision_id"] is None
        assert edited["published_revision_id"] == str(revision_id)
        new_id = UUID(str(edited["draft_head_revision_id"]))
        assert new_id != revision_id
        assert await editor.latest_check(new_id) is None
        with pytest.raises(ConflictError):
            await editor.action(
                article_id,
                "approve",
                RevisionAction(
                    expected_version=4,
                    revision_id=new_id,
                ),
            )
        await session.rollback()
        removed = await editor.action(
            article_id,
            "unpublish",
            RevisionAction(
                expected_version=4,
                revision_id=revision_id,
            ),
        )
        assert removed["published_revision_id"] is None
        assert await session.scalar(select(func.count()).select_from(ArticleOutbox)) == 2
        assert await session.scalar(select(func.count()).select_from(AdminAuditLog)) == 4
    await engine.dispose()


@pytest.mark.asyncio
async def test_optimistic_race_loser_does_not_write_revision(tmp_path: Path) -> None:
    config = settings(tmp_path)
    await schema(config)
    engine = create_engine(config)
    sessions = create_session_factory(engine)
    async with sessions() as session:
        article, revision = await seed_revision(session)
        article_id, revision_id = article.id, revision.id
    async with sessions() as first, sessions() as second:
        payload = EditRequest(expected_version=1, revision_id=revision_id, content=draft())
        await service(first).edit(article_id, payload)
        with pytest.raises(ConflictError):
            await service(second).edit(article_id, payload)
        await second.rollback()
        assert await second.scalar(select(func.count()).select_from(ArticleRevision)) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_new_failed_check_invalidates_old_passed_approval(tmp_path: Path) -> None:
    config = settings(tmp_path)
    await schema(config)
    engine = create_engine(config)
    async with create_session_factory(engine)() as session:
        article, revision = await seed_revision(session)
        editor = service(session)
        await editor.action(
            article.id,
            "approve",
            RevisionAction(
                expected_version=1,
                revision_id=revision.id,
            ),
        )
        previous = await editor.latest_check(revision.id)
        assert previous is not None
        session.add(
            ArticleCheck(
                revision_id=revision.id,
                report={**previous.report, "passed": False},
                created_at=datetime.now(UTC) + timedelta(seconds=1),
            )
        )
        await session.commit()
        with pytest.raises(ConflictError):
            await editor.action(
                article.id,
                "publish",
                RevisionAction(
                    expected_version=2,
                    revision_id=revision.id,
                ),
            )
    await engine.dispose()


@pytest.mark.asyncio
async def test_source_visibility_or_change_blocks_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = settings(tmp_path)
    await schema(config)
    engine = create_engine(config)
    async with create_session_factory(engine)() as session:
        article, revision = await seed_revision(session)
        doc = KnowledgeDoc("history:one", "history", "История", "История клуба.", "/club#history")
        sheet = build_fact_sheet([doc], date(2026, 11, 16), now=CLOCK)
        # Fixture represents a persisted, formerly checked revision with a visible source.
        revision.fact_sheet = sheet.model_dump(mode="json")
        await session.commit()

        async def hidden(*args: object, **kwargs: object) -> list[KnowledgeDoc]:
            return []

        monkeypatch.setattr("predatory_beavers.modules.editorial.service.build_corpus", hidden)
        assert not await service(session)._fresh_sheet(sheet)

        async def unrelated(*args: object, **kwargs: object) -> list[KnowledgeDoc]:
            return [doc, KnowledgeDoc("other", "rules", "Правила", "Другое", "/rules")]

        monkeypatch.setattr("predatory_beavers.modules.editorial.service.build_corpus", unrelated)
        assert await service(session)._fresh_sheet(sheet)
    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_no_transaction_during_provider_and_human_draft_preserved(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    await schema(config)
    engine = create_engine(config)
    sessions = create_session_factory(engine)
    async with sessions() as session:
        article, revision = await seed_revision(session)
        revision.origin = "human"
        article.published_revision_id = revision.id
        job = EditorialJob(
            article_id=article.id,
            generation_key="once",
            base_head_revision_id=revision.id,
            fact_sheet=revision.fact_sheet,
            brief="",
            status="queued",
            deadline_at=datetime.now(UTC) + timedelta(seconds=120),
        )
        session.add(job)
        await session.commit()
        article_id, old_id, job_id = article.id, revision.id, job.id
    gateway = FakeGateway()
    worker = EditorialWorker(MascotConfig.from_settings(config), sessions, gateway)
    assert await worker.run_once()
    assert not await worker.run_once()
    assert gateway.calls == 1
    async with sessions() as session:
        saved = await session.get(EditorialJob, job_id)
        assert saved is not None and saved.status == "ready_for_review"
        current = await session.get(Article, article_id)
        assert current is not None
        assert current.draft_head_revision_id == old_id
        assert current.published_revision_id == old_id
        assert saved.result_revision_id != old_id
        assert saved.usage and saved.usage["input_tokens"] == 80
    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_paid_attempt_never_retried_and_fencing_blocks_late_writer(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    await schema(config)
    engine = create_engine(config)
    sessions = create_session_factory(engine)
    async with sessions() as session:
        article, revision = await seed_revision(session)
        token = uuid4()
        job = EditorialJob(
            article_id=article.id,
            generation_key="crashed",
            fact_sheet=revision.fact_sheet,
            brief="",
            status="running",
            claim_token=token,
            provider_attempt_state="started",
            lease_until=datetime.now(UTC) - timedelta(seconds=1),
            deadline_at=datetime.now(UTC) + timedelta(seconds=30),
        )
        session.add(job)
        await session.commit()
        job_id = job.id
    gateway = FakeGateway()
    worker = EditorialWorker(MascotConfig.from_settings(config), sessions, gateway)
    assert not await worker.run_once()
    await worker.process_job(job_id, token)
    assert gateway.calls == 0
    async with sessions() as session:
        saved = await session.get(EditorialJob, job_id)
        assert saved is not None and saved.status == "interrupted"
        assert saved.provider_attempt_state == "unknown"
        assert saved.error_code == "provider_outcome_unknown"
    await engine.dispose()


@pytest.mark.asyncio
async def test_two_workers_share_single_database_lane(tmp_path: Path) -> None:
    config = settings(tmp_path)
    await schema(config)
    engine = create_engine(config)
    sessions = create_session_factory(engine)
    async with sessions() as session:
        article, revision = await seed_revision(session)
        for i in range(2):
            session.add(
                EditorialJob(
                    article_id=article.id,
                    generation_key=str(i),
                    fact_sheet=revision.fact_sheet,
                    brief="",
                    status="queued",
                    deadline_at=datetime.now(UTC) + timedelta(seconds=120),
                )
            )
        await session.commit()
    worker = EditorialWorker(MascotConfig.from_settings(config), sessions, FakeGateway())
    async with sessions() as first, sessions() as second:
        claimed = await worker.claim_next(first)
        assert claimed is not None
        assert await worker.claim_next(second) is None
        lease = await second.get(EditorialLease, 1)
        assert lease is not None and lease.token == claimed.claim_token
    await engine.dispose()


async def seed_editor(config: Settings, role: UserRole = UserRole.EDITOR) -> str:
    token = "editor-fixture-token"
    engine = create_engine(config)
    async with create_session_factory(engine)() as session:
        user = User(
            id=uuid4(),
            username="editor",
            email="editor@example.test",
            password_hash="unused",
            role=role,
        )
        session.add(user)
        await session.flush()
        session.add(
            Session(
                user_id=user.id,
                token_hash=hash_token(token),
                csrf_token_hash=hash_token(session_csrf_token(token)),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        await session.commit()
    await engine.dispose()
    return token


def test_http_editor_auth_csrf_generation_idempotency_and_public_draft_404(tmp_path: Path) -> None:
    config = settings(tmp_path)
    asyncio.run(schema(config))
    token = asyncio.run(seed_editor(config))
    with TestClient(create_app(config)) as client:
        assert client.get("/api/v1/admin/articles").status_code == 401
        client.cookies.set(config.session_cookie_name, token)
        payload = {"week_start": "2026-11-16", "brief": "Первый выпуск"}
        assert client.post("/api/v1/admin/editorial/generate", json=payload).status_code == 403
        headers = {
            config.csrf_header_name: session_csrf_token(token),
            "Idempotency-Key": str(uuid4()),
        }
        response = client.post("/api/v1/admin/editorial/generate", json=payload, headers=headers)
        assert response.status_code == 200, response.text
        repeated = client.post("/api/v1/admin/editorial/generate", json=payload, headers=headers)
        assert repeated.json()["data"]["id"] == response.json()["data"]["id"]
        conflict = client.post(
            "/api/v1/admin/editorial/generate", json={**payload, "brief": "другое"}, headers=headers
        )
        assert conflict.status_code == 409
        items = client.get("/api/v1/admin/articles").json()["data"]["items"]
        assert len(items) == 1
        slug = items[0]["slug"]
        public = client.get("/api/v1/articles/" + slug)
        assert public.status_code == 404
        assert public.headers["cache-control"] == "private, no-store"
        assert client.get("/news/" + slug).status_code == 404
        assert client.get("/api/v1/articles").json()["data"]["items"] == []


def test_http_user_role_cannot_access_editorial(tmp_path: Path) -> None:
    config = settings(tmp_path)
    asyncio.run(schema(config))
    token = asyncio.run(seed_editor(config, UserRole.USER))
    with TestClient(create_app(config)) as client:
        client.cookies.set(config.session_cookie_name, token)
        assert client.get("/api/v1/admin/articles").status_code == 403


def test_production_or_disabled_editorial_has_no_routes(tmp_path: Path) -> None:
    config = settings(tmp_path)
    config.env = "prod"
    with TestClient(create_app(config)) as client:
        assert client.get("/news").status_code == 404
        assert client.get("/api/v1/admin/articles").status_code == 404


def test_ssr_publication_and_immediate_unpublish_escape_html(tmp_path: Path) -> None:
    config = settings(tmp_path)
    asyncio.run(schema(config))

    async def setup() -> tuple[UUID, UUID]:
        engine = create_engine(config)
        async with create_session_factory(engine)() as session:
            article, revision = await seed_revision(session)
            revision.content = {**revision.content, "title": "<b>Проверка</b>"}
            article.published_revision_id = revision.id
            article.published_at = CLOCK
            await session.commit()
            ids = article.id, revision.id
        await engine.dispose()
        return ids

    article_id, _ = asyncio.run(setup())
    with TestClient(create_app(config)) as client:
        response = client.get("/news/test-column")
        assert response.status_code == 200
        assert "&lt;b&gt;Проверка&lt;/b&gt;" in response.text
        assert "<h1><b>" not in response.text
        assert "application/ld+json" in response.text
        assert response.headers["cache-control"] == "private, no-store"
        assert client.get("/news/rss.xml").status_code == 200

        async def remove() -> None:
            engine = create_engine(config)
            async with create_session_factory(engine)() as session:
                await session.execute(
                    update(Article)
                    .where(Article.id == article_id)
                    .values(published_revision_id=None)
                )
                await session.commit()
            await engine.dispose()

        asyncio.run(remove())
        assert client.get("/news/test-column").status_code == 404
        assert client.get("/api/v1/articles/test-column").status_code == 404
        assert "test-column" not in client.get("/news/rss.xml").text


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "deadline", "human_edit"])
async def test_provider_completion_races_are_checked_before_saving(
    tmp_path: Path,
    operation: str,
) -> None:
    config = settings(tmp_path)
    await schema(config)
    engine = create_engine(config)
    sessions = create_session_factory(engine)
    async with sessions() as session:
        article, revision = await seed_revision(session)
        article_id, old_id = article.id, revision.id
        job = EditorialJob(
            article_id=article_id,
            generation_key="racy",
            base_head_revision_id=old_id,
            fact_sheet=revision.fact_sheet,
            brief="",
            status="queued",
            deadline_at=datetime.now(UTC) + timedelta(seconds=120),
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    class ConcurrentGateway(FakeGateway):
        async def complete(self, **kwargs: object) -> CompletionResult:
            # A separate writer must commit while provider work is in flight.
            async with sessions() as session:
                if operation == "cancel":
                    await service(session).cancel(job_id)
                elif operation == "human_edit":
                    await service(session).edit(
                        article_id,
                        EditRequest(
                            expected_version=1,
                            revision_id=old_id,
                            content=draft(),
                        ),
                    )
                else:
                    await session.execute(
                        update(EditorialJob)
                        .where(
                            EditorialJob.id == job_id,
                        )
                        .values(deadline_at=datetime.now(UTC) - timedelta(seconds=1))
                    )
                    await session.commit()
            return await super().complete(**kwargs)

    gateway = ConcurrentGateway()
    worker = EditorialWorker(MascotConfig.from_settings(config), sessions, gateway)
    assert await worker.run_once()
    assert gateway.calls == 1
    async with sessions() as session:
        saved = await session.get(EditorialJob, job_id)
        assert saved is not None and saved.provider_attempt_state == "settled"
        if operation == "human_edit":
            current = await session.get(Article, article_id)
            assert current is not None and current.draft_head_revision_id != old_id
            human = await session.get(ArticleRevision, current.draft_head_revision_id)
            assert human is not None and human.origin == "human"
            assert current.published_revision_id is None
            assert saved.result_revision_id != human.id
        else:
            assert saved.status == ("cancelled" if operation == "cancel" else "failed")
            assert saved.result_revision_id is None
            assert await session.scalar(select(func.count()).select_from(ArticleRevision)) == 1
    await engine.dispose()


def test_editorial_migration_roundtrip_preserves_existing_tables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    from alembic import command
    from alembic.config import Config

    from predatory_beavers.settings import get_settings

    database = tmp_path / "migration.db"
    monkeypatch.setenv("APP_DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    get_settings.cache_clear()
    migration_config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    try:
        command.upgrade(migration_config, "0022")
        command.upgrade(migration_config, "0023")
        command.upgrade(migration_config, "0023")
        with sqlite3.connect(database) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert {
                "articles",
                "article_revisions",
                "article_checks",
                "editorial_jobs",
                "editorial_lease",
                "article_outbox",
                "mascot_runs",
            } <= tables
        command.downgrade(migration_config, "0022")
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT count(*) FROM mascot_runs").fetchone() == (0,)
        command.upgrade(migration_config, "0023")
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_cancel_after_claim_finishes_without_provider_call_or_lane_delay(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    await schema(config)
    engine = create_engine(config)
    sessions = create_session_factory(engine)
    async with sessions() as session:
        article, revision = await seed_revision(session)
        for identity in ("cancel-before-call", "next-job"):
            session.add(
                EditorialJob(
                    article_id=article.id,
                    generation_key=identity,
                    fact_sheet=revision.fact_sheet,
                    brief="",
                    status="queued",
                    deadline_at=datetime.now(UTC) + timedelta(seconds=120),
                )
            )
        await session.commit()
    gateway = FakeGateway()
    worker = EditorialWorker(MascotConfig.from_settings(config), sessions, gateway)
    async with sessions() as session:
        claimed = await worker.claim_next(session)
        assert claimed is not None and claimed.claim_token is not None
        job_id, token = claimed.id, claimed.claim_token
    async with sessions() as session:
        await service(session).cancel(job_id)
    await worker.process_job(job_id, token)
    assert gateway.calls == 0
    async with sessions() as session:
        job = await session.get(EditorialJob, job_id)
        assert job is not None and job.status == "cancelled"
        assert job.provider_attempt_state is None
        lease = await session.get(EditorialLease, 1)
        assert lease is not None and lease.token is None
        next_job = await worker.claim_next(session)
        assert next_job is not None and next_job.id != job_id
    await engine.dispose()


def test_standalone_worker_resolves_audit_fk_without_api_imports() -> None:
    import os
    import subprocess
    import sys

    code = """
import asyncio
import sys
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from predatory_beavers.modules.editorial.worker import EditorialWorker
from predatory_beavers.modules.audit.models import AdminAuditLog, AuditAction
from predatory_beavers.db.base import Base
assert 'predatory_beavers.api.main' not in sys.modules
assert 'users' in Base.metadata.tables
async def verify():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine)() as session:
        session.add(AdminAuditLog(actor_username='standalone-worker', action=AuditAction.CREATE,
                                 entity_type='article_revision', entity_id='fixture'))
        await session.commit()
    await engine.dispose()
asyncio.run(verify())
assert 'predatory_beavers.api.main' not in sys.modules
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_worker_loop_survives_iteration_failure(tmp_path: Path) -> None:
    config = settings(tmp_path)
    engine = create_engine(config)
    stop = asyncio.Event()

    class RecoveringWorker(EditorialWorker):
        attempts = 0

        async def run_once(self) -> bool:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("temporary persistence failure")
            stop.set()
            return False

    worker = RecoveringWorker(
        MascotConfig.from_settings(config),
        create_session_factory(engine),
        FakeGateway(),
    )
    await asyncio.wait_for(worker.run(stop), timeout=5)
    assert worker.attempts == 2
    await engine.dispose()
