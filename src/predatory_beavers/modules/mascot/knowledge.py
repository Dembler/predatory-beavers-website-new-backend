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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from difflib import SequenceMatcher
from fractions import Fraction

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


async def _append_current_rosters(
    session: AsyncSession,
    documents: list[KnowledgeDoc],
    *,
    now: datetime | None = None,
) -> None:
    """Only the same current, published, listed ASB members as the public roster.

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
        title = f"Состав {label} команды ВГУ — сезон {season}"
        summary = (
            f"По сохранённому опубликованному снимку состав {label} команды ВГУ "
            f"относится к сезону {season}. "
            f"В заявке {len(members)} игроков: {', '.join(member.full_name for member in members)}."
            if members
            else f"Состав {label} команды ВГУ на сезон {season} не опубликован или не подтверждён. "
            "Список прошлых сезонов не подтверждает текущий состав."
        )
        if snapshot and snapshot.is_published:
            summary += (
                f" Снимок проверен {snapshot.checked_at.isoformat()}; "
                "последующие изменения не подтверждены."
            )
        _append_passages(
            documents,
            doc_id=f"roster:{category}:{season}",
            kind="roster",
            title=title,
            text=summary,
            url=f"/teams/{category}",
            team_category=category,
            season=season,
            discipline="5x5",
        )
        for member in members:
            public = player_read(member)
            pieces = [
                f"По сохранённому опубликованному снимку {public.full_name} включён "
                f"в состав {label} команды ВГУ на сезон {season}. "
                "Последующие изменения заявки не подтверждены."
            ]
            if public.statistics_season:
                pieces.append(
                    f"Индивидуальная статистика относится к сезону {public.statistics_season}, "
                    "он может отличаться от сезона состава."
                )
                for label_stat, value in (
                    ("Матчей", public.games_played),
                    ("Очков в среднем за матч", public.points_per_game),
                    ("Передач в среднем за матч", public.assists_per_game),
                    ("Подборов в среднем за матч", public.rebounds_per_game),
                    ("Минут в среднем за матч", public.minutes_per_game),
                ):
                    if value is not None:
                        pieces.append(f"{label_stat}: {value}")
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
                season=season,
                discipline="5x5",
            )
        _append_points_comparisons(documents, members, category=category, season=season)


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
            if navigation_target != "matches" or calendar_window is None:
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

    def compatible(doc: KnowledgeDoc) -> bool:
        if doc.kind in ("match", "result") and not opponent_matches(doc):
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
        if requested_day and doc.kind in ("match", "result"):
            local = _local_start(doc)
            return bool(local and local.date() == requested_day)
        if requested_month_day and doc.kind in ("match", "result"):
            local = _local_start(doc)
            return bool(local and (local.month, local.day) == requested_month_day)
        return True

    scoped = [doc for doc in documents if compatible(doc)]
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
            if doc.kind in ("roster", "roster_player") or doc.doc_id.startswith("navigation:teams-")
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
