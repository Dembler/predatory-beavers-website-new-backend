from datetime import date
from uuid import uuid4

from predatory_beavers.modules.results.honours import honours_summary, unique_tournament_results
from predatory_beavers.modules.results.models import ArchiveResult


def result(**overrides: object) -> ArchiveResult:
    values: dict[str, object] = {
        "id": uuid4(),
        "team": "men",
        "discipline": "3x3",
        "year": 2024,
        "season": "2024",
        "tournament": "Кубок города",
        "location": "Воронеж",
        "status": "finished",
        "opponent_name": None,
        "place": 1,
        "source_label": "Архив клуба",
        "source": "club_archive",
        "external_id": "fixture-result",
        "source_url": None,
    }
    return ArchiveResult(**(values | overrides))


def test_trophies_exclude_games_unknown_places_and_duplicate_citations() -> None:
    summary = honours_summary(
        [
            result(),
            result(source_url="https://example.com/cup"),
            result(
                tournament="Суперфинал Чемпионата АССК России · высший дивизион",
                external_id="sponsors-xlsx-10-men-2024",
            ),
            result(team="women", place=2),
            result(year=2025, place=3),
            result(tournament="Финал", opponent_name="Соперник", club_score=90, opponent_score=80),
            result(tournament="Участие", place=None),
            result(tournament="Будущий турнир", status="scheduled"),
        ]
    )
    assert summary["titles"] == 2
    assert summary["championships"] == 1
    assert summary["podiums"] == 4
    assert summary["silver"] == 1
    assert summary["bronze"] == 1
    assert summary["from_year"] == 2024
    assert summary["to_year"] == 2025


def test_empty_honours_have_no_fabricated_years_or_titles() -> None:
    summary = honours_summary([])
    assert summary["titles"] == 0
    assert summary["championships"] == 0
    assert summary["from_year"] is None
    assert summary["items"] == []


def test_catalog_keeps_fourth_place_and_participation_without_inventing_medals() -> None:
    fourth = result(tournament="Кубок", place=4)
    participation = result(tournament="Фестиваль", status="participated", place=None)
    assert set(row.id for row in unique_tournament_results([fourth, participation])) == {
        fourth.id,
        participation.id,
    }
    assert honours_summary([fourth, participation])["podiums"] == 0


def test_academic_season_report_does_not_count_december_medal_again_in_june() -> None:
    december = result(year=2012, season="2012-2013", place=2)
    june_report = result(year=2013, season="2012-2013", place=2)
    assert honours_summary([december, june_report])["silver"] == 1


def test_stages_divisions_teams_disciplines_and_distinct_editions_stay_separate() -> None:
    rows = [
        result(tournament="Кубок · отбор"),
        result(tournament="Кубок · суперфинал", place=2),
        result(tournament="Кубок · второй дивизион"),
        result(team="women"),
        result(discipline="phygital"),
        result(event_date=date(2024, 1, 10)),
        result(event_date=date(2024, 10, 10)),
    ]
    assert len(unique_tournament_results(rows)) == len(rows)
    # An undated report cannot establish a third edition of the same cup.
    assert len(unique_tournament_results(rows + [result()])) == len(rows)


def test_conflicting_places_are_withheld_and_championship_substring_is_not_proof() -> None:
    assert honours_summary([result(place=1), result(place=2)])["podiums"] == 0
    summary = honours_summary([result(tournament="Отборочный этап чемпионата")])
    assert summary["titles"] == 1
    assert summary["championships"] == 0


def test_venue_detail_and_undated_citation_do_not_duplicate_a_dated_event() -> None:
    dated = result(event_date=date(2024, 10, 10), location="Воронеж · арена")
    undated = result(location="Воронеж", source_url="https://example.com/report")
    assert len(unique_tournament_results([dated, undated])) == 1
