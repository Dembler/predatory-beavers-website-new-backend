"""Immutable public evidence for a weekly column; no invented analytics."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict

from predatory_beavers.modules.mascot.knowledge import KnowledgeDoc, corpus_hash, normalize_text

MOSCOW = timezone(timedelta(hours=3))
_UNSAFE = re.compile(
    r"игнориру\w*\s+(?:все|предыдущ|инструкц)|system\s*prompt|"
    r"ignore\s+(?:all|previous)|(?:api[_ -]?key|password|пароль)\s*[:=]|"
    r"\+\d[\d ()-]{8,}|<\s*(?:script|iframe)|javascript:|"
    r"[\w.+-]+@[\w.-]+\.[a-z]{2,}|личн\w*\s+(?:телефон|адрес)",
    re.I,
)


class FactSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str
    title: str
    url: str
    excerpt: str
    content_hash: str


class ColumnFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source_ref: str
    text: str
    kind: str
    team_scope: str


class FactSheet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    week_start: str
    week_end: str
    as_of: str
    team_scope: str
    discipline: str
    data_revision: str
    sources: list[FactSource]
    facts: list[ColumnFact]
    caveats: list[str]
    fingerprint: str


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def source_hash(doc: KnowledgeDoc) -> str:
    """Identical dependency identity for the editor and publication freshness gate."""
    return corpus_hash([doc])


def safe_public_url(value: str) -> bool:
    return bool(
        re.fullmatch(r"/(?:matches|club|rules|contacts|teams)(?:[/#?][\w/#?=&.-]*)?", value)
    )


def _safe(doc: KnowledgeDoc) -> bool:
    public_text = " ".join(
        value
        for value in (doc.text, doc.title, doc.opponent_name, doc.competition_name, doc.season)
        if value
    )
    return safe_public_url(doc.url) and not _UNSAFE.search(public_text)


def _local(doc: KnowledgeDoc) -> datetime | None:
    if doc.starts_at is None:
        return None
    return (
        doc.starts_at if doc.starts_at.tzinfo else doc.starts_at.replace(tzinfo=UTC)
    ).astimezone(MOSCOW)


def _deduplicate(docs: list[KnowledgeDoc]) -> tuple[list[KnowledgeDoc], bool]:
    groups: dict[tuple[str | None, ...], list[KnowledgeDoc]] = {}
    for doc in docs:
        local = _local(doc)
        key = (
            doc.team_category,
            doc.discipline,
            local.date().isoformat() if local else None,
            normalize_text(doc.opponent_name or ""),
            doc.match_status,
            doc.season,
        )
        groups.setdefault(key, []).append(doc)
    result = []
    conflict = False
    for group in groups.values():
        timed = [doc for doc in group if doc.date_precision == "minute"]
        timed_starts = {doc.starts_at for doc in timed}
        scores = {
            (doc.club_score, doc.opponent_score)
            for doc in group
            if doc.match_status == "finished"
            and type(doc.club_score) is int
            and type(doc.opponent_score) is int
        }
        if len(scores) > 1 and len(timed_starts) <= 1:
            conflict = True
            continue
        selected = timed or group
        seen: set[tuple[str | None, ...]] = set()
        for doc in sorted(selected, key=lambda item: (item.kind != "match", item.doc_id)):
            local = _local(doc)
            identity = (
                local.isoformat() if local else None,
                str(doc.club_score),
                str(doc.opponent_score),
            )
            if identity not in seen:
                result.append(doc)
                seen.add(identity)
    return sorted(
        result, key=lambda item: (_local(item) or datetime.min.replace(tzinfo=UTC), item.doc_id)
    ), conflict


def build_fact_sheet(
    documents: list[KnowledgeDoc],
    week_start: date,
    *,
    team_scope: str = "both",
    discipline: str = "all",
    now: datetime | None = None,
) -> FactSheet:
    if week_start.weekday() != 0:
        raise ValueError("week_start must be an ISO Monday")
    if team_scope not in {"men", "women", "both"}:
        raise ValueError("Unsupported team scope")
    if discipline not in {"5x5", "3x3", "phygital", "all"}:
        raise ValueError("Unsupported discipline")
    clock = now or datetime.now(UTC)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=UTC)
    last = week_start + timedelta(days=6)
    sources: list[FactSource] = []
    facts: list[ColumnFact] = []
    caveats = [
        "Это сохранённые публичные сведения, а не полный календарь всех игр клуба.",
        "Нет полной статистики недели: герой недели, форма и личные встречи не вычислялись.",
    ]

    def add(doc: KnowledgeDoc, text: str, kind: str) -> None:
        if doc.doc_id not in {source.ref for source in sources}:
            sources.append(
                FactSource(
                    ref=doc.doc_id,
                    title=doc.title,
                    url=doc.url,
                    excerpt=doc.text,
                    content_hash=source_hash(doc),
                )
            )
        facts.append(
            ColumnFact(
                id=f"fact-{len(facts) + 1}",
                source_ref=doc.doc_id,
                text=text,
                kind=kind,
                team_scope=doc.team_category or "both",
            )
        )

    candidates = []
    for doc in documents:
        local = _local(doc)
        if (
            doc.kind not in {"match", "result"}
            or not doc.opponent_name
            or not _safe(doc)
            or local is None
            or doc.date_precision not in {"day", "minute"}
            or not week_start <= local.date() <= last
            or doc.match_status not in {"scheduled", "finished"}
            or team_scope != "both"
            and doc.team_category != team_scope
            or discipline != "all"
            and doc.discipline != discipline
        ):
            continue
        if doc.match_status == "finished" and local > clock.astimezone(MOSCOW):
            caveats.append("Запись с будущей датой и статусом завершённой игры не использована.")
            continue
        candidates.append(doc)
    selected, conflict = _deduplicate(candidates)
    if conflict:
        caveats.append("Противоречащие друг другу счета исключены до проверки редактором.")
    if len(selected) > 8:
        caveats.append("Для выпуска выбраны только первые восемь подтверждённых записей недели.")
    for doc in selected[:8]:
        local = _local(doc)
        assert local is not None
        team = {"men": "Мужская команда", "women": "Женская команда"}.get(
            doc.team_category or "", "Команда клуба"
        )
        when = local.date().isoformat()
        if doc.date_precision == "minute":
            when += " в " + local.strftime("%H:%M") + " по Москве"
        else:
            when += "; время начала не опубликовано"
        text = (
            f"{team} ВГУ; дисциплина {doc.discipline or 'не уточнена'}; "
            f"соперник — {doc.opponent_name}; дата — {when}. "
        )
        if doc.match_status == "scheduled":
            text += "В источнике игра указана в расписании; итоговый счёт не опубликован."
            kind = "fixture"
        else:
            kind = "result"
            if (
                type(doc.club_score) is int
                and type(doc.opponent_score) is int
                and doc.club_score >= 0
                and doc.opponent_score >= 0
            ):
                result = (
                    "победа ВГУ"
                    if doc.club_score > doc.opponent_score
                    else "поражение ВГУ"
                    if doc.club_score < doc.opponent_score
                    else "в записи равный счёт; итог требует проверки редактором"
                )
                text += (
                    f"Итоговый счёт в порядке ВГУ : соперник — "
                    f"{doc.club_score}:{doc.opponent_score}; {result}."
                )
                if doc.club_score == doc.opponent_score:
                    caveats.append(
                        "Запись с равным счётом не подтверждает ничью или победителя: "
                        "нужно проверить итог матча."
                    )
            else:
                text += "Матч отмечен завершённым; подтверждённого счёта нет."
        if doc.competition_name:
            text += f" Соревнование — {doc.competition_name}."
        if doc.season:
            text += f" Сезон — {doc.season}."
        text += (
            " ВГУ играет дома."
            if doc.club_side == "home"
            else " ВГУ играет в гостях."
            if doc.club_side == "away"
            else " Домашний или выездной статус не подтверждён."
        )
        add(doc, text, kind)
    # A deliberately bounded historical sidebar, kept distinct from this week's games.
    history = sorted(
        [doc for doc in documents if doc.kind == "history" and _safe(doc)],
        key=lambda item: item.doc_id,
    )
    for doc in history[:1]:
        add(doc, "Историческая справка из сохранённого архива: " + doc.text, "history")
    if not selected:
        caveats.append("Подтверждённых матчевых записей выбранной недели в снимке не найдено.")
    body = dict(
        schema_version=1,
        week_start=week_start.isoformat(),
        week_end=last.isoformat(),
        as_of=clock.astimezone(UTC).isoformat(),
        team_scope=team_scope,
        discipline=discipline,
        data_revision=corpus_hash(documents),
        sources=sources,
        facts=facts,
        caveats=list(dict.fromkeys(caveats)),
    )
    fingerprint = canonical_hash(
        {
            **body,
            "sources": [source.model_dump() for source in sources],
            "facts": [fact.model_dump() for fact in facts],
        }
    )
    return FactSheet(**body, fingerprint=fingerprint)
