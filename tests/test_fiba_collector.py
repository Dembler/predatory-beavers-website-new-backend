"""FIBA adapter contract tests use documented fields, never live credentials."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest

from scripts.collect_fiba_games import API_BASE, collect_watched
from scripts.import_public_results import PublicResult

ROOT = Path(__file__).resolve().parents[1]
REGISTRATION = "1054cee5-eb42-4b5b-b73d-da6762dc9e70"
TEAM = "11111111-1111-4111-8111-111111111111"
OPPONENT = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
DIVISION = "44444444-4444-4444-8444-444444444444"
GAME = "55555555-5555-4555-8555-555555555555"
GAME_TWO = "66666666-6666-4666-8666-666666666666"


def watchlist(tmp_path: Path, **overrides: object) -> Path:
    config = json.loads((ROOT / "docs/fiba-watchlist.json").read_text(encoding="utf-8"))
    config["registrations"][0].update(overrides)
    target = tmp_path / "watchlist.json"
    target.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return target


def game(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "GameId": GAME,
        "GameHomeTeamId": TEAM,
        "GameAwayTeamId": OPPONENT,
        "GameStatus": "Official",
        "GameHomeTeamScore": 21,
        "GameAwayTeamScore": 14,
        "GameWinnerTeamId": TEAM,
        "GameDates": {"StartDate": "2025-12-05T23:30:00-03:00"},
    }
    result.update(overrides)
    return result


def client_for(
    games: list[dict[str, object]], *, registration_patch: dict[str, object] | None = None
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(API_BASE)
        assert request.headers["X-Application-Key"] == "test-key"
        if request.url.path == f"/api/TeamRegistrationPublic/{REGISTRATION}":
            team = {"TeamId": TEAM, "TeamInEventId": REGISTRATION, "DivisionId": DIVISION}
            team.update(registration_patch or {})
            return httpx.Response(200, json={"Team": team, "ResponseStatus": 200})
        if request.url.path == f"/api/DivisionTeamsCore/{DIVISION}":
            return httpx.Response(
                200,
                json={
                    "Teams": [
                        {"TeamId": TEAM, "TeamName": "ВГУ"},
                        {"TeamId": OPPONENT, "TeamName": "СевГУ"},
                        {"TeamId": OTHER, "TeamName": "Другая команда"},
                    ]
                },
            )
        if request.url.path == f"/api/DivisionGames/{DIVISION}":
            return httpx.Response(200, json={"Games": games})
        raise AssertionError(f"Unexpected request: {request.url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_missing_key_performs_no_requests_and_preserves_watchlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("APP_FIBA_API_KEY", raising=False)
    path = watchlist(tmp_path)
    original = path.read_bytes()

    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("No-key collection must never call FIBA")

    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        report = collect_watched(path, client=client)
    assert report["status"] == "auth_required"
    assert report["results"] == []
    assert report["registrations"][0]["status"] == "auth_required"
    assert report["coverage"][0]["status"] == "auth_required"
    assert path.read_bytes() == original


def test_collects_exact_team_normalizes_side_and_ignores_foreign_game(tmp_path: Path) -> None:
    path = watchlist(tmp_path)
    away = game(
        GameId=GAME_TWO,
        GameHomeTeamId=OPPONENT,
        GameAwayTeamId=TEAM,
        GameHomeTeamScore=11,
        GameAwayTeamScore=21,
    )
    foreign = game(GameId="77777777-7777-4777-8777-777777777777", GameHomeTeamId=OTHER)
    with client_for([game(), away, foreign]) as client:
        report = collect_watched(path, "test-key", client)
    assert report["status"] == "ok"
    assert len(report["results"]) == 2
    for row in report["results"]:
        validated = PublicResult.model_validate(row)
        assert validated.team == "men"
        assert validated.club_score == 21
        assert validated.opponent_name == "СевГУ"
        # Preserve the date in the published offset, not the UTC conversion.
        assert str(validated.event_date) == "2025-12-05"
        assert validated.source == "fiba3x3"
    assert report["registrations"][0]["external_ids"] == [GAME, GAME_TWO]
    assert report["registrations"][0]["division_id"] == DIVISION
    assert report["coverage"][0]["status"] == "partial"
    assert report["scope"] == "explicit_registrations_only"


@pytest.mark.parametrize("status", ["Scheduled", "Unofficial", "OfficialUnscored", "Validated"])
def test_unfinished_scores_never_become_results(tmp_path: Path, status: str) -> None:
    with client_for([game(GameStatus=status)]) as client:
        report = collect_watched(watchlist(tmp_path), "test-key", client)
    row = report["results"][0]
    assert row["status"] == ("scheduled" if status == "Scheduled" else "unpublished")
    assert row["club_score"] is row["opponent_score"] is None


def test_official_forfeit_with_consistent_zero_score_is_finished(tmp_path: Path) -> None:
    with client_for([game(GameStatus="OfficialForfeit", GameAwayTeamScore=0)]) as client:
        report = collect_watched(watchlist(tmp_path), "test-key", client)
    assert report["results"][0]["status"] == "finished"
    assert report["results"][0]["opponent_score"] == 0


@pytest.mark.parametrize(
    "patch",
    [
        {"GameWinnerTeamId": OPPONENT},
        {"GameAwayTeamScore": 21},
        {"GameHomeTeamScore": None},
        {"GameAwayTeamScore": True},
        {"GameDates": None},
        {"GameDates": {"StartDate": "not-a-date"}},
        {"GameId": "bad-game-id"},
        {"GameStatus": "FutureUndocumentedState"},
        {"GameStatus": {}},
    ],
)
def test_invalid_game_discards_entire_registration_including_prior_good_rows(
    tmp_path: Path, patch: dict[str, object]
) -> None:
    broken = game(GameId=GAME_TWO)
    broken.update(patch)
    with client_for([game(), broken]) as client:
        report = collect_watched(watchlist(tmp_path), "test-key", client)
    assert report["results"] == []
    assert report["status"] == "unavailable"
    assert report["registrations"][0]["status"] == "invalid_data"
    assert report["registrations"][0]["external_ids"] == []


def test_supports_documented_backing_date_fields_and_configured_gender(tmp_path: Path) -> None:
    fixture = game(GameDates={"<StartDate>k__BackingField": "2025-12-06T00:30:00+03:00"})
    with client_for([fixture]) as client:
        report = collect_watched(watchlist(tmp_path, team="women"), "test-key", client)
    assert report["results"][0]["team"] == "women"
    assert report["results"][0]["event_date"] == "2025-12-06"


@pytest.mark.parametrize(
    "registration_patch",
    [
        {"TeamInEventId": OPPONENT},
        {"TeamId": "bad-id"},
        {"CategoryId": OPPONENT},
        {"EventId": OPPONENT},
    ],
)
def test_registration_identity_must_match_explicit_watch(
    tmp_path: Path, registration_patch: dict[str, object]
) -> None:
    with client_for([game()], registration_patch=registration_patch) as client:
        report = collect_watched(watchlist(tmp_path), "test-key", client)
    assert report["registrations"][0]["status"] == "invalid_data"
    assert report["results"] == []


@pytest.mark.parametrize("code", [401, 403])
def test_auth_errors_are_reported_without_leaking_body_or_key(tmp_path: Path, code: int) -> None:
    secret = "secret-not-for-reports"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(code, text=secret)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = collect_watched(watchlist(tmp_path), secret, client)
    assert report["status"] == "auth_required"
    assert report["results"] == []
    assert secret not in json.dumps(report)


def test_auth_failure_inside_http_200_is_not_an_empty_success(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ResponseStatus": 403, "ResponseMessage": "secret"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = collect_watched(watchlist(tmp_path), "test-key", client)
    assert report["status"] == "auth_required"
    assert report["results"] == []


def test_malformed_response_status_is_reported(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ResponseStatus": [], "ResponseStatusCode": {}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = collect_watched(watchlist(tmp_path), "test-key", client)
    assert report["registrations"][0]["status"] == "invalid_data"
    assert report["results"] == []


def test_timeout_is_non_destructive_and_does_not_expose_exception(tmp_path: Path) -> None:
    path = watchlist(tmp_path)
    before = path.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("request-secret", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = collect_watched(path, "test-key", client)
    assert report["registrations"][0]["status"] == "upstream_unavailable"
    assert report["results"] == []
    assert "request-secret" not in json.dumps(report)
    assert path.read_bytes() == before


def test_disabled_watch_and_invalid_uuid_never_call_api(tmp_path: Path) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Disabled or invalid watch must not call FIBA")

    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        report = collect_watched(watchlist(tmp_path, enabled=False), "test-key", client)
        assert report["status"] == "disabled"
        assert report["coverage"] == []
        report = collect_watched(watchlist(tmp_path, registration_id="invalid"), "test-key", client)
        assert report["status"] == "invalid_watchlist"


def test_duplicate_or_conflicting_watches_are_rejected(tmp_path: Path) -> None:
    path = watchlist(tmp_path)
    config = json.loads(path.read_text(encoding="utf-8"))
    duplicate = copy.deepcopy(config["registrations"][0])
    duplicate["team"] = "women"
    config["registrations"].append(duplicate)
    path.write_text(json.dumps(config), encoding="utf-8")
    assert collect_watched(path, "test-key")["status"] == "invalid_watchlist"


def test_environment_key_is_supported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_FIBA_API_KEY", "test-key")
    with client_for([]) as client:
        report = collect_watched(watchlist(tmp_path), client=client)
    assert report["status"] == "ok"
    assert report["results"] == []


def test_failure_of_second_registration_preserves_first_success(tmp_path: Path) -> None:
    path = watchlist(tmp_path)
    config = json.loads(path.read_text(encoding="utf-8"))
    second = copy.deepcopy(config["registrations"][0])
    second["registration_id"] = OTHER
    second["source_url"] = second["source_url"].replace(REGISTRATION, OTHER)
    second["team"] = "women"
    config["registrations"].append(second)
    path.write_text(json.dumps(config), encoding="utf-8")
    base = client_for([game()])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(OTHER):
            return httpx.Response(503, text="Temporarily unavailable")
        return base.send(request)

    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            report = collect_watched(path, "test-key", client)
    finally:
        base.close()
    assert report["status"] == "partial"
    assert [row["external_id"] for row in report["results"]] == [GAME]
    assert [item["status"] for item in report["registrations"]] == ["ok", "upstream_unavailable"]
    assert report["registrations"][1]["external_ids"] == []
