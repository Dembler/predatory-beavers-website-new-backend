"""Evaluation runner checks: real pipeline, fake transport, no billed requests."""

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from predatory_beavers.modules.mascot.agent import GREETING_REPLY, SAFE_FALLBACK_REPLY
from predatory_beavers.modules.mascot.config import MascotConfig
from predatory_beavers.modules.mascot.gateway import (
    CompletionResult,
    CompletionUsage,
    GatewayUnavailable,
)
from predatory_beavers.modules.mascot.knowledge import KnowledgeDoc
from predatory_beavers.settings import Settings

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_bobrik.py"
SPEC = importlib.util.spec_from_file_location("evaluate_bobrik", SCRIPT)
assert SPEC and SPEC.loader
evaluation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = evaluation
SPEC.loader.exec_module(evaluation)
CLOCK = datetime(2026, 11, 16, 10, tzinfo=UTC)
DOC = KnowledgeDoc("history:1", "history", "История", "Клуб основан в 1998 году.", "/club")


def config() -> MascotConfig:
    return MascotConfig.from_settings(
        Settings(
            _env_file=None,
            env="test",
            mascot_enabled=True,
            llm_api_key="not-a-real-key",
        )
    )


def case(case_id: str = "001", question: str = "Когда основан клуб?") -> dict:
    return {
        "id": case_id,
        "question": question,
        "category": "history",
        "tone": "polite",
        "base_id": "history-founded",
        "expectations": ["Answer only supported facts"],
    }


class FakeGateway:
    def __init__(self, content: str = '{"text":"Клуб основан в 1998 году.","source_ids":[1]}'):
        self.content = content
        self.calls = 0
        self.error = None
        self.requests = []

    async def complete(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        return CompletionResult(
            content=self.content,
            model="fake-deepseek",
            usage=CompletionUsage(
                input_tokens=90,
                output_tokens=14,
                details={"opencode": {"billing_source": "go"}},
            ),
        )


def test_case_validation_rejects_duplicates_before_provider_use(tmp_path):
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text(json.dumps(case()) + "\n" + json.dumps(case("002")), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        evaluation.load_cases(cases_path)
    cases_path.write_text(json.dumps(case(question="a" * 1001)), encoding="utf-8")
    with pytest.raises(ValueError, match="production limit"):
        evaluation.load_cases(cases_path)


@pytest.mark.asyncio
async def test_supported_answer_records_actual_usage_and_preserves_case_metadata():
    gateway = FakeGateway()
    row = await evaluation.evaluate_case(case(), [DOC], CLOCK, config(), gateway)
    assert row["guard_outcome"] == "accepted"
    assert row["displayed_answer"] == "Клуб основан в 1998 году."
    assert row["parsed"] == {"text": row["displayed_answer"], "source_ids": [1]}
    assert row["citations"] == [{"title": "История", "url": "/club"}]
    assert row["usage"]["input_tokens"] == 90
    assert row["usage"]["details"]["opencode"]["billing_source"] == "go"
    assert row["provider_model"] == "fake-deepseek"
    assert row["case"]["base_id"] == "history-founded"
    assert row["case"]["expectations"] == ["Answer only supported facts"]
    request = gateway.requests[0]
    assert request["system"] == evaluation.agent.SYSTEM_PROMPT
    assert request["max_tokens"] == 500
    assert request["temperature"] == 0.2
    assert "not-a-real-key" not in json.dumps(row)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "outcome", "displayed"),
    [
        (
            '{"text":"Клуб основан в 2026 году.","source_ids":[1]}',
            "unsupported_numbers_fallback",
            SAFE_FALLBACK_REPLY,
        ),
        ('{"text":"Ответ","source_ids":[2]}', "invalid_model_response", None),
        ('{"text":"Ответ","source_ids":[],"extra":"bad"}', "invalid_model_response", None),
        ("broken JSON", "invalid_model_response", None),
    ],
)
async def test_guard_outcome_matches_worker(content, outcome, displayed):
    gateway = FakeGateway(content)
    row = await evaluation.evaluate_case(case(), [DOC], CLOCK, config(), gateway)
    assert row["guard_outcome"] == outcome
    assert row["displayed_answer"] == displayed
    assert row["raw_content"] == content
    assert row["usage"]["output_tokens"] == 14
    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_greeting_shortcut_makes_no_provider_request():
    gateway = FakeGateway()
    row = await evaluation.evaluate_case(case(question="Привет!"), [DOC], CLOCK, config(), gateway)
    assert row["displayed_answer"] == GREETING_REPLY
    assert row["guard_outcome"] == "greeting_shortcut"
    assert row["provider_attempted"] is False
    assert row["usage"] is None
    assert gateway.calls == 0


@pytest.mark.asyncio
async def test_provider_error_is_separate_and_never_retried():
    gateway = FakeGateway()
    gateway.error = GatewayUnavailable()
    row = await evaluation.evaluate_case(case(), [DOC], CLOCK, config(), gateway)
    assert gateway.calls == 1
    assert row["gateway_error"] == {
        "code": "llm_unavailable",
        "retryable": True,
        "outcome": "unknown",
        "automatic_retries": 0,
    }
    assert row["usage"] is None
    assert row["displayed_answer"] is None


@pytest.mark.asyncio
async def test_resume_skips_completed_and_unknown_billed_outcomes(tmp_path):
    gateway = FakeGateway()
    cases = [case("001"), case("002"), case("003")]
    await evaluation.run_cases(cases[:1], [DOC], CLOCK, config(), gateway, tmp_path, concurrency=1)
    with (tmp_path / "attempts.jsonl").open("a", encoding="utf-8") as output:
        output.write('{"case_id":"002","started_at":"2026-11-16T10:00:00+00:00"}\n')
    summary = await evaluation.run_cases(
        cases,
        [DOC],
        CLOCK,
        config(),
        gateway,
        tmp_path,
        concurrency=1,
    )
    rows = evaluation.read_checkpoint(tmp_path / "responses.jsonl")
    assert [row["case"]["id"] for row in rows] == ["001", "003"]
    assert summary["unknown_outcomes_skipped_on_resume"] == ["002"]
    assert gateway.calls == 2


@pytest.mark.asyncio
async def test_circuit_breaker_stops_new_calls_and_persists_results(tmp_path):
    gateway = FakeGateway()
    gateway.error = GatewayUnavailable()
    summary = await evaluation.run_cases(
        [case(str(index)) for index in range(20)],
        [DOC],
        CLOCK,
        config(),
        gateway,
        tmp_path,
        concurrency=1,
    )
    assert gateway.calls == 5
    assert summary["completed_cases"] == 5
    assert summary["stopped_after_gateway_errors"] is True
    assert len(evaluation.read_checkpoint(tmp_path / "responses.jsonl")) == 5


def test_resume_freezes_clock_and_corpus_and_rejects_changed_prompt(tmp_path, monkeypatch):
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text(json.dumps(case()) + "\n", encoding="utf-8")
    out_dir = tmp_path / "run"
    evaluation.prepare_snapshot(out_dir, cases_path, config(), [DOC], resume=False, clock=CLOCK)
    restored, restored_clock, _ = evaluation.prepare_snapshot(
        out_dir,
        cases_path,
        config(),
        [],
        resume=True,
    )
    assert restored == [DOC]
    assert restored_clock == CLOCK
    monkeypatch.setattr(evaluation.agent, "SYSTEM_PROMPT", "Changed rules")
    with pytest.raises(ValueError, match="pipeline changed"):
        evaluation.prepare_snapshot(out_dir, cases_path, config(), [], resume=True)


@pytest.mark.asyncio
async def test_sqlite_corpus_connection_is_read_only(tmp_path, monkeypatch):
    database = tmp_path / "bobrik-local.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database.as_posix()}")
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE marker (value TEXT)"))
    await engine.dispose()
    monkeypatch.setattr(evaluation, "ISOLATED_DATABASE", database)

    async def attempt_write(session, *, now=None):
        await session.execute(text("INSERT INTO marker VALUES ('should fail')"))
        return []

    monkeypatch.setattr(evaluation.knowledge, "build_corpus", attempt_write)
    with pytest.raises(Exception, match="readonly"):
        await evaluation.read_public_corpus(database)


def test_damaged_checkpoint_is_never_silently_retried(tmp_path):
    checkpoint = tmp_path / "responses.jsonl"
    checkpoint.write_text('{"case":', encoding="utf-8")
    with pytest.raises(ValueError, match="Damaged checkpoint"):
        evaluation.read_checkpoint(checkpoint)
