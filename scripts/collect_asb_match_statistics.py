"""Collect public official ASB 5x5 protocols; dry-run by default.

Uses reviewed docs/results-asb.json identities; no credentials or environment files.
--apply requires an explicit working database; regular predatory_beavers.db is refused.
Network calls happen before the offline atomic import. Public profiles/PII are not retained.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import httpx

from predatory_beavers.integrations.asb.statistics import game_url, normalize_game_statistics
from predatory_beavers.modules.matches.statistics_schemas import GameStatisticsRead

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".data/asb-match-statistics.json"


def _regresses(previous: GameStatisticsRead, current: GameStatisticsRead) -> bool:
    """Keep a complete revision when a later upstream response loses fields."""
    if (previous.home_score, previous.away_score) != (current.home_score, current.away_score):
        return False  # Corrected totals are a different revision, never merge them.
    if previous.period_scores and not current.period_scores:
        return True
    players = {(p.team_side, p.external_player_id): p for p in current.box_score}
    for player in previous.box_score:
        updated = players.get((player.team_side, player.external_player_id))
        if updated is None:
            return True
        for field in type(player.statistics).model_fields:
            if (
                getattr(player.statistics, field) is not None
                and getattr(updated.statistics, field) is None
            ):
                return True
        periods = {p.period: p.statistics for p in updated.period_statistics}
        for period in player.period_statistics:
            if period.period not in periods:
                return True
            for field in type(period.statistics).model_fields:
                if (
                    getattr(period.statistics, field) is not None
                    and getattr(periods[period.period], field) is None
                ):
                    return True
    return False


def collect(
    games: list[dict[str, Any]],
    *,
    season: str | None = None,
    game_ids: set[str] | None = None,
) -> dict[str, Any]:
    selected = [
        game
        for game in games
        if game.get("team") in {"men", "women"}
        and game.get("GameStatus") == 1
        and (season is None or game["season"] == season)
        and (not game_ids or str(game["GameID"]) in game_ids)
    ]
    # Shared source identity must not silently cross club sides or seasons.
    identities: dict[str, tuple[str, str]] = {}
    for game in selected:
        key, identity = str(game["GameID"]), (game["team"], game["season"])
        if key in identities and identities[key] != identity:
            raise ValueError("conflicting reviewed game identities")
        identities[key] = identity
    fetched_at = datetime.now(UTC)

    def fetch(item: tuple[str, tuple[str, str]]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        external_id, (team, game_season) = item
        url = game_url(external_id, game_season)
        report: dict[str, Any] = {
            "external_id": external_id,
            "team": team,
            "season": game_season,
            "source_url": url,
        }
        try:
            with httpx.Client(timeout=35, follow_redirects=False, trust_env=False) as client:
                for attempt in range(3):
                    try:
                        with client.stream("GET", url) as response:
                            response.raise_for_status()
                            if response.headers.get("content-type", "").split(";", 1)[0] != (
                                "application/json"
                            ):
                                raise ValueError("non-JSON protocol")
                            chunks, size = [], 0
                            for chunk in response.iter_bytes():
                                size += len(chunk)
                                if size > 2_000_000:
                                    raise ValueError("protocol exceeds limit")
                                chunks.append(chunk)
                            raw = json.loads(b"".join(chunks))
                        break
                    except httpx.HTTPError:
                        if attempt == 2:
                            raise
                        time.sleep(0.5)
            data = normalize_game_statistics(
                raw,
                external_id=external_id,
                team=cast(Literal["men", "women"], team),
                season=game_season,
                fetched_at=fetched_at,
            )
            report["status"] = "collected" if data else "unavailable"
            if data:
                report.update(
                    periods=len(data.period_scores),
                    players=len(data.box_score),
                    player_periods_status=data.player_periods_status,
                )
            return data.model_dump(mode="json") if data else None, report
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            report.update(status="rejected", error_type=type(exc).__name__)
            return None, report

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fetch, identities.items()))
    return {
        "version": 1,
        "fetched_at": fetched_at.isoformat(),
        "statistics": [data for data, _ in results if data],
        "coverage": [report for _, report in results],
    }


def apply_statistics(
    database: Path,
    payload: dict[str, Any],
    *,
    apply: bool = False,
) -> dict[str, int]:
    """A complete normalized revision replaces a revision, never merges null fields."""
    database = database.resolve()
    if database.name == "predatory_beavers.db":
        raise ValueError("regular database is not a match-statistics import target")
    rows = [GameStatisticsRead.model_validate(row) for row in payload["statistics"]]
    if len({(r.source, r.external_id) for r in rows}) != len(rows):
        raise ValueError("duplicate statistics identities")
    # Dry-run opens read-only and requires no schema migration.
    uri = database.as_uri() + ("?mode=rw" if apply else "?mode=ro")
    counts = dict(created=0, updated=0, unchanged=0, stale=0, missing_parent=0, regressed=0)
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        tables = {
            r[0] for r in connection.execute("select name from sqlite_master where type='table'")
        }
        if apply and "match_statistics" not in tables:
            raise ValueError("apply migration 0024 before importing match statistics")
        for row in rows:
            parent = connection.execute(
                "SELECT a.* FROM archive_results a WHERE source=? AND external_id=?",
                (row.source, row.external_id),
            ).fetchone()
            match = connection.execute(
                "SELECT m.*,t.category,t.active AS team_active,t.is_deleted AS team_deleted,"
                "c.season,c.active AS competition_active,c.is_deleted AS competition_deleted "
                "FROM matches m JOIN teams t ON t.id=m.team_id "
                "JOIN competitions c ON c.id=m.competition_id "
                "WHERE m.source=? AND m.external_id=?",
                (row.source, row.external_id),
            ).fetchone()
            if parent is None and match is None:
                counts["missing_parent"] += 1
                continue
            club, opponent = (
                (row.home_score, row.away_score)
                if row.club_side == "home"
                else (row.away_score, row.home_score)
            )
            archive_agrees = parent is None or (
                parent["team"],
                parent["season"],
                parent["discipline"],
                parent["status"],
                parent["club_score"],
                parent["opponent_score"],
            ) == (row.team, row.season, row.discipline, "finished", club, opponent)
            # A manual Match takes precedence over an archive copy everywhere.
            match_agrees = match is None or (
                str(match["category"]).casefold() == row.team
                and match["season"] == row.season
                and str(match["status"]).casefold() == "finished"
                and str(match["club_side"]).casefold() == row.club_side
                and (match["home_score"], match["away_score"]) == (row.home_score, row.away_score)
                and all(match[key] for key in ("active", "team_active", "competition_active"))
                and not any(
                    match[key] for key in ("is_deleted", "team_deleted", "competition_deleted")
                )
            )
            if not (match_agrees if match else archive_agrees):
                counts["stale"] += 1
                continue
            encoded = row.model_dump(mode="json")
            old = (
                connection.execute(
                    "SELECT id,payload FROM match_statistics WHERE source=? AND external_id=?",
                    (row.source, row.external_id),
                ).fetchone()
                if "match_statistics" in tables
                else None
            )
            if old:
                previous = json.loads(old["payload"])
                if _regresses(GameStatisticsRead.model_validate(previous), row):
                    counts["regressed"] += 1
                    continue
                previous.pop("fetched_at", None)
                comparison = dict(encoded)
                comparison.pop("fetched_at", None)
                if previous == comparison:
                    counts["unchanged"] += 1
                    continue
            action = "updated" if old else "created"
            counts[action] += 1
            if not apply:
                continue
            stamp = datetime.now(UTC).isoformat()
            if old:
                connection.execute(
                    "UPDATE match_statistics SET payload=?,fetched_at=?,updated_at=? WHERE id=?",
                    (
                        json.dumps(encoded, ensure_ascii=False),
                        row.fetched_at.isoformat(),
                        stamp,
                        old["id"],
                    ),
                )
            else:
                connection.execute(
                    "INSERT INTO match_statistics (id,created_at,updated_at,source,external_id,"
                    "team,season,discipline,payload,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        uuid4().hex,
                        stamp,
                        stamp,
                        row.source,
                        row.external_id,
                        row.team,
                        row.season,
                        row.discipline,
                        json.dumps(encoded, ensure_ascii=False),
                        row.fetched_at.isoformat(),
                    ),
                )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", help="Default: latest reviewed season")
    parser.add_argument("--all", action="store_true", help="Collect all reviewed 5x5 seasons")
    parser.add_argument("--games", help="Comma-separated reviewed ASB game IDs")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--input", type=Path, help="Offline previously collected normalized bundle")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--apply", action="store_true", help="Write only to the explicit database")
    args = parser.parse_args()
    if args.apply and not args.database:
        parser.error("--apply requires --database")
    if args.input:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
    else:
        games = json.loads((ROOT / "docs/results-asb.json").read_text(encoding="utf-8"))["games"]
        season = args.season or (None if args.all else max(g["season"] for g in games))
        payload = collect(
            games, season=season, game_ids=set(args.games.split(",")) if args.games else None
        )
        args.output.parent.mkdir(exist_ok=True, parents=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    report = {"collected": len(payload["statistics"]), "coverage": payload.get("coverage", [])}
    if args.database:
        if args.apply:
            from sports_refresh_runtime import daily_backup

            daily_backup(args.database.resolve())
        report["import"] = apply_statistics(args.database, payload, apply=args.apply)
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
