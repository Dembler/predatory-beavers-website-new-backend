import json
from datetime import UTC, datetime

import pytest

from predatory_beavers.modules.mascot.agent import parse_reply
from predatory_beavers.modules.mascot.knowledge import KnowledgeDoc
from predatory_beavers.modules.mascot.validation import reply_grounding_issues, safety_fallback

CLOCK = datetime(2026, 11, 16, 22, 30, tzinfo=UTC)  # Moscow is already November 17.


def check(answer: str, sources=None, question="Расскажи о клубе"):
    return reply_grounding_issues(answer, sources or [], question=question, now=CLOCK)


def test_clock_does_not_license_an_invented_club_year():
    assert check("Клуб переехал в 2026 году.") == ["unsupported_number:2026"]


def test_format_names_can_appear_in_an_uncited_clarification():
    assert check("Уточни, пожалуйста: тебя интересует 3×3 или 5x5?") == []
    assert check("В команде 5 игроков.") == ["unsupported_number:5"]


def test_exact_moscow_date_is_allowed_only_as_a_clock_statement():
    assert check("Сегодня 17 ноября 2026 года.", question="Какая сейчас дата?") == []
    assert check("Сегодня 17.11.2026.", question="Какая сейчас дата?") == []
    assert check("Сегодня 2026-11-17.", question="Какая сейчас дата?") == []
    assert check("Сегодня 16 ноября 2026 года.", question="Какая сейчас дата?")
    assert check("Клуб основан 17 ноября 2026 года.", question="Какая сейчас дата?")


def test_emergency_service_number_does_not_turn_into_an_archive_fallback():
    assert check("При немедленной опасности позвони в экстренную службу: 112.") == []
    assert check("У капитана номер 112.") == ["unsupported_number:112"]
    assert "unsupported_number:999" in check("Экстренная помощь: 999.")
    assert check("При серьёзной травме вызови скорую: 112.") == []


def test_calendar_answer_can_state_exact_current_day_without_year():
    assert check("Сегодня, 17 ноября, анонса матча нет.", question="Есть матч сегодня?") == []
    assert check("Сейчас 17 ноября 2026 года.", question="Какое расписание на эту неделю?") == []
    assert check("Сегодня, 16 ноября, анонса матча нет.", question="Есть матч сегодня?")
    assert check("Матч назначен на 17 ноября.", question="Есть матч сегодня?")
    assert check("Сегодня 17 ноября 2025 года.", question="Есть матч сегодня?")


def test_rejected_answer_about_hiding_an_injury_keeps_support_instead_of_archive():
    question = "А как скрыть травму и продолжать играть?"
    answer = "Не скрывай травму. Если есть непосредственная опасность, набери 112."
    assert check(answer, question=question) == ["unsupported_number:112"]
    support = safety_fallback(question)
    assert support is not None and support.startswith("Прекрати игру")
    assert "архив" not in support
    assert check(support, question=question) == []
    assert safety_fallback("Как игрок в прошлом сезоне скрывал травму и продолжал играть?") is None


def test_numbers_in_separate_sources_cannot_be_spliced_into_a_score():
    sources = [
        KnowledgeDoc("1", "match", "Матч", "Счёт: 90:25.", "/matches"),
        KnowledgeDoc("2", "rules", "Время", "24 секунды", "/rules"),
    ]
    assert check("Итоговый счёт 90:24.", sources) == ["unsupported_score:90:24"]
    assert check("Счёт: 25:90.", sources) == ["unsupported_score:25:90"]
    assert check("Счёт матча — 90:25.", sources) == []


def test_clock_time_and_colon_punctuation_are_not_mistaken_for_a_score():
    source = KnowledgeDoc("1", "match", "Матч", "Начало 12:30", "/matches")
    assert check("Начало в 12:30.", [source]) == []


@pytest.mark.parametrize(
    ("question", "answer"),
    [
        (
            "Ты тупой бот, игрок получил травму, что ему делать?",
            "В найденных материалах клуба нет данных о травмах, поэтому не буду ничего "
            "выдумывать. При серьёзной травме позвони в экстренную службу: 112.",
        ),
        (
            "Игрок получил травму, что ему делать?",
            "За деталями по состоянию игрока стоит уточнить у клуба.",
        ),
        (
            "А игрок получил травму, чо ему делать?",
            "В найденных материалах клуба нет данных о травмах. При серьёзной травме "
            "обратись в экстренную службу: 112, а клубные детали уточни у клуба.",
        ),
        (
            "У меня паническая атака перед матчем, что делать?",
            "В сохранённом архиве клуба нет советов о панике, поэтому не могу помочь.",
        ),
        (
            "Я больше не хочу жить",
            "В сохранённых материалах клуба есть только правила игры.",
        ),
        (
            "Мой друг говорит о самоубийстве, что мне делать?",
            "Нет рекомендаций по этой теме в архиве клуба.",
        ),
    ],
)
def test_valid_reply_with_archive_excuse_requires_safety_support(question, answer):
    reply = parse_reply(json.dumps({"text": answer, "source_ids": []}))
    assert reply is not None
    assert check(reply.text, question=question) == ["crisis_archive_deflection"]
    support = safety_fallback(question)
    assert support is not None
    assert "рядом" in support
    assert "112" in support
    assert "архив" not in support
    if "травму" in question:
        assert support.startswith("Прекрати игру")


@pytest.mark.parametrize(
    "question",
    [
        "Какие травмы были у Леонида Юдина в прошлом сезоне?",
        "Что делали врачи, когда игрок сломал ногу в прошлом сезоне?",
        "Как помочь игроку после травмы в прошлом сезоне по архиву?",
        "Что известно о самоубийстве игрока в прошлом сезоне?",
        "Помоги узнать, какие травмы были у игроков в 2023 году?",
    ],
)
def test_historical_health_question_is_not_replaced_by_crisis_support(question):
    answer = "В архиве клуба нет данных об этом. За деталями уточни в клубе."
    assert check(answer, question=question) == []
    assert safety_fallback(question) is None


def test_present_distress_is_not_hidden_by_a_reference_to_an_earlier_event():
    question = "У меня сейчас паническая атака, как помочь себе? В прошлом сезоне уже была."
    assert check("В архиве клуба нет советов о панике.", question=question) == [
        "crisis_archive_deflection"
    ]
    assert safety_fallback(question) is not None


def test_a_year_in_a_current_request_does_not_cancel_safety_support():
    question = "В 2026 году мне очень плохо, я хочу навредить себе."
    assert check("В архиве клуба нет советов по этому вопросу.", question=question) == [
        "crisis_archive_deflection"
    ]
    assert safety_fallback(question) is not None


def test_direct_help_from_a_club_medic_is_not_an_archive_excuse():
    assert (
        check(
            "Прекрати игру и позови клубного врача или человека рядом. "
            "При непосредственной опасности позвони в экстренную службу: 112.",
            question="Что делать при переломе прямо во время игры?",
        )
        == []
    )
