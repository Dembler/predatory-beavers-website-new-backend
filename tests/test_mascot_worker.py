"""Worker and knowledge-corpus tests for the mascot prototype.

Uses a fake gateway (no paid calls) to verify answer+citation persistence,
timeout handling without retry, numeric-claim rejection with a safe fallback,
and public corpus filtering across all saved years.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from predatory_beavers.db.base import Base
from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.auth import models as auth_models  # noqa: F401
from predatory_beavers.modules.club.models import Team, TeamCategory
from predatory_beavers.modules.content.models import ContentBlock
from predatory_beavers.modules.mascot.agent import SAFE_FALLBACK_REPLY, parse_reply
from predatory_beavers.modules.mascot.config import MascotConfig
from predatory_beavers.modules.mascot.gateway import (
    CompletionResult,
    CompletionUsage,
    GatewayDeadlineExceeded,
    GatewayUnavailable,
)
from predatory_beavers.modules.mascot.knowledge import (
    KnowledgeDoc,
    build_corpus,
    corpus_hash,
    retrieve,
    unsupported_numbers,
)
from predatory_beavers.modules.mascot.models import MascotRun, MascotRunEvent, MascotSession
from predatory_beavers.modules.mascot.security import hash_token
from predatory_beavers.modules.mascot.service import cancel_run
from predatory_beavers.modules.mascot.worker import MascotWorker
from predatory_beavers.modules.matches.models import ClubSide, Competition, Match, MatchStatus
from predatory_beavers.modules.results.models import ArchiveResult
from predatory_beavers.settings import Settings


class FakeGateway:
    def __init__(
        self,
        reply_content: str | None = None,
        error: Exception | None = None,
        usage: CompletionUsage | None = None,
    ) -> None:
        self.reply_content = reply_content
        self.error = error
        self.usage = usage or CompletionUsage(input_tokens=12, output_tokens=5)
        self.calls = 0

    async def complete(
        self, *, system, user, max_tokens, temperature, deadline, cancel_check, session_id=None
    ):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return CompletionResult(
            content=self.reply_content or '{"text": "ок", "source_ids": []}',
            usage=self.usage,
            model="fake-model",
        )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        env="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'worker.sqlite').as_posix()}",
        log_json=False,
        cookie_secure=False,
        mascot_enabled=True,
    )


async def _seed_session_and_run(settings: Settings, message: str = "Когда основан клуб?") -> str:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add_all(
            [
                ContentBlock(
                    section="history",
                    title="История клуба",
                    body="Клуб основан в 1998 году в Воронеже.",
                    active=True,
                ),
                MascotSession(
                    token_hash=hash_token("token"),
                    csrf_token_hash=hash_token("csrf"),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                ),
            ]
        )
        await session.flush()
        mascot_session = await session.scalar(select(MascotSession))
        run = MascotRun(
            session_id=mascot_session.id,
            client_request_id=str(uuid4()),
            request_payload_hash="seed",
            message=message,
            status="queued",
            deadline_at=datetime.now(UTC) + timedelta(seconds=20),
        )
        session.add(run)
        await session.commit()
        run_id = str(run.id)
    await engine.dispose()
    return run_id


async def _read_run(settings: Settings, run_id: str) -> tuple[MascotRun, list[MascotRunEvent]]:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    async with factory() as session:
        run = await session.scalar(select(MascotRun).where(MascotRun.id == UUID(run_id)))
        events = list(
            await session.scalars(
                select(MascotRunEvent)
                .where(MascotRunEvent.run_id == UUID(run_id))
                .order_by(MascotRunEvent.seq)
            )
        )
    await engine.dispose()
    assert run is not None
    return run, events


async def _run_worker_once(settings: Settings, gateway: FakeGateway) -> None:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    worker = MascotWorker(MascotConfig.from_settings(settings), factory, gateway)
    await worker.run_once()
    await engine.dispose()


def test_worker_persists_answer_citations_and_usage(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    run_id = asyncio.run(_seed_session_and_run(settings))
    gateway = FakeGateway(
        reply_content=json.dumps({"text": "Клуб основан в 1998 году.", "source_ids": [1]})
    )
    asyncio.run(_run_worker_once(settings, gateway))
    run, events = asyncio.run(_read_run(settings, run_id))

    assert gateway.calls == 1
    assert run.status == "completed"
    assert run.provider_attempt_state == "settled"
    assert run.provider_input_tokens == 12
    assert run.provider_output_tokens == 5
    assert run.data_revision

    assert [event.event_type for event in events] == [
        "status",
        "status",
        "status",
        "answer",
        "done",
    ]
    answer = next(event for event in events if event.event_type == "answer")
    assert answer.payload["text"] == "Клуб основан в 1998 году."
    assert answer.payload["claims_version"] == "prototype-v1"
    assert answer.payload["citations"] == [{"title": "История клуба", "url": "/club#history"}]
    done = next(event for event in events if event.event_type == "done")
    assert done.payload["usage"] == {"input_tokens": 12, "output_tokens": 5}
    progress = [event.payload for event in events if event.event_type == "status"]
    assert [step["stage"] for step in progress] == ["reading_data", "thinking", "validating"]
    assert all(isinstance(step["message"], str) for step in progress)
    assert progress[1]["source_count"] == 1
    assert progress[1]["source_titles"] == ["История клуба"]
    timestamps = [datetime.fromisoformat(step["at"]) for step in progress]
    finished_at = datetime.fromisoformat(done.payload["at"])
    assert timestamps == sorted(timestamps)
    assert finished_at >= timestamps[-1]
    assert done.payload["duration_ms"] >= 0
    assert "1998" not in json.dumps(progress)  # Stage metadata does not copy source passages.


def test_worker_deadline_does_not_retry(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    run_id = asyncio.run(_seed_session_and_run(settings))
    gateway = FakeGateway(error=GatewayDeadlineExceeded())
    asyncio.run(_run_worker_once(settings, gateway))
    run, events = asyncio.run(_read_run(settings, run_id))

    assert gateway.calls == 1
    assert run.status == "interrupted"
    assert run.provider_attempt_state == "unknown"
    assert run.provider_input_tokens is None
    error_event = next(event for event in events if event.event_type == "error")
    assert error_event.payload["code"] == "deadline_exceeded"
    assert error_event.payload["retryable"] is True
    assert datetime.fromisoformat(error_event.payload["at"]).tzinfo is not None
    assert error_event.payload["duration_ms"] >= 0


def test_worker_unavailable_is_failed_without_retry(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    run_id = asyncio.run(_seed_session_and_run(settings))
    gateway = FakeGateway(error=GatewayUnavailable())
    asyncio.run(_run_worker_once(settings, gateway))
    run, events = asyncio.run(_read_run(settings, run_id))

    assert gateway.calls == 1
    assert run.status == "failed"
    error_event = next(event for event in events if event.event_type == "error")
    assert error_event.payload["code"] == "llm_unavailable"


def test_worker_rejects_unsupported_numbers_with_safe_fallback(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    run_id = asyncio.run(_seed_session_and_run(settings))
    # The seeded history has no "2026"; the model invents it.
    gateway = FakeGateway(
        reply_content=json.dumps({"text": "Клуб переехал в 2026 году.", "source_ids": [1]})
    )
    asyncio.run(_run_worker_once(settings, gateway))
    run, events = asyncio.run(_read_run(settings, run_id))

    assert run.status == "completed"
    answer = next(event for event in events if event.event_type == "answer")
    assert answer.payload["text"] == SAFE_FALLBACK_REPLY
    assert answer.payload["citations"] == [
        {"title": "Матчи", "url": "/matches"},
        {"title": "История клуба", "url": "/club#history"},
    ]


@pytest.mark.parametrize(
    "content",
    [
        '{"text":"Позвони на непроверенную горячую линию 999999.","source_ids":[]}',
        '{"text":"Поддержка есть, но модель забыла поле source_ids."}',
    ],
)
def test_rejected_crisis_reply_keeps_help_instead_of_archive_fallback(
    tmp_path: Path, content: str
) -> None:
    settings = _settings(tmp_path)
    run_id = asyncio.run(_seed_session_and_run(settings, "Мне плохо, хочу навредить себе."))
    gateway = FakeGateway(reply_content=content)
    asyncio.run(_run_worker_once(settings, gateway))
    run, events = asyncio.run(_read_run(settings, run_id))
    answer = next(event for event in events if event.event_type == "answer")
    assert run.status == "completed"
    assert gateway.calls == 1
    assert run.provider_attempt_state == "settled"
    assert run.provider_input_tokens == 12
    assert "112" in answer.payload["text"]
    assert "рядом" in answer.payload["text"]
    assert "архив" not in answer.payload["text"]
    assert "999999" not in answer.payload["text"]
    assert answer.payload["citations"] == []


def test_corpus_includes_only_public_records_across_all_years(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = create_engine(settings)
    factory = create_session_factory(engine)

    async def seed() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add_all(
                [
                    ContentBlock(
                        section="history", title="Публичная история", body="Текст", active=True
                    ),
                    ContentBlock(
                        section="history", title="Скрытая история", body="Текст", active=False
                    ),
                    ContentBlock(section="rules", title="Правила", body="Текст", active=True),
                    ContentBlock(
                        section="rules",
                        title="Удалённые правила",
                        body="Текст",
                        active=True,
                        is_deleted=True,
                    ),
                    ArchiveResult(
                        source="club_archive",
                        external_id="g-2008",
                        discipline="5x5",
                        team="men",
                        season="2008-2009",
                        year=2008,
                        tournament="Турнир 2008",
                        team_name="ВГУ",
                        opponent_name="Соперник",
                        status="finished",
                        source_label="Архив",
                        date_precision="day",
                    ),
                    ArchiveResult(
                        source="club_archive",
                        external_id="t-2024",
                        discipline="5x5",
                        team="men",
                        season="2024-2025",
                        year=2024,
                        tournament="Турнир 2024",
                        team_name="ВГУ",
                        opponent_name=None,
                        place=1,
                        status="finished",
                        source_label="Архив",
                        date_precision="day",
                    ),
                    Team(slug="men", name="Хищные Бобры", category=TeamCategory.MEN, active=True),
                    Team(
                        slug="women-hidden",
                        name="Скрытая",
                        category=TeamCategory.WOMEN,
                        active=False,
                    ),
                ]
            )
            await session.commit()

    asyncio.run(seed())

    async def collect() -> list:
        async with factory() as session:
            return await build_corpus(session)

    documents = asyncio.run(collect())
    asyncio.run(engine.dispose())

    history_titles = [doc.title for doc in documents if doc.kind == "history"]
    rules_titles = [doc.title for doc in documents if doc.kind == "rules"]
    result_texts = [doc.text for doc in documents if doc.kind == "result"]
    team_titles = [doc.title for doc in documents if doc.kind == "team"]

    assert "Публичная история" in history_titles
    assert "Скрытая история" not in history_titles
    assert "Правила" in rules_titles
    assert "Удалённые правила" not in rules_titles
    assert len(result_texts) == 2  # both saved years: complete history, not current-only
    assert any("2008" in text for text in result_texts)
    assert any("2024" in text for text in result_texts)
    assert "Хищные Бобры" in team_titles
    assert "Скрытая" not in team_titles


def test_public_match_visibility_and_complete_content_chunks(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    async def collect() -> list[KnowledgeDoc]:
        engine = create_engine(settings)
        factory = create_session_factory(engine)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                public_team = Team(slug="public", name="Бобры", category=TeamCategory.MEN)
                hidden_team = Team(
                    slug="hidden", name="Скрытые", category=TeamCategory.MEN, active=False
                )
                deleted_team = Team(
                    slug="deleted", name="Удалённые", category=TeamCategory.MEN, is_deleted=True
                )
                public_competition = Competition(name="Публичный турнир", season="2026")
                hidden_competition = Competition(name="Скрытый турнир", season="2026", active=False)
                deleted_competition = Competition(
                    name="Удалённый турнир", season="2026", is_deleted=True
                )
                session.add_all(
                    [
                        public_team,
                        hidden_team,
                        deleted_team,
                        public_competition,
                        hidden_competition,
                        deleted_competition,
                    ]
                )
                await session.flush()
                cases = [
                    ("Публичный", public_team, public_competition, True, False),
                    ("Скрытая команда", hidden_team, public_competition, True, False),
                    ("Удалённая команда", deleted_team, public_competition, True, False),
                    ("Скрытое соревнование", public_team, hidden_competition, True, False),
                    ("Удалённое соревнование", public_team, deleted_competition, True, False),
                    ("Неактивный матч", public_team, public_competition, False, False),
                    ("Удалённый матч", public_team, public_competition, True, True),
                ]
                for opponent, team, competition, active, deleted in cases:
                    session.add(
                        Match(
                            team_id=team.id,
                            competition_id=competition.id,
                            starts_at=datetime.now(UTC) + timedelta(days=1),
                            club_side=ClubSide.HOME,
                            home_team_name="Бобры",
                            away_team_name=opponent,
                            status=MatchStatus.SCHEDULED,
                            active=active,
                            is_deleted=deleted,
                        )
                    )
                session.add(
                    ContentBlock(
                        section="history",
                        title="История",
                        body="Начало истории. " * 150
                        + "Уникальное событие: суперфинал в 2007 году.",
                    )
                )
                await session.commit()
                return await build_corpus(session)
        finally:
            await engine.dispose()

    documents = asyncio.run(collect())
    matches = [doc for doc in documents if doc.kind == "match"]
    assert len(matches) == 1
    assert matches[0].title == "Бобры — Публичный"
    assert matches[0].match_status == "scheduled"
    assert matches[0].team_category == "men"
    history = [doc for doc in documents if doc.kind == "history"]
    assert len(history) > 1
    assert all(len(doc.text) <= 600 for doc in history)
    assert len({doc.doc_id for doc in history}) == len(history)
    found = retrieve("Когда был суперфинал?", documents)
    assert found and "2007" in found[0].text


def _match_doc(
    doc_id: str, starts_at: datetime, *, status: str = "scheduled", category: str = "men"
) -> KnowledgeDoc:
    return KnowledgeDoc(
        doc_id=doc_id,
        kind="match",
        title=doc_id,
        text=f"Матч Бобры — соперник. Дата: {starts_at.isoformat()}",
        url="/matches",
        starts_at=starts_at,
        match_status=status,
        team_category=category,
    )


def test_next_match_uses_future_schedule_and_requested_team() -> None:
    now = datetime(2026, 11, 16, tzinfo=UTC)
    women = _match_doc("match:women", now + timedelta(days=1), category="women")
    men = _match_doc("match:men", now + timedelta(days=2))
    documents = [
        _match_doc("match:old", now - timedelta(days=1)),
        _match_doc("match:finished", now + timedelta(hours=1), status="finished"),
        _match_doc("match:later", now + timedelta(days=10)),
        men,
        women,
    ]
    assert retrieve("Когда ближайший матч?", documents, now=now) == [women]
    assert retrieve("Когда ближайший матч?", documents, now=now, team_category="men") == [men]
    assert retrieve("Когда следующая игра мужской команды?", documents, now=now) == [men]
    assert retrieve("Ближайший матч девушек", documents, now=now) == [women]
    assert retrieve("Когда ближайший матч?", documents, now=now + timedelta(days=30)) == []
    past = KnowledgeDoc("result:old", "result", "Матч 2007", "Матч сыгран в 2007 году.", "/matches")
    assert retrieve("Когда был матч 2007 года?", [*documents, past], now=now)[0] == past


def test_history_rules_ranking_and_numeric_token_grounding() -> None:
    history = KnowledgeDoc("history:1", "history", "О клубе", "Создан в 1998 году.", "/club")
    rules = KnowledgeDoc("rules:1", "rules", "Игра", "Штрафной бросок: 1 очко.", "/rules")
    result = KnowledgeDoc("result:1", "result", "Турнир", "Счёт 160:62 в 2026 году.", "/matches")
    documents = [rules, result, history]
    assert retrieve("Расскажи историю клуба", documents)[0] == history
    assert retrieve("Какие правила баскетбола?", documents)[0] == rules
    assert retrieve("Нет подходящего источника", documents) == []
    assert unsupported_numbers("Счёт 60:62, год 26", [result]) == ["60", "26"]
    assert unsupported_numbers("Счёт 160:62 в 2026 году", [result]) == []
    assert corpus_hash(documents) == corpus_hash(list(reversed(documents)))
    assert corpus_hash([result]) != corpus_hash([history])


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "Ответ"},
        {"text": "Ответ", "source_ids": [], "extra": "ignored"},
        {"text": " ", "source_ids": []},
        {"text": "я" * 4001, "source_ids": []},
        {"text": "Ответ", "source_ids": [True]},
        {"text": "Ответ", "source_ids": [0]},
        {"text": "Ответ", "source_ids": [-1]},
        {"text": "Ответ", "source_ids": [1, 2, 3, 4, 5, 6, 7]},
    ],
)
def test_agent_reply_rejects_schema_violations(payload: object) -> None:
    assert parse_reply(json.dumps(payload)) is None


def test_agent_reply_accepts_bounded_reply_and_hides_config_secret(tmp_path: Path) -> None:
    reply = parse_reply('{"text":"  Ответ  ","source_ids":[1,1,2]}')
    assert reply is not None
    assert reply.text == "Ответ"
    assert reply.source_ids == [1, 2]
    settings = _settings(tmp_path).model_copy(update={"llm_api_key": None})
    assert MascotConfig.from_settings(settings).llm_chat_model == "deepseek-v4.1-flash"
    from dataclasses import replace

    config = replace(MascotConfig.from_settings(settings), llm_api_key="test-secret")
    assert "test-secret" not in repr(config)


class PendingGateway(FakeGateway):
    """A provider response held until the test releases it."""

    def __init__(self) -> None:
        super().__init__(reply_content='{"text":"Ответ по архиву","source_ids":[1]}')
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, **kwargs):
        self.started.set()
        await self.release.wait()
        return await super().complete(**kwargs)


@pytest.mark.asyncio
async def test_public_progress_is_available_during_generation_and_replays_unchanged(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    run_id = await _seed_session_and_run(settings)
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(
            ContentBlock(
                section="history",
                title="Секретный черновик",
                body="Непубличная история клуба в 1998 году.",
                active=False,
            )
        )
        await session.commit()
    gateway = PendingGateway()
    worker = MascotWorker(MascotConfig.from_settings(settings), factory, gateway)
    task = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(gateway.started.wait(), timeout=5)
        pending_run, pending_events = await _read_run(settings, run_id)
        assert pending_run.status == "running"
        assert [event.payload["stage"] for event in pending_events] == [
            "reading_data",
            "thinking",
        ]
        assert pending_events[-1].payload["source_titles"] == ["История клуба"]
        assert "Секретный" not in json.dumps(
            [event.payload for event in pending_events], ensure_ascii=False
        )
        gateway.release.set()
        assert await asyncio.wait_for(task, timeout=5)
    finally:
        gateway.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()
    _, finished_events = await _read_run(settings, run_id)
    assert [event.payload for event in finished_events[:2]] == [
        event.payload for event in pending_events
    ]
    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_two_concurrent_workers_make_only_one_provider_attempt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    run_id = await _seed_session_and_run(settings)
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    gateway = PendingGateway()
    workers = [
        MascotWorker(MascotConfig.from_settings(settings), factory, gateway) for _ in range(2)
    ]
    tasks = [asyncio.create_task(worker.run_once()) for worker in workers]
    try:
        await asyncio.wait_for(gateway.started.wait(), timeout=5)
        gateway.release.set()
        processed = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    finally:
        gateway.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()
    run, events = await _read_run(settings, run_id)
    assert sorted(processed) == [False, True]
    assert gateway.calls == 1
    assert run.status == "completed"
    assert run.provider_attempt_state == "settled"
    assert sum(event.event_type == "answer" for event in events) == 1
    assert sum(event.event_type == "done" for event in events) == 1
    assert [event.seq for event in events] == list(range(1, len(events) + 1))


@pytest.mark.asyncio
async def test_cancel_during_pending_provider_response_publishes_no_answer(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    run_id = await _seed_session_and_run(settings)
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    gateway = PendingGateway()
    worker = MascotWorker(MascotConfig.from_settings(settings), factory, gateway)
    task = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(gateway.started.wait(), timeout=5)
        async with factory() as session:
            run = await session.get(MascotRun, UUID(run_id))
            assert run is not None
            cancelled = await cancel_run(session, run)
            assert cancelled.status == "cancelling"
        gateway.release.set()
        assert await asyncio.wait_for(task, timeout=5)
    finally:
        gateway.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()
    run, events = await _read_run(settings, run_id)
    assert gateway.calls == 1
    assert run.status == "cancelled"
    assert run.provider_attempt_state == "settled"
    assert run.provider_input_tokens == 12
    assert run.provider_output_tokens == 5
    assert not any(event.event_type in {"answer", "done"} for event in events)
    assert sum(event.event_type == "cancelled" for event in events) == 1


def test_invalid_reply_retains_actual_settled_usage(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    run_id = asyncio.run(_seed_session_and_run(settings))
    details: dict[str, object] = {
        "cost": {"total": 0.0002},
        "opencode": {"billing_source": "go"},
        "total_tokens": 64,
    }
    gateway = FakeGateway(
        reply_content="invalid JSON",
        usage=CompletionUsage(input_tokens=47, output_tokens=17, details=details),
    )
    asyncio.run(_run_worker_once(settings, gateway))
    run, events = asyncio.run(_read_run(settings, run_id))
    assert gateway.calls == 1
    assert run.status == "failed"
    assert run.provider_attempt_state == "settled"
    assert run.provider_input_tokens == 47
    assert run.provider_output_tokens == 17
    assert run.provider_usage == details
    assert not any(event.event_type in {"answer", "done"} for event in events)
    error = next(event for event in events if event.event_type == "error")
    assert error.payload["code"] == "invalid_model_response"


@pytest.mark.asyncio
async def test_stale_started_run_becomes_unknown_and_is_never_rebilled(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    run_id = await _seed_session_and_run(settings)
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    gateway = FakeGateway()
    try:
        async with factory() as session:
            await session.execute(
                update(MascotRun)
                .where(MascotRun.id == UUID(run_id))
                .values(
                    status="running",
                    claim_token=uuid4(),
                    provider_attempt_state="started",
                    started_at=datetime.now(UTC) - timedelta(seconds=90),
                    lease_until=datetime.now(UTC) - timedelta(seconds=30),
                )
            )
            await session.commit()
        worker = MascotWorker(MascotConfig.from_settings(settings), factory, gateway)
        assert await worker.run_once() is False
        assert await worker.run_once() is False
    finally:
        await engine.dispose()
    run, events = await _read_run(settings, run_id)
    assert gateway.calls == 0
    assert run.status == "interrupted"
    assert run.provider_attempt_state == "unknown"
    assert run.provider_input_tokens is None
    assert run.provider_output_tokens is None
    assert run.provider_usage is None
    assert [event.event_type for event in events] == ["error"]
    assert events[0].payload["code"] == "deadline_exceeded"
