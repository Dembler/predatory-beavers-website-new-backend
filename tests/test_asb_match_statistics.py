"""Actual official protocols plus adversarial period/identity/incomplete revisions."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from predatory_beavers.integrations.asb.statistics import normalize_game_statistics

FIXTURES = Path(__file__).parent / "fixtures"
FETCHED = datetime(2026, 11, 17, tzinfo=UTC)


def protocol(game="1138965"):
    return json.loads((FIXTURES / f"asb-game-{game}.json").read_text(encoding="utf-8"))


def normalize(data, game="1138965", team="men"):
    return normalize_game_statistics(
        data,
        external_id=game,
        team=team,
        season="2026-2027",
        fetched_at=FETCHED,
    )


@pytest.mark.parametrize(
    ("game", "team", "final", "quarters", "leader", "points", "quarter_points"),
    [
        (
            "1138965",
            "men",
            (60, 51),
            [(19, 12), (12, 11), (17, 16), (12, 12)],
            "129137",
            19,
            [7, 2, 6, 4],
        ),
        (
            "1138973",
            "women",
            (25, 90),
            [(11, 23), (3, 23), (4, 24), (7, 20)],
            "502016",
            7,
            [4, 0, 2, 1],
        ),
    ],
)
def test_real_protocols_reconcile_full_game_and_per_period_shooting(
    game,
    team,
    final,
    quarters,
    leader,
    points,
    quarter_points,
):
    result = normalize(protocol(game), game, team)
    assert result is not None
    assert (result.home_score, result.away_score) == final
    assert [(p.home_score, p.away_score) for p in result.period_scores] == quarters
    assert len(result.box_score) == 24
    assert result.player_periods_status == "partial"
    player = next(p for p in result.box_score if p.external_player_id == leader)
    assert player.statistics.points == points
    assert [p.statistics.points for p in player.period_statistics] == quarter_points
    assert all(p.statistics.assists is None for p in player.period_statistics)
    assert all(p.statistics.rebounds is None for p in player.period_statistics)
    assert all(p.statistics.minutes_seconds is None for p in player.period_statistics)
    assert player.played is True
    for side in ("home", "away"):
        assert sum(p.statistics.points for p in result.box_score if p.team_side == side) == (
            getattr(result, side + "_score")
        )
    # Only the sport projection survives; raw birth dates/profile measurements never do.
    encoded = result.model_dump_json()
    assert all(k not in encoded for k in ("birthday", "height", "weight", "countryId", "photo"))
    assert not any(p.full_name.endswith("Coach") for p in result.box_score)


def test_away_club_mapping_is_official_and_does_not_flip_period_scores():
    data = protocol()
    data["result"]["team1"]["teamId"] = 7635
    data["result"]["team2"]["teamId"] = 7433
    for row in data["result"]["teams"]:
        row["teamId"] = 7635 if row["teamNumber"] == 1 else 7433
    result = normalize(data)
    assert result.club_side == "away"
    assert result.period_scores[0].home_score == 19
    assert result.period_scores[0].away_score == 12


@pytest.mark.parametrize("score", ["19:12,31:23,48:39,60:51", "19:12,12:11,17:16,11:12", ""])
def test_cumulative_mismatched_or_missing_periods_are_never_guessed(score):
    data = protocol()
    data["result"]["game"]["fullScore"] = score
    result = normalize(data)
    assert result is not None and len(result.box_score) == 24
    assert result.period_scores == []
    assert result.player_periods_status == "unavailable"
    assert all(not p.period_statistics for p in result.box_score)


def test_overtime_has_separate_number_and_is_included_in_final_sum():
    data = protocol()
    data["result"]["game"].update(
        fullScore="20:20,20:20,20:20,10:10,5:3,4:2",
        periods=6,
        score1=79,
        score2=75,
    )
    result = normalize(data)
    assert result.box_score == []  # The original player totals no longer match this synthetic game.
    assert [p.label for p in result.period_scores][-2:] == ["ОТ1", "ОТ2"]
    assert [p.kind for p in result.period_scores] == ["quarter"] * 4 + ["overtime"] * 2


@pytest.mark.parametrize("status", ["Forfeit", "Scheduled", "Postponed", "Cancelled", "Live"])
def test_nonplayed_and_unfinished_games_do_not_invent_protocols(status):
    data = protocol()
    data["result"]["game"].update(gameStatus=status, score1=20, score2=0)
    assert normalize(data) is None


@pytest.mark.parametrize(
    "data", [{}, {"status": "Ok", "result": None}, {"status": "Ok", "result": {}}]
)
def test_empty_200_is_unavailable_not_an_empty_authoritative_revision(data):
    assert normalize(data) is None


@pytest.mark.parametrize("change", ["game", "team", "season", "3x3", "box_side"])
def test_protocol_identity_and_discipline_must_match_reviewed_game(change):
    data = protocol()
    if change == "game":
        data["result"]["game"]["id"] = 1
    elif change == "team":
        data["result"]["team1"]["teamId"] = 1
    elif change == "season":
        data["result"]["game"]["defaultZoneDateTime"] = "2025-06-01T12:00:00+03:00"
    elif change == "3x3":
        data["result"]["live"]["playersOnCourt"] = 3
    else:
        data["result"]["teams"][0]["teamId"] = 1
    with pytest.raises(ValueError):
        normalize(data)


def test_dnp_unknown_time_rebounder_and_jersey_zero_are_distinct():
    data = protocol()
    row = next(
        p
        for team in data["result"]["teams"]
        for p in team["starts"]
        if p.get("personId") == 584961 and p["startRole"] == "Player"
    )
    result = normalize(data)
    dnp = next(p for p in result.box_score if p.external_player_id == "584961")
    assert dnp.played is False and dnp.period_statistics == []
    row["stats"]["second"] = None
    row["displayNumber"] = 0
    unknown = next(p for p in normalize(data).box_score if p.external_player_id == "584961")
    assert unknown.played is None and unknown.jersey_number == "0"
    row["stats"].update(second=0, offReb=1)
    rebounder = next(p for p in normalize(data).box_score if p.external_player_id == "584961")
    assert rebounder.played is True


@pytest.mark.parametrize("damage", ["duplicate", "missing_shot", "wrong_period", "wrong_player"])
def test_incomplete_or_ambiguous_play_by_play_cannot_invent_quarter_statistics(damage):
    data = protocol()
    plays = data["result"]["plays"]
    shot = next(e for e in plays if e["play"] in {1, 2, 3})
    if damage == "duplicate":
        plays.append(copy.deepcopy(shot))
    elif damage == "missing_shot":
        plays.remove(shot)
    elif damage == "wrong_period":
        shot["period"] = 99
    else:
        shot["startNum"] = 999999
    result = normalize(data)
    assert len(result.period_scores) == 4 and len(result.box_score) == 24
    assert result.player_periods_status == "unavailable"
    assert all(not p.period_statistics for p in result.box_score)


def test_duplicate_player_and_unknown_shooting_totals_are_not_silently_merged():
    data = protocol()
    team = data["result"]["teams"][0]
    player = next(p for p in team["starts"] if p["startRole"] == "Player")
    team["starts"].append(copy.deepcopy(player))
    with pytest.raises(ValueError, match="duplicate"):
        normalize(data)
    team["starts"].pop()
    player["stats"]["shot2"] = None
    result = normalize(data)
    assert len(result.box_score) == 24
    assert result.player_periods_status == "unavailable"
    actual = next(p for p in result.box_score if p.external_player_id == str(player["personId"]))
    assert actual.statistics.two_points_attempted is None
