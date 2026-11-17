"""Public local corpus and deterministic retrieval for the Bobrik prototype.

All saved years are indexed, using the same publication rules as public APIs.
Long content is split into bounded passages without dropping its tail. Ranking
uses Russian word prefixes; fixture queries respect source date precision.
FTS, editorial data revisions and multi-turn memory remain outside this slice.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from difflib import SequenceMatcher
from fractions import Fraction
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from predatory_beavers.modules.club.models import Team
from predatory_beavers.modules.content.models import ContentBlock
from predatory_beavers.modules.matches.models import Match, MatchStatus, Venue
from predatory_beavers.modules.matches.visibility import public_match_conditions
from predatory_beavers.modules.results.models import ArchiveResult
from predatory_beavers.modules.roster.models import AsbRosterMember, AsbRosterSnapshot
from predatory_beavers.modules.roster.router import current_season, player_read

_PUBLIC_CONTENT_SECTIONS = (
    "history",
    "rules",
    "contact_channels",
    "player_roles",
    "viewing_tips",
    "captains",
)
_URLS = {
    "history": "/club#history",
    "rules": "/rules",
    "contact_channels": "/contacts",
    "player_roles": "/rules#player-roles-title",
    "viewing_tips": "/rules#viewing-tips-title",
    "captains": "/contacts#captains-title",
    "match": "/matches",
    "result": "/matches",
    "team": "/club",
}
_PASSAGE_CHAR_LIMIT = 600
_DEFAULT_LIMIT = 6
_WORD_RE = re.compile(r"[а-яёa-z0-9]+")
_NUMBER_RE = re.compile(r"\d+")
_MOSCOW = timezone(timedelta(hours=3))
_NAVIGATION = (
    (
        "bobrik",
        "Бобрик — маскот клуба",
        "/club",
        "Бобрик — маскот баскетбольного клуба ВГУ «Хищные Бобры», мужской и женской "
        "команд Воронежского государственного университета. Он помогает находить "
        "сведения о клубе и объяснения баскетбола. Причина выбора имени и животного "
        "в сохранённых материалах не подтверждена.",
    ),
    (
        "matches",
        "Расписание и результаты матчей",
        "/matches",
        "Расписание, календарь предстоящих матчей и сохранённые результаты доступны "
        "в разделе «Матчи» по адресу /matches. Там можно выбрать мужскую или женскую команду. "
        "Наличие раздела не подтверждает, что новая игра уже назначена.",
    ),
    (
        "teams-men",
        "Состав мужской команды",
        "/teams/men",
        "Опубликованный состав мужской команды и доступная статистика игроков находятся "
        "на странице /teams/men. Сезон состава и сезон статистики могут различаться.",
    ),
    (
        "teams-women",
        "Состав женской команды",
        "/teams/women",
        "Опубликованный состав женской команды и доступная статистика игроков находятся "
        "на странице /teams/women. Сезон состава и сезон статистики могут различаться.",
    ),
    (
        "contacts",
        "Связаться с клубом и капитанами",
        "/contacts",
        "Официальные каналы связи с клубом и опубликованные сведения о капитанах "
        "собраны в разделе «Контакты» по адресу /contacts. Для записи на тренировки, "
        "вступления в команду и уточнения посещения матча обратись через эти каналы; "
        "конкретные условия нужно подтвердить у клуба.",
    ),
    (
        "rules",
        "Правила баскетбола для новичков",
        "/rules",
        "Объяснения правил баскетбола, игровых позиций и советы болельщикам "
        "собраны в разделе «Правила» по адресу /rules.",
    ),
    (
        "history",
        "История и достижения клуба",
        "/club#history",
        "Сохранённая история клуба находится в разделе /club#history. Турниры и "
        "подтверждённые достижения представлены на странице клуба /club.",
    ),
    (
        "standings",
        "Турнирные таблицы",
        "/standings",
        "Опубликованные турнирные таблицы можно посмотреть в разделе /standings. "
        "При чтении нужно учитывать команду, турнир, стадию и сезон; старый снимок "
        "таблицы не подтверждает положение команды сегодня.",
    ),
)
_STOP_WORDS = {
    "а",
    "бы",
    "в",
    "во",
    "вы",
    "где",
    "для",
    "до",
    "его",
    "ее",
    "есть",
    "же",
    "за",
    "и",
    "из",
    "или",
    "к",
    "как",
    "когда",
    "ли",
    "мне",
    "мы",
    "на",
    "не",
    "но",
    "о",
    "об",
    "он",
    "она",
    "по",
    "про",
    "расскажи",
    "с",
    "со",
    "то",
    "ты",
    "у",
    "что",
    "это",
    "я",
    "пожалуйста",
    "подскажи",
    "скажите",
    "скажи",
    "бобрик",
    "блин",
    "объясни",
    "расскажите",
    "покажи",
    "давай",
    "пожалуй",
}


@dataclass(frozen=True, slots=True)
class KnowledgeDoc:
    doc_id: str
    kind: str
    title: str
    text: str
    url: str
    starts_at: datetime | None = None
    match_status: str | None = None
    team_category: str | None = None
    season: str | None = None
    discipline: str | None = None
    event_year: int | None = None
    date_precision: str | None = None
    source_label: str | None = None
    source_url: str | None = None
    opponent_name: str | None = None
    competition_name: str | None = None
    is_title: bool = False
    club_score: int | None = None
    opponent_score: int | None = None
    club_side: str | None = None
    match_id: str | None = None
    player_name: str | None = None
    player_id: str | None = None
    player_team_side: str | None = None
    period_number: int | None = None
    statistics_scope: str | None = None
    statistics_season: str | None = None
    statistic_metric: str | None = None


def normalize_text(value: str) -> str:
    return value.casefold().replace("ё", "е")


def _season_key(value: str) -> str:
    match = re.fullmatch(r"(20\d{2})\s*[-/]\s*(20\d{2}|\d{2})", value)
    if match:
        first, last = match.groups()
        return f"{first}-{last if len(last) == 4 else first[:2] + last}"
    return value


def _team_category(value: str) -> str | None:
    words = _WORD_RE.findall(normalize_text(value))
    women = any(word.startswith(("жен", "девуш")) or word == "women" for word in words)
    men = any(word.startswith(("муж", "парн", "юнош")) or word == "men" for word in words)
    if women == men:
        return None
    return "women" if women else "men"


def _tokens(value: str) -> set[str]:
    return {
        (
            "фол"
            if token.startswith("фол")
            else "мяч"
            if token.startswith("мяч")
            else "рук"
            if token.startswith("рук")
            else "очк"
            if token.startswith("очк")
            else "игра"
            if token.startswith("игр") and not token.startswith("игрок")
            else token
            if token.isdigit() or len(token) <= 4
            else token[:5]
        )
        for token in _WORD_RE.findall(normalize_text(value))
        if token not in _STOP_WORDS
    }


def _passages(value: str) -> list[str]:
    """Split all normalized content, including unusually long unbroken words."""
    text = " ".join(value.split())
    passages: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + _PASSAGE_CHAR_LIMIT, len(text))
        if end < len(text) and text[end] != " ":
            boundary = text.rfind(" ", start, end)
            if boundary > start:
                end = boundary
        passages.append(text[start:end].strip())
        start = end
        while start < len(text) and text[start] == " ":
            start += 1
    return passages


def _append_passages(
    documents: list[KnowledgeDoc],
    *,
    doc_id: str,
    kind: str,
    title: str,
    text: str,
    url: str,
    team_category: str | None = None,
    season: str | None = None,
    discipline: str | None = None,
    event_year: int | None = None,
    starts_at: datetime | None = None,
    match_status: str | None = None,
    date_precision: str | None = None,
    source_label: str | None = None,
    source_url: str | None = None,
    opponent_name: str | None = None,
    competition_name: str | None = None,
    is_title: bool = False,
    club_score: int | None = None,
    opponent_score: int | None = None,
    club_side: str | None = None,
    match_id: str | None = None,
    player_name: str | None = None,
    player_id: str | None = None,
    player_team_side: str | None = None,
    period_number: int | None = None,
    statistics_scope: str | None = None,
    statistics_season: str | None = None,
    statistic_metric: str | None = None,
) -> None:
    for index, passage in enumerate(_passages(text), start=1):
        documents.append(
            KnowledgeDoc(
                doc_id=doc_id if index == 1 else f"{doc_id}:part{index}",
                kind=kind,
                title=title,
                text=passage,
                url=url,
                team_category=team_category,
                season=season,
                discipline=discipline,
                event_year=event_year,
                starts_at=starts_at,
                match_status=match_status,
                date_precision=date_precision,
                source_label=source_label,
                source_url=source_url,
                opponent_name=opponent_name,
                competition_name=competition_name,
                is_title=is_title,
                club_score=club_score,
                opponent_score=opponent_score,
                club_side=club_side,
                match_id=match_id,
                player_name=player_name,
                player_id=player_id,
                player_team_side=player_team_side,
                period_number=period_number,
                statistics_scope=statistics_scope,
                statistics_season=statistics_season,
                statistic_metric=statistic_metric,
            )
        )


async def build_corpus(session: AsyncSession, *, now: datetime | None = None) -> list[KnowledgeDoc]:
    documents: list[KnowledgeDoc] = []
    content_rows = (
        await session.scalars(
            select(ContentBlock).where(
                ContentBlock.is_deleted.is_(False),
                ContentBlock.active.is_(True),
                ContentBlock.section.in_(_PUBLIC_CONTENT_SECTIONS),
            )
        )
    ).all()
    for block in content_rows:
        parts = [block.title, block.subtitle or "", block.body]
        if block.section == "captains":
            parts.insert(0, "Опубликованные сведения о капитане команды:")
        # Raw contacts metadata is intentionally excluded from model prompts.
        _append_passages(
            documents,
            doc_id=f"{block.section}:{block.id}",
            kind=block.section,
            title=block.title,
            text=" ".join(parts),
            url=_URLS[block.section],
            team_category=_team_category(" ".join(parts)) if block.section == "captains" else None,
        )

    match_rows = (
        await session.scalars(
            select(Match)
            .join(Match.team)
            .join(Match.competition)
            .options(
                selectinload(Match.competition), selectinload(Match.team), selectinload(Match.venue)
            )
            .where(
                *public_match_conditions(),
                Match.status.in_([MatchStatus.SCHEDULED, MatchStatus.FINISHED]),
            )
        )
    ).all()
    for match in match_rows:
        title = f"{match.home_team_name} — {match.away_team_name}"
        pieces = [title]
        pieces.append(f"Соревнование: {match.competition.name} ({match.competition.season})")
        pieces.append(f"Команда клуба: {match.team.name}")
        pieces.append(
            f"Категория: {'женская' if match.team.category.value == 'women' else 'мужская'}"
        )
        pieces.append("Дисциплина: 5x5")
        pieces.append(f"Дата: {match.starts_at.isoformat()}")
        if match.home_score is not None and match.away_score is not None:
            pieces.append(f"Счёт: {match.home_score}:{match.away_score}")
        pieces.append(f"Статус: {match.status.value}")
        if match.venue and match.venue.active and not match.venue.is_deleted:
            pieces.append(f"Зал: {match.venue.name}. Адрес: {match.venue.address}")
        for index, passage in enumerate(_passages(" ".join(pieces)), start=1):
            documents.append(
                KnowledgeDoc(
                    doc_id=f"match:{match.id}" if index == 1 else f"match:{match.id}:part{index}",
                    kind="match",
                    title=title,
                    text=passage,
                    url=_URLS["match"],
                    starts_at=match.starts_at,
                    match_status=match.status.value,
                    team_category=match.team.category.value,
                    season=match.competition.season,
                    discipline="5x5",
                    event_year=match.starts_at.astimezone(_MOSCOW).year,
                    date_precision="minute",
                    opponent_name=(
                        match.away_team_name
                        if match.club_side.value == "home"
                        else match.home_team_name
                    ),
                    competition_name=match.competition.name,
                    club_score=(
                        match.home_score if match.club_side.value == "home" else match.away_score
                    ),
                    opponent_score=(
                        match.away_score if match.club_side.value == "home" else match.home_score
                    ),
                    club_side=match.club_side.value,
                    match_id=(
                        f"{match.source}:{match.external_id}"
                        if match.external_id
                        else str(match.id)
                    ),
                )
            )

    result_rows = (await session.scalars(select(ArchiveResult))).all()
    for result in result_rows:
        pieces = [f"{result.team_name}: {result.tournament}"]
        pieces.append(f"Категория: {'женская' if result.team == 'women' else 'мужская'}")
        pieces.append(f"Дисциплина: {result.discipline}")
        if result.opponent_name:
            pieces.append(f"Соперник: {result.opponent_name}")
            if result.club_score is not None and result.opponent_score is not None:
                pieces.append(f"Счёт (ВГУ : соперник): {result.club_score}:{result.opponent_score}")
        elif result.place is not None:
            pieces.append(f"Место: {result.place}")
        if result.event_date is not None and result.date_precision == "day":
            pieces.append(f"Дата: {result.event_date.isoformat()}; время начала не опубликовано")
        elif result.event_date is not None and result.date_precision == "month":
            pieces.append(
                f"Месяц события: {result.event_date.isoformat()[:7]}; точный день неизвестен"
            )
        else:
            pieces.append(f"Год события: {result.year}; точная дата неизвестна")
        if result.location:
            pieces.append(f"Место проведения: {result.location}")
        pieces.append(f"Сезон: {result.season}")
        pieces.append(f"Статус записи: {result.status}")
        pieces.append(f"Источник: {result.source_label}")
        if result.notes:
            pieces.append(f"Примечание источника: {result.notes}")
        _append_passages(
            documents,
            doc_id=f"result:{result.id}",
            kind="result",
            title=result.tournament,
            text=" ".join(pieces),
            url=_URLS["result"],
            team_category=result.team,
            season=result.season,
            discipline=result.discipline,
            event_year=result.year,
            match_status=result.status,
            date_precision=result.date_precision,
            source_label=result.source_label,
            source_url=result.source_url,
            opponent_name=result.opponent_name,
            competition_name=result.tournament,
            is_title=result.place == 1,
            club_score=result.club_score,
            opponent_score=result.opponent_score,
            match_id=f"{result.source}:{result.external_id}" if result.opponent_name else None,
            starts_at=(
                datetime.combine(result.event_date, datetime.min.time(), tzinfo=_MOSCOW)
                if result.event_date and result.date_precision == "day"
                else None
            ),
        )

    team_rows = (
        await session.scalars(select(Team).where(Team.is_deleted.is_(False), Team.active.is_(True)))
    ).all()
    for team in team_rows:
        _append_passages(
            documents,
            doc_id=f"team:{team.id}",
            kind="team",
            title=team.name,
            text=f"{team.name} (категория: {team.category.value})",
            url=_URLS["team"],
            team_category=team.category.value,
        )
    await _append_match_statistics(session, documents)
    await _append_current_rosters(session, documents, now=now)
    venues = (
        await session.scalars(
            select(Venue).where(
                Venue.active.is_(True),
                Venue.is_deleted.is_(False),
            )
        )
    ).all()
    for venue in venues:
        _append_passages(
            documents,
            doc_id=f"venue:{venue.id}",
            kind="venue",
            title=venue.name,
            text=f"Зал: {venue.name}. Адрес: {venue.address}. {venue.description or ''}",
            url="/contacts#venues-title",
        )
    for identity, title, url, text in _NAVIGATION:
        documents.append(
            KnowledgeDoc(
                f"navigation:{identity}",
                "navigation",
                title,
                text,
                url,
                team_category=(
                    "men"
                    if identity == "teams-men"
                    else "women"
                    if identity == "teams-women"
                    else None
                ),
            )
        )
    return documents


_STAT_LABELS = (
    ("points", "Очки"),
    ("rebounds", "Подборы"),
    ("assists", "Передачи"),
    ("steals", "Перехваты"),
    ("blocks", "Блок-шоты"),
    ("turnovers", "Потери"),
    ("fouls", "Фолы"),
    ("offensive_rebounds", "Подборы в нападении"),
    ("defensive_rebounds", "Подборы в защите"),
    ("minutes_seconds", "Время на площадке, секунд"),
    ("free_throws_made", "Штрафные, попадания"),
    ("free_throws_attempted", "Штрафные, попытки"),
    ("two_points_made", "Двухочковые, попадания"),
    ("two_points_attempted", "Двухочковые, попытки"),
    ("three_points_made", "Трёхочковые, попадания"),
    ("three_points_attempted", "Трёхочковые, попытки"),
    ("plus_minus", "Плюс-минус"),
)


async def _append_match_statistics(session: AsyncSession, documents: list[KnowledgeDoc]) -> None:
    # The public helper shares parent visibility, identity/orientation validation
    # and field projection with the lazy public API. It never performs a fetch.
    from predatory_beavers.modules.matches.statistics import public_statistics

    for protocol in await public_statistics(session):
        _append_game_protocol(documents, protocol.model_dump(mode="json"))


def _append_game_protocol(documents: list[KnowledgeDoc], protocol: dict[str, Any]) -> None:
    """Index only the validated public DTO, never provider events or personal data."""
    identity = f"{protocol['source']}:{protocol['external_id']}"
    parents = [doc for doc in documents if doc.match_id == identity and _is_fixture(doc)]
    if not parents:
        return
    parent = next((doc for doc in parents if doc.kind == "match"), parents[0])
    if parent.match_status != "finished":
        return
    title = f"{protocol['home_team_name']} — {protocol['away_team_name']}"
    local_start = _local_start(parent)
    day = local_start.date().isoformat() if local_start else "дата не уточнена"
    final = (
        f"Итог (хозяева:гости): {protocol['home_score']}:{protocol['away_score']}. "
        if protocol.get("home_score") is not None and protocol.get("away_score") is not None
        else "Итоговый счёт не опубликован. "
    )
    scope = f"Официальный протокол АСБ. {title}; {protocol['season']}; {day}. " + final
    metadata: dict[str, Any] = {
        "url": protocol["source_url"],
        "match_id": identity,
        "starts_at": parent.starts_at,
        "match_status": parent.match_status,
        "team_category": protocol["team"],
        "season": protocol["season"],
        "discipline": protocol["discipline"],
        "event_year": parent.event_year,
        "date_precision": parent.date_precision,
        "opponent_name": parent.opponent_name,
        "competition_name": parent.competition_name,
        "source_label": protocol["source_label"],
        "source_url": protocol["source_url"],
    }
    _append_passages(
        documents,
        doc_id=f"game-coverage:{identity}",
        kind="match_stats_coverage",
        title=f"Официальный протокол: {title}",
        text=scope
        + "Источник: официальный протокол АСБ. "
        + (
            "Счета периодов опубликованы. "
            if protocol["period_scores"]
            else "Счета периодов не опубликованы. "
        )
        + f"Индивидуальная статистика по периодам: {protocol['player_periods_status']}. "
        "Показатели матча и четверти не заменяются сезонными средними; "
        "неопубликованное значение неизвестно, а не равно нулю.",
        statistics_scope="game",
        **metadata,
    )
    for period in protocol["period_scores"]:
        number = period["number"]
        _append_passages(
            documents,
            doc_id=f"game-period:{identity}:{number}",
            kind="match_period",
            title=f"{title} — {period['label']}",
            text=scope + f"{period['label']}; "
            f"{'четверть' if period['kind'] == 'quarter' else 'овертайм'}, период {number}. "
            f"Счёт периода (хозяева:гости): {period['home_score']}:{period['away_score']}. "
            f"Очки только за этот период, не нарастающий итог: "
            f"{protocol['home_team_name']} {period['home_score']}, "
            f"{protocol['away_team_name']} {period['away_score']}. "
            "Порядок: хозяева : гости. Эти командные очки не определяют очки отдельных игроков.",
            statistics_scope="period",
            period_number=number,
            **metadata,
        )
    players = protocol["box_score"]
    for player in players:
        name = player["full_name"]
        player_id = player["external_player_id"]
        side = player["team_side"]
        team_name = protocol[f"{side}_team_name"]
        label = (
            f"{name}, №{player['jersey_number'] or 'не указан'}, {team_name}, ID АСБ {player_id}"
        )
        did_not_play = player.get("played") is False
        participation = (
            " Не выходил на площадку (DNP); это не сыгранные нули." if did_not_play else ""
        )
        stats_rows = [(None, {} if did_not_play else player["statistics"])] + [
            (item["period"], item["statistics"])
            for item in player["period_statistics"]
            if not did_not_play
        ]
        for number, stats in stats_rows:
            period_label = "весь матч" if number is None else f"период {number}"
            source_note = (
                " Очки и броски за этот период опубликованы; прочие поля помечены отдельно."
                if number is not None
                and stats.get("points") is not None
                and any(stats.get(key) is not None for key, _ in _STAT_LABELS[10:16])
                else " Только официальный протокол; неизвестное не равно нулю."
            )
            fields = [
                f"{label_stat}: {stats[key]}"
                for key, label_stat in _STAT_LABELS
                if stats.get(key) is not None
            ]
            missing = [label_stat for key, label_stat in _STAT_LABELS if stats.get(key) is None]
            if missing:
                fields.append("Не опубликовано: " + ", ".join(missing))
            # Usually one compact passage per player/period. Repeat the exact
            # scope on every overflow passage instead of five fixed chunks.
            for offset, field_group in enumerate(_passages("; ".join(fields))):
                _append_passages(
                    documents,
                    doc_id=f"game-player:{identity}:{side}:{player_id}:{number}:{offset}",
                    kind="match_player_stats",
                    title=f"{label} — {period_label}",
                    text=scope
                    + f"Игрок: {label}; {period_label}. "
                    + field_group
                    + ". "
                    + participation
                    + source_note,
                    player_name=name,
                    player_id=player_id,
                    player_team_side=side,
                    period_number=number,
                    statistics_scope="game" if number is None else "period",
                    **metadata,
                )
    period_numbers = {None} | {
        item["period"] for player in players for item in player["period_statistics"]
    }
    for number in period_numbers:
        for comparison_scope in ("all", "club", "opponent"):
            eligible = [
                player
                for player in players
                if comparison_scope == "all"
                or (player["team_side"] == protocol["club_side"]) == (comparison_scope == "club")
            ]
            values = [
                (
                    player,
                    {}
                    if player.get("played") is False
                    else player["statistics"]
                    if number is None
                    else next(
                        (
                            item["statistics"]
                            for item in player["period_statistics"]
                            if item["period"] == number
                        ),
                        {},
                    ),
                )
                for player in eligible
            ]
            group_label = {
                "all": "все участники обеих команд",
                "club": "игроки ВГУ",
                "opponent": "игроки соперника",
            }[comparison_scope]
            for metric, metric_label in _STAT_LABELS[:3] + (_STAT_LABELS[9],):
                comparable = [
                    (player, stats[metric])
                    for player, stats in values
                    if stats.get(metric) is not None
                ]
                prefix = (
                    scope
                    + ("Весь матч. " if number is None else f"Только период {number}. ")
                    + f"{group_label}; метрика: {metric_label}. "
                )
                # Explicit per-metric missingness permits honest sparse answers,
                # including period assists/minutes without borrowing game totals.
                entries = [
                    f"{player['full_name']} (№{player['jersey_number'] or '?'}, "
                    f"ID АСБ {player['external_player_id']}): "
                    + (
                        "не выходил на площадку (DNP)"
                        if player.get("played") is False
                        else str(stats[metric])
                        if stats.get(metric) is not None
                        else "не опубликовано"
                    )
                    for player, stats in values
                ]
                for index, group in enumerate(_bounded_groups(entries, 360)):
                    _append_passages(
                        documents,
                        doc_id=f"game-summary:{identity}:{number}:{comparison_scope}:{metric}:{index}",
                        kind="match_player_summary",
                        title=f"{title} — {metric_label}",
                        text=prefix
                        + "; ".join(group)
                        + ". Неизвестное не равно нулю; это не сезонные средние.",
                        period_number=number,
                        statistics_scope=f"summary_{comparison_scope}",
                        statistic_metric=metric,
                        **metadata,
                    )
                if not comparable:
                    continue
                maximum = max(value for _, value in comparable)
                leaders = [player for player, value in comparable if value == maximum]
                unknown = sum(
                    player.get("played") is not False and stats.get(metric) is None
                    for player, stats in values
                )
                did_not_play_names = [
                    player["full_name"] for player in eligible if player.get("played") is False
                ]
                participation_note = (
                    f"Не выходили на площадку (DNP): {', '.join(did_not_play_names)}. "
                    if did_not_play_names
                    else ""
                )
                names = "; ".join(
                    f"{player['full_name']} (№{player['jersey_number'] or '?'}, "
                    f"{protocol[player['team_side'] + '_team_name']}, "
                    f"ID АСБ {player['external_player_id']})"
                    for player in leaders
                )
                _append_passages(
                    documents,
                    doc_id=f"game-points:{identity}:{number}:{comparison_scope}:{metric}",
                    kind="match_points_comparison",
                    title=f"{title} — максимум: {metric_label}",
                    text=prefix + f"Среди {len(comparable)} игроков с опубликованным показателем "
                    f"максимум {maximum}; "
                    f"разделяют {len(leaders)}: {names}. "
                    f"Показатель неизвестен у {unknown} игроков; DNP в это число не входит. "
                    + participation_note
                    + "Максимум этой группы и периода; не рекорд клуба.",
                    period_number=number,
                    statistics_scope=f"comparison_{comparison_scope}",
                    statistic_metric=metric,
                    **metadata,
                )


def _bounded_groups(values: list[str], maximum: int) -> list[list[str]]:
    groups: list[list[str]] = []
    for value in values:
        if not groups or sum(len(item) + 2 for item in groups[-1]) + len(value) > maximum:
            groups.append([])
        groups[-1].append(value)
    return groups


async def _append_current_rosters(
    session: AsyncSession,
    documents: list[KnowledgeDoc],
    *,
    now: datetime | None = None,
) -> None:
    """Current published ASB members, or an explicitly historical public fallback.

    Historical members never silently become the current team. Birth dates,
    personal contacts, photo origins and provider identifiers are not included.
    """
    season = current_season((now or datetime.now(UTC)).date())
    for category, label in (("men", "мужской"), ("women", "женской")):
        snapshot = await session.scalar(
            select(AsbRosterSnapshot).where(
                AsbRosterSnapshot.team == category,
                AsbRosterSnapshot.season == season,
            )
        )
        if not snapshot or not snapshot.is_published:
            snapshot = await session.scalar(
                select(AsbRosterSnapshot)
                .where(
                    AsbRosterSnapshot.team == category,
                    AsbRosterSnapshot.season <= season,
                    AsbRosterSnapshot.is_published.is_(True),
                )
                .order_by(AsbRosterSnapshot.season.desc())
                .limit(1)
            )
        selected_season = snapshot.season if snapshot else season
        members = []
        if snapshot and snapshot.is_published:
            members = list(
                (
                    await session.scalars(
                        select(AsbRosterMember)
                        .where(
                            AsbRosterMember.snapshot_id == snapshot.id,
                            AsbRosterMember.is_listed.is_(True),
                        )
                        .order_by(AsbRosterMember.full_name, AsbRosterMember.id)
                    )
                ).all()
            )
        title = f"Состав {label} команды ВГУ — сезон {selected_season}"
        summary = (
            f"По сохранённому опубликованному снимку состав {label} команды ВГУ "
            f"относится к сезону {selected_season}. "
            f"В заявке {len(members)} игроков: {', '.join(member.full_name for member in members)}."
            if members
            else f"Состав {label} команды ВГУ на сезон {season} не опубликован или не подтверждён. "
            "Список прошлых сезонов не подтверждает текущий состав."
        )
        if members and selected_season != season:
            summary += (
                f" Это последний опубликованный состав; текущий сезон {season} "
                "не подтверждён. Эта заявка не является текущим составом."
            )
        if snapshot and snapshot.is_published:
            summary += (
                f" Снимок проверен {snapshot.checked_at.isoformat()}; "
                "последующие изменения не подтверждены."
            )
        _append_passages(
            documents,
            doc_id=f"roster:{category}:{selected_season}",
            kind="roster",
            title=title,
            text=summary,
            url=f"/teams/{category}",
            team_category=category,
            season=selected_season,
            discipline="5x5",
        )
        for member in members:
            public = player_read(member)
            pieces = [
                f"По сохранённому опубликованному снимку {public.full_name} включён "
                f"в состав {label} команды ВГУ на сезон {selected_season}. "
                "Последующие изменения заявки не подтверждены."
            ]
            if selected_season != season:
                pieces.append(
                    f"Это последний опубликованный исторический состав, не текущий: "
                    f"заявка сезона {season} не подтверждена."
                )
            if public.statistics_season:
                pieces.append(
                    f"Индивидуальная статистика относится к сезону {public.statistics_season}, "
                    "он может отличаться от сезона состава. Это сезонные средние, "
                    "не показатели отдельного матча или четверти."
                )
                stat_pieces = [
                    f"{public.full_name}: сезонная статистика {label} команды ВГУ "
                    f"за {public.statistics_season}; опубликованная заявка {selected_season}. "
                    "Показатели в среднем за матч за этот сезон статистики. "
                    "Это не статистика отдельного матча или четверти; "
                    "отсутствие показателя не означает ноль."
                ]
                for label_stat, value in (
                    ("Матчей", public.games_played),
                    ("Очков в среднем за матч", public.points_per_game),
                    ("Передач в среднем за матч", public.assists_per_game),
                    ("Подборов в среднем за матч", public.rebounds_per_game),
                    ("Минут в среднем за матч", public.minutes_per_game),
                ):
                    if value is not None:
                        stat_pieces.append(f"{label_stat}: {value}")
                _append_passages(
                    documents,
                    doc_id=f"season-player-stats:{member.id}",
                    kind="player_season_stats",
                    title=f"{public.full_name} — сезонная статистика {public.statistics_season}",
                    text=" ".join(stat_pieces),
                    url=f"/teams/{category}/players/{member.id}",
                    team_category=category,
                    season=public.statistics_season,
                    statistics_season=public.statistics_season,
                    statistics_scope="season",
                    player_name=public.full_name,
                    source_label="Опубликованная статистика АСБ",
                    source_url=public.statistics_source_url,
                    discipline="5x5",
                )
            else:
                pieces.append(
                    "Индивидуальная статистика не опубликована; "
                    "отсутствие показателя не означает ноль."
                )
            _append_passages(
                documents,
                doc_id=f"roster-player:{member.id}",
                kind="roster_player",
                title=public.full_name,
                text=" ".join(pieces),
                url=f"/teams/{category}/players/{member.id}",
                team_category=category,
                season=selected_season,
                player_name=public.full_name,
                discipline="5x5",
            )
        _append_points_comparisons(documents, members, category=category, season=selected_season)


def _append_points_comparisons(
    documents: list[KnowledgeDoc],
    members: list[AsbRosterMember],
    *,
    category: str,
    season: str,
) -> None:
    """Compare complete structured public snapshots, never a retrieved top-k.

    Statistics from different seasons or teams are separate comparisons. Raw
    totals/games determine ties before display rounding; missing values and
    zero games never become zero points per game.
    """
    groups: dict[str, list[AsbRosterMember]] = {}
    for member in members:
        if (
            member.statistics_season
            and member.games_played is not None
            and member.games_played > 0
            and member.points_total is not None
        ):
            groups.setdefault(_season_key(member.statistics_season), []).append(member)
    for statistics_season, comparable in sorted(groups.items()):
        averages = {
            member.id: Fraction(member.points_total or 0, member.games_played or 1)
            for member in comparable
        }
        maximum = max(averages.values())
        leaders = [member for member in comparable if averages[member.id] == maximum]
        scope = ("Мужская команда ВГУ;" if category == "men" else "Женская команда ВГУ;") + (
            f" опубликованная заявка сезона {season}; статистика сезона {statistics_season}. "
            f"Метрика: очки в среднем за матч. Число сопоставимых игроков: {len(comparable)}. "
            "Учтена вся заявка с опубликованными очками и положительным числом матчей "
            "за этот сезон статистики. "
        )
        # Repeat the scope and limitations on every tied leader's passage, so
        # passage limits cannot turn a subset into an unrestricted club record.
        for index, leader in enumerate(leaders):
            documents.append(
                KnowledgeDoc(
                    doc_id=f"points-comparison:{category}:{season}:{statistics_season}:{index}",
                    kind="points_comparison",
                    title=f"Сравнение результативности: {category}, {statistics_season}",
                    text=(
                        scope + f"Максимум в этой группе: {round(float(maximum), 2)}. "
                        f"Игрок с этим максимумом: {leader.full_name}; "
                        f"матчей: {leader.games_played}. "
                        f"Всего разделяют максимум: {len(leaders)}. "
                        f"Вне сравнения этой группы: {len(members) - len(comparable)} игроков "
                        "(другой сезон статистики или недостаточно данных). "
                        "Число матчей у игроков может различаться. Это не лидер всего клуба "
                        "за все сезоны; без статистики показатель неизвестен, а не равен нулю."
                    ),
                    url=f"/teams/{category}",
                    team_category=category,
                    season=statistics_season,
                    discipline="5x5",
                )
            )


def corpus_hash(documents: list[KnowledgeDoc]) -> str:
    """Hash source text plus metadata used by deterministic retrieval."""
    canonical = [
        {
            "doc_id": doc.doc_id,
            "kind": doc.kind,
            "title": doc.title,
            "text": doc.text,
            "url": doc.url,
            "starts_at": doc.starts_at.isoformat() if doc.starts_at else None,
            "match_status": doc.match_status,
            "team_category": doc.team_category,
            "season": doc.season,
            "discipline": doc.discipline,
            "event_year": doc.event_year,
            "date_precision": doc.date_precision,
            "source_label": doc.source_label,
            "source_url": doc.source_url,
            "opponent_name": doc.opponent_name,
            "competition_name": doc.competition_name,
            "is_title": doc.is_title,
            "club_score": doc.club_score,
            "opponent_score": doc.opponent_score,
            "club_side": doc.club_side,
            "match_id": doc.match_id,
            "player_name": doc.player_name,
            "player_id": doc.player_id,
            "player_team_side": doc.player_team_side,
            "period_number": doc.period_number,
            "statistics_scope": doc.statistics_scope,
            "statistics_season": doc.statistics_season,
            "statistic_metric": doc.statistic_metric,
        }
        for doc in sorted(documents, key=lambda doc: doc.doc_id)
    ]
    canonical_json = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    encoded = canonical_json.encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _local_start(doc: KnowledgeDoc) -> datetime | None:
    if doc.starts_at is None:
        return None
    value = doc.starts_at
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(_MOSCOW)


def _is_fixture(doc: KnowledgeDoc) -> bool:
    return doc.starts_at is not None and (
        doc.kind == "match"
        and doc.date_precision not in ("month", "year")
        or doc.kind == "result"
        and doc.date_precision in ("day", "minute")
        and doc.opponent_name is not None
    )


def _unique_fixtures(documents: list[KnowledgeDoc]) -> list[KnowledgeDoc]:
    """Keep passages, prefer a timed fixture over its day-only archive copy.

    Match identity uses structured metadata, never arbitrary source prose. If
    two timed meetings share a day, do not collapse them into a single match.
    Unknown identity is kept, rather than guessing that two records duplicate.
    """
    identities: dict[tuple[str | None, ...], list[KnowledgeDoc]] = {}
    for doc in documents:
        local = _local_start(doc)
        identity: tuple[str | None, ...] = (
            doc.team_category,
            doc.discipline,
            _season_key(doc.season) if doc.season else None,
            local.date().isoformat() if local else None,
            normalize_text(doc.opponent_name) if doc.opponent_name else None,
            normalize_text(doc.competition_name) if doc.competition_name else None,
            doc.match_status,
        )
        if not doc.opponent_name or not doc.competition_name:
            identity = (doc.doc_id.split(":part", maxsplit=1)[0],)
        identities.setdefault(identity, []).append(doc)
    selected: list[KnowledgeDoc] = []
    for group in identities.values():
        bases = {doc.doc_id.split(":part", maxsplit=1)[0] for doc in group}
        timed_starts = {_local_start(doc) for doc in group if doc.date_precision != "day"}
        if len(bases) == 1 or len(timed_starts) > 1:
            selected.extend(group)
            continue
        preferred = min(
            group,
            key=lambda doc: (doc.date_precision == "day", doc.kind != "match", doc.doc_id),
        ).doc_id.split(":part", maxsplit=1)[0]
        selected.extend(doc for doc in group if doc.doc_id.split(":part")[0] == preferred)
    return selected


def _comparison_guard(category: str | None, season: str | None) -> KnowledgeDoc:
    return KnowledgeDoc(
        "points-comparison:scope",
        "points_comparison_scope",
        "Границы сравнения результативности игроков",
        "Карточки отдельных игроков и их выборка не подтверждают самого результативного "
        "игрока клуба. Подтверждён только максимум очков в среднем за матч внутри каждой "
        "отдельной полной сводки: одна команда, один сезон статистики и опубликованная "
        "заявка. Такие максимумы не доказывают лидерство всего клуба за все сезоны. "
        "При отсутствии полной сводки или нужного сезона определить лидера нельзя. "
        "Разные число матчей и сезоны нужно называть явно; отсутствие статистики не равно нулю.",
        f"/teams/{category}" if category else "/club",
        team_category=category,
        season=season,
        discipline="5x5",
    )


_GAME_STAT_KINDS = {
    "match_period",
    "match_player_stats",
    "match_stats_coverage",
    "match_points_comparison",
    "match_player_summary",
}


def _requested_period(normalized: str) -> int | None:
    """Period numbers refer to official periods, never fractions of final scores."""
    overtime = re.search(r"\b(?:от|ot)\s*([1-9])\b", normalized)
    if overtime:
        return 4 + int(overtime.group(1))
    if not re.search(r"четверт|период|оверта", normalized):
        return None
    if "оверта" in normalized:
        numbered_overtime = re.search(
            r"\b([1-9])(?:\s*[-–]?\s*[аяйюеи]{1,2})?\s*оверта", normalized
        )
        if numbered_overtime:
            return 4 + int(numbered_overtime.group(1))
        for stem, number in (("перв", 5), ("втор", 6), ("трет", 7)):
            if any(word.startswith(stem) for word in _WORD_RE.findall(normalized)):
                return number
    numbered = re.search(r"\b([1-9])(?:\s*[-–]?\s*[аяйюеи]{1,2})?\s*(?:четверт|период)", normalized)
    if numbered:
        return int(numbered.group(1))
    for stems, number in (
        (("перв",), 1),
        (("втор",), 2),
        (("трет", "треть", "третя"), 3),
        (("четвертую", "четвертой", "четвертая", "четвертом"), 4),
        (("пят",), 5),
        (("шест",), 6),
    ):
        if any(
            word.startswith(stems) for word in _WORD_RE.findall(normalized) if word != "четверть"
        ):
            return number
    return None


def _requested_statistic(normalized: str) -> str:
    for stems, metric in (
        (("передач", "ассист"), "assists"),
        (("подбор", "подбир"), "rebounds"),
        (("минут", "секунд"), "minutes_seconds"),
        (("перехват",), "steals"),
        (("блок",), "blocks"),
        (("потер",), "turnovers"),
    ):
        if any(word.startswith(stems) for word in _WORD_RE.findall(normalized)):
            return metric
    return "points"


def _game_statistics_sources(
    scoped: list[KnowledgeDoc],
    *,
    normalized: str,
    current: datetime,
    period: int | None,
    period_query: bool,
    player_names: set[str],
    player_query: bool,
    comparison: bool,
    latest: bool,
    limit: int,
) -> list[KnowledgeDoc]:
    fixtures = _unique_fixtures(
        [
            doc
            for doc in scoped
            if _is_fixture(doc)
            and doc.match_status == "finished"
            and (local := _local_start(doc)) is not None
            and local <= current
        ]
    )
    if latest and len({doc.team_category for doc in fixtures if doc.team_category}) > 1:
        fixtures = []  # A two-team personal-stat question needs an explicit team.
    if latest and fixtures:
        last = max(_local_start(doc) or current for doc in fixtures)
        last_day = last.date()
        if any(
            doc.date_precision == "day" and (_local_start(doc) or current).date() == last_day
            for doc in fixtures
        ):
            fixtures = [
                doc for doc in fixtures if (_local_start(doc) or current).date() == last_day
            ]
        else:
            fixtures = [doc for doc in fixtures if _local_start(doc) == last]
    identities = {doc.match_id or doc.doc_id.split(":part")[0] for doc in fixtures}
    if len(identities) != 1:
        return [
            KnowledgeDoc(
                "match-statistics:scope",
                "match_stats_scope",
                "Уточнение матча для статистики",
                "Для статистики нужно уточнить команду, соперника или дату конкретного матча. "
                "Не выбирай произвольный протокол и не заменяй его сезонными средними.",
                "/matches",
            )
        ][:limit]
    identity = next(iter(identities))
    fixture = fixtures[0]
    data = [doc for doc in scoped if doc.match_id == identity and doc.kind in _GAME_STAT_KINDS]
    metric = _requested_statistic(normalized)
    opponent_name = fixture.opponent_name or ""
    opponent_subjects = {
        normalize_text(re.sub(r"\s*\([^)]*\)", "", opponent_name)).strip(),
        *(
            normalize_text(alias)
            for alias in re.findall(r"\b[А-ЯЁA-Z]{2,}(?:-\d+)?\b", opponent_name)
        ),
    } - {""}
    opponent_subject = any(
        re.search(r"(?:\bу\s+|игрок\w*\s+)" + re.escape(subject) + r"(?=$|[^\w-])", normalized)
        for subject in opponent_subjects
    )
    comparison_scope = (
        "opponent"
        if re.search(r"соперник", normalized) or opponent_subject
        else "club"
        if re.search(r"\bнаш|\bвгу\b|команд", normalized)
        else "all"
    )

    def wanted_period(doc: KnowledgeDoc) -> bool:
        if period is not None:
            return doc.period_number == period
        return doc.period_number is not None if period_query else doc.period_number is None

    if player_query:
        wanted = [doc for doc in data if doc.kind == "match_player_stats"]
        wanted = [doc for doc in wanted if wanted_period(doc)]
        if player_names:
            wanted = [doc for doc in wanted if doc.player_name in player_names]
        elif not comparison:
            if any(
                word.startswith(("игрок", "кажд", "всем", "всех"))
                for word in _WORD_RE.findall(normalized)
            ):
                wanted = [
                    doc
                    for doc in data
                    if doc.kind == "match_player_summary"
                    and wanted_period(doc)
                    and doc.statistic_metric == metric
                    and doc.statistics_scope == f"summary_{comparison_scope}"
                ]
            else:
                # A name not present in official data must not select another player.
                wanted = []
        if comparison and not player_names:
            wanted = [
                doc
                for doc in data
                if doc.kind == "match_points_comparison"
                and wanted_period(doc)
                and doc.statistics_scope == f"comparison_{comparison_scope}"
                and doc.statistic_metric == metric
            ]
        if player_names and len({(doc.player_team_side, doc.player_id) for doc in wanted}) > 1:
            return [
                KnowledgeDoc(
                    f"match-statistics:player-ambiguous:{identity}",
                    "match_stats_scope",
                    "Нужно уточнить игрока",
                    f"В протоколе {fixture.title} найдены разные игроки с подходящими именами. "
                    "Уточни полное имя, команду или игровой номер; не объединяй их статистику.",
                    fixture.url,
                )
            ][:limit]
    else:
        wanted = [
            doc
            for doc in data
            if doc.kind == "match_period" and (period is None or doc.period_number == period)
        ]
    scope = (
        f"Матч: {fixture.title}; сезон {fixture.season}; "
        f"дата {(_local_start(fixture) or current).date().isoformat()}. "
    )
    category_label = "мужской" if fixture.team_category == "men" else "женской"
    latest_fact = (
        f"По опубликованному календарю на {current.astimezone(_MOSCOW).date().isoformat()} "
        f"это последний завершённый матч {category_label} "
        "команды в выбранных условиях. "
        if latest
        else ""
    )
    if not wanted:
        metric_label = dict(_STAT_LABELS).get(metric, metric)
        published_periods = sorted(
            {doc.period_number for doc in data if doc.kind == "match_period" and doc.period_number}
        )
        coverage = (
            "В этом протоколе опубликованы периоды: "
            + ", ".join(str(number) for number in published_periods)
            + ". "
            if published_periods
            else ""
        )
        detail = (
            f"Показатель «{metric_label}» игроков за период {period or 'из запроса'} "
            "не опубликован "
            "в найденном официальном протоколе."
            if player_query and period_query
            else f"Показатель «{metric_label}» нужного игрока за матч не опубликован "
            "в найденном протоколе."
            if player_query
            else f"Счёт периода {period} не опубликован в найденном официальном протоколе."
            if period is not None
            else "Счета периодов не опубликованы в найденном официальном протоколе."
        )
        return [
            KnowledgeDoc(
                f"match-statistics:missing:{identity}:{period}",
                "match_stats_scope",
                f"Нет подтверждённого показателя: {fixture.title}",
                scope
                + latest_fact
                + coverage
                + detail
                + " Скажи об этом коротко. Отсутствие показателя не равно нулю; "
                "нельзя вычислять четверти или индивидуальные очки из итогового счёта "
                "либо сезонных средних.",
                data[0].url if data else fixture.url,
                match_id=identity,
                team_category=fixture.team_category,
                season=fixture.season,
                period_number=period,
                starts_at=fixture.starts_at,
            )
        ][:limit]
    # Do not truncate a tie/comparison into an assertion about only one leader.
    if (comparison or not player_names and player_query) and len(wanted) > limit:
        return [
            KnowledgeDoc(
                f"match-statistics:comparison-limit:{identity}:{period}",
                "match_stats_scope",
                "Недостаточно места для полного сравнения",
                scope + "Полная сводка разделивших максимум не помещается в ответ. "
                "Нельзя называть одного лидера по усечённой выборке.",
                fixture.url,
            )
        ][:limit]
    selected = sorted(wanted, key=lambda doc: (doc.period_number or 0, doc.doc_id))[:limit]
    return (
        [replace(doc, text=latest_fact + doc.text) for doc in selected] if latest_fact else selected
    )


@dataclass(frozen=True, slots=True)
class _ScoreMarginQuery:
    threshold: int
    comparison: str
    outcome: str | None
    club_side: str | None


def _score_margin_query(normalized: str) -> _ScoreMarginQuery | None:
    """Recognize a fixture's score difference, separately from player points."""
    words = _WORD_RE.findall(normalized)
    loss = any(word.startswith(("проигр", "уступ", "поражен")) for word in words)
    win = any(word.startswith(("выигр", "побед")) for word in words)
    fixture = any(word.startswith(("матч", "встреч")) for word in words)
    margin = any(word.startswith(("разниц", "отрыв")) for word in words)
    if not ((loss or win) or fixture and margin):
        return None
    match = re.search(
        r"\b(не\s+меньше|не\s+менее|не\s+больше|не\s+более|как\s+минимум|"
        r"как\s+максимум|больше|более|свыше|меньше|менее)\s+(\d{1,3})\b(?![.,]\d)",
        normalized,
    )
    if not match or not (margin or re.search(r"\d+\s+очк", normalized)):
        return None
    operator, threshold = match.groups()
    operator = " ".join(operator.split())
    comparison = (
        "gte"
        if operator in ("не меньше", "не менее", "как минимум")
        else "lte"
        if operator in ("не больше", "не более", "как максимум")
        else "gt"
        if operator in ("больше", "более", "свыше")
        else "lt"
    )
    home = any(word == "дома" or word.startswith("домашн") for word in words)
    away = any(word == "гостях" or word.startswith("выезд") for word in words)
    return _ScoreMarginQuery(
        int(threshold),
        comparison,
        "loss" if loss and not win else "win" if win and not loss else None,
        "home" if home and not away else "away" if away and not home else None,
    )


def _score_margin_sources(
    query: _ScoreMarginQuery,
    documents: list[KnowledgeDoc],
    *,
    limit: int,
    current: datetime,
) -> list[KnowledgeDoc]:
    """Return positive witnesses from structured final scores, not a record.

    A missing witness says nothing about unrecorded matches. Club orientation
    comes from score metadata, and home/away only from an explicit club side.
    """
    operator = {"gt": "больше", "gte": "не меньше", "lt": "меньше", "lte": "не больше"}[
        query.comparison
    ]
    condition = f"{operator} {query.threshold} очков"
    witnesses: list[KnowledgeDoc] = []
    seen: set[str] = set()
    for doc in sorted(documents, key=lambda doc: (_local_start(doc) or current, doc.doc_id)):
        identity = doc.doc_id.split(":part", maxsplit=1)[0]
        if identity in seen:
            continue
        seen.add(identity)
        if (
            doc.kind not in ("match", "result")
            or doc.match_status != "finished"
            or type(doc.club_score) is not int
            or type(doc.opponent_score) is not int
            or doc.club_score < 0
            or doc.opponent_score < 0
            or not doc.opponent_name
            or query.club_side is not None
            and doc.club_side != query.club_side
        ):
            continue
        local = _local_start(doc)
        if (
            local is not None
            and local > current
            or (doc.event_year is not None and doc.event_year > current.astimezone(_MOSCOW).year)
        ):
            continue
        difference = doc.club_score - doc.opponent_score
        if query.outcome == "loss" and difference >= 0:
            continue
        if query.outcome == "win" and difference <= 0:
            continue
        margin = abs(difference)
        matches = {
            "gt": margin > query.threshold,
            "gte": margin >= query.threshold,
            "lt": margin < query.threshold,
            "lte": margin <= query.threshold,
        }[query.comparison]
        if not matches:
            continue
        if local is not None:
            event_date = (
                local.date().isoformat() if doc.date_precision == "day" else local.isoformat()
            )
        else:
            event_date = (
                f"год {doc.event_year}; точная дата неизвестна"
                if doc.event_year is not None
                else "точная дата неизвестна"
            )
        outcome = "поражение ВГУ" if difference < 0 else "победа ВГУ" if difference > 0 else "ничья"
        witnesses.append(
            KnowledgeDoc(
                doc_id=f"score-margin:{identity}",
                kind="score_margin_witness",
                title=f"ВГУ — {doc.opponent_name}: подтверждённая разница счёта",
                text=(
                    f"Подтверждённый завершённый матч: ВГУ — {doc.opponent_name}. "
                    f"Категория: {doc.team_category}; дисциплина: {doc.discipline}; "
                    f"сезон: {doc.season}; соревнование: {doc.competition_name}; "
                    f"дата: {event_date}. "
                    f"Очки ВГУ: {doc.club_score}; очки соперника: {doc.opponent_score}. "
                    f"Счёт в порядке ВГУ:соперник — {doc.club_score}:{doc.opponent_score}. "
                    f"Разница: {margin} очков, {outcome}; это {condition}. "
                    "Это положительный пример из сохранённого архива, а не самая большая "
                    "разница за всю историю клуба. Домашний или выездной статус: "
                    f"{doc.club_side or 'не подтверждён'}."
                ),
                url=doc.url,
                starts_at=doc.starts_at,
                match_status=doc.match_status,
                team_category=doc.team_category,
                season=doc.season,
                discipline=doc.discipline,
                event_year=doc.event_year,
                date_precision=doc.date_precision,
                source_label=doc.source_label,
                source_url=doc.source_url,
                opponent_name=doc.opponent_name,
                competition_name=doc.competition_name,
                club_score=doc.club_score,
                opponent_score=doc.opponent_score,
                club_side=doc.club_side,
            )
        )
        if len(witnesses) >= limit:
            break
    if witnesses:
        return witnesses
    return [
        KnowledgeDoc(
            "score-margin:scope",
            "score_margin_scope",
            "Ограничения сохранённого архива счетов",
            f"В сохранённых опубликованных результатах с подтверждённым счётом "
            f"не найден подходящий пример с разницей {condition} при заданных условиях. "
            "Это не доказывает, что таких матчей никогда не было: архив может быть неполным, "
            "а часть счетов или домашних/выездных статусов неизвестна. Подтверждённого "
            "рекорда за всю историю клуба здесь нет. По названию зала нельзя определить, "
            "играла ли команда дома или на выезде.",
            "/matches",
        )
    ]


def retrieve(
    query: str,
    documents: list[KnowledgeDoc],
    limit: int = _DEFAULT_LIMIT,
    *,
    now: datetime | None = None,
    team_category: str | None = None,
) -> list[KnowledgeDoc]:
    if limit <= 0:
        return []
    normalized = normalize_text(query)
    for alias, replacement in (
        (r"\bq\s*([1-4])\b", r"\1 четверть"),
        (r"\bquarter\s*([1-4])\b", r"\1 четверть"),
        (r"\bnext\s+(?:match|game)\b", "следующий матч"),
        (r"\broster\b", "состав"),
        (r"\bbasketball\b", "баскетбол"),
        (r"\bteam\b", "команда"),
        (r"\bwomen(?:['’]s)?\b", "женская"),
        (r"\bmen(?:['’]s)?\b", "мужская"),
        (r"\bkak\b", "как"),
        (r"\bkada\b", "когда"),
        (r"\bstat\b", "стать"),
        (r"\bigrokom\b", "игроком"),
        (r"\bkluba\b", "клуба"),
        (r"\bpopast\b", "попасть"),
        (r"\btrenirovku\b", "тренировку"),
        (r"\bposmotret\b", "посмотреть"),
        (r"\braspisanie\b", "расписание"),
        (r"\bna\s+etu\s+nedelu\b", "на эту неделю"),
        (r"\bmatchei\b", "матчей"),
        (r"\bmatche\b", "матче"),
        (r"\bresults?\b", "результаты"),
        (r"\bproshlyi\b", "прошлый"),
        (r"\bsezon\b", "сезон"),
        (r"\bvgpu\b", "вгпу"),
        (r"\bnoyabrya\b", "ноября"),
        (r"\bкада\b", "когда"),
        (r"\bростер\w*\b", "состав"),
        (r"\bзаявк\w*\b", "состав"),
    ):
        normalized = re.sub(alias, replacement, normalized)
    normalized = re.sub(
        r"\b(?:ot|от)\s*([1-9])\b",
        lambda match: f"{4 + int(match.group(1))} период",
        normalized,
    )
    words = _WORD_RE.findall(normalized)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    local_day = current.astimezone(_MOSCOW).date()
    week_start = local_day - timedelta(days=local_day.weekday())
    calendar_window = None
    if any(word.startswith("выходн") for word in words):
        saturday = week_start + timedelta(days=5)
        if any(word.startswith("следующ") for word in words):
            saturday += timedelta(days=7)
        calendar_window = (saturday, saturday + timedelta(days=1))
    elif any(word.startswith("недел") for word in words):
        offset = (
            7
            if any(word.startswith("следующ") for word in words)
            else -7
            if any(word.startswith("прошл") for word in words)
            else 0
        )
        calendar_window = (
            week_start + timedelta(days=offset),
            week_start + timedelta(days=offset + 6),
        )
    navigation_target = None
    navigation_documents: list[KnowledgeDoc] = []
    asks_navigation = (
        any(
            word.startswith(("посмотр", "найти", "откры", "расписан", "календар", "ссылк"))
            for word in words
        )
        or "где" in words
        and any(
            word.startswith(("сайт", "раздел", "истор", "состав", "результат")) for word in words
        )
    )
    if asks_navigation and any(
        word.startswith(("матч", "игр", "расписан", "календар", "результат")) for word in words
    ):
        navigation_target = "matches"
    elif asks_navigation and any(word.startswith(("таблиц", "турнирн")) for word in words):
        navigation_target = "standings"
    elif asks_navigation and any(word.startswith("истор") for word in words):
        navigation_target = "history"
    elif any(
        word.startswith(("связа", "контакт", "записа", "попаст", "вступ", "трениров"))
        for word in words
    ):
        navigation_target = "contacts"
    elif asks_navigation and any(word.startswith(("правил", "баскетбол")) for word in words):
        navigation_target = "rules"
    elif ("кто" in words and "ты" in words) or any(
        word.startswith(("маскот", "талисман")) for word in words
    ):
        navigation_target = "bobrik"
    if navigation_target:
        found = [doc for doc in documents if doc.doc_id == f"navigation:{navigation_target}"]
        if found:
            if (navigation_target != "matches" or calendar_window is None) and not any(
                word.startswith(("четверт", "статист", "протокол")) for word in words
            ):
                return found[:limit]
            # A calendar question needs dated records as well as the page link.
            navigation_documents = found
    category = team_category if team_category in ("men", "women") else None
    women = any(word.startswith(("жен", "девуш")) for word in words)
    men = any(word.startswith(("муж", "парн", "юнош")) for word in words)
    if women != men:
        category = "women" if women else "men"
    elif women and men:
        category = None
    discipline = None
    if re.search(r"3\s*[xх×]\s*3|три\s+на\s+три", normalized):
        discipline = "3x3"
    elif "фиджитал" in normalized or "phygital" in normalized:
        discipline = "phygital"
    elif re.search(r"5\s*[xх×]\s*5|пять\s+на\s+пять", normalized):
        discipline = "5x5"
    season_match = re.search(r"\b20\d{2}\s*[-/]\s*(?:20\d{2}|\d{2})(?![-\d])\b", normalized)
    requested_season = _season_key(season_match.group()) if season_match else None
    years = {int(year) for year in re.findall(r"\b20\d{2}\b", normalized)}
    requested_day = None
    requested_month_day: tuple[int, int] | None = None
    for marker, delta in (("сегодня", 0), ("завтра", 1), ("вчера", -1)):
        if marker in words:
            requested_day = (current.astimezone(_MOSCOW) + timedelta(days=delta)).date()
            break
    date_match = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b", normalized)
    iso_match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", normalized)
    month_names = (
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
    named_date = re.search(
        r"\b(\d{1,2})\s+(" + "|".join(month_names) + r")(?:\s+(20\d{2}))?\b", normalized
    )
    try:
        if date_match:
            day, month, year = (int(value) for value in date_match.groups())
            requested_day = datetime(year, month, day).date()
        elif iso_match:
            year, month, day = (int(value) for value in iso_match.groups())
            requested_day = datetime(year, month, day).date()
        elif named_date:
            day = int(named_date.group(1))
            month = month_names.index(named_date.group(2)) + 1
            if named_date.group(3):
                requested_day = datetime(int(named_date.group(3)), month, day).date()
            else:
                # A missing year is not permission to assume the current one.
                datetime(2000, month, day)  # Validate day/month, including leap day.
                requested_month_day = (month, day)
    except ValueError:
        return []  # An impossible requested date must not retrieve a different day.

    has_match = any(
        word.startswith(("матч", "встреч"))
        or word.startswith("игр")
        and not word.startswith("игрок")
        for word in words
    )
    opponent_aliases = {
        normalize_text(alias)
        for doc in documents
        if doc.kind == "match" or doc.opponent_name is not None
        for alias in re.findall(r"\b[А-ЯЁA-Z]{2,}(?:-\d+)?\b", doc.opponent_name or doc.title)
        if normalize_text(alias) not in {"вгу", "бк", "сск"}
    }
    opponent_words = set(re.findall(r"[а-яa-z0-9]+(?:-\d+)?", normalized))
    requested_opponents = {alias for alias in opponent_aliases if alias in opponent_words}
    if has_match:
        explicit_opponent = re.search(
            r"(?:\bс\s+|\bпротив\s+|\bвгу\s*[—–-]\s*)([а-яa-z]+(?:-\d+)?)",
            normalized,
        )
        if explicit_opponent and not explicit_opponent.group(1).startswith(
            (
                "наш",
                "муж",
                "жен",
                "соперник",
                "вгу",
                "команд",
                "учет",
                "мяч",
                "разниц",
                "счет",
                "результ",
                "истор",
                "архив",
                "очк",
                "передач",
                "подбор",
            )
        ):
            requested_opponents.add(explicit_opponent.group(1))

    def opponent_matches(doc: KnowledgeDoc) -> bool:
        return not requested_opponents or bool(
            requested_opponents
            & set(
                re.findall(
                    r"[а-яa-z0-9]+(?:-\d+)?",
                    normalize_text(doc.opponent_name or doc.title),
                )
            )
        )

    roster_query = any(word.startswith(("состав", "игрок", "статист")) for word in words)
    if roster_query and any(word.startswith(("правил", "площадк")) for word in words):
        roster_query = False
    captain_query = any(word.startswith("капитан") for word in words)
    rules_terms = (
        "правил",
        "пробеж",
        "ведени",
        "оверта",
        "штраф",
        "двойн",
        "фол",
        "аут",
        "трехоч",
        "двухоч",
        "четвер",
        "опорн",
    )
    rules_query = any(word.startswith(rules_terms) for word in words)
    if any(word.startswith(("длит", "время")) for word in words) and has_match:
        rules_query = True
    if "сколько" in words and any(word.startswith(("секунд", "минут", "очков")) for word in words):
        rules_query = not any(
            word.startswith(("набрал", "забил", "игрок", "статист")) for word in words
        )
    if any(word.startswith("тайм") for word in words):
        rules_query = True
    if any(word.startswith("игрок") for word in words) and any(
        word.startswith("площадк") for word in words
    ):
        rules_query = True
    if any(word.startswith("рук") for word in words) and any(
        word.startswith(("брос", "задел")) for word in words
    ):
        rules_query = True

    query_name_tokens = _tokens(normalized)
    player_name_matches = {
        doc.player_name: len(_tokens(doc.player_name) & query_name_tokens)
        for doc in documents
        if doc.player_name
    }
    maximum_name_match = max(player_name_matches.values(), default=0)
    named_players = {
        name
        for name, count in player_name_matches.items()
        if count == maximum_name_match and count > 0
    }
    period = _requested_period(normalized)
    period_query = any(word.startswith(("четверт", "период", "оверта")) for word in words)
    player_terms = bool(named_players) or any(
        word.startswith(("игрок", "кажд", "подбир", "подбор", "передач", "ассист"))
        for word in words
    )
    rule_explanation = (
        any(word.startswith(("длит", "правил")) for word in words)
        or "что" in words
        and "такое" in words
        or any(word.startswith(("минут", "секунд")) for word in words)
        and not player_terms
    )
    season_average = bool(re.search(r"в\s+среднем|сезонн|за\s+сезон", normalized))
    game_statistics = (
        period_query
        and not rule_explanation
        or (has_match or requested_day is not None or bool(requested_opponents))
        and not rule_explanation
        and not season_average
        and (
            bool(named_players)
            or any(
                word.startswith(
                    (
                        "статист",
                        "протокол",
                        "набрал",
                        "очк",
                        "подбор",
                        "подбир",
                        "передач",
                        "результатив",
                    )
                )
                for word in words
            )
            or "boxscore" in words
            or "кто" in words
            and any(word.startswith(("больш", "результатив", "лидер")) for word in words)
        )
    )
    if _score_margin_query(normalized) is not None and not period_query:
        game_statistics = False
    if game_statistics or named_players:
        rules_query = False

    def compatible(doc: KnowledgeDoc) -> bool:
        if (
            doc.kind in ("match", "result") or doc.kind in _GAME_STAT_KINDS
        ) and not opponent_matches(doc):
            return False
        if category and doc.team_category and doc.team_category != category:
            return False
        if discipline and doc.discipline and doc.discipline != discipline:
            return False
        if requested_season and doc.season and _season_key(doc.season) != requested_season:
            return False
        if requested_season and doc.kind.startswith("roster") and not doc.season:
            return False
        if (
            years
            and not requested_season
            and doc.event_year is not None
            and doc.event_year not in years
        ):
            return False
        if requested_day and (doc.kind in ("match", "result") or doc.kind in _GAME_STAT_KINDS):
            local = _local_start(doc)
            return bool(local and local.date() == requested_day)
        if requested_month_day and (
            doc.kind in ("match", "result") or doc.kind in _GAME_STAT_KINDS
        ):
            local = _local_start(doc)
            return bool(local and (local.month, local.day) == requested_month_day)
        return True

    scoped = [doc for doc in documents if compatible(doc)]
    if game_statistics:
        if calendar_window:
            first_day, last_day = calendar_window
            scoped = [
                doc
                for doc in scoped
                if (local := _local_start(doc)) is not None
                and first_day <= local.date() <= last_day
            ]
        comparison = (
            any(
                word in ("кто", "кого") or word.startswith(("лучш", "лидер", "топ"))
                for word in words
            )
            or bool(re.search(r"\bсам\w*\s+результатив", normalized))
        ) and any(
            word.startswith(
                ("очк", "набрал", "результатив", "больш", "подбир", "подбор", "передач")
            )
            for word in words
        )
        unknown_name = re.search(r"\bу\s+([а-я]+)", normalized)
        asks_named_unknown = bool(
            unknown_name
            and unknown_name.group(1)
            not in {
                "нас",
                "вас",
                "меня",
                "тебя",
                "него",
                "нее",
                "них",
                "вгу",
                *opponent_aliases,
            }
            and not unknown_name.group(1).startswith("соперник")
        )
        player_query = (
            player_terms
            or comparison
            or asks_named_unknown
            or not period_query
            and any(word.startswith("набрал") for word in words)
        )
        return _game_statistics_sources(
            scoped,
            normalized=normalized,
            current=current,
            period=period,
            period_query=period_query,
            player_names=named_players,
            player_query=player_query,
            comparison=comparison,
            latest=any(word.startswith(("последн", "прошедш", "недавн")) for word in words),
            limit=limit,
        )
    if calendar_window and has_match and not rules_query:
        first_day, last_day = calendar_window
        fixtures = _unique_fixtures(
            [
                doc
                for doc in scoped
                if _is_fixture(doc)
                and (local := _local_start(doc)) is not None
                and first_day <= local.date() <= last_day
            ]
        )
        fixtures.sort(key=lambda doc: (_local_start(doc) or current, doc.doc_id))
        # The actual records take precedence if the result limit is small.
        return [*fixtures, *navigation_documents][:limit]
    if (requested_day or requested_month_day) and has_match and not rules_query:
        fixtures = _unique_fixtures([doc for doc in scoped if _is_fixture(doc)])
        return sorted(fixtures, key=lambda doc: (_local_start(doc) or current, doc.doc_id))[:limit]

    margin_query = _score_margin_query(normalized)
    if margin_query is not None and not rules_query:
        return _score_margin_sources(margin_query, scoped, limit=limit, current=current)
    fixture_points_query = (
        any(
            word.startswith(("разниц", "проигр", "уступ", "поражен", "выигр", "счет"))
            for word in words
        )
        or has_match
        and not any(word.startswith(("игрок", "результатив")) for word in words)
    )
    points_query = any(
        word.startswith(("результатив", "бомбардир", "снайпер", "очк", "забива")) for word in words
    )
    comparison_query = points_query and any(
        word == "кто" or word.startswith(("сам", "лучш", "больш", "лидер", "топ")) for word in words
    )
    if comparison_query and not rules_query and not fixture_points_query:
        comparisons = [doc for doc in scoped if doc.kind == "points_comparison"]
        guard = _comparison_guard(category, requested_season)
        # Never silently truncate a complete comparison or tie to a top-k.
        if len(comparisons) >= limit:
            return [guard]
        return [guard, *sorted(comparisons, key=lambda doc: doc.doc_id)][:limit]
    competition_query = any(
        word.startswith(("соревнован", "турнир", "лиг")) for word in words
    ) and any(word.startswith(("участв", "игра", "выступ")) for word in words)
    if competition_query and not rules_query:
        wanted_season = requested_season or (
            None if years else current_season(current.astimezone(_MOSCOW).date())
        )
        groups: dict[tuple[str | None, ...], KnowledgeDoc] = {}
        recent = sorted(
            scoped, key=lambda doc: (_local_start(doc) or current, doc.doc_id), reverse=True
        )
        for doc in recent:
            if (
                doc.kind not in ("match", "result")
                or not doc.competition_name
                or not doc.season
                or (wanted_season and _season_key(doc.season) != wanted_season)
            ):
                continue
            identity = (
                doc.team_category,
                doc.discipline,
                _season_key(doc.season),
                normalize_text(doc.competition_name),
            )
            groups.setdefault(identity, doc)
        # Alternate team categories, so two-team questions cannot spend every
        # slot on several competitions of just the men's or women's team.
        by_category = [
            [doc for doc in groups.values() if doc.team_category == value]
            for value in ("men", "women", None)
        ]
        selected_competitions: list[KnowledgeDoc] = []
        while any(by_category) and len(selected_competitions) < limit:
            for choices in by_category:
                if choices and len(selected_competitions) < limit:
                    selected_competitions.append(choices.pop(0))
        return selected_competitions
    if captain_query:
        scoped = [
            doc for doc in scoped if doc.kind == "captains" or doc.doc_id == "navigation:contacts"
        ]
    elif roster_query:
        scoped = [
            doc
            for doc in scoped
            if doc.kind in ("roster", "roster_player", "player_season_stats")
            or doc.doc_id.startswith("navigation:teams-")
        ]
    elif rules_query:
        scoped = [doc for doc in scoped if doc.kind in ("rules", "player_roles", "viewing_tips")]
    history_query = any(
        word.startswith(("истор", "основ", "созда", "назван", "появил")) for word in words
    )
    history_query = history_query or (
        "расскажи" in words and any(word.startswith("клуб") for word in words)
    )
    achievements_query = (
        any(word.startswith(("побед", "выигры", "достижен", "трофе", "золот")) for word in words)
        and not has_match
    )
    if achievements_query and not (captain_query or roster_query or rules_query):
        titles = [doc for doc in scoped if doc.kind == "result" and doc.is_title]
        history_sources = [doc for doc in scoped if doc.kind == "history"]
        # First places are structured archive facts; do not infer tournament
        # titles from an isolated winning fixture or a lexical score.
        if titles:
            titles.sort(key=lambda doc: (doc.event_year or 0, doc.doc_id), reverse=True)
            unique_titles: dict[str, KnowledgeDoc] = {}
            for doc in titles:
                unique_titles.setdefault(doc.doc_id.split(":part")[0], doc)
            return list(unique_titles.values())[:limit]
        scoped = history_sources
    if history_query and not (captain_query or roster_query or rules_query):
        scoped = [doc for doc in scoped if doc.kind == "history"]
    past_match = any(
        word in {"был", "была", "были"}
        or word.startswith(("прошл", "последн", "сыгран", "состоял"))
        or (word.isdigit() and len(word) == 4)
        for word in words
    )
    next_match = (
        has_match
        and not rules_query
        and (
            ("когда" in words and not past_match)
            or any(word.startswith(("ближай", "следующ", "предстоящ")) for word in words)
        )
    )
    if next_match:
        scheduled = [
            doc
            for doc in scoped
            if _is_fixture(doc)
            and doc.match_status == "scheduled"
            and doc.starts_at is not None
            and (
                (_local_start(doc) or current).date() >= current.astimezone(_MOSCOW).date()
                if doc.date_precision == "day"
                else (_local_start(doc) or current) >= current
            )
            and (category is None or doc.team_category == category)
        ]
        scheduled = _unique_fixtures(scheduled)
        scheduled.sort(key=lambda doc: (_local_start(doc) or current, doc.doc_id))
        if not scheduled:
            return []
        # Unknown times on the first calendar day cannot be ordered as midnight.
        first_day = (_local_start(scheduled[0]) or current).date()
        if any(
            doc.date_precision == "day" and (_local_start(doc) or current).date() == first_day
            for doc in scheduled
        ):
            return [doc for doc in scheduled if (_local_start(doc) or current).date() == first_day][
                :limit
            ]
        # Return only the earliest timed fixture, including all its passages.
        first_id = scheduled[0].doc_id.split(":part", maxsplit=1)[0]
        return [doc for doc in scheduled if doc.doc_id.split(":part")[0] == first_id][:limit]

    latest_match = (
        has_match
        and not rules_query
        and any(word.startswith(("последн", "прошедш", "недавн")) for word in words)
    )
    if latest_match and not requested_day:
        finished = [
            doc
            for doc in scoped
            if _is_fixture(doc)
            and doc.match_status == "finished"
            and doc.starts_at
            and (_local_start(doc) or current) <= current
        ]
        finished = _unique_fixtures(finished)
        finished.sort(key=lambda doc: (_local_start(doc) or current, doc.doc_id), reverse=True)
        if not finished:
            return []
        last_day = (_local_start(finished[0]) or current).date()
        if any(
            doc.date_precision == "day" and (_local_start(doc) or current).date() == last_day
            for doc in finished
        ):
            return [doc for doc in finished if (_local_start(doc) or current).date() == last_day][
                :limit
            ]
        first_id = finished[0].doc_id.split(":part", maxsplit=1)[0]
        return [doc for doc in finished if doc.doc_id.split(":part")[0] == first_id][:limit]

    query_tokens = _tokens(normalized)
    if named_players:
        scoped = [doc for doc in scoped if doc.player_name in named_players]
    if (
        not game_statistics
        and any(
            word.startswith(("статист", "очк", "набрал", "подбор", "передач", "средн"))
            for word in words
        )
        and not rules_query
        and not has_match
    ):
        seasonal = [doc for doc in scoped if doc.kind == "player_season_stats"]
        if seasonal:
            scoped = seasonal
    if not query_tokens:
        return []
    history = history_query
    rules = any(word.startswith("правил") for word in words)
    role_query = any(
        word.startswith(("разыгрыва", "форвард", "центров", "позиц", "амплуа")) for word in words
    )
    tips_query = any(word.startswith(("болельщ", "трибун")) for word in words)
    term_sets = {doc.doc_id: _tokens(doc.text + " " + doc.title) for doc in scoped}
    frequency = Counter(term for terms in term_sets.values() for term in terms)
    weights = {
        term: 1 + math.log((len(scoped) + 1) / (count + 1)) for term, count in frequency.items()
    }
    scored: list[tuple[float, KnowledgeDoc]] = []
    for doc in scoped:
        score = sum(weights[term] for term in query_tokens & _tokens(doc.text))
        score += 2 * sum(weights[term] for term in query_tokens & _tokens(doc.title))
        if history and doc.kind == "history":
            score += 4
        if rules and doc.kind == "rules":
            score += 4
        if roster_query and doc.kind in ("roster", "roster_player"):
            score += 4
        if role_query and doc.kind == "player_roles":
            score += 4
        if tips_query and doc.kind == "viewing_tips":
            score += 4
        if captain_query and doc.kind == "captains":
            score += 6
        if score > 0:
            scored.append((score, doc))
    scored.sort(key=lambda pair: (-pair[0], pair[1].doc_id))
    if scored:
        selected = [doc for _, doc in scored[:limit]]
        origin_query = history and any(
            word.startswith(("основ", "созда", "появил", "назван", "имен")) for word in words
        )
        if origin_query and limit >= 2:
            # Foundation and adoption of the current name are distinct events.
            # Include the two actual archive passages, never a synthesized year.
            essential = []
            for pattern in (
                r"основан|основание|начало университетского клуба",
                r"получил[аи]? сво[ею] нынешн|нынешнее название|имя «",
            ):
                candidate = next(
                    (doc for _, doc in scored if re.search(pattern, normalize_text(doc.text))), None
                )
                if candidate and candidate not in essential:
                    essential.append(candidate)
            selected = essential + [doc for doc in selected if doc not in essential]
        return selected[:limit]
    # A typo fallback is deliberately conservative: require a long, non-numeric
    # near-match. Never approximate years, scores, names with short prefixes, or
    # use semantic/kind boosts when there is no lexical evidence at all.
    if len(query_tokens) != 1:
        return []
    fuzzy: list[tuple[float, KnowledgeDoc]] = []
    for doc in scoped:
        terms = _tokens(doc.text + " " + doc.title)
        similarities = [
            SequenceMatcher(None, query_term, term).ratio()
            for query_term in query_tokens
            for term in terms
            if len(query_term) >= 5
            and len(term) >= 5
            and not query_term.isdigit()
            and not term.isdigit()
        ]
        similarity = max(similarities, default=0.0)
        if similarity >= 0.8:
            fuzzy.append((similarity, doc))
    fuzzy.sort(key=lambda pair: (-pair[0], pair[1].doc_id))
    return [doc for _, doc in fuzzy[:limit]]


def extract_number_tokens(text: str) -> list[str]:
    return _NUMBER_RE.findall(text)


def unsupported_numbers(answer_text: str, passages: list[KnowledgeDoc]) -> list[str]:
    """Reject numeric tokens absent from cited passages, including substrings.

    This checks only numeric presence; it does not prove attribution of a score
    or verify non-numeric claims, which require later structured fact checks.
    """
    source_tokens = {token for passage in passages for token in extract_number_tokens(passage.text)}
    return [token for token in extract_number_tokens(answer_text) if token not in source_tokens]
