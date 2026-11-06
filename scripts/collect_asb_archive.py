"""Collect every season enumerated by ASB's public team widget API.

Only explicit team/game/competition fields are retained. Player endpoints are never called.
Run separately from the offline, transactional import_public_results.py importer.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/results-asb.json"
BASE = "https://asb.infobasket.su/Widget"
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


def collect() -> dict[str, object]:
    records, coverage, teams = [], [], []
    fetched_at = datetime.now(UTC).isoformat()
    with httpx.Client(timeout=35, follow_redirects=False, trust_env=False) as client:
        for slug, team_id in [("men", 7433), ("women", 7695)]:
            seasons_url = f"{BASE}/GetTeamSeasons/{team_id}"
            response = client.get(seasons_url)
            response.raise_for_status()
            seasons = response.json()
            # GetTeamSeasons excludes the new season until at least one game is played.
            current = client.get(f"{BASE}/GetTeamComps/{team_id}")
            current.raise_for_status()
            for comp in current.json():
                name = comp.get("CompNameEn", "")
                if comp.get("Level") == 0 and name.startswith("Season "):
                    end = int(name.rsplit("/", 1)[-1])
                    if not any(row["CompID"] == comp["CompID"] for row in seasons):
                        seasons.insert(0, {"CompID": comp["CompID"], "Year": end})
            teams.append(
                {
                    "team": slug,
                    "external_team_id": str(team_id),
                    "seasons_url": seasons_url,
                    "enumerated_seasons": len(seasons),
                }
            )
            for season in seasons:
                comp_id, year = season["CompID"], season["Year"]
                season_name = f"{year - 1}-{year}"
                games_url = f"{BASE}/TeamGames/{team_id}?compId={comp_id}&format=json"
                response = client.get(games_url)
                response.raise_for_status()
                games = response.json()
                if not isinstance(games, list):
                    raise ValueError(f"Unexpected games response: {games_url}")
                # The legacy widget silently falls back to last season for a season with
                # no published games. Never label those duplicate results as a new year.
                returned = len(games)
                games = [
                    game
                    for game in games
                    if datetime(year - 1, 7, 1)
                    <= datetime.strptime(game["GameDate"], "%d.%m.%Y")
                    < datetime(year, 7, 1)
                ]
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
                    records.append(record)
                coverage.append(
                    {
                        "provider": "asb",
                        "team": slug,
                        "season": season_name,
                        "records": len(games),
                        "source_url": games_url,
                        "status": "imported" if games else "no_published_games",
                        "note": (
                            "Все опубликованные игры официального TeamGames для сезона"
                            if len(games) == returned
                            else "Виджет вернул игры другого сезона; они исключены из этого года"
                        ),
                    }
                )
                print(slug, season_name, len(games), flush=True)
    return {
        "version": 1,
        "fetched_at": fetched_at,
        "teams": teams,
        "coverage": coverage,
        "games": records,
    }


if __name__ == "__main__":
    data = collect()
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(data['games'])} games to {OUT}")
