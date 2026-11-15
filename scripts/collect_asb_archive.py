"""Collect verified games from both public ASB statistics APIs.

Only explicit team/game/competition fields are retained. Player endpoints are never called.
Run separately from the offline, transactional import_public_results.py importer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/results-asb.json"
BASE = "https://asb.infobasket.su/Widget"
MODERN_BASE = "https://asb.sportoteka.org/api/abc"
CACHE = ROOT / "data/results-research/asb-games"
FINISHED = {"Result", "ResultConfirmed", "Forfeit"}
FIELDS = {
    "GameID",
    "GameDate",
    "HasTime",
    "GameTimeMsk",
    "GameStatus",
    "TeamAid",
    "TeamBid",
    "TeamNameAru",
    "TeamNameBru",
    "ScoreA",
    "ScoreB",
    "TeamLogoA",
    "TeamLogoB",
    "ArenaId",
    "ArenaRu",
    "CompNameRu",
    "LeagueNameRu",
    "LeagueShortNameRu",
}


def modern_game(
    item: dict[str, Any], team: str, team_id: int, season: str, comp_id: int, url: str
) -> dict[str, Any] | None:
    """Retain only team, competition and match fields from Sportoteka."""
    game, first, second = item["game"], item["team1"], item["team2"]
    if team_id not in [first["teamId"], second["teamId"]]:
        raise ValueError("ASB returned another team's game")
    comp, league, arena = item.get("comp") or {}, item.get("league") or {}, item.get("arena") or {}
    stamp = datetime.fromisoformat(game["defaultZoneDateTime"])
    if stamp.tzinfo is None:
        raise ValueError("ASB kickoff must include its official timezone")
    stamp = stamp.astimezone(timezone(timedelta(hours=3)))
    start, end = map(int, season.split("-"))
    if not datetime(start, 7, 1).date() <= stamp.date() < datetime(end, 7, 1).date():
        return None
    status = game["gameStatus"]
    return dict(
        GameID=game["id"],
        GameDate=stamp.strftime("%d.%m.%Y"),
        HasTime=bool(game.get("hasTime")),
        GameTimeMsk=stamp.strftime("%H:%M"),
        GameStatus=1 if status in FINISHED else 0,
        official_status=status,
        TeamAid=first["teamId"],
        TeamBid=second["teamId"],
        TeamNameAru=first["name"]
        + (f" ({first['regionName']})" if first.get("regionName") else ""),
        TeamNameBru=second["name"]
        + (f" ({second['regionName']})" if second.get("regionName") else ""),
        ScoreA=game.get("score1") if status in FINISHED else None,
        ScoreB=game.get("score2") if status in FINISHED else None,
        TeamLogoA=first.get("logo"),
        TeamLogoB=second.get("logo"),
        ArenaId=arena.get("id"),
        ArenaRu=arena.get("name"),
        CompNameRu=comp.get("name"),
        LeagueNameRu=league.get("name"),
        LeagueShortNameRu=league.get("abcName"),
        team=team,
        external_team_id=str(team_id),
        season=season,
        season_comp_id=str(comp_id),
        source_url=url,
    )


def collect(*, current_only: bool = False) -> dict[str, Any]:
    records, coverage, teams = [], [], []
    fetched_at = datetime.now(UTC).isoformat()
    CACHE.mkdir(parents=True, exist_ok=True)
    with (
        httpx.Client(timeout=35, follow_redirects=False, trust_env=False) as client,
        ThreadPoolExecutor(max_workers=6) as pool,
    ):

        def fetch(url: str) -> Any:
            for attempt in range(3):
                try:
                    response = client.get(url)
                    response.raise_for_status()
                    data = response.json()
                    cache = CACHE / (hashlib.sha256(url.encode()).hexdigest()[:20] + ".json")
                    cache.write_text(
                        json.dumps(
                            dict(url=url, fetched_at=fetched_at, data=data),
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf8",
                    )
                    return data
                except (httpx.HTTPError, ValueError):
                    if attempt == 2:
                        raise
                    time.sleep(1)

        for slug, team_id in [("men", 7433), ("women", 7695)]:
            seasons_url = f"{BASE}/GetTeamSeasons/{team_id}"
            seasons = fetch(seasons_url)
            # GetTeamSeasons excludes the new season until at least one game is played.
            current = fetch(f"{BASE}/GetTeamComps/{team_id}")
            for comp in current:
                name = comp.get("CompNameEn", "")
                if comp.get("Level") == 0 and name.startswith("Season "):
                    end = int(name.rsplit("/", 1)[-1])
                    if not any(row["CompID"] == comp["CompID"] for row in seasons):
                        seasons.insert(0, {"CompID": comp["CompID"], "Year": end})
            seasons = list({row["Year"]: row for row in reversed(seasons)}.values())
            seasons.sort(key=lambda row: row["Year"], reverse=True)
            teams.append(
                {
                    "team": slug,
                    "external_team_id": str(team_id),
                    "seasons_url": seasons_url,
                    "enumerated_seasons": len(seasons),
                }
            )
            if current_only:
                now = datetime.now(timezone(timedelta(hours=3)))
                end_year = now.year + (now.month >= 7)
                seasons = [row for row in seasons if row["Year"] == end_year]
                if not seasons:
                    raise ValueError("Current official season identity is missing")
            requests = {}
            for season in seasons:
                comp_id, year = season["CompID"], season["Year"]
                widget_url = f"{BASE}/TeamGames/{team_id}?compId={comp_id}&format=json"
                modern_url = (
                    f"{MODERN_BASE}/teams/games?teamId={team_id}&tag=asb"
                    f"&season={year}&lang=ru&maxResultCount=1000"
                )
                requests[comp_id] = (pool.submit(fetch, widget_url), pool.submit(fetch, modern_url))
            for season in seasons:
                comp_id, year = season["CompID"], season["Year"]
                season_name = f"{year - 1}-{year}"
                games_url = f"{BASE}/TeamGames/{team_id}?compId={comp_id}&format=json"
                games = requests[comp_id][0].result()
                if not isinstance(games, list):
                    raise ValueError(f"Unexpected games response: {games_url}")
                # The legacy widget silently falls back to last season for a season with
                # no published games. Never label those duplicate results as a new year.
                raw_count = len(games)
                games = [
                    game
                    for game in games
                    if datetime(year - 1, 7, 1)
                    <= datetime.strptime(game["GameDate"], "%d.%m.%Y")
                    < datetime(year, 7, 1)
                ]
                combined = {}
                for game in games:
                    if team_id not in [game["TeamAid"], game["TeamBid"]]:
                        raise ValueError("ASB returned another team's game")
                    record = {key: value for key, value in game.items() if key in FIELDS}
                    record.update(
                        team=slug,
                        external_team_id=str(team_id),
                        season=season_name,
                        season_comp_id=str(comp_id),
                        source_url=games_url,
                    )
                    combined[str(game["GameID"])] = record
                modern_url = (
                    f"{MODERN_BASE}/teams/games?teamId={team_id}&tag=asb"
                    f"&season={year}&lang=ru&maxResultCount=1000"
                )
                modern = requests[comp_id][1].result()
                if modern.get("status") != "Ok" or not isinstance(modern.get("items"), list):
                    raise ValueError(f"Unexpected modern ASB response: {modern_url}")
                if modern.get("totalCount", 0) != len(modern["items"]):
                    raise ValueError(f"Truncated modern ASB response: {modern_url}")
                for item in modern["items"]:
                    modern_record = modern_game(
                        item, slug, team_id, season_name, comp_id, modern_url
                    )
                    if modern_record:
                        identity = str(modern_record["GameID"])
                        # Keep reviewed historical widget names, logos and source URLs;
                        # Sportoteka supplies explicit statuses (including Scheduled).
                        if identity in combined:
                            for key in [
                                "official_status",
                                "GameStatus",
                                "ScoreA",
                                "ScoreB",
                                "HasTime",
                                "GameTimeMsk",
                                "GameDate",
                            ]:
                                combined[identity][key] = modern_record[key]
                            combined[identity]["status_source_url"] = modern_url
                        else:
                            combined[identity] = modern_record
                games = list(combined.values())
                records.extend(games)
                source_urls = [games_url, modern_url]
                if year == max(row["Year"] for row in seasons):
                    tag = "asb-cfo-36-m" if slug == "men" else "asb-cfo-36-w"
                    regional_url = (
                        f"{MODERN_BASE}/comps/calendar?tag={tag}"
                        f"&season={year}&lang=ru&maxResultCount=1000"
                    )
                    regional = fetch(regional_url)
                    if regional.get("status") != "Ok":
                        raise ValueError(f"Unexpected regional calendar: {regional_url}")
                    regional_ids = {
                        str(row["game"]["id"])
                        for row in regional.get("items", [])
                        if team_id in [row["team1"]["teamId"], row["team2"]["teamId"]]
                    }
                    if not regional_ids <= set(combined):
                        raise ValueError(
                            "Team calendar is missing games from the regional calendar"
                        )
                    source_urls.append(regional_url)
                coverage.append(
                    {
                        "provider": "asb",
                        "team": slug,
                        "season": season_name,
                        "records": len(games),
                        "source_url": games_url if games else modern_url,
                        "status": "imported" if games else "no_published_games",
                        "checked_at": fetched_at,
                        "source_urls": source_urls,
                        "raw_widget_records": raw_count,
                        "modern_records": len(modern["items"]),
                        "scheduled_records": sum(
                            g.get("official_status") == "Scheduled" for g in games
                        ),
                        "note": (
                            "Проверены официальный архив TeamGames "
                            "и новый календарь АСБ Sportoteka. "
                            + (
                                "Опубликованные игры объединены по GameID."
                                if games
                                else "Матчи команды на этот сезон пока не опубликованы; "
                                "игры другого сезона, возвращённые старым виджетом, исключены."
                            )
                        ),
                    }
                )
                print(slug, season_name, len(games), flush=True)
    if OUT.exists():
        previous = json.loads(OUT.read_text(encoding="utf8"))
        if current_only:
            refreshed = {(row["team"], row["season"]) for row in coverage}
            records = [
                row for row in previous["games"] if (row["team"], row["season"]) not in refreshed
            ] + records
            coverage = [
                row for row in previous["coverage"] if (row["team"], row["season"]) not in refreshed
            ] + coverage
            teams = previous["teams"]
        missing = {str(g["GameID"]) for g in previous["games"]} - {
            str(g["GameID"]) for g in records
        }
        if missing:
            raise ValueError(
                f"Previously verified games missing; review before replacing: {missing}"
            )
    return {
        "version": 2,
        "fetched_at": fetched_at,
        "teams": teams,
        "coverage": coverage,
        "games": records,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--current",
        action="store_true",
        help="Refresh the current season and preserve the verified archive",
    )
    data = collect(current_only=parser.parse_args().current)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(data['games'])} games to {OUT}")
