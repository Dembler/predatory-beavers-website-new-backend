"""Offline, idempotent import of verified public team results into the local database.

Preview (default) never writes to the database. --apply first takes a consistent SQLite
backup, migrates, then inserts/updates archive-owned records in one transaction. Existing
matches, competitions and venues are left unchanged, including any local editor changes.
The source database data/predatory_beavers.db is never modified.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from alembic import command
from alembic.config import Config
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from predatory_beavers.settings import get_settings

ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "data/local.db"


class PublicResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=32)
    external_id: str = Field(min_length=1, max_length=200)
    discipline: Literal["5x5", "3x3", "phygital"]
    team: Literal["men", "women"]
    season: str = Field(min_length=1, max_length=32)
    year: int = Field(ge=2000, le=2200)
    tournament: str = Field(min_length=1, max_length=500)
    event_date: date | None
    date_precision: Literal["day", "year"]
    location: str | None = Field(max_length=500)
    team_name: str = Field(min_length=1, max_length=200)
    opponent_name: str | None = Field(max_length=200)
    club_score: int | None = Field(ge=0)
    opponent_score: int | None = Field(ge=0)
    place: int | None = Field(ge=1)
    status: Literal["finished", "participated", "scheduled", "unpublished", "cancelled"]
    source_url: HttpUrl | None
    source_label: str = Field(min_length=1, max_length=200)
    emblem_url: str | None = Field(max_length=2048)
    opponent_emblem_url: str | None = Field(max_length=2048)
    notes: str | None

    @field_validator("emblem_url", "opponent_emblem_url")
    @classmethod
    def safe_emblem(cls, value: str | None) -> str | None:
        if value and not (value.startswith("https://") or value.startswith("/images/")):
            raise ValueError("Emblem must be a public HTTPS URL or a site image")
        return value

    @model_validator(mode="after")
    def consistent_result(self) -> PublicResult:
        if (self.club_score is None) != (self.opponent_score is None):
            raise ValueError("Scores must be present as a pair")
        if self.date_precision == "day" and self.event_date is None:
            raise ValueError("Exact dates must be supplied by the source")
        if self.event_date and self.event_date.year != self.year:
            raise ValueError("Event year must agree with its date")
        return self


def stable_id(kind: str, identity: str) -> str:
    return uuid5(NAMESPACE_URL, f"predatory-beavers/{kind}/{identity}").hex


def asb_result(game: dict[str, object]) -> PublicResult:
    home = str(game["TeamAid"]) == game["external_team_id"]
    event_date = datetime.strptime(str(game["GameDate"]), "%d.%m.%Y").date()
    finished = game["GameStatus"] == 1
    return PublicResult(
        source="asb",
        external_id=str(game["GameID"]),
        discipline="5x5",
        team=game["team"],
        season=str(game["season"]),
        year=event_date.year,
        tournament="АСБ · " + str(game.get("LeagueNameRu") or "Чемпионат АСБ"),
        event_date=event_date,
        date_precision="day",
        location=game.get("ArenaRu") or None,
        team_name=str(game["TeamNameAru" if home else "TeamNameBru"]),
        opponent_name=str(game["TeamNameBru" if home else "TeamNameAru"]),
        club_score=game["ScoreA" if home else "ScoreB"] if finished else None,
        opponent_score=game["ScoreB" if home else "ScoreA"] if finished else None,
        place=None,
        status="finished" if finished else "unpublished",
        source_url=game["source_url"],
        source_label="АСБ · официальный архив",
        emblem_url=game.get("TeamLogoA" if home else "TeamLogoB") or None,
        opponent_emblem_url=game.get("TeamLogoB" if home else "TeamLogoA") or None,
        notes=(
            str(game.get("CompNameRu") or "")
            + (" · Итог не опубликован в источнике" if not finished else "")
        )
        or None,
    )


def load_sources() -> tuple[list[PublicResult], list[dict[str, object]], list[dict[str, object]]]:
    asb = json.loads((ROOT / "docs/results-asb.json").read_text(encoding="utf-8"))
    curated = json.loads((ROOT / "docs/results-curated.json").read_text(encoding="utf-8"))
    results = [asb_result(game) for game in asb["games"]]
    results += [PublicResult.model_validate(row) for row in curated["results"]]
    identities = [(row.source, row.external_id) for row in results]
    if len(identities) != len(set(identities)):
        raise ValueError("Duplicate result identity in source manifest")
    legacy = json.loads(
        (ROOT / "docs/results-asb-legacy-coverage.json").read_text(encoding="utf-8")
    )
    return results, asb["coverage"] + legacy + curated["coverage"], asb["games"]


def backup_database(database: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = database.with_name(f"{database.stem}.before-results-archive-{stamp}.db")
    with sqlite3.connect(database) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    return backup


def migrate(database: Path) -> None:
    os.environ["APP_DATABASE_URL"] = "sqlite+aiosqlite:///" + database.as_posix()
    get_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")


def apply_import(
    database: Path,
    results: list[PublicResult],
    coverage: list[dict[str, object]],
    games: list[dict[str, object]],
) -> dict[str, int]:
    counts = {
        "archive_created": 0,
        "archive_updated": 0,
        "archive_unchanged": 0,
        "matches_created": 0,
        "matches_preserved": 0,
        "matches_unpublished_skipped": 0,
    }
    now = datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        for result in results:
            values = result.model_dump(mode="json")
            existing = connection.execute(
                "SELECT * FROM archive_results WHERE source=? AND external_id=?",
                (result.source, result.external_id),
            ).fetchone()
            if existing is None:
                values.update(
                    id=stable_id("result", f"{result.source}/{result.external_id}"),
                    created_at=now,
                    updated_at=now,
                )
                columns = ",".join(values)
                connection.execute(
                    f"INSERT INTO archive_results ({columns}) "
                    f"VALUES ({','.join('?' for _ in values)})",
                    tuple(values.values()),
                )
                counts["archive_created"] += 1
            elif any(existing[key] != value for key, value in values.items()):
                values["updated_at"] = now
                connection.execute(
                    "UPDATE archive_results SET "
                    f"{','.join(key + '=?' for key in values)} WHERE id=?",
                    (*values.values(), existing["id"]),
                )
                counts["archive_updated"] += 1
            else:
                counts["archive_unchanged"] += 1
        for item in coverage:
            values = {
                key: item[key]
                for key in ["provider", "team", "season", "records", "status", "source_url", "note"]
            }
            values.update(
                id=stable_id("coverage", f"{item['provider']}/{item['team']}/{item['season']}"),
                created_at=now,
                updated_at=now,
            )
            connection.execute(
                f"INSERT INTO archive_coverage ({','.join(values)}) "
                f"VALUES ({','.join('?' for _ in values)}) "
                "ON CONFLICT(provider,team,season) DO UPDATE SET records=excluded.records, "
                "status=excluded.status,source_url=excluded.source_url,note=excluded.note "
                "WHERE records!=excluded.records OR status!=excluded.status OR "
                "source_url!=excluded.source_url OR note!=excluded.note",
                tuple(values.values()),
            )
        team_ids = dict(connection.execute("SELECT slug,id FROM teams"))
        for slug in sorted({str(game["team"]) for game in games} - set(team_ids)):
            team_ids[slug] = stable_id("team", slug)
            connection.execute(
                "INSERT INTO teams (id,slug,name,category,active,is_deleted,created_at,updated_at) "
                "VALUES (?,?,?,?,1,0,?,?)",
                (team_ids[slug], slug, "ВГУ — Хищные бобры", slug.upper(), now, now),
            )
        for game in games:
            # GameStatus=0 does not establish a postponement or a 0:0 result.
            # Retain the source record in the archive, outside the match calendar.
            if game["GameStatus"] != 1:
                counts["matches_unpublished_skipped"] += 1
                continue
            existing = connection.execute(
                "SELECT id FROM matches WHERE source='asb' AND external_id=?",
                (str(game["GameID"]),),
            ).fetchone()
            if existing:
                counts["matches_preserved"] += 1
                continue
            comp_id = connection.execute(
                "SELECT id FROM competitions WHERE source='asb' AND external_id=?",
                (game["season_comp_id"],),
            ).fetchone()
            if not comp_id:
                new_id = stable_id("competition", "asb/" + str(game["season_comp_id"]))
                connection.execute(
                    "INSERT INTO competitions (id,name,season,source,external_id,active,is_deleted,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,1,0,?,?)",
                    (
                        new_id,
                        f"АСБ · сезон {game['season']}",
                        game["season"],
                        "asb",
                        game["season_comp_id"],
                        now,
                        now,
                    ),
                )
                comp_id = (new_id,)
            venue_id = None
            if game.get("ArenaId"):
                venue = connection.execute(
                    "SELECT id FROM venues WHERE source='asb' AND external_id=?",
                    (str(game["ArenaId"]),),
                ).fetchone()
                if venue:
                    venue_id = venue[0]
                else:
                    venue_id = stable_id("venue", "asb/" + str(game["ArenaId"]))
                    connection.execute(
                        "INSERT INTO venues (id,name,address,description,source,external_id,active,"
                        "is_deleted,created_at,updated_at) VALUES (?,?,?,?,?,?,0,0,?,?)",
                        (
                            venue_id,
                            game.get("ArenaRu") or f"Площадка АСБ {game['ArenaId']}",
                            "Адрес не указан в протоколе АСБ",
                            "Историческая площадка; адрес не подтверждён.",
                            "asb",
                            str(game["ArenaId"]),
                            now,
                            now,
                        ),
                    )
            game_time = str(game.get("GameTimeMsk") or "00:00")
            starts = datetime.strptime(f"{game['GameDate']} {game_time}", "%d.%m.%Y %H:%M")
            # All enumerated games are after 2014, when Moscow adopted permanent UTC+3.
            # This fixed official timestamp conversion also works on Windows without tzdata.
            starts = starts.replace(tzinfo=timezone(timedelta(hours=3))).astimezone(UTC)
            values = {
                "id": stable_id("match", "asb/" + str(game["GameID"])),
                "team_id": team_ids[str(game["team"])],
                "competition_id": comp_id[0],
                "venue_id": venue_id,
                "starts_at": starts.replace(tzinfo=None).isoformat(sep=" "),
                "club_side": "HOME" if str(game["TeamAid"]) == game["external_team_id"] else "AWAY",
                "home_team_name": game["TeamNameAru"],
                "away_team_name": game["TeamNameBru"],
                "home_score": game["ScoreA"],
                "away_score": game["ScoreB"],
                "status": "FINISHED",
                "source": "asb",
                "external_id": str(game["GameID"]),
                "home_logo_url": game.get("TeamLogoA") or None,
                "away_logo_url": game.get("TeamLogoB") or None,
                "notes": game.get("CompNameRu"),
                "featured": 0,
                "is_deleted": 0,
                "created_at": now,
                "updated_at": now,
            }
            connection.execute(
                f"INSERT INTO matches ({','.join(values)}) "
                f"VALUES ({','.join('?' for _ in values)})",
                tuple(values.values()),
            )
            counts["matches_created"] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--database", type=Path, default=DATABASE)
    args = parser.parse_args()
    database = args.database.resolve()
    if database == (ROOT / "data/predatory_beavers.db").resolve():
        raise SystemExit("The preserved source database must never be modified")
    if not database.exists():
        raise SystemExit("Target local database must already exist")
    results, coverage, games = load_sources()
    print(
        json.dumps(
            {
                "results": len(results),
                "asb_games": len(games),
                "coverage": len(coverage),
                "apply": args.apply,
                "database": str(database),
            },
            ensure_ascii=True,
        )
    )
    if not args.apply:
        return
    backup = backup_database(database)
    print("Backup:", backup)
    migrate(database)
    counts = apply_import(database, results, coverage, games)
    report = {
        "applied_at": datetime.now(UTC).isoformat(),
        "database": str(database),
        "backup": str(backup),
        **counts,
    }
    (ROOT / "data/results-archive-import-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(counts))


if __name__ == "__main__":
    main()
