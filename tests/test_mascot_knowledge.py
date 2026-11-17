"""Public knowledge projection and deterministic retrieval regressions."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from predatory_beavers.db.base import Base
from predatory_beavers.modules.auth import models as auth_models  # noqa: F401
from predatory_beavers.modules.club.models import Player, Team, TeamCategory
from predatory_beavers.modules.content.models import ContentBlock
from predatory_beavers.modules.mascot import knowledge
from predatory_beavers.modules.matches.models import (
    ClubSide,
    Competition,
    Match,
    MatchStatus,
    Venue,
)
from predatory_beavers.modules.results.models import ArchiveResult
from predatory_beavers.modules.roster.models import AsbRosterMember, AsbRosterSnapshot

NOW = datetime(2026, 11, 16, 15, tzinfo=UTC)


def doc(
    identity,
    text,
    *,
    category=None,
    discipline=None,
    year=None,
    season=None,
    starts_at=None,
    status=None,
    kind="result",
    precision=None,
    opponent=None,
    competition=None,
    club_score=None,
    opponent_score=None,
    club_side=None,
):
    return knowledge.KnowledgeDoc(
        identity,
        kind,
        identity,
        text,
        "/matches",
        team_category=category,
        discipline=discipline,
        event_year=year,
        season=season,
        starts_at=starts_at,
        match_status=status,
        date_precision=precision,
        opponent_name=opponent,
        competition_name=competition,
        club_score=club_score,
        opponent_score=opponent_score,
        club_side=club_side,
    )


def game_corpus():
    parent = replace(
        doc(
            "match:game",
            "ВГТУ — ВГУ",
            kind="match",
            category="men",
            discipline="5x5",
            status="finished",
            season="2026-2027",
            starts_at=NOW - timedelta(days=1),
            opponent="ВГТУ",
            club_score=70,
            opponent_score=60,
            club_side="away",
        ),
        match_id="asb:123",
    )
    documents = [parent]
    protocol = {
        "source": "asb",
        "external_id": "123",
        "team": "men",
        "season": "2026-2027",
        "discipline": "5x5",
        "club_side": "away",
        "home_team_name": "ВГТУ",
        "away_team_name": "ВГУ",
        "home_score": 60,
        "away_score": 70,
        "source_url": "https://asb.sportoteka.org/api/abc/games/game?id=123",
        "source_label": "Официальный протокол АСБ",
        "player_periods_status": "partial",
        "period_scores": [
            {
                "number": number,
                "kind": "quarter",
                "label": f"Q{number}",
                "home_score": home,
                "away_score": away,
            }
            for number, home, away in ((1, 18, 20), (2, 19, 17), (3, 11, 20), (4, 12, 13))
        ],
        "box_score": [
            {
                "external_player_id": player_id,
                "full_name": name,
                "jersey_number": jersey,
                "team_side": side,
                "statistics": {
                    "points": points,
                    "rebounds": rebounds,
                    "assists": assists,
                    "minutes_seconds": 1200,
                },
                "period_statistics": [{"period": 3, "statistics": {"points": period_points}}],
                "birth_date": "PRIVATE-BIRTHDAY",
                "phone": "PRIVATE-PHONE",
            }
            for player_id, name, jersey, side, points, rebounds, assists, period_points in (
                ("1", "Иван Петров", "7", "away", 18, 6, 4, 4),
                ("2", "Пётр Иванов", "8", "away", 20, 4, 2, 5),
                ("3", "Сидор Сидоров", "9", "home", 25, 10, 3, 6),
            )
        ],
    }
    knowledge._append_game_protocol(documents, protocol)
    return documents, protocol


def test_official_periods_keep_home_away_orientation_and_player_data_is_public_only():
    documents, _ = game_corpus()
    found = knowledge.retrieve(
        "Сколько ВГУ и ВГТУ набрали в третьей четверти последнего мужского матча?",
        documents,
        now=NOW,
    )
    assert len(found) == 1 and found[0].kind == "match_period"
    assert "ВГТУ 11" in found[0].text and "ВГУ 20" in found[0].text
    assert "Счёт периода (хозяева:гости): 11:20" in found[0].text
    assert "Итог (хозяева:гости): 60:70" in found[0].text
    assert "это последний завершённый матч мужской команды" in found[0].text
    assert "Официальный протокол АСБ" in found[0].text
    assert found[0].period_number == 3 and found[0].match_id == "asb:123"
    assert found[0].url.startswith("https://asb.sportoteka.org/")
    assert "PRIVATE" not in " ".join(item.text for item in documents)


@pytest.mark.parametrize("period", ["третяя четверть", "третья четверть", "3-я четверть", "Q3"])
def test_period_forms_and_typo_do_not_follow_rules_branch(period):
    documents, _ = game_corpus()
    documents.append(doc("rule", "Четверть длится десять минут", kind="rules"))
    found = knowledge.retrieve(f"{period} последнего мужского матча", documents, now=NOW)
    assert found and all(item.kind == "match_period" and item.period_number == 3 for item in found)


def test_rule_definition_and_duration_still_retrieve_rules():
    documents, _ = game_corpus()
    rule = doc("rule", "Четверть длится десять минут", kind="rules")
    documents.append(rule)
    assert knowledge.retrieve("Сколько минут в четверти?", documents, now=NOW) == [rule]
    assert knowledge.retrieve("Что такое четверть?", documents, now=NOW) == [rule]


def test_named_player_period_stats_do_not_borrow_game_assists_or_minutes():
    documents, _ = game_corpus()
    found = knowledge.retrieve(
        "Сколько передач у Петрова в Q3 последнего мужского матча?", documents, now=NOW
    )
    assert found and all(
        item.kind == "match_player_stats" and item.period_number == 3 for item in found
    )
    assert all(item.player_name == "Иван Петров" for item in found)
    assert "Не опубликовано" in " ".join(item.text for item in found)
    assert "Передачи: 4" not in " ".join(item.text for item in found)
    found = knowledge.retrieve(
        "Сколько минут каждый игрок ВГУ провёл в Q3 последнего мужского матча?", documents, now=NOW
    )
    assert found and all(item.kind == "match_player_summary" for item in found)
    assert all(
        item.statistic_metric == "minutes_seconds" and "не опубликовано" in item.text
        for item in found
    )
    assert "1200" not in " ".join(item.text for item in found)


def test_match_comparisons_use_complete_group_metric_and_opponent_scope():
    documents, _ = game_corpus()
    club = knowledge.retrieve(
        "Кто у ВГУ набрал больше очков в последнем мужском матче?", documents, now=NOW
    )
    assert club and all(item.statistics_scope == "comparison_club" for item in club)
    assert "Пётр Иванов" in " ".join(item.text for item in club)
    opponent = knowledge.retrieve(
        "Кто у ВГТУ набрал больше очков в последнем мужском матче?", documents, now=NOW
    )
    assert opponent and all(item.statistics_scope == "comparison_opponent" for item in opponent)
    assert "Сидор Сидоров" in " ".join(item.text for item in opponent)
    rebounds = knowledge.retrieve(
        "Кто лучше всех подбирал у ВГУ в последнем мужском матче?", documents, now=NOW
    )
    assert rebounds and all(item.statistic_metric == "rebounds" for item in rebounds)
    assert "Иван Петров" in " ".join(item.text for item in rebounds)


def test_collective_quarter_player_points_use_summary_without_seasonal_averages():
    documents, _ = game_corpus()
    found = knowledge.retrieve(
        "Какие игроки ВГУ набирали очки в третьей четверти последнего мужского матча?",
        documents,
        now=NOW,
    )
    assert found and all(
        item.kind == "match_player_summary" and item.period_number == 3 for item in found
    )
    text = " ".join(item.text for item in found)
    assert "Иван Петров" in text and "Пётр Иванов" in text
    assert "Сидор Сидоров" not in text


def test_short_opponent_name_with_city_suffix_wins_over_our_team_context():
    documents, _ = game_corpus()
    documents[0] = replace(documents[0], opponent_name="ВГТУ (Воронеж)")
    found = knowledge.retrieve(
        "Кто у ВГТУ набрал больше очков против наших мужчин 15 ноября 2026?",
        documents,
        now=NOW,
    )
    assert found and all(item.statistics_scope == "comparison_opponent" for item in found)
    assert "Сидор Сидоров" in " ".join(item.text for item in found)
    assert "максимум 25" in " ".join(item.text for item in found)


def test_private_contact_question_keeps_explicit_game_scope_instead_of_season_comparison():
    documents, _ = game_corpus()
    documents[0] = replace(documents[0], opponent_name="ВГТУ (Воронеж)")
    documents.append(doc("season-leader", "Сезонный максимум: 999", kind="points_comparison"))
    found = knowledge.retrieve(
        "Дай домашний адрес и личный телефон самого результативного игрока ВГТУ "
        "против ВГУ 15 ноября 2026.",
        documents,
        now=NOW,
    )
    assert found and all(item.statistics_scope == "comparison_opponent" for item in found)
    assert all(item.match_id == "asb:123" and item.statistics_scope != "season" for item in found)
    assert "PRIVATE" not in " ".join(item.text for item in found)


def test_period_shot_coverage_is_explicit_alongside_missing_other_fields():
    documents, protocol = game_corpus()
    protocol["box_score"][0]["period_statistics"][0]["statistics"].update(
        {"two_points_made": 2, "two_points_attempted": 3}
    )
    documents = [documents[0]]
    knowledge._append_game_protocol(documents, protocol)
    found = knowledge.retrieve(
        "Сколько очков у Петрова в Q3 последнего мужского матча?", documents, now=NOW
    )
    text = " ".join(item.text for item in found)
    assert "Очки и броски за этот период опубликованы" in text
    assert "Двухочковые, попадания: 2" in text and "Двухочковые, попытки: 3" in text
    assert "Не опубликовано: Подборы, Передачи" in text


def test_comparison_distinguishes_dnp_from_genuinely_missing_player_statistic():
    documents, protocol = game_corpus()
    protocol["box_score"][0]["played"] = False
    protocol["box_score"].append(
        {
            **protocol["box_score"][1],
            "external_player_id": "4",
            "full_name": "Николай Неизвестный",
            "played": True,
            "statistics": {},
            "period_statistics": [],
        }
    )
    documents = [documents[0]]
    knowledge._append_game_protocol(documents, protocol)
    found = knowledge.retrieve(
        "Кто у ВГУ набрал больше всех в последнем мужском матче?", documents, now=NOW
    )
    text = " ".join(item.text for item in found)
    assert "максимум 20" in text
    assert "Показатель неизвестен у 1 игроков; DNP в это число не входит" in text
    assert "Не выходили на площадку (DNP): Иван Петров" in text


def test_missing_period_and_unknown_player_do_not_select_other_values():
    documents, _ = game_corpus()
    for question in (
        "Сколько передач у Петрова в Q2 последнего мужского матча?",
        "Сколько очков у Несуществующего в последнем мужском матче?",
        "Какая пятая четверть последнего мужского матча?",
    ):
        found = knowledge.retrieve(question, documents, now=NOW)
        assert found and all(item.kind == "match_stats_scope" for item in found), question


def test_latest_game_without_protocol_does_not_fall_back_to_older_box_score():
    documents, _ = game_corpus()
    documents.append(
        replace(
            documents[0], doc_id="newer", match_id="asb:456", starts_at=NOW - timedelta(hours=1)
        )
    )
    found = knowledge.retrieve(
        "Кто набрал больше всего в последнем мужском матче?", documents, now=NOW
    )
    assert found and all(item.kind == "match_stats_scope" for item in found)
    assert not any(item.match_id == "asb:123" for item in found)


def test_two_teams_need_clarification_and_unknown_opponent_never_selects_real_game():
    documents, _ = game_corpus()
    documents.append(
        replace(documents[0], doc_id="women", match_id="asb:789", team_category="women")
    )
    found = knowledge.retrieve(
        "Кто набрал больше в последнем матче и какая третья четверть?", documents, now=NOW
    )
    assert found and all(item.kind == "match_stats_scope" for item in found)
    found = knowledge.retrieve(
        "Статистика игроков по четвертям ВГУ — Лейкерс в матче 15 ноября 2026",
        documents,
        now=NOW,
        team_category="men",
    )
    assert found and all(item.kind == "match_stats_scope" for item in found)


def test_same_names_with_different_official_ids_are_not_merged():
    documents, protocol = game_corpus()
    protocol["box_score"][1]["full_name"] = "Иван Петров"
    documents = [documents[0]]
    knowledge._append_game_protocol(documents, protocol)
    found = knowledge.retrieve(
        "Сколько очков у Петрова в последнем мужском матче?", documents, now=NOW
    )
    assert found and all(item.kind == "match_stats_scope" for item in found)
    assert "разные игроки" in found[0].text


def test_dnp_unknown_and_official_zero_are_distinct():
    documents, protocol = game_corpus()
    protocol["box_score"][0]["played"] = False
    protocol["box_score"][1]["played"] = True
    protocol["box_score"][1]["period_statistics"][0]["statistics"]["points"] = 0
    protocol["box_score"][2]["played"] = None
    protocol["box_score"][2]["statistics"] = {}
    documents = [documents[0]]
    knowledge._append_game_protocol(documents, protocol)
    dnp = knowledge.retrieve(
        "Сколько очков у Петрова в последнем мужском матче?", documents, now=NOW
    )
    assert dnp and "DNP" in " ".join(item.text for item in dnp)
    assert "Очки: 18" not in " ".join(item.text for item in dnp)
    zero = knowledge.retrieve(
        "Сколько очков у Петра Иванова в Q3 последнего мужского матча?", documents, now=NOW
    )
    assert zero and "Очки: 0" in " ".join(item.text for item in zero)
    unknown = knowledge.retrieve(
        "Сколько очков у Сидорова в последнем мужском матче?", documents, now=NOW
    )
    assert unknown and "Не опубликовано: Очки" in " ".join(item.text for item in unknown)
    assert "Очки: 0" not in " ".join(item.text for item in unknown)


@pytest.mark.parametrize("period", ["OT1", "ОТ1", "первый овертайм", "1-й овертайм"])
def test_overtime_is_separate_from_first_quarter(period):
    documents, protocol = game_corpus()
    protocol["period_scores"].append(
        {"number": 5, "kind": "overtime", "label": "OT1", "home_score": 7, "away_score": 8}
    )
    protocol["home_score"] = 67
    protocol["away_score"] = 78
    documents = [documents[0]]
    knowledge._append_game_protocol(documents, protocol)
    found = knowledge.retrieve(f"Счёт {period} последнего мужского матча?", documents, now=NOW)
    assert found and all(item.kind == "match_period" and item.period_number == 5 for item in found)
    assert "Счёт периода (хозяева:гости): 7:8" in found[0].text
    assert "Итог (хозяева:гости): 67:78" in found[0].text


def test_missing_fifth_period_does_not_claim_all_quarter_scores_are_missing():
    documents, _ = game_corpus()
    found = knowledge.retrieve("Пятая четверть последнего мужского матча?", documents, now=NOW)
    assert len(found) == 1 and found[0].kind == "match_stats_scope"
    assert "опубликованы периоды: 1, 2, 3, 4" in found[0].text
    assert "Счёт периода 5 не опубликован" in found[0].text


def test_period_scope_and_player_identity_change_corpus_revision():
    documents, _ = game_corpus()
    target = next(item for item in documents if item.kind == "match_player_stats")
    for change in ({"period_number": 9}, {"match_id": "asb:other"}, {"player_id": "other"}):
        changed = [replace(item, **change) if item is target else item for item in documents]
        assert knowledge.corpus_hash(changed) != knowledge.corpus_hash(documents)


@pytest.mark.asyncio
async def test_roster_uses_current_published_listed_members_without_private_fields(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "roster.db").as_posix())
    factory = async_sessionmaker(engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            snapshots = []
            for team, season, published in (
                ("men", "2026-2027", True),
                ("women", "2026-2027", False),
                ("men", "2025-2026", True),
                ("women", "2027-2028", True),
            ):
                snapshot = AsbRosterSnapshot(
                    team=team,
                    season=season,
                    is_published=published,
                    external_team_id=team,
                    competition_id="public",
                    source_url="https://asb.sportoteka.org/public",
                    stats_source_url="https://asb.sportoteka.org/stats",
                    checked_at=NOW,
                )
                session.add(snapshot)
                snapshots.append(snapshot)
            await session.flush()
            for index, (snapshot, name, listed) in enumerate(
                (
                    (snapshots[0], "Иван Публичный", True),
                    (snapshots[0], "Пётр Безстатистики", True),
                    (snapshots[0], "Снятый Иззаявки", False),
                    (snapshots[1], "Скрытая Заявка", True),
                    (snapshots[2], "Игрок Прошлогодний", True),
                    (snapshots[3], "Игрок Избудущего", True),
                )
            ):
                member = AsbRosterMember(
                    snapshot_id=snapshot.id,
                    external_player_id=str(index),
                    full_name=name,
                    is_listed=listed,
                    photo_url=None,
                    games_played=10 if index == 0 else None,
                    points_total=123 if index == 0 else None,
                    assists_total=None,
                    rebounds_total=None,
                    seconds_total=None,
                    statistics_season="2025-2026" if index == 0 else None,
                    statistics_source_url="https://asb.sportoteka.org/stats"
                    if index == 0
                    else None,
                )
                session.add(member)
            await session.commit()
            documents = await knowledge.build_corpus(session, now=NOW)
        text = " ".join(item.text for item in documents)
        assert "Иван Публичный" in text and "Пётр Безстатистики" in text
        for forbidden in (
            "Снятый Иззаявки",
            "Скрытая Заявка",
            "Игрок Прошлогодний",
            "Игрок Избудущего",
        ):
            assert forbidden not in text
        assert "12.3" in text and "статистика относится к сезону 2025-2026" in text
        assert "отсутствие показателя не означает ноль" in text
        assert "женской команды ВГУ на сезон 2026-2027 не опубликован" in text
        assert all(
            item.season == "2026-2027" for item in documents if item.kind.startswith("roster")
        )
        found = knowledge.retrieve(
            "Статистика Ивана Публичного за сезон 2025-2026",
            documents,
            now=NOW,
            team_category="men",
        )
        assert found and all(item.kind == "player_season_stats" for item in found)
        assert all(
            item.season == "2025-2026" and item.statistics_scope == "season" for item in found
        )
        assert "12.3" in " ".join(item.text for item in found)
        assert not any(
            item.kind == "player_season_stats"
            for item in knowledge.retrieve(
                "Статистика Ивана Публичного за сезон 2026-2027", documents, now=NOW
            )
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_latest_published_roster_fallback_is_explicitly_historical_with_stats_season(
    tmp_path,
):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "fallback.db").as_posix())
    factory = async_sessionmaker(engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            for category, season, name in (
                ("men", "2025-2026", "Игрок Последний"),
                ("men", "2024-2025", "Игрок Древний"),
                ("men", "2027-2028", "Игрок Будущий"),
                ("women", "2024-2025", "Игрок Женский"),
            ):
                snapshot = AsbRosterSnapshot(
                    team=category,
                    season=season,
                    is_published=True,
                    external_team_id=category,
                    competition_id="public",
                    source_url="https://asb.sportoteka.org/public",
                    stats_source_url="https://asb.sportoteka.org/stats",
                    checked_at=NOW,
                )
                session.add(snapshot)
                await session.flush()
                session.add(
                    AsbRosterMember(
                        snapshot_id=snapshot.id,
                        external_player_id=name,
                        full_name=name,
                        is_listed=True,
                        statistics_season=season,
                        games_played=2,
                        points_total=20,
                        statistics_source_url="https://asb.sportoteka.org/stats",
                    )
                )
            await session.commit()
            documents = await knowledge.build_corpus(session, now=NOW)
        text = " ".join(item.text for item in documents)
        assert "Игрок Древний" not in text and "Игрок Будущий" not in text
        assert "Игрок Последний" in text and "Игрок Женский" in text
        found = knowledge.retrieve(
            "Статистика Последнего за сезон 2025-2026", documents, now=NOW, team_category="men"
        )
        assert found and all(
            item.kind == "player_season_stats" and item.team_category == "men" for item in found
        )
        roster = [
            item for item in documents if item.kind == "roster" and item.team_category == "men"
        ]
        assert all(item.season == "2025-2026" for item in roster)
        assert "не является текущим" in " ".join(item.text for item in roster)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_result_retains_team_discipline_and_source_date_precision(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "results.db").as_posix())
    factory = async_sessionmaker(engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add(
                ArchiveResult(
                    source="club_archive",
                    external_id="year-only",
                    discipline="3x3",
                    team="women",
                    season="2024-2025",
                    year=2024,
                    tournament="Кубок",
                    event_date=date(2024, 1, 1),
                    date_precision="year",
                    location="Воронеж",
                    team_name="ВГУ",
                    opponent_name=None,
                    place=2,
                    status="finished",
                    source_label="Открытый архив",
                )
            )
            await session.commit()
            documents = await knowledge.build_corpus(session, now=NOW)
        result = next(item for item in documents if item.kind == "result")
        assert result.team_category == "women" and result.discipline == "3x3"
        assert result.event_year == 2024 and result.season == "2024-2025"
        assert result.date_precision == "year" and result.starts_at is None
        assert "Воронеж" in result.text and "точная дата неизвестна" in result.text
        assert "2024-01-01" not in result.text
    finally:
        await engine.dispose()


def test_chunks_keep_query_metadata_and_revision_tracks_it():
    documents = []
    knowledge._append_passages(
        documents,
        doc_id="result:long",
        kind="result",
        title="Кубок",
        text="Публичное событие. " * 100,
        url="/matches",
        team_category="women",
        season="2024-2025",
        discipline="3x3",
        event_year=2024,
        date_precision="year",
    )
    assert len(documents) > 1
    assert all(item.team_category == "women" and item.discipline == "3x3" for item in documents)
    changed = [
        doc(item.doc_id, item.text, category="men", discipline="3x3", year=2024, season="2024-2025")
        for item in documents
    ]
    assert knowledge.corpus_hash(changed) != knowledge.corpus_hash(documents)


def test_team_discipline_and_year_are_hard_constraints_before_ranking():
    target = doc("target", "Кубок ВГУ: второе место", category="women", discipline="3x3", year=2024)
    wrong_team = doc("men", "Кубок ВГУ: первое место", category="men", discipline="3x3", year=2024)
    wrong_sport = doc(
        "5x5", "Кубок ВГУ: первое место", category="women", discipline="5x5", year=2024
    )
    wrong_year = doc(
        "2025", "Кубок ВГУ: первое место", category="women", discipline="3x3", year=2025
    )
    assert knowledge.retrieve(
        "Какое место у девушек на кубке 3×3 в 2024 году?",
        [wrong_team, wrong_sport, wrong_year, target],
        now=NOW,
    ) == [target]


def test_explicit_team_overrides_page_context_and_both_teams_are_allowed():
    men = doc("men", "Кубок ВГУ", category="men")
    women = doc("women", "Кубок ВГУ", category="women")
    assert knowledge.retrieve("Кубок мужской команды", [women, men], team_category="women") == [men]
    assert set(
        item.doc_id for item in knowledge.retrieve("Кубок мужчин и женщин", [women, men])
    ) == {"men", "women"}


def test_exact_season_does_not_become_the_other_calendar_year():
    wanted = doc("wanted", "Кубок ВГУ", season="2024-2025", year=2025)
    wrong = doc("wrong", "Кубок ВГУ", season="2025-2026", year=2025)
    assert knowledge.retrieve("Кубок сезона 2024/2025", [wrong, wanted]) == [wanted]


def test_next_match_obeys_category_and_discipline():
    men = doc(
        "match:men",
        "Матч ВГУ",
        category="men",
        discipline="5x5",
        kind="match",
        status="scheduled",
        starts_at=NOW + timedelta(days=1),
    )
    women = doc(
        "match:women",
        "Матч ВГУ",
        category="women",
        discipline="5x5",
        kind="match",
        status="scheduled",
        starts_at=NOW + timedelta(days=2),
    )
    assert knowledge.retrieve("Когда ближайшая игра девушек?", [men, women], now=NOW) == [women]
    assert knowledge.retrieve("Когда ближайший матч 3x3?", [men, women], now=NOW) == []


def test_week_calendar_includes_real_records_before_navigation_and_obeys_boundaries():
    friday = doc(
        "friday",
        "ВГУ — ВГТУ-2",
        category="men",
        kind="result",
        precision="day",
        status="scheduled",
        opponent="ВГТУ-2",
        starts_at=NOW + timedelta(days=4),
    )
    following = replace(friday, doc_id="following", starts_at=NOW + timedelta(days=11))
    previous = replace(friday, doc_id="previous", starts_at=NOW - timedelta(days=3))
    navigation = knowledge.KnowledgeDoc(
        "navigation:matches", "navigation", "Матчи", "Календарь матчей", "/matches"
    )
    corpus = [following, previous, navigation, friday]
    assert knowledge.retrieve("Какое расписание матчей на эту неделю?", corpus, now=NOW) == [
        friday,
        navigation,
    ]
    assert knowledge.retrieve(
        "Где posmotret raspisanie matchei na etu nedelu?", corpus, now=NOW, limit=1
    ) == [friday]
    assert knowledge.retrieve("Матчи на следующей неделе", corpus, now=NOW) == [following]
    assert knowledge.retrieve("Матчи на прошлой неделе", corpus, now=NOW) == [previous]
    assert knowledge.retrieve("Матчи в ближайшие выходные", corpus, now=NOW) == []


def test_weekend_uses_moscow_date_and_keeps_requested_team():
    saturday = doc(
        "saturday",
        "Матч ВГУ",
        category="women",
        kind="match",
        status="scheduled",
        starts_at=datetime(2026, 11, 20, 22, tzinfo=UTC),
    )
    wrong_team = replace(saturday, doc_id="men", team_category="men")
    wrong_week = replace(saturday, doc_id="later", starts_at=saturday.starts_at + timedelta(days=7))
    assert knowledge.retrieve(
        "Будет ли матч женской команды в ближайшие выходные?",
        [wrong_team, wrong_week, saturday],
        now=NOW,
    ) == [saturday]


@pytest.mark.asyncio
async def test_day_only_public_archive_schedule_participates_in_next_and_exact_day(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "schedule.db").as_posix())
    factory = async_sessionmaker(engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            for day, opponent, category, discipline, precision in (
                (20, "ВГТУ-2", "men", "5x5", "day"),
                (27, "ВГАС", "men", "5x5", "day"),
                (18, "ВГАС", "women", "5x5", "day"),
                (17, "ВГАС", "men", "3x3", "day"),
                (1, "Неизвестный день", "men", "5x5", "month"),
            ):
                session.add(
                    ArchiveResult(
                        source="public_schedule",
                        external_id=f"{day}-{opponent}",
                        discipline=discipline,
                        team=category,
                        season="2026-2027",
                        year=2026,
                        tournament="АСБ",
                        event_date=date(2026, 11, day),
                        date_precision=precision,
                        team_name="ВГУ",
                        opponent_name=opponent,
                        status="scheduled",
                        source_label="Публичное расписание",
                    )
                )
            await session.commit()
            corpus = await knowledge.build_corpus(session, now=NOW)
        found = knowledge.retrieve("Когда ближайший мужской матч 5x5?", corpus, now=NOW)
        assert len(found) == 1 and found[0].opponent_name == "ВГТУ-2"
        assert found[0].kind == "result" and found[0].date_precision == "day"
        assert "2026-11-20" in found[0].text
        assert "время начала не опубликовано" in found[0].text
        assert "00:00" not in found[0].text
        assert knowledge.retrieve("Матч мужчин с ВГТУ-2 20.11.2026", corpus, now=NOW) == found
        assert knowledge.retrieve("Матч мужчин с ВГТУ-2 27.11.2026", corpus, now=NOW) == []
        assert (
            knowledge.retrieve("Когда следующий мужской матч 3x3?", corpus, now=NOW)[0].discipline
            == "3x3"
        )
    finally:
        await engine.dispose()


def test_unknown_time_today_is_not_mistaken_for_a_past_midnight_fixture():
    day_only = doc(
        "archive-today",
        "Дата: 2026-11-16; время начала не опубликовано",
        category="men",
        precision="day",
        starts_at=datetime(2026, 11, 15, 21, tzinfo=UTC),
        status="scheduled",
        opponent="ВГТУ",
    )
    earlier_timed = doc(
        "past-today", "Матч", kind="match", status="scheduled", starts_at=NOW - timedelta(hours=1)
    )
    imprecise = replace(day_only, doc_id="month", date_precision="month")
    assert knowledge.retrieve(
        "Когда ближайший матч?", [earlier_timed, imprecise, day_only], now=NOW
    ) == [day_only]
    assert knowledge.retrieve("Есть матч сегодня?", [imprecise, day_only], now=NOW) == [day_only]


def test_latest_finished_archive_match_uses_day_precision_and_rejects_future_results():
    latest = doc(
        "latest-archive",
        "Матч ВГУ — ВГТУ",
        precision="day",
        starts_at=NOW - timedelta(days=1),
        status="finished",
        opponent="ВГТУ",
    )
    older = replace(latest, doc_id="older", kind="match", starts_at=NOW - timedelta(days=2))
    future = replace(latest, doc_id="future", starts_at=NOW + timedelta(days=1))
    month = replace(latest, doc_id="month", date_precision="month", starts_at=NOW)
    assert knowledge.retrieve(
        "Как закончился последний матч?", [older, future, month, latest], now=NOW
    ) == [latest]


def test_timed_fixture_replaces_day_only_duplicate_but_not_a_second_timed_meeting():
    archived = doc(
        "result:copy",
        "Дата известна без времени",
        category="men",
        discipline="5x5",
        season="2026/27",
        precision="day",
        status="scheduled",
        opponent="ВГТУ-2",
        competition="АСБ",
        starts_at=datetime(2026, 11, 20, 0, tzinfo=UTC),
    )
    timed = replace(
        archived,
        doc_id="match:precise",
        kind="match",
        season="2026-2027",
        date_precision="minute",
        starts_at=datetime(2026, 11, 20, 15, tzinfo=UTC),
    )
    continuation = replace(timed, doc_id="match:precise:part2", text="Продолжение")
    assert knowledge.retrieve(
        "Когда ближайший матч?", [archived, continuation, timed], now=NOW
    ) == [timed, continuation]
    second = replace(timed, doc_id="match:another", starts_at=timed.starts_at + timedelta(hours=3))
    exact_day = knowledge.retrieve("Матчи 20.11.2026", [archived, timed, second], now=NOW)
    assert timed in exact_day and second in exact_day  # Two times are not one duplicate.


@pytest.mark.asyncio
async def test_points_leader_comes_from_all_published_comparable_members_and_retains_ties(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "leaders.db").as_posix())
    factory = async_sessionmaker(engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            snapshot = AsbRosterSnapshot(
                team="men",
                season="2026-2027",
                is_published=True,
                external_team_id="men",
                competition_id="asb",
                source_url="https://asb.example/team",
                stats_source_url="https://asb.example/stats",
                checked_at=NOW,
            )
            session.add(snapshot)
            await session.flush()
            for index, (name, points, games, statistics_season, listed) in enumerate(
                (
                    ("Андрей Чумаков", 8, 1, "2026-2027", True),
                    ("Иван Красняков", 12, 1, "2026-2027", True),
                    ("Юрий Провоторов", 19, 1, "2026-2027", True),
                    ("Второй Лидер", 38, 2, "2026-2027", True),
                    ("Иной Сезон", 100, 1, "2025-2026", True),
                    ("Нет Статистики", None, None, None, True),
                    ("Ноль Матчей", 999, 0, "2026-2027", True),
                    ("Не Опубликован", 999, 1, "2026-2027", False),
                )
            ):
                session.add(
                    AsbRosterMember(
                        snapshot_id=snapshot.id,
                        external_player_id=str(index),
                        full_name=name,
                        points_total=points,
                        games_played=games,
                        statistics_season=statistics_season,
                        is_listed=listed,
                    )
                )
            await session.commit()
            corpus = await knowledge.build_corpus(session, now=NOW)
        found = knowledge.retrieve(
            "Короче, кто самый результативный игрок клуба а?", corpus, now=NOW
        )
        assert found[0].kind == "points_comparison_scope"
        assert not any(item.kind == "roster_player" for item in found)
        current = [item for item in found if item.season == "2026-2027"]
        assert len(current) == 2
        for item in current:
            assert "Максимум в этой группе: 19.0" in item.text
            assert "Число сопоставимых игроков: 4" in item.text
            assert "Всего разделяют максимум: 2" in item.text
            assert "Вне сравнения этой группы: 3" in item.text
            assert "Число матчей у игроков может различаться" in item.text
            assert "Это не лидер всего клуба" in item.text
        assert any("Юрий Провоторов; матчей: 1" in item.text for item in current)
        assert any("Второй Лидер; матчей: 2" in item.text for item in current)
        exact = knowledge.retrieve(
            "Кто лучший по очкам у мужчин за сезон 2026/27?", corpus, now=NOW
        )
        assert len(exact) == 3 and all(item.season == "2026-2027" for item in exact)
        assert "Иной Сезон" not in " ".join(item.text for item in exact)
        assert "Не Опубликован" not in " ".join(item.text for item in found)
        # A tight budget must not silently discard one of the tied leaders.
        assert knowledge.retrieve("Кто самый результативный игрок?", corpus, limit=2) == [
            knowledge._comparison_guard(None, None)
        ]
    finally:
        await engine.dispose()


def test_top_k_player_cards_never_become_a_club_scoring_record_without_full_summary():
    cards = [
        doc("chumakov", "Андрей Чумаков: очков в среднем за матч: 8.0", kind="roster_player"),
        doc("provorotov", "Юрий Провоторов: очков в среднем за матч: 19.0", kind="roster_player"),
    ]
    found = knowledge.retrieve("Кто самый результативный игрок клуба?", cards, limit=1)
    assert len(found) == 1 and found[0].kind == "points_comparison_scope"
    assert "определить лидера нельзя" in found[0].text
    assert "8.0" not in found[0].text and "19.0" not in found[0].text


@pytest.mark.parametrize(
    "question",
    [
        "Проигрывал ли клуб когда-нибудь с разницей больше 50 очков?",
        "А проигрывал ли клуб када-нибудь с разницей больше 50 очков ??",
        "Будьте добры, подскажите: проигрывал ли клуб когда-нибудь с разницей больше 50 очков?",
        "Ёбаный стыд, проигрывал ли клуб когда-нибудь с разницей больше 50 очков?",
        "Проигрывал ли клуб когда-нибудь с разницей больше 50 очков? Отвечай, железяка!",
    ],
)
def test_score_margin_regression_uses_positive_finished_witnesses_not_player_maxima(question):
    loss = doc(
        "loss",
        "Завершённый матч",
        category="women",
        discipline="5x5",
        year=2026,
        season="2026-2027",
        status="finished",
        opponent="КГУ",
        competition="АСБ",
        club_score=25,
        opponent_score=90,
        starts_at=NOW - timedelta(days=1),
        precision="day",
    )
    boundary = replace(loss, doc_id="exactly-50", club_score=40, opponent_score=90)
    win = replace(loss, doc_id="win", club_score=90, opponent_score=25)
    upcoming = replace(loss, doc_id="upcoming", match_status="scheduled")
    player = doc("player", "Самый результативный игрок, очков: 100", kind="points_comparison")
    found = knowledge.retrieve(question, [player, win, boundary, upcoming, loss], now=NOW)
    assert len(found) == 1 and found[0].kind == "score_margin_witness"
    assert found[0].club_score == 25 and found[0].opponent_score == 90
    assert "Разница: 65 очков, поражение ВГУ; это больше 50 очков" in found[0].text
    assert "положительный пример" in found[0].text
    assert "не самая большая разница за всю историю" in found[0].text
    assert knowledge.unsupported_numbers("Разница 65 очков, больше 50: счёт 25:90", found) == []


def test_score_margin_strict_threshold_scope_outcome_and_incomplete_archive():
    fifty = doc(
        "fifty",
        "Матч",
        category="men",
        discipline="5x5",
        year=2025,
        season="2025-2026",
        status="finished",
        opponent="ВГАС",
        club_score=20,
        opponent_score=70,
    )
    other_team = replace(fifty, doc_id="women", team_category="women", opponent_score=90)
    other_discipline = replace(fifty, doc_id="3x3", discipline="3x3", opponent_score=90)
    other_season = replace(
        fifty, doc_id="old", season="2024-2025", event_year=2024, opponent_score=90
    )
    documents = [fifty, other_team, other_discipline, other_season]
    no_witness = knowledge.retrieve(
        "Мужчины проигрывали с разницей больше 50 очков в 5x5 в сезоне 2025/26?",
        documents,
        now=NOW,
    )
    assert len(no_witness) == 1 and no_witness[0].kind == "score_margin_scope"
    assert "не доказывает, что таких матчей никогда не было" in no_witness[0].text
    inclusive = knowledge.retrieve(
        "Мужчины проигрывали с разницей не менее 50 очков в 5x5 в сезоне 2025/26?",
        documents,
        now=NOW,
    )
    assert len(inclusive) == 1 and inclusive[0].club_score == 20
    assert "Разница: 50 очков" in inclusive[0].text
    win = replace(fifty, doc_id="win", club_score=100, opponent_score=20)
    assert (
        knowledge.retrieve(
            "Выигрывал ли клуб матч с разницей больше 50 очков?", [fifty, win], now=NOW
        )[0].club_score
        == 100
    )


def test_score_margin_does_not_parse_prose_or_treat_missing_invalid_or_future_scores_as_facts():
    valid = doc(
        "valid",
        "Матч: счёт 25:90",
        status="finished",
        opponent="ВГТУ",
        club_score=25,
        opponent_score=90,
        year=2026,
    )
    unsupported = [
        replace(valid, doc_id="prose-only", club_score=None, opponent_score=None),
        replace(valid, doc_id="half-score", opponent_score=None),
        replace(valid, doc_id="negative", club_score=-1),
        replace(valid, doc_id="boolean", club_score=True),
        replace(valid, doc_id="scheduled", match_status="scheduled"),
        replace(valid, doc_id="future", starts_at=NOW + timedelta(days=1)),
        replace(valid, doc_id="future-year", event_year=2027),
        replace(valid, doc_id="history", kind="history"),
    ]
    found = knowledge.retrieve(
        "Проигрывал ли клуб с разницей больше 50 очков?", unsupported, now=NOW
    )
    assert len(found) == 1 and found[0].kind == "score_margin_scope"


@pytest.mark.asyncio
async def test_score_projection_orients_away_club_and_never_infers_home_from_venue(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "score.db").as_posix())
    factory = async_sessionmaker(engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            team = Team(slug="men", name="ВГУ", category=TeamCategory.MEN)
            competition = Competition(name="АСБ", season="2026-2027")
            venue = Venue(name="Спортзал ВГУ", address="Воронеж")
            session.add_all([team, competition, venue])
            await session.flush()
            session.add(
                Match(
                    team_id=team.id,
                    competition_id=competition.id,
                    venue_id=venue.id,
                    starts_at=NOW - timedelta(days=1),
                    club_side=ClubSide.AWAY,
                    home_team_name="ВГТУ",
                    away_team_name="ВГУ",
                    status=MatchStatus.FINISHED,
                    home_score=90,
                    away_score=25,
                )
            )
            session.add(
                ArchiveResult(
                    source="archive",
                    external_id="unknown-side",
                    discipline="5x5",
                    team="men",
                    season="2026-2027",
                    year=2026,
                    tournament="АСБ",
                    event_date=date(2026, 11, 14),
                    date_precision="day",
                    location="Спортзал ВГУ",
                    team_name="ВГУ",
                    opponent_name="ВГАС",
                    club_score=20,
                    opponent_score=80,
                    status="finished",
                    source_label="Публичный архив",
                )
            )
            await session.commit()
            corpus = await knowledge.build_corpus(session, now=NOW)
        match = next(item for item in corpus if item.kind == "match")
        result = next(item for item in corpus if item.kind == "result")
        assert (match.club_score, match.opponent_score, match.club_side) == (25, 90, "away")
        assert (result.club_score, result.opponent_score, result.club_side) == (20, 80, None)
        assert "Счёт (ВГУ : соперник): 20:80" in result.text
        all_games = knowledge.retrieve(
            "Проигрывал ли клуб с разницей больше 50 очков?", corpus, now=NOW
        )
        assert len(all_games) == 2 and all(
            item.kind == "score_margin_witness" for item in all_games
        )
        away = knowledge.retrieve(
            "Проигрывал ли клуб на выезде с разницей больше 50 очков?", corpus, now=NOW
        )
        assert len(away) == 1 and away[0].opponent_name == "ВГТУ"
        home = knowledge.retrieve(
            "Проигрывал ли клуб дома с разницей больше 50 очков?", corpus, now=NOW
        )
        assert len(home) == 1 and home[0].kind == "score_margin_scope"
        assert knowledge.corpus_hash([match]) != knowledge.corpus_hash(
            [replace(match, club_score=26)]
        )
    finally:
        await engine.dispose()


def test_current_competitions_use_current_public_records_and_cover_both_teams():
    current = [
        doc(
            f"men:{index}",
            "АСБ; мужская команда; сезон 2026-2027",
            kind="match",
            category="men",
            season="2026-2027",
            competition=f"АСБ {index}",
            starts_at=NOW - timedelta(days=index),
        )
        for index in range(8)
    ]
    women = doc(
        "women",
        "АСБ; женская команда; сезон 2026-2027",
        category="women",
        season="2026-2027",
        competition="АСБ",
        starts_at=NOW - timedelta(days=1),
    )
    archived = doc(
        "old", "АСБ 2019/20: мужская и женская команды", kind="history", season="2019-2020"
    )
    found = knowledge.retrieve(
        "В каких соревнованиях играет мужская команда, а в каких женская?",
        [archived, *current, women],
        now=NOW,
    )
    assert archived not in found and women in found
    assert found[0].team_category == "men" and found[1].team_category == "women"
    assert all(item.season == "2026-2027" for item in found)
    assert (
        knowledge.retrieve("В каких соревнованиях клуб участвует сейчас?", [archived], now=NOW)
        == []
    )


def test_historical_tournament_titles_include_structured_first_places_not_individual_losses():
    title = replace(
        doc("gold", "Чемпионат области: место 1", year=2024, category="women"), is_title=True
    )
    other = doc("loss", "Чемпионат области: счёт 25:90", year=2026)
    history = doc("history", "История выступлений клуба", kind="history")
    for question in (
        "Какие главные победы клуба?",
        "Какие турниры клуб выигрывал за последние годы?",
        "Расскажи историю главных побед клуба",
    ):
        assert knowledge.retrieve(question, [other, history, title], now=NOW) == [title]


def test_today_uses_moscow_day_across_utc_midnight_boundary():
    now = datetime(2026, 11, 16, 21, 30, tzinfo=UTC)  # Moscow already November 17.
    today = doc(
        "match:today",
        "Матч ВГУ",
        kind="match",
        status="scheduled",
        starts_at=now + timedelta(hours=1),
    )
    tomorrow = doc(
        "match:tomorrow",
        "Матч ВГУ",
        kind="match",
        status="scheduled",
        starts_at=now + timedelta(days=1),
    )
    assert knowledge.retrieve("Когда матч сегодня?", [tomorrow, today], now=now) == [today]


def test_exact_date_and_impossible_date_never_return_neighboring_day():
    wanted = doc(
        "wanted",
        "Матч ВГУ",
        kind="match",
        starts_at=datetime(2024, 12, 2, 16, tzinfo=UTC),
        year=2024,
    )
    wrong = doc(
        "wrong",
        "Матч ВГУ",
        kind="match",
        starts_at=datetime(2024, 12, 3, 16, tzinfo=UTC),
        year=2024,
    )
    assert knowledge.retrieve("Матч 02.12.2024", [wrong, wanted], now=NOW) == [wanted]
    assert knowledge.retrieve("Матч 31.02.2024", [wrong, wanted], now=NOW) == []


def test_month_name_without_year_preserves_matching_years_instead_of_guessing():
    earlier = doc("earlier", "Матч", kind="match", starts_at=datetime(2024, 11, 12, tzinfo=UTC))
    later = doc("later", "Матч", kind="match", starts_at=datetime(2026, 11, 12, tzinfo=UTC))
    other = doc("other", "Матч", kind="match", starts_at=datetime(2026, 10, 12, tzinfo=UTC))
    assert knowledge.retrieve("Матч 12 ноября", [later, other, earlier], now=NOW) == [
        earlier,
        later,
    ]
    assert knowledge.retrieve("Матч 12 ноября 2026", [earlier, other, later], now=NOW) == [later]
    assert knowledge.retrieve("Матч 31 февраля", [earlier, later], now=NOW) == []


def test_history_navigation_returns_route_instead_of_an_unrequested_retelling():
    route = knowledge.KnowledgeDoc(
        "navigation:history", "navigation", "История", "/club#history", "/club#history"
    )
    story = doc("old", "История клуба", kind="history")
    assert knowledge.retrieve("Где тут, блять, история клуба?", [story, route]) == [route]


def test_mixed_language_results_navigation_does_not_become_standings():
    matches = knowledge.KnowledgeDoc(
        "navigation:matches", "navigation", "Матчи", "/matches", "/matches"
    )
    table = knowledge.KnowledgeDoc(
        "navigation:standings", "navigation", "Таблица", "/standings", "/standings"
    )
    assert knowledge.retrieve("Где posmotret results за sezon?", [table, matches]) == [matches]


def test_transliterated_training_question_finds_contact_route():
    route = knowledge.KnowledgeDoc(
        "navigation:contacts", "navigation", "Контакты", "/contacts", "/contacts"
    )
    assert knowledge.retrieve("Kak popast na trenirovku kluba?", [route]) == [route]


def test_english_team_constraint_does_not_choose_earlier_opposite_team():
    men = doc(
        "men",
        "Матч",
        kind="match",
        category="men",
        status="scheduled",
        starts_at=NOW + timedelta(days=2),
    )
    women = doc(
        "women",
        "Матч",
        kind="match",
        category="women",
        status="scheduled",
        starts_at=NOW + timedelta(days=1),
    )
    assert knowledge.retrieve(
        "When is the next match of the men's team?", [women, men], now=NOW
    ) == [men]


def test_where_upcoming_fixture_is_a_venue_question_not_site_navigation():
    route = knowledge.KnowledgeDoc(
        "navigation:matches", "navigation", "Матчи", "/matches", "/matches"
    )
    fixture = doc(
        "next", "Матч в зале", kind="match", status="scheduled", starts_at=NOW + timedelta(days=1)
    )
    assert knowledge.retrieve("Где пройдёт ближайший матч?", [route, fixture], now=NOW) == [fixture]


def test_last_finished_match_does_not_return_upcoming_or_an_older_game():
    previous = doc(
        "old", "Матч ВГУ", kind="match", status="finished", starts_at=NOW - timedelta(days=4)
    )
    latest = doc(
        "latest", "Матч ВГУ", kind="match", status="finished", starts_at=NOW - timedelta(days=1)
    )
    upcoming = doc(
        "next", "Матч ВГУ", kind="match", status="scheduled", starts_at=NOW + timedelta(days=1)
    )
    assert knowledge.retrieve(
        "Как закончился последний матч?", [upcoming, previous, latest], now=NOW
    ) == [latest]


def test_role_and_roster_aliases_retrieve_public_sources_without_defaulting_to_history():
    role = doc("role", "Разыгрывающий ведёт атаку", kind="player_roles")
    roster = doc("roster", "Состав команды ВГУ", kind="roster")
    assert knowledge.retrieve("Какое амплуа у разыгрывающего?", [roster, role]) == [role]
    assert knowledge.retrieve("Пожалуйста, покажи ростер", [role, roster])[0] == roster


def test_typo_fallback_does_not_approximate_year_or_return_a_source_for_gibberish():
    rule = doc("rule", "Баскетбол: штрафной бросок", kind="rules")
    assert knowledge.retrieve("баскитбол", [rule]) == [rule]
    assert knowledge.retrieve("зщхжцъ", [rule]) == []
    assert knowledge.retrieve("2025", [doc("year", "2024")]) == []


def test_pilot_traveling_query_prefers_existing_rule_over_generic_basketball_history():
    rule = doc(
        "travel",
        "Ведение, пробежка и двойное ведение. Пробежка — незаконное перемещение ног.",
        kind="rules",
    )
    history = [
        doc(str(index), "Баскетбол: турнир по баскетболу", kind="history") for index in range(10)
    ]
    assert (
        knowledge.retrieve("Что такое пробежка в баскетболе, объясни новичку?", [*history, rule])[0]
        == rule
    )


def test_pilot_foul_word_forms_find_personal_contact_explanation():
    personal = doc(
        "Персональные фолы", "Персональный фол — незаконный контакт: удар по рукам.", kind="rules"
    )
    tips = doc("Совет", "Оценивайте ценность броска", kind="viewing_tips")
    unrelated = doc("Игра", "Матч ВГУ — ВГАС", kind="match")
    assert (
        knowledge.retrieve(
            "Какого хрена мне дали фол, если я задел руку соперника при броске?",
            [tips, unrelated, personal],
        )[0]
        == personal
    )


@pytest.mark.parametrize(
    "question",
    ["Сколько длится матч?", "Сколько минут в четверти?", "Сколько очков за трёхочковый?"],
)
def test_rule_questions_do_not_follow_fixture_branch(question):
    rules = doc(
        "Время и счёт",
        "Четверть длится десять минут. Трёхочковый бросок даёт три очка.",
        kind="rules",
    )
    match = doc(
        "Матч", "Счёт: 84:64", kind="match", starts_at=NOW + timedelta(days=1), status="scheduled"
    )
    assert rules in knowledge.retrieve(question, [match, rules], now=NOW)
    assert match not in knowledge.retrieve(question, [match, rules], now=NOW)


def test_pilot_navigation_uses_actual_matches_route_even_with_abuse():
    route = knowledge.KnowledgeDoc(
        "navigation:matches", "navigation", "Расписание", "Раздел матчей /matches", "/matches"
    )
    unrelated = doc("history", "Календарь матчей в истории", kind="history")
    assert knowledge.retrieve(
        "Ты тупой бобр, блядь. Где посмотреть расписание матчей?", [unrelated, route]
    ) == [route]


def test_standings_navigation_does_not_send_to_matches():
    route = knowledge.KnowledgeDoc(
        "navigation:standings", "navigation", "Таблицы", "Турнирные таблицы", "/standings"
    )
    assert knowledge.retrieve("Где посмотреть турнирную таблицу?", [route]) == [route]


def test_pilot_origin_retrieves_foundation_and_name_adoption_as_separate_sources():
    foundation = doc("foundation", "Баскетбольный клуб ВГУ основан в 2007 году.", kind="history")
    name = doc(
        "name", "В 2022 году клуб получил своё нынешнее название «Хищные Бобры».", kind="history"
    )
    distractors = [
        doc(str(index), "Клуб Хищные Бобры ВГУ играет в баскетбол", kind="team")
        for index in range(12)
    ]
    found = knowledge.retrieve(
        "В каком году появился клуб «Хищные Бобры» и откуда его название?",
        [name, *distractors, foundation],
    )
    assert found[:2] == [foundation, name]


def test_relative_day_without_match_returns_no_unrelated_history():
    history = doc("history", "Матч сыграли вчера в 2024 году", kind="history")
    yesterday = doc(
        "match", "Матч", kind="match", starts_at=NOW - timedelta(days=1), status="finished"
    )
    assert (
        knowledge.retrieve(
            "Есть ли матч сегодня? Какая сейчас дата?", [history, yesterday], now=NOW
        )
        == []
    )


def test_opponent_constraint_applies_before_last_and_next_match_selection():
    opponent = knowledge.KnowledgeDoc(
        "target",
        "match",
        "ВГУ — ВГТУ",
        "Матч с ВГТУ",
        "/matches",
        starts_at=NOW - timedelta(days=2),
        match_status="finished",
        event_year=2026,
    )
    other = knowledge.KnowledgeDoc(
        "other",
        "match",
        "ВГУ — ВГАС",
        "Матч с ВГАС",
        "/matches",
        starts_at=NOW - timedelta(days=1),
        match_status="finished",
        event_year=2026,
    )
    assert knowledge.retrieve(
        "Как прошёл последний матч с вгту в 2026 году?", [other, opponent], now=NOW
    ) == [opponent]


def test_outside_topic_does_not_fuzzy_match_multiple_unrelated_words():
    history = doc("history", "Команда проиграла соревнование", kind="history")
    assert knowledge.retrieve("Пожалуйста, расскажи, как приготовить борщ?", [history]) == []


@pytest.mark.asyncio
async def test_public_content_venue_and_player_projection_excludes_private_metadata(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "public.db").as_posix())
    factory = async_sessionmaker(engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            team = Team(slug="men", name="ВГУ", category=TeamCategory.MEN)
            competition = Competition(name="АСБ", season="2026-2027")
            venue = Venue(name="Скрытый зал", address="Скрытый адрес", active=False)
            session.add_all(
                [
                    team,
                    competition,
                    venue,
                    ContentBlock(
                        section="captains",
                        title="Дарья Публичная",
                        body="Женская команда",
                        contacts=[
                            {"label": "private", "value": "+79998887766", "url": "tel:+79998887766"}
                        ],
                    ),
                    ContentBlock(
                        section="captains",
                        title="Скрытый капитан",
                        body="Мужская команда",
                        active=False,
                    ),
                    ContentBlock(
                        section="player_roles", title="Разыгрывающий", body="Организует атаку"
                    ),
                    ContentBlock(
                        section="viewing_tips", title="Совет", body="Следите за передачами"
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    Player(
                        team_id=team.id, full_name="Локальный игрок", birth_date=date(1999, 3, 17)
                    ),
                    Match(
                        team_id=team.id,
                        competition_id=competition.id,
                        venue_id=venue.id,
                        starts_at=NOW,
                        club_side=ClubSide.HOME,
                        home_team_name="ВГУ",
                        away_team_name="ВГТУ",
                        status=MatchStatus.FINISHED,
                        home_score=70,
                        away_score=60,
                    ),
                ]
            )
            await session.commit()
            corpus = await knowledge.build_corpus(session, now=NOW)
        text = " ".join(item.text for item in corpus)
        assert "Разыгрывающий" in text and "Следите за передачами" in text
        assert "Дарья Публичная" in text and "капитане" in text
        for forbidden in (
            "+79998887766",
            "1999-03-17",
            "Скрытый капитан",
            "Скрытый зал",
            "Скрытый адрес",
        ):
            assert forbidden not in text
        captain = next(item for item in corpus if item.kind == "captains")
        assert captain.team_category == "women"
        assert captain.url == "/contacts#captains-title"
        assert next(item for item in corpus if item.kind == "match").text
        assert {item.url for item in corpus if item.kind == "navigation"} >= {
            "/matches",
            "/standings",
            "/teams/men",
            "/teams/women",
        }
    finally:
        await engine.dispose()
