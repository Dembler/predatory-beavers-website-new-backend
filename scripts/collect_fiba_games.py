"""Collect official FIBA 3x3 games for explicitly watched event registrations.

This module is read-only: it returns PublicResult-compatible JSON rows and never
replaces a manifest or writes a database. An orchestrator may merge successful
registrations; failed ones must retain their previous imported games. It does
not discover future club registrations from the one historical registration.

Run ``python -m scripts.collect_fiba_games`` to inspect the structured report.
APP_FIBA_API_KEY is sent only to the fixed official API host, never printed.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, model_validator

from scripts.import_public_results import PublicResult

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WATCHLIST = ROOT / "docs/fiba-watchlist.json"
API_BASE = "https://api.fiba3x3.com/api"
LIMITATION = (
    "Отслеживаются только явно добавленные регистрации FIBA 3×3. "
    "Новые турниры клуба автоматически по этой регистрации не обнаруживаются."
)
KNOWN_STATUSES = {
    "Deleted",
    "Invisible",
    "Unknown",
    "Imported",
    "Scheduled",
    "Calculated",
    "Validated",
    "Unofficial",
    "OfficialUnscored",
    "OfficialForfeit",
    "Official",
}


class WatchedRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team: Literal["men", "women"]
    registration_id: UUID
    category_id: UUID
    event_id: UUID | None = None
    season: str = Field(pattern=r"^20\d{2}-20\d{2}$")
    tournament: str = Field(min_length=1, max_length=500)
    source_url: HttpUrl
    location: str | None = Field(default=None, max_length=500)
    enabled: bool = True

    @model_validator(mode="after")
    def official_registration_url(self) -> WatchedRegistration:
        if any(
            value is not None and value.int == 0
            for value in (self.registration_id, self.category_id, self.event_id)
        ):
            raise ValueError("Watchlist identifiers cannot be empty UUIDs")
        url = urlsplit(str(self.source_url))
        expected = f"/categories/{self.category_id}/teams/{self.registration_id}"
        if url.scheme != "https" or url.hostname != "play.fiba3x3.com":
            raise ValueError("source_url must be the official FIBA 3x3 registration URL")
        if url.path.rstrip("/").lower() != expected:
            raise ValueError("source_url must match category_id and registration_id")
        return self


class Watchlist(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    scope: Literal["explicit_registrations_only"]
    note: str = ""
    registrations: list[WatchedRegistration]

    @model_validator(mode="after")
    def unique_registrations(self) -> Watchlist:
        identities = [item.registration_id for item in self.registrations]
        if len(identities) != len(set(identities)):
            raise ValueError("A registration may be watched only once")
        return self


class FibaCollectionError(Exception):
    def __init__(self, status: str, message: str) -> None:
        self.status = status
        super().__init__(message)


def _uuid(value: object, field: str) -> str:
    try:
        result = UUID(str(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise FibaCollectionError("invalid_data", f"Invalid UUID in {field}") from error
    if result.int == 0:
        raise FibaCollectionError("invalid_data", f"Missing assigned UUID in {field}")
    return str(result)


def _get(client: httpx.Client, path: str, api_key: str) -> dict[str, Any]:
    try:
        response = client.get(
            API_BASE + path,
            headers={"X-Application-Key": api_key, "Accept": "application/json"},
            timeout=20,
            follow_redirects=False,
        )
    except httpx.RequestError as error:
        raise FibaCollectionError("upstream_unavailable", "FIBA API request failed") from error
    if response.status_code in {401, 403}:
        raise FibaCollectionError("auth_required", "FIBA API denied the application key")
    if response.status_code != 200:
        raise FibaCollectionError(
            "upstream_unavailable", f"FIBA API returned HTTP {response.status_code}"
        )
    try:
        payload = response.json()
    except ValueError as error:
        raise FibaCollectionError("invalid_data", "FIBA API returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise FibaCollectionError("invalid_data", "FIBA API response must be an object")
    code = payload.get("ResponseStatus")
    status_name = payload.get("ResponseStatusCode")
    if code in (401, 403) or status_name in ("Unauthorized", "Forbidden"):
        raise FibaCollectionError("auth_required", "FIBA API denied the application key")
    if code is not None and type(code) is not int:
        raise FibaCollectionError("invalid_data", "FIBA API response status is invalid")
    if status_name is not None and not isinstance(status_name, str):
        raise FibaCollectionError("invalid_data", "FIBA API response status code is invalid")
    if status_name not in (None, "OK"):
        raise FibaCollectionError(
            "upstream_unavailable", "FIBA API reported an unsuccessful response"
        )
    if code is not None and code != 200:
        raise FibaCollectionError(
            "upstream_unavailable", "FIBA API reported an unsuccessful response"
        )
    return payload


def _game_date(game: dict[str, Any]) -> date:
    dates = game.get("GameDates")
    if not isinstance(dates, dict):
        raise FibaCollectionError("invalid_data", "GameDates is missing")
    raw = dates.get("StartDate") or dates.get("<StartDate>k__BackingField")
    if not isinstance(raw, str) or not raw.strip():
        raise FibaCollectionError("invalid_data", "Game start date is not published")
    try:
        # Preserve the calendar day in the source's published offset. We do not
        # turn an unknown kickoff into midnight or guess an event timezone.
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError as error:
        raise FibaCollectionError(
            "invalid_data", "Game start date is not valid ISO-8601"
        ) from error


def _score(value: object) -> int:
    if type(value) is not int or value < 0:
        raise FibaCollectionError("invalid_data", "Official game has invalid or missing scores")
    return value


def _collect_registration(
    watch: WatchedRegistration, client: httpx.Client, api_key: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    registration = _get(client, f"/TeamRegistrationPublic/{watch.registration_id}", api_key)
    team = registration.get("Team")
    if not isinstance(team, dict):
        raise FibaCollectionError("invalid_data", "Registration did not contain a team")
    if _uuid(team.get("TeamInEventId"), "TeamInEventId") != str(watch.registration_id):
        raise FibaCollectionError("invalid_data", "Registration identity does not match watchlist")
    division_id = _uuid(team.get("DivisionId"), "DivisionId")
    team_id = _uuid(team.get("TeamId"), "TeamId")
    if team.get("CategoryId") is not None:
        if _uuid(team["CategoryId"], "CategoryId") != str(watch.category_id):
            raise FibaCollectionError(
                "invalid_data", "Registration category does not match watchlist"
            )
    event_id = str(watch.event_id) if watch.event_id else None
    if team.get("EventId") is not None:
        returned_event = _uuid(team["EventId"], "EventId")
        if event_id and returned_event != event_id:
            raise FibaCollectionError("invalid_data", "Registration event does not match watchlist")
        event_id = returned_event
    names_payload = _get(client, f"/DivisionTeamsCore/{division_id}", api_key)
    teams = names_payload.get("Teams")
    if not isinstance(teams, list):
        raise FibaCollectionError("invalid_data", "Division teams must be an array")
    names: dict[str, str] = {}
    for item in teams:
        if not isinstance(item, dict):
            raise FibaCollectionError("invalid_data", "Invalid division team")
        identity = _uuid(item.get("TeamId"), "TeamId")
        name = item.get("TeamName")
        if not isinstance(name, str) or not name.strip():
            raise FibaCollectionError("invalid_data", "Division team name is missing")
        if identity in names and names[identity] != name.strip():
            raise FibaCollectionError("invalid_data", "Conflicting division team names")
        names[identity] = name.strip()
    if team_id not in names:
        raise FibaCollectionError("invalid_data", "Watched team is absent from its division")
    games_payload = _get(client, f"/DivisionGames/{division_id}", api_key)
    games = games_payload.get("Games")
    if not isinstance(games, list):
        raise FibaCollectionError("invalid_data", "Division games must be an array")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for game in games:
        if not isinstance(game, dict):
            raise FibaCollectionError("invalid_data", "Invalid division game")
        # Foreign games and incomplete knockout placeholders are not club games.
        home_raw, away_raw = game.get("GameHomeTeamId"), game.get("GameAwayTeamId")
        if team_id not in {str(home_raw).lower(), str(away_raw).lower()}:
            continue
        state = game.get("GameStatus")
        if not isinstance(state, str):
            raise FibaCollectionError("invalid_data", "FIBA game status must be a string")
        if state in {"Deleted", "Invisible"}:
            continue
        if state not in KNOWN_STATUSES:
            raise FibaCollectionError("invalid_data", "Unknown FIBA game status")
        game_id = _uuid(game.get("GameId"), "GameId")
        if game_id in seen:
            raise FibaCollectionError("invalid_data", "Duplicate game identity in division")
        seen.add(game_id)
        home_id, away_id = _uuid(home_raw, "GameHomeTeamId"), _uuid(away_raw, "GameAwayTeamId")
        if home_id == away_id or home_id not in names or away_id not in names:
            raise FibaCollectionError("invalid_data", "Game teams are not valid division opponents")
        day = _game_date(game)
        home = home_id == team_id
        status = "scheduled" if state == "Scheduled" else "unpublished"
        home_score = away_score = None
        if state in {"Official", "OfficialForfeit"}:
            home_score = _score(game.get("GameHomeTeamScore"))
            away_score = _score(game.get("GameAwayTeamScore"))
            winner = _uuid(game.get("GameWinnerTeamId"), "GameWinnerTeamId")
            expected = home_id if home_score > away_score else away_id
            if home_score == away_score or winner != expected:
                raise FibaCollectionError("invalid_data", "Official score and winner disagree")
            status = "finished"
        result = PublicResult(
            source="fiba3x3",
            external_id=game_id,
            discipline="3x3",
            team=watch.team,
            season=watch.season,
            year=day.year,
            tournament=watch.tournament,
            event_date=day,
            date_precision="day",
            location=watch.location,
            team_name=names[team_id],
            opponent_name=names[away_id if home else home_id],
            club_score=home_score if home else away_score,
            opponent_score=away_score if home else home_score,
            place=None,
            status=status,
            source_url=watch.source_url,
            source_label="FIBA 3×3 · официальный протокол",
            emblem_url=None,
            opponent_emblem_url=None,
            notes=f"Статус FIBA: {state}. Регистрация: {watch.registration_id}.",
        )
        results.append(result.model_dump(mode="json"))
    return results, {"team_id": team_id, "division_id": division_id, "event_id": event_id}


def collect_watched(
    watchlist_path: Path | str = DEFAULT_WATCHLIST,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Return JSON results, per-registration outcomes, and limited coverage.

    ``api_key=None`` reads APP_FIBA_API_KEY. A missing/empty key performs zero
    requests. Neither a denied key nor a malformed response exposes source text
    or secrets in reports. Only status=ok registrations are safe to merge.
    ``results`` never contains partial rows from a failed registration.
    """
    report: dict[str, Any] = {
        "provider": "fiba3x3",
        "status": "disabled",
        "scope": "explicit_registrations_only",
        "limitation": LIMITATION,
        "results": [],
        "coverage": [],
        "registrations": [],
        "errors": [],
    }
    try:
        config = Watchlist.model_validate_json(Path(watchlist_path).read_text(encoding="utf-8"))
    except (OSError, ValidationError) as error:
        report["status"] = "invalid_watchlist"
        report["errors"] = [{"status": "invalid_watchlist", "message": type(error).__name__}]
        return report
    key = (os.environ.get("APP_FIBA_API_KEY", "") if api_key is None else api_key).strip()
    active = [watch for watch in config.registrations if watch.enabled]
    owns_client = client is None and bool(key) and bool(active)
    if owns_client:
        client = httpx.Client()
    denied = not bool(key)
    try:
        for watch in config.registrations:
            outcome: dict[str, Any] = {
                "registration_id": str(watch.registration_id),
                "team": watch.team,
                "season": watch.season,
                "source_url": str(watch.source_url),
                "status": "disabled",
                "records": 0,
                "external_ids": [],
            }
            report["registrations"].append(outcome)
            if not watch.enabled:
                continue
            if denied:
                outcome.update(status="auth_required", message="APP_FIBA_API_KEY is required")
                continue
            try:
                assert client is not None
                rows, metadata = _collect_registration(watch, client, key)
                known = {row["external_id"] for row in report["results"]}
                if any(row["external_id"] in known for row in rows):
                    raise FibaCollectionError(
                        "invalid_data", "Game belongs to multiple watched teams"
                    )
                report["results"].extend(rows)
                outcome.update(
                    status="ok",
                    records=len(rows),
                    external_ids=[row["external_id"] for row in rows],
                    **metadata,
                )
            except (FibaCollectionError, ValidationError) as error:
                status = error.status if isinstance(error, FibaCollectionError) else "invalid_data"
                message = (
                    str(error) if isinstance(error, FibaCollectionError) else "Invalid game data"
                )
                outcome.update(status=status, message=message)
                report["errors"].append(
                    {
                        "registration_id": str(watch.registration_id),
                        "status": status,
                        "message": message,
                    }
                )
                if status == "auth_required":
                    denied = True
    finally:
        if owns_client and client is not None:
            client.close()
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for outcome in report["registrations"]:
        if outcome["status"] != "disabled":
            groups[(outcome["team"], outcome["season"])].append(outcome)
    for (team, season), outcomes in groups.items():
        statuses = {item["status"] for item in outcomes}
        status = "partial" if "ok" in statuses else outcomes[0]["status"]
        report["coverage"].append(
            {
                "provider": "fiba3x3",
                "team": team,
                "season": season,
                "records": sum(item["records"] for item in outcomes),
                "status": status,
                "source_url": outcomes[0]["source_url"],
                "note": LIMITATION + " Состояния регистраций: " + ", ".join(sorted(statuses)) + ".",
            }
        )
    statuses = {item["status"] for item in report["registrations"] if item["status"] != "disabled"}
    if statuses:
        report["status"] = (
            "ok"
            if statuses == {"ok"}
            else "partial"
            if "ok" in statuses
            else "auth_required"
            if statuses == {"auth_required"}
            else "unavailable"
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST)
    args = parser.parse_args()
    print(json.dumps(collect_watched(args.watchlist), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
