import json
from datetime import date
from pathlib import Path
from uuid import uuid4

from predatory_beavers.modules.results.honours import honours_summary
from predatory_beavers.modules.results.models import ArchiveResult
from predatory_beavers.modules.results.tournaments_router import (
    TournamentContext,
    load_contexts,
    tournament_catalogue,
)


def result(**overrides: object) -> ArchiveResult:
    values: dict[str, object] = {
        "id": uuid4(),
        "external_id": str(uuid4()),
        "source": "club_archive",
        "team": "men",
        "discipline": "3x3",
        "year": 2024,
        "season": "2024",
        "tournament": "Кубок регионов · суперфинал",
        "location": "Санкт-Петербург",
        "status": "finished",
        "opponent_name": None,
        "place": 4,
        "team_name": "ВГУ",
        "date_precision": "year",
        "source_label": "Архив клуба",
    }
    return ArchiveResult(**(values | overrides))


def context(key: str, term: str) -> TournamentContext:
    return TournamentContext(
        key=key,
        name=term,
        match_terms=[term],
        description="Проверенное описание турнира",
        organizer="АСБ",
        official_url="https://asbasket.ru/",
        logo_url="/images/tournaments/asb.svg",
        logo_alt="Эмблема АСБ, организатора турнира",
        logo_scope="organizer",
        sources=[],
    )


def test_tournament_history_keeps_non_podium_results_and_distinct_stages() -> None:
    catalogue = tournament_catalogue(
        [
            result(),
            result(),
            result(tournament="Кубок регионов · отборочный тур", location="Краснодар", place=1),
            result(team="women", place=None, status="participated"),
            result(opponent_name="Соперник", club_score=80, opponent_score=40, place=1),
        ],
        [context("asb", "Кубок"), context("regions", "Кубок регионов")],
    )
    assert len(catalogue.items) == 1
    assert catalogue.items[0].key == "regions"
    assert catalogue.result_count == 3
    assert sorted(row.place or 99 for row in catalogue.items[0].results) == [1, 4, 99]
    assert catalogue.items[0].logo_scope == "organizer"


def test_new_tournament_is_preserved_without_invented_context_or_logo() -> None:
    row = result(tournament="Новый турнир")
    catalogue = tournament_catalogue([row], [context("asb", "Кубок")])
    assert catalogue.result_count == 1
    assert catalogue.items[0].name == row.tournament
    assert catalogue.items[0].logo_url is None
    assert catalogue.items[0].description == ""
    assert catalogue.items[0].results[0].id == row.id


def test_reviewed_catalogue_covers_every_curated_award_and_non_podium_result() -> None:
    root = Path(__file__).resolve().parents[1]
    context_manifest = json.loads(
        (root / "docs/club-tournament-context.json").read_text(encoding="utf-8")
    )
    assert load_contexts() == [
        TournamentContext.model_validate(item) for item in context_manifest["items"]
    ]
    manifest = json.loads((root / "docs/results-curated.json").read_text(encoding="utf-8"))
    rows = []
    for raw in manifest["results"]:
        values = dict(raw)
        if values["event_date"]:
            values["event_date"] = date.fromisoformat(values["event_date"])
        rows.append(ArchiveResult(id=uuid4(), **values))
    catalogue = tournament_catalogue(rows, load_contexts())
    assert catalogue.result_count > 0
    assert all(not item.key.startswith("event-") for item in catalogue.items)
    assert all(item.description and item.logo_url and item.logo_alt for item in catalogue.items)
    recorded = {str(row.id) for item in catalogue.items for row in item.results}
    summary = honours_summary(rows)
    assert {item["id"] for item in summary["items"]} <= recorded
    assert (
        sum(row.place in (1, 2, 3) for item in catalogue.items for row in item.results)
        == (summary["podiums"])
    )
    assert any(row.place is None for item in catalogue.items for row in item.results)
    assert any(row.place and row.place > 3 for item in catalogue.items for row in item.results)
