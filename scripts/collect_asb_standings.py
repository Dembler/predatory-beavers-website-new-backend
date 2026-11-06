"""Refresh the reviewed ASB stage archive from official public endpoints.

Every raw response is cached as evidence; no database is opened. A playoff is
published as its actual games, never as the registration list's numbered rows.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from predatory_beavers.modules.standings.logos import team_logo_path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://asb.infobasket.su"
CACHE = ROOT / "data/standings-research"
MANIFEST = ROOT / "docs/asb-standings.json"
TEAMS = {"men": 7433, "women": 7695}


def walk(nodes, path=(), ancestors=()):
    for node in nodes:
        here = (*path, node["CompShortNameRu"].strip())
        yield node, here, ancestors
        yield from walk(node.get("Children", []), here, (*ancestors, node))


def season_date(value: str, season: str) -> str | None:
    if len(value) > 10:
        date = (
            datetime.strptime(value, "%d.%m.%Y %H:%M")
            .replace(tzinfo=timezone(timedelta(hours=3)))
            .astimezone(UTC)
        )
    else:
        date = datetime.strptime(value, "%d.%m.%Y").replace(tzinfo=UTC)
    start, end = map(int, season.split("-"))
    if not datetime(start, 7, 1, tzinfo=UTC) <= date < datetime(end, 7, 1, tzinfo=UTC):
        return None
    return date.isoformat()


def team_name(team):
    return team.get("CompTeamNameRu") or team.get("CompTeamShortNameRu") or "Участник уточняется"


def playoff_games(pairs, season, source_url):
    result = []
    for pair in pairs:
        first, second = pair.get("TeamName1") or {}, pair.get("TeamName2") or {}
        names = {str(t.get("TeamID")): team_name(t) for t in [first, second]}
        for score in pair.get("Scores") or []:
            date = season_date(score["GameDate"], season) if score.get("GameDate") else None
            if score.get("GameDate") and date is None:
                continue
            home = str(score.get("Team1id"))
            away = str(score.get("Team2id"))
            scores = (score.get("Score") or "").split(":")
            home_score, away_score = (
                tuple(int(s.strip()) for s in scores)
                if len(scores) == 2 and all(s.strip().isdigit() for s in scores)
                else (None, None)
            )
            # Score is in the bracket pair's TeamName1/TeamName2 order.
            # Team1id/Team2id identify actual home/away teams; HomeTeam points
            # to the home team's bracket slot. Verified against TeamGames.
            if score.get("HomeTeam") == 2:
                home_score, away_score = away_score, home_score
            result.append(
                dict(
                    external_id=str(score["GameID"]),
                    round_name=pair.get("RoundNameRu") or "Плей-офф",
                    round=pair.get("Round", 0),
                    tree=pair.get("Tree", 1),
                    sort_order=pair.get("Sort", 0),
                    starts_at=date,
                    date_precision="time" if len(score.get("GameDate") or "") > 10 else "day",
                    home_team_name=names.get(home, "Участник уточняется"),
                    home_team_id=home,
                    home_team_logo_url=None,
                    away_team_name=names.get(away, "Участник уточняется"),
                    away_team_id=away,
                    away_team_logo_url=None,
                    home_score=home_score,
                    away_score=away_score,
                    status=(
                        "finished"
                        if home_score is not None
                        else (
                            "unpublished"
                            if date and datetime.fromisoformat(date) < datetime.now(UTC)
                            else "scheduled"
                        )
                    ),
                    source_url=source_url,
                )
            )
    return result


async def collect(*, refresh: bool = False):
    CACHE.mkdir(exist_ok=True)
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    previous = {(t["team"], t["competition_id"]): t for t in document["tables"]}
    results_archive = json.loads((ROOT / "docs/results-asb.json").read_text(encoding="utf8"))
    semaphore = asyncio.Semaphore(6)
    requests = []
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:

        async def fetch(path):
            url = BASE + path
            filename = CACHE / (hashlib.sha256(url.encode()).hexdigest()[:20] + ".json")
            async with semaphore:
                if filename.exists() and not refresh:
                    return json.loads(filename.read_text(encoding="utf8"))["data"]
                for attempt in range(3):
                    try:
                        response = await client.get(url)
                        response.raise_for_status()
                        data = response.json()
                        filename.write_text(
                            json.dumps(
                                dict(url=url, fetched_at=datetime.now(UTC).isoformat(), data=data),
                                ensure_ascii=False,
                                indent=2,
                            )
                            + "\n",
                            encoding="utf8",
                        )
                        requests.append(url)
                        return data
                    except (httpx.HTTPError, ValueError):
                        if attempt == 2:
                            raise
                        await asyncio.sleep(1)

        seasons = {}
        for row in results_archive["coverage"]:
            if row["season"] >= "2018-2019" and row["season"] <= "2025-2026":
                seasons[row["season"]] = parse_qs(urlsplit(row["source_url"]).query)["compId"][0]
        trees = dict(
            zip(
                seasons,
                await asyncio.gather(*(fetch(f"/Widget/CompIssue/{i}") for i in seasons.values())),
                strict=True,
            )
        )
        jobs = []
        for slug, team_id in TEAMS.items():
            for season, season_id in seasons.items():
                jobs.append((slug, team_id, season, season_id))
        comps = await asyncio.gather(
            *(fetch(f"/Widget/GetTeamComps/{team_id}?season={sid}") for _, team_id, _, sid in jobs)
        )
        candidates = []
        audits = []
        for (slug, team_id, season, season_id), participation in zip(jobs, comps, strict=True):
            by_id = {
                n["CompID"]: (n, path, parents) for n, path, parents in walk(trees[season]["Comps"])
            }
            tournaments = [r for r in participation if r.get("Level") == 1]
            for tournament in tournaments:
                root_id = tournament["CompID"]
                if root_id not in by_id:
                    continue
                root, root_path, parents = by_id[root_id]
                relevant = list(walk([root], root_path[:-1], parents))
                checked = []
                for node, path, ancestors in relevant:
                    ctype = node["CompType"]
                    if ctype == 0:
                        endpoint = "CompTeamResults"
                    elif ctype in [4, 8, 10] or (
                        ctype == 1 and not any(a["CompType"] in [4, 8, 10] for a in ancestors)
                    ):
                        endpoint = "PlayOff"
                    else:
                        continue
                    checked.append(str(node["CompID"]))
                    candidates.append((slug, team_id, season, tournament, node, path, endpoint))
                audits.append(
                    dict(
                        team=slug,
                        season=season,
                        tournament_id=str(root_id),
                        tournament_name=tournament["CompNameRu"],
                        source_url=f"{BASE}/Widget/GetTeamComps/{team_id}?season={season_id}",
                        checked_competition_ids=checked,
                    )
                )
        payloads = await asyncio.gather(
            *(
                fetch(f"/Widget/{endpoint}/{node['CompID']}?format=json")
                for _, _, _, _, node, _, endpoint in candidates
            )
        )
        tables = []
        for candidate, payload in zip(candidates, payloads, strict=True):
            slug, team_id, season, tournament, node, path, endpoint = candidate
            if not isinstance(payload, list):
                continue
            comp_id = str(node["CompID"])
            source_url = f"{BASE}/Widget/{endpoint}/{comp_id}?format=json"
            rows, games = [], []
            if endpoint == "CompTeamResults":
                if not any(r.get("TeamID") == team_id for r in payload):
                    continue
                if not payload or any(
                    not r.get("Standings") or r["Standings"].get("StandingGame") is None
                    for r in payload
                ):
                    continue
                for r in payload:
                    stats = r["Standings"]
                    old = next(
                        (
                            v
                            for v in previous.get((slug, comp_id), {}).get("rows", [])
                            if v["external_team_id"] == str(r["TeamID"])
                        ),
                        {},
                    )
                    rows.append(
                        dict(
                            position=r["Place"],
                            team_name=team_name(r["CompTeamName"]),
                            external_team_id=str(r["TeamID"]),
                            team_logo_url=old.get("team_logo_url"),
                            played=stats["StandingGame"],
                            wins=stats["StandingWin"],
                            losses=stats["StandingLose"],
                            draws=stats.get("StandingDraw") or 0,
                            table_points=stats["StandingPoints"],
                            points_for=stats["StandingGoalPlus"],
                            points_against=stats["StandingGoalMinus"],
                        )
                    )
            else:
                games = playoff_games(payload, season, source_url)
                if not any(str(team_id) in [g["home_team_id"], g["away_team_id"]] for g in games):
                    continue
            own_games = [g for g in games if str(team_id) in [g["home_team_id"], g["away_team_id"]]]
            last = max((g["starts_at"] for g in own_games if g["starts_at"]), default=None)
            last_precision = next(
                (g["date_precision"] for g in own_games if g["starts_at"] == last), None
            )
            if rows:
                # A season TeamGames result is not sufficient evidence for the
                # date of an individual group. Reuse the reviewed exact date.
                last = previous.get((slug, comp_id), {}).get("last_game_at")
                last_precision = previous.get((slug, comp_id), {}).get("last_game_precision")
            kind = (
                "playoff"
                if games
                else ("classification" if "мест" in path[-1].lower() else "group")
            )
            table = dict(
                team=slug,
                external_team_id=str(team_id),
                season=season,
                competition_id=comp_id,
                name=" / ".join(path[2:]) or path[-1],
                path=list(path),
                source_url=source_url,
                tournament_id=str(tournament["CompID"]),
                tournament_name=tournament["CompNameRu"].strip(),
                stage_name=" / ".join(
                    path[
                        len(
                            next(
                                p
                                for n, p, _ in walk(trees[season]["Comps"])
                                if n["CompID"] == tournament["CompID"]
                            )
                        ) :
                    ]
                )
                or path[-1],
                stage_kind=kind,
                stage_order=next(
                    i
                    for i, (n, _, _) in enumerate(walk(trees[season]["Comps"]))
                    if n["CompID"] == node["CompID"]
                ),
                last_game_at=last,
                last_game_precision=last_precision,
                rows=rows,
                games=games,
            )
            tables.append(table)
            print(slug, season, comp_id, kind, len(rows), len(games), flush=True)
        # Preserve prior verified stages if the public tree no longer lists one.
        found = {(t["team"], t["competition_id"]) for t in tables}
        missing = [t for key, t in previous.items() if key not in found]
        if missing:
            identities = [(t["team"], t["competition_id"]) for t in missing]
            raise ValueError(
                f"Previously verified tables missing; review before replacing: {identities}"
            )
        tables.sort(key=lambda t: (t["season"], t["team"], t["stage_order"]))
        for table in tables:
            for row in table["rows"]:
                row["team_logo_url"] = (
                    team_logo_path(row["external_team_id"], row["team_name"])
                    or row["team_logo_url"]
                )
            for game in table["games"]:
                for side in ["home", "away"]:
                    game[side + "_team_logo_url"] = team_logo_path(
                        game[side + "_team_id"], game[side + "_team_name"]
                    )
        for audit in audits:
            stages = [
                t
                for t in tables
                if (t["team"], t["season"], t["tournament_id"])
                == (audit["team"], audit["season"], audit["tournament_id"])
            ]
            audit.update(
                numerical_table_count=sum(bool(t["rows"]) for t in stages),
                playoff_stage_count=sum(bool(t["games"]) for t in stages),
                published_game_count=sum(len(t["games"]) for t in stages),
                status="published" if stages else "no_published_stages_for_verified_identity",
            )
        for coverage in document["coverage"]:
            stages = [
                t
                for t in tables
                if (t["team"], t["season"]) == (coverage["team"], coverage["season"])
            ]
            if not stages:
                continue
            coverage.update(
                records=len(stages),
                numerical_table_count=sum(bool(t["rows"]) for t in stages),
                playoff_stage_count=sum(bool(t["games"]) for t in stages),
                published_game_count=sum(len(t["games"]) for t in stages),
                tournament_count=len({t["tournament_id"] for t in stages}),
                status="published",
                checked_competition_ids=sorted(
                    {
                        i
                        for a in audits
                        if (a["team"], a["season"]) == (coverage["team"], coverage["season"])
                        for i in a["checked_competition_ids"]
                    }
                ),
                note=(
                    "Проверены все доступные этапы чемпионатов из официального указателя команды "
                    "за сезон. Числовые таблицы и фактические игры сеток сохранены отдельно. "
                    "Отсутствие опубликованной сетки не означает отсутствие участия или плей-офф."
                ),
            )
        document.update(
            version=1,
            fetched_at=datetime.now(UTC).isoformat(),
            tables=tables,
            tournament_coverage=audits,
        )
        MANIFEST.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
        )
        (CACHE / "collection-audit.json").write_text(
            json.dumps(
                dict(
                    fetched_at=document["fetched_at"],
                    stage_count=len(tables),
                    row_count=sum(len(t["rows"]) for t in tables),
                    game_count=sum(len(t["games"]) for t in tables),
                    tournaments=audits,
                    new_requests=requests,
                ),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf8",
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh", action="store_true", help="Refetch official evidence responses"
    )
    asyncio.run(collect(refresh=parser.parse_args().refresh))
