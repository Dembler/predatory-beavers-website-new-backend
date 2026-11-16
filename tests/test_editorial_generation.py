"""Publication checks must inspect author prose independently from cited facts."""

from datetime import UTC, date, datetime, timedelta

import pytest

from predatory_beavers.modules.editorial.facts import build_fact_sheet
from predatory_beavers.modules.editorial.generation import (
    ColumnDraft,
    ColumnSection,
    check_draft,
    generate_column,
    render_paragraphs,
)
from predatory_beavers.modules.mascot.gateway import (
    CompletionResult,
    CompletionUsage,
    GatewayInvalidResponse,
    HttpLLMGateway,
)
from predatory_beavers.modules.mascot.knowledge import KnowledgeDoc


def evidence():
    return build_fact_sheet(
        [
            KnowledgeDoc(
                "game",
                "match",
                "Матч",
                "Запись матча",
                "/matches",
                starts_at=datetime(2026, 11, 10, 14, tzinfo=UTC),
                match_status="finished",
                team_category="men",
                date_precision="minute",
                discipline="5x5",
                opponent_name="Атлант",
                club_score=81,
                opponent_score=73,
            ),
            KnowledgeDoc("history", "history", "История", "Клуб основан давно.", "/club#history"),
        ],
        date(2026, 11, 9),
        now=datetime(2026, 11, 13, tzinfo=UTC),
    )


def draft() -> ColumnDraft:
    # Fixed-length prose intentionally isolates checks from paid text generation.
    body = " ".join(["болельщик поддержка уважение баскетбол" for _ in range(91)])
    return ColumnDraft(
        title="Колонка Бобрика",
        lead="Привет, друзья!",
        sections=[
            ColumnSection(heading="На трибуне", body=body, fact_ids=["fact-1"]),
            ColumnSection(heading="История клуба", body=body, fact_ids=["fact-2"]),
        ],
    )


def test_valid_structure_renders_exact_evidence_and_requires_human_review() -> None:
    sheet = evidence()
    result = check_draft(draft(), sheet)
    assert result.passed
    assert 700 <= result.word_count <= 1000
    assert "human_semantic_review_required" in result.warnings
    rendered = render_paragraphs(draft(), sheet)
    assert any(part["body"] == sheet.facts[0].text for part in rendered)
    assert "81:73" not in draft().model_dump_json()
    assert result.fact_sheet_fingerprint == sheet.fingerprint


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Мы победили вчера.", "unverified_action_or_result_in_author_prose"),
        ("Передам капитану.", "unverified_action_or_result_in_author_prose"),
        ("Я видел, как наши боролись до конца.", "unverified_action_or_result_in_author_prose"),
        ("В две тысячи первом году.", "unverified_action_or_result_in_author_prose"),
        ("Счёт 99:00.", "fact_in_author_prose:numbers"),
        ("Наш соперник Атлант.", "named_opponent_in_author_prose"),
        ("Читайте https://evil.example/", "unsafe_author_prose"),
        ("secret@example.com", "unsafe_author_prose"),
        ("<script>bad</script>", "unsafe_author_prose"),
    ],
)
def test_generated_and_manually_edited_prose_have_identical_gates(text: str, code: str) -> None:
    candidate = draft().model_copy(update={"lead": text})
    assert code in check_draft(candidate, evidence()).issues


@pytest.mark.parametrize(
    ("ids", "code"),
    [
        (["unknown"], "unknown_fact_reference"),
        (["fact-1", "fact-1"], "duplicate_fact_reference"),
        ([], "omitted_week_fact"),
    ],
)
def test_fact_references_are_not_optional_or_arbitrary(ids: list[str], code: str) -> None:
    candidate = draft()
    candidate.sections[0].fact_ids = ids
    assert code in check_draft(candidate, evidence()).issues


def test_history_labelling_and_short_text_block_publication() -> None:
    candidate = draft()
    candidate.sections[1].heading = "Новости недели"
    candidate.sections[0].body = "Короткий абзац."
    result = check_draft(candidate, evidence())
    assert "history_must_be_labelled" in result.issues
    assert "word_count_out_of_range" in result.warnings
    assert "word_count_too_short" not in result.issues


def test_editorial_target_warns_without_forcing_padding_but_empty_column_blocks() -> None:
    candidate = draft()
    candidate.sections[0].body = " ".join(["поддержка"] * 200)
    target_warning = check_draft(candidate, evidence())
    assert target_warning.passed
    assert "word_count_out_of_range" in target_warning.warnings
    candidate.sections[1].body = "Коротко."
    assert "word_count_too_short" in check_draft(candidate, evidence()).issues


class RecordingGateway:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict] = []

    async def complete(self, **kwargs) -> CompletionResult:
        self.calls.append(kwargs)
        return CompletionResult(self.content, CompletionUsage(10, 20), "paid-model")


async def not_cancelled() -> bool:
    return False


@pytest.mark.asyncio
async def test_invalid_schema_has_no_fallback_or_paid_retry() -> None:
    gateway = RecordingGateway('{"title":"bad","extra":"untrusted"}')
    result = await generate_column(
        evidence(),
        "brief",
        gateway,
        deadline=datetime.now(UTC) + timedelta(seconds=10),
        cancel_check=not_cancelled,
    )
    assert result.draft is None and result.checks is None
    assert result.raw_content == gateway.content
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["max_tokens"] == 4000
    assert result.usage.output_tokens == 20


@pytest.mark.asyncio
async def test_valid_response_uses_same_check_and_preserves_raw_usage() -> None:
    gateway = RecordingGateway(draft().model_dump_json())
    result = await generate_column(
        evidence(),
        "Не выдумывай счёт",
        gateway,
        deadline=datetime.now(UTC) + timedelta(seconds=10),
        cancel_check=not_cancelled,
    )
    assert result.checks and result.checks.passed
    assert result.model == "paid-model"
    assert result.raw_content == gateway.content
    assert "untrusted_editorial_brief" in gateway.calls[0]["user"]
    assert result.draft == draft()


def test_editorial_response_limit_does_not_relax_the_chat_limit() -> None:
    content = "А" * 14_000
    body = {"model": "editor", "choices": [{"message": {"content": content}}]}
    with pytest.raises(GatewayInvalidResponse):
        HttpLLMGateway._parse(body)
    assert HttpLLMGateway._parse(body, max_content_chars=36_000).content == content
    body["choices"] = [{"message": {"content": "А" * 36_001}}]
    with pytest.raises(GatewayInvalidResponse):
        HttpLLMGateway._parse(body, max_content_chars=36_000)
