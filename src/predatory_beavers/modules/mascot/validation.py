"""Shared, conservative reply checks used by the worker and live evaluation.

These checks catch unsupported digits, invented score pairs and archive excuses
in explicit requests for urgent help. They are not a semantic fact checker:
a valid citation can still be irrelevant or misread.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

from predatory_beavers.modules.mascot.knowledge import KnowledgeDoc, unsupported_numbers

MOSCOW = timezone(timedelta(hours=3), "Europe/Moscow")
_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_DATE_QUESTION = re.compile(
    r"сегодня|завтра|вчера|сейчас|текущ|какая\s+дата|недел|выходн", re.IGNORECASE
)
_EMERGENCY = re.compile(r"экстренн|скор\w*\s+помощ|\bскорую\b|служб\w*\s+спасения", re.IGNORECASE)
_SCORE = re.compile(r"(?<![\d:])(\d{1,3})\s*:\s*(\d{1,3})(?![\d:])")
_SCORE_LABEL = re.compile(
    r"сч[её]т\w*\s*(?:(?:был|стал|будет|в\s+итоге|матча)\s*)?[:—–-]?\s*$", re.I
)
_SELF_HARM = re.compile(
    r"навредить\s+себе|причинить\s+себе\s+вред|убить\s+себя|"
    r"покончить\s+с\s+собой|самоубий|суицид|порезать\s+себя|не\s+хочу\s+жить",
    re.IGNORECASE,
)
_HEALTH = re.compile(
    r"травм|перелом|сломал|кровотеч|не\s+дышит|потер\w*\s+сознание|"
    r"боль\w*\s+в\s+груди|таблет|лекарств|дозиров|паническ\w*\s+атак",
    re.IGNORECASE,
)
_HELP_REQUEST = re.compile(
    r"\b(?:что|ч[оёе])\s+(?:(?:мне|ему|ей|нам|им)\s+)?(?:делать|сделать|предпринять)|"
    r"как\s+(?:(?:мне|ему|ей|нам|им)\s+)?(?:помочь|поступить|действовать)|"
    r"\bпомоги(?:те)?\b(?!\s+(?:(?:мне|нам)\s+)?"
    r"(?:узнать|найти|рассказать|проверить))|\bнужна\s+помощь\b",
    re.IGNORECASE,
)
_CURRENT_DISTRESS = re.compile(
    r"\bу\s+(?:меня|нас)\b|\bмне\s+(?:очень\s+)?плохо\b|"
    r"\bхочу\s+(?:навредить\s+себе|причинить\s+себе\s+вред)\b|"
    r"\bне\s+хочу\s+жить\b|"
    r"\b(?:мой|моя)\s+(?:друг|подруга|реб[её]нок)\b[^.!?\n]{0,60}"
    r"\b(?:говорит|хочет|собирается)\b",
    re.IGNORECASE,
)
_HISTORICAL_CONTEXT = re.compile(
    r"\bв\s+(?:прошл\w*|позапрошл\w*)\s+(?:год|сезон|матч)\w*|"
    r"\bкогда[-\s]то\b|\bраньше\b|\bистори\w*",
    re.IGNORECASE,
)
_IMMEDIATE_CONTEXT = re.compile(
    r"\bсейчас\b|\bсрочно\b|\bтолько\s+что\b|\bпрямо\s+во\s+время\b", re.IGNORECASE
)
_ARCHIVE_DEFLECTION = re.compile(
    r"(?:в\s+(?:сохран[её]нн\w*\s+)?архив\w*|"
    r"в\s+(?:найденн\w*|сохран[её]нн\w*|клубн\w*)\s+материал\w*|"
    r"у\s+клуба|в\s+клубе)[^.!?\n]{0,120}\b(?:нет|только|лишь|не\s+указ\w*)\b|"
    r"\bнет\s+(?:данных|сведений|информации|советов|рекомендаций|указаний)"
    r"[^.!?\n]{0,80}\b(?:архив\w*|клуб\w*)|"
    r"\b(?:уточни\w*|спроси\w*|узна\w*)[^.!?\n]{0,65}\b(?:у\s+клуба|в\s+клубе)",
    re.IGNORECASE,
)
_INJURY = re.compile(r"травм|перелом|сломал|кровотеч", re.IGNORECASE)
_CONCEAL_INJURY = re.compile(
    r"(?:скрыть|утаить|спрятать)\s+травм|продолж\w*\s+игра\w*", re.IGNORECASE
)


def _needs_safety_support(question: str) -> bool:
    """Recognize explicit help requests, without treating club history as a crisis."""
    if not (_SELF_HARM.search(question) or _HEALTH.search(question)):
        return False
    if _HISTORICAL_CONTEXT.search(question) and not _IMMEDIATE_CONTEXT.search(question):
        return False
    return bool(
        _HELP_REQUEST.search(question)
        or _CURRENT_DISTRESS.search(question)
        or _IMMEDIATE_CONTEXT.search(question)
        or _INJURY.search(question)
        and _CONCEAL_INJURY.search(question)
    )


def safety_fallback(question: str) -> str | None:
    """Never replace a rejected crisis answer with a link to the club archive.

    This is a conservative last resort after a failed reply check, not a crisis
    classifier or clinical guidance. It makes no diagnosis or treatment claim.
    """
    if not _needs_safety_support(question):
        return None
    if _SELF_HARM.search(question):
        return (
            "Мне жаль, что тебе сейчас так тяжело. Пожалуйста, позови близкого "
            "человека, который сможет побыть рядом, и отойди от всего, чем можно "
            "причинить себе вред. При непосредственной опасности позвони в местную "
            "экстренную службу; в России это 112. Ты важен, помощь стоит позвать прямо сейчас."
        )
    if _INJURY.search(question):
        return (
            "Прекрати игру и позови человека рядом на помощь. Обратись к медицинскому "
            "специалисту. Если есть непосредственная угроза жизни "
            "или серьёзная травма — свяжись с местной экстренной службой; в России это 112."
        )
    return (
        "Позови близкого человека, который сможет побыть рядом, и обратись к медицинскому "
        "специалисту. При непосредственной угрозе жизни свяжись с местной экстренной "
        "службой; в России это 112."
    )


def _without_runtime_facts(answer: str, question: str, now: datetime) -> str:
    """Allow exact clock statements, without blessing every occurrence of its year.

    Emergency 112 is a pinned public fact, not a club source. It is only allowed
    alongside an explicit emergency-service reference. Official reference:
    https://mchs.gov.ru/deyatelnost/bezopasnost-grazhdan/kak-pravilno-vyzvat-skoruyu_5
    """
    # These are names of the supported basketball formats, including in a
    # clarification or refusal. They are not asserted scores or roster sizes.
    text = re.sub(r"\b(?:3\s*[×xх]\s*3|5\s*[×xх]\s*5)\b", "[формат игры]", answer, flags=re.I)
    if _DATE_QUESTION.search(question):
        local = (now.replace(tzinfo=UTC) if now.tzinfo is None else now).astimezone(MOSCOW)
        dates = [
            ("сегодня", local),
            ("завтра", local + timedelta(days=1)),
            ("вчера", local - timedelta(days=1)),
        ]
        for label, moment in dates:
            if label != "сегодня" and label not in question.casefold():
                continue
            forms = (
                moment.strftime("%Y-%m-%d"),
                moment.strftime("%d.%m.%Y"),
                f"{moment.day} {_MONTHS[moment.month - 1]} {moment.year}",
                f"{moment.day} {_MONTHS[moment.month - 1]}",
            )
            for value in forms:
                # A full date must be explicitly attributed to the runtime day.
                pattern = (
                    rf"((?:{label}|сейчас|текущая\s+дата)[^.!?\n\d]{{0,35}}){re.escape(value)}"
                )
                text = re.sub(pattern, r"\1[дата часов]", text, flags=re.IGNORECASE)
        if re.search(r"время|час|сейчас", question, re.I):
            clock = local.strftime("%H:%M")
            text = re.sub(
                rf"((?:сейчас|время)[^.!?\n\d]{{0,25}}){re.escape(clock)}",
                r"\1[время часов]",
                text,
                flags=re.IGNORECASE,
            )
    if _EMERGENCY.search(text):
        text = re.sub(r"(?<!\d)112(?!\d)", "[экстренная служба]", text)
    return text


def reply_grounding_issues(
    answer: str,
    cited: list[KnowledgeDoc],
    *,
    question: str,
    now: datetime,
) -> list[str]:
    checked = _without_runtime_facts(answer, question, now)
    issues = [f"unsupported_number:{number}" for number in unsupported_numbers(checked, cited)]
    if _needs_safety_support(question) and _ARCHIVE_DEFLECTION.search(answer):
        issues.append("crisis_archive_deflection")
    supported_pairs = {
        (match.group(1), match.group(2)) for doc in cited for match in _SCORE.finditer(doc.text)
    }
    for match in _SCORE.finditer(checked):
        # Clock times and incidental colon-separated numbers are not scores.
        if not _SCORE_LABEL.search(checked[max(0, match.start() - 45) : match.start()]):
            continue
        pair = (match.group(1), match.group(2))
        if pair not in supported_pairs:
            issues.append(f"unsupported_score:{pair[0]}:{pair[1]}")
    return list(dict.fromkeys(issues))
