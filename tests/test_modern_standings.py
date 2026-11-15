import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from collect_asb_standings import modern_tables  # noqa: E402
from import_asb_standings import OfficialTable  # noqa: E402


def payload():
    return {
        "status": "Ok",
        "totalCount": 1,
        "items": [
            {
                "comp": {
                    "id": 1458985,
                    "season": 2027,
                    "compType": "RoundRobin",
                    "name": "Круговой турнир",
                },
                "standings": [
                    {
                        "teamId": 7433,
                        "name": "ВГУ",
                        "place": 2,
                        "totalGames": 5,
                        "totalWin": 4,
                        "totalDefeat": 1,
                        "totalDraw": None,
                        "totalPoints": 9,
                        "totalGoalPlus": 400,
                        "totalGoalMinus": 350,
                        "logo": None,
                    }
                ],
            }
        ],
    }


def normalize(data):
    return modern_tables(
        data,
        {"compId": 1415975, "name": "Воронежская область (м)"},
        [],
        team="men",
        team_id=7433,
        season="2026-2027",
        source_url="https://asb.sportoteka.org/api/abc/comps/standings?tag=asb-cfo-36-m&season=2027&lang=ru",
    )


def test_new_season_uses_full_official_totals_even_without_individual_games():
    tables = normalize(payload())
    table = OfficialTable.model_validate(tables[0])
    assert table.season == "2026-2027"
    assert table.competition_id == "1458985"
    assert table.tournament_id == "1415975"
    assert table.rows[0].played == 5
    assert table.rows[0].points_for == 400
    assert table.rows[0].points_difference == 50
    assert not table.games


def test_wrong_season_registration_lists_and_truncated_tables_are_rejected():
    data = payload()
    data["items"][0]["comp"]["season"] = 2026
    assert normalize(data) == []
    data = payload()
    data["items"][0]["standings"][0]["totalGames"] = None
    with pytest.raises(ValueError, match="Registration lists"):
        normalize(data)
    data = payload()
    data["totalCount"] = 2
    with pytest.raises(ValueError, match="Incomplete"):
        normalize(data)


def test_official_source_is_bound_to_team_and_season():
    table = normalize(payload())[0]
    for invalid in [
        table["source_url"].replace("season=2027", "season=2026"),
        table["source_url"].replace("asb-cfo-36-m", "asb-cfo-36-w"),
        table["source_url"].replace("asb.sportoteka.org", "example.test"),
    ]:
        with pytest.raises(ValidationError):
            OfficialTable.model_validate({**table, "source_url": invalid})
