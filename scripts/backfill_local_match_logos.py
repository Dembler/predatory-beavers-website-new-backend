"""Backfill only missing match logo URLs in ignored data/local.db from official ASB JSON.

Preview is read-only apart from an ignored copy of the fetched public JSON.
--apply makes a consistent SQLite backup, migrates, and fills only logo URL columns.
Names must match exactly and have one unambiguous ASB team identity and logo.
No match dates, scores, statuses, notes, uploaded assets, or timestamps are modified.
"""

import argparse
import hashlib
import json
import os
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from alembic import command
from alembic.config import Config
from pydantic import TypeAdapter

from predatory_beavers.integrations.asb.schemas import AsbGame
from predatory_beavers.settings import get_settings

ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "data/local.db"


def unambiguous_team_logos(games: list[AsbGame]) -> dict[str, str]:
    identities: dict[str, set[int]] = defaultdict(set)
    candidates: dict[str, set[str]] = defaultdict(set)
    for game in games:
        for name, team_id, logo in (
            (game.home_team_name, game.home_team_id, game.home_team_logo_url),
            (game.away_team_name, game.away_team_id, game.away_team_logo_url),
        ):
            identities[name].add(team_id)
            if logo is not None:
                candidates[name].add(str(logo))
    return {
        name: next(iter(logos))
        for name, logos in candidates.items()
        if len(logos) == 1 and len(identities[name]) == 1
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not DATABASE.exists():
        raise SystemExit("Ignored data/local.db must exist; source database is never modified")
    source_database = ROOT / "data/predatory_beavers.db"
    source_hash = hashlib.sha256(source_database.read_bytes()).hexdigest()
    with sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True) as connection:
        targets = connection.execute(
            "SELECT DISTINCT c.id, c.external_id, j.external_team_id "
            "FROM competitions c JOIN matches m ON m.competition_id=c.id "
            "JOIN teams t ON m.team_id=t.id "
            "JOIN import_jobs j ON j.competition_external_id=c.external_id "
            "AND j.team_slug=t.slug "
            "WHERE c.source='asb' AND j.provider='asb' AND j.status='COMPLETED' "
            "AND c.is_deleted=0 AND m.is_deleted=0"
        ).fetchall()
        columns_before = [item[1] for item in connection.execute("PRAGMA table_info(matches)")]
        preserved_columns = [
            name for name in columns_before if name not in {"home_logo_url", "away_logo_url"}
        ]
        preserved_sql = ", ".join(f'"{name}"' for name in preserved_columns)
        before = connection.execute(f"SELECT {preserved_sql} FROM matches ORDER BY id").fetchall()

    sources = []
    artifact_dir = ROOT / "data/test-run-match-logos"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(UTC).isoformat()
    with httpx.Client(timeout=25, follow_redirects=False, trust_env=False) as client:
        for competition_id, external_id, team_id in targets:
            url = f"https://asb.infobasket.su/Widget/TeamGames/{team_id}"
            response = client.get(url, params={"compId": external_id, "format": "json"})
            response.raise_for_status()
            games = TypeAdapter(list[AsbGame]).validate_json(response.content)
            if any(int(team_id) not in {g.home_team_id, g.away_team_id} for g in games):
                raise RuntimeError("Official ASB response contains an unexpected team")
            artifact = artifact_dir / f"team-{team_id}-competition-{external_id}.json"
            artifact.write_bytes(response.content)
            sources.append((competition_id, games))
            metadata = {
                "source_url": str(response.url),
                "fetched_at": fetched_at,
                "sha256": hashlib.sha256(response.content).hexdigest(),
                "games": len(games),
            }
            artifact.with_suffix(".source.json").write_text(
                json.dumps(metadata, indent=2), encoding="utf-8"
            )
            print(f"Official source: {response.url}; games: {len(games)}")
    if not args.apply:
        print("Preview only; --apply backs up, migrates, and fills missing match logo URLs")
        return
    backup = DATABASE.with_name(
        f"local.before-match-logos-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:6]}.db"
    )
    with sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True) as connection:
        with sqlite3.connect(backup) as target:
            connection.backup(target)
    print(f"Consistent backup: data/{backup.name}")
    os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{DATABASE.as_posix()}"
    get_settings.cache_clear()
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    changed = 0
    with sqlite3.connect(DATABASE) as connection:
        connection.row_factory = sqlite3.Row
        for competition_id, games in sources:
            logos_by_name = unambiguous_team_logos(games)
            games_by_id = {str(game.game_id): game for game in games}
            matches = connection.execute(
                "SELECT * FROM matches WHERE competition_id=? AND is_deleted=0",
                (competition_id,),
            ).fetchall()
            for match in matches:
                if match["source"] == "asb":
                    game = games_by_id.get(match["external_id"])
                    if game is None:
                        continue
                    if (match["home_team_name"], match["away_team_name"]) != (
                        game.home_team_name,
                        game.away_team_name,
                    ):
                        continue
                    home = str(game.home_team_logo_url) if game.home_team_logo_url else None
                    away = str(game.away_team_logo_url) if game.away_team_logo_url else None
                elif match["source"] == "manual" and match["status"] == "SCHEDULED":
                    home = logos_by_name.get(match["home_team_name"])
                    away = logos_by_name.get(match["away_team_name"])
                else:
                    continue
                values = (
                    match["home_logo_url"] or (str(home) if home else None),
                    match["away_logo_url"] or (str(away) if away else None),
                )
                if values != (match["home_logo_url"], match["away_logo_url"]):
                    connection.execute(
                        "UPDATE matches SET home_logo_url=?, away_logo_url=? WHERE id=?",
                        (*values, match["id"]),
                    )
                    changed += 1
        after = [
            tuple(row)
            for row in connection.execute(f"SELECT {preserved_sql} FROM matches ORDER BY id")
        ]
        if before != after:
            raise RuntimeError("Match fields changed outside logo URLs; rolling back")
        if hashlib.sha256(source_database.read_bytes()).hexdigest() != source_hash:
            raise RuntimeError("Source database changed unexpectedly; rolling back")
        complete = connection.execute(
            "SELECT count(*) FROM matches WHERE is_deleted=0 "
            "AND home_logo_url IS NOT NULL AND away_logo_url IS NOT NULL"
        ).fetchone()[0]
    print(f"Logo URLs backfilled for {changed} matches; {complete} matches have both logos")
    print("All original match fields and source database checksum are unchanged")


if __name__ == "__main__":
    main()
