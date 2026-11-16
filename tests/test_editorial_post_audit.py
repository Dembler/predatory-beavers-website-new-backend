import json
from datetime import UTC, date, datetime, timedelta

import pytest

from predatory_beavers.modules.editorial.facts import build_fact_sheet
from predatory_beavers.modules.editorial.generation import ColumnDraft, check_draft, generate_column
from predatory_beavers.modules.mascot.gateway import CompletionResult, CompletionUsage
from predatory_beavers.modules.mascot.knowledge import KnowledgeDoc


def sheet():
    return build_fact_sheet([], date(2026, 11, 9), now=datetime(2026, 11, 13, tzinfo=UTC))


def draft(lead):
    return ColumnDraft.model_validate(
        dict(
            title="Разговор с Бобриком",
            lead=lead,
            sections=[dict(heading="О баскетболе", body=" ".join(["поддержка"] * 720))],
        )
    )


def test_missing_fact_ids_retains_editable_draft_without_inventing_references():
    candidate = draft("Давайте поговорим о баскетболе.")
    assert candidate.sections[0].fact_ids == []
    assert check_draft(candidate, sheet()).passed


@pytest.mark.parametrize(
    "lead",
    [
        "Помню овации после турнира.",
        "Вспоминаю наш первый матч.",
        "Я был на игре и видел всё сам.",
        "Мне запомнилось общее фото.",
    ],
)
def test_unverified_personal_memories_block(lead):
    assert "unverified_personal_memory" in check_draft(draft(lead), sheet()).issues


@pytest.mark.parametrize(
    "lead", ["Я люблю смотреть на передачи.", "Представим тёплую встречу болельщиков."]
)
def test_mascot_preferences_and_explicit_fiction_are_allowed(lead):
    assert check_draft(draft(lead), sheet()).passed


def test_reader_invitation_uses_available_chat():
    assert (
        "unsupported_reader_channel" in check_draft(draft("Пишите в комментариях."), sheet()).issues
    )
    assert check_draft(draft("Расскажи мне в чате с Бобриком."), sheet()).passed


def test_absent_records_are_not_absent_events():
    assert (
        "absence_of_evidence_is_not_no_events"
        in check_draft(draft("Матчей не было."), sheet()).issues
    )
    assert check_draft(draft("В доступном снимке матчей нет."), sheet()).passed


def test_recorded_equal_scores_do_not_establish_draw():
    doc = KnowledgeDoc(
        "game",
        "match",
        "Матч",
        "Счёт",
        "/matches",
        starts_at=datetime(2026, 11, 11, tzinfo=UTC),
        match_status="finished",
        opponent_name="Соперник",
        date_precision="minute",
        discipline="5x5",
        club_score=73,
        opponent_score=73,
    )
    evidence = build_fact_sheet([doc], date(2026, 11, 9), now=datetime(2026, 11, 13, tzinfo=UTC))
    assert "73:73" in evidence.facts[0].text
    assert "ничья" not in evidence.facts[0].text
    assert "итог требует проверки" in evidence.facts[0].text


@pytest.mark.parametrize(
    "lead",
    [
        "Если бы я был на трибуне, я бы поддерживал команды.",
        "Я не помню подобного, поэтому не стану додумывать.",
        "Если ваша команда уступила, это не причина для оскорблений.",
        "Представим, что команда уступила в концовке.",
        "Следите за тем, кто кому передал мяч.",
        "Не стоит рубить с плеча после матча.",
        "Счёт, лучший игрок, итоговое место — не сохранились.",
        "Подробности не сохранились: ни счёт, ни лучший игрок, ни место.",
        "В ней не сохранены счёт, лучший игрок и итоговое место.",
        "Герой недели, форма и личные встречи не вычислялись.",
        "Я не стану дорисовывать детали или делать вид, что помню тот турнир.",
        "Если итог не в нашу пользу, поддержка всё равно нужна.",
    ],
)
def test_conditional_scenes_and_basketball_tutorial_are_not_asserted_results(lead):
    assert check_draft(draft(lead), sheet()).passed


@pytest.mark.parametrize(
    "lead,code",
    [
        ("Команда уступила вчера.", "unverified_action_or_result_in_author_prose"),
        (
            "Если говорить честно, команда уступила вчера.",
            "unverified_action_or_result_in_author_prose",
        ),
        ("Я передал сообщение капитану.", "unverified_action_or_result_in_author_prose"),
        ("Отправлю тренеру.", "unverified_action_or_result_in_author_prose"),
        ("Нужно убить соперника.", "unsafe_author_prose"),
        (
            "Лучший игрок — Саша, его рекорд неизвестен.",
            "unverified_action_or_result_in_author_prose",
        ),
        (
            "Подробности не сохранились, но лучший игрок — Саша.",
            "unverified_action_or_result_in_author_prose",
        ),
        ("Девушки потерпели поражение.", "unverified_action_or_result_in_author_prose"),
        ("Там зафиксировано поражение.", "unverified_action_or_result_in_author_prose"),
        ("Итог получился не в нашу пользу.", "unverified_action_or_result_in_author_prose"),
        ("Указан равный счёт.", "unverified_action_or_result_in_author_prose"),
        ("Матч завершился ничьей.", "unverified_action_or_result_in_author_prose"),
        ("Мужская команда сыграла в формате пять на пять.", "fact_in_author_prose:numbers"),
        ("Девушки играют три на три.", "fact_in_author_prose:numbers"),
        ("Я не стану делать вид, что помню, но вспоминаю овации.", "unverified_personal_memory"),
    ],
)
def test_local_exceptions_do_not_disable_real_claim_and_safety_guards(lead, code):
    assert code in check_draft(draft(lead), sheet()).issues


@pytest.mark.asyncio
async def test_generation_supplies_exact_fact_groups_instead_of_generic_lecture_outline():
    from test_editorial_generation import evidence

    captured = {}

    class Gateway:
        async def complete(self, **kwargs):
            captured.update(kwargs)
            return CompletionResult(
                draft("Привет!").model_dump_json(), CompletionUsage(1, 2), "model"
            )

    async def active():
        return False

    await generate_column(
        evidence(),
        "Кратко",
        Gateway(),
        deadline=datetime.now(UTC) + timedelta(seconds=10),
        cancel_check=active,
    )
    payload = json.loads(captured["user"])
    assert payload["required_fact_placements"] == [
        {"heading_hint": "Подтверждённые записи итогов недели", "fact_ids": ["fact-1"]},
        {"heading_hint": "История клуба: отдельно от этой недели", "fact_ids": ["fact-2"]},
    ]
    assert payload["section_count_target"] == 3
