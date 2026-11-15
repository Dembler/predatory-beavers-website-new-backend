"""Offline, idempotent import of verified public team results into the local database.

Preview (default) never writes to the database. --apply first takes a consistent SQLite
backup, migrates, then inserts/updates archive-owned records in one transaction. Existing
match fields are refreshed from a stored provider projection, preserving editor overrides.
Upcoming fixtures live only in archive_results; this avoids a stale scheduled match copy
masking a subsequently published result in the unified calendar.
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

from predatory_beavers.modules.imports.models import MatchSourceSnapshot as _Snapshot  # noqa: F401
from predatory_beavers.settings import get_settings

ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "data/local.db"
# A score, its publication status, kickoff and editorial note describe one result.
# Refreshing only part of it after an editor changes it could contradict that edit.
RESULT_FIELDS = frozenset({"starts_at", "status", "home_score", "away_score", "notes"})


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


def asb_status(game: dict[str, object], *, today: date | None = None) -> str:
    """An unpublished 0:0 is never a result; future dated fixtures are schedules."""
    official = game.get("official_status")
    if official in {"Cancelled", "Canceled", "Deleted"}:
        return "cancelled"
    if game["GameStatus"] == 1:
        return "finished"
    event_date = datetime.strptime(str(game["GameDate"]), "%d.%m.%Y").date()
    today = today or datetime.now(timezone(timedelta(hours=3))).date()
    return "scheduled" if event_date >= today and official in {None, "Scheduled"} else "unpublished"


def asb_result(game: dict[str, object]) -> PublicResult:
    home = str(game["TeamAid"]) == game["external_team_id"]
    event_date = datetime.strptime(str(game["GameDate"]), "%d.%m.%Y").date()
    status = asb_status(game)
    finished = status == "finished"
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
        status=status,
        source_url=game["source_url"],
        source_label="АСБ · официальные матчи",
        emblem_url=game.get("TeamLogoA" if home else "TeamLogoB") or None,
        opponent_emblem_url=game.get("TeamLogoB" if home else "TeamLogoA") or None,
        notes=(
            str(game.get("CompNameRu") or "")
            + (" · Итог не опубликован в источнике" if status == "unpublished" else "")
        )
        or None,
    )


def load_sources() -> tuple[list[PublicResult], list[dict[str, object]], list[dict[str, object]]]:
    asb = json.loads((ROOT / "docs/results-asb.json").read_text(encoding="utf-8"))
    manifests = [
        json.loads((ROOT / "docs" / filename).read_text(encoding="utf-8"))
        for filename in (
            "results-curated.json",
            "results-3x3-official.json",
            "results-phygital-official.json",
            "results-fiba3x3.json",
        )
    ]
    rows = [row for manifest in manifests for row in manifest["results"]]
    reviewed = {(row["source"], row["external_id"]): row for row in rows}
    corrections = json.loads(
        (ROOT / "docs/results-public-corrections.json").read_text(encoding="utf-8")
    )
    for patch in corrections["patches"]:
        key = (patch["source"], patch["external_id"])
        if key not in reviewed:
            raise ValueError(f"Reviewed correction has no source result: {key}")
        reviewed[key].update(patch["updates"])
    results = [asb_result(game) for game in asb["games"]]
    results += [PublicResult.model_validate(row) for row in rows]
    identities = [(row.source, row.external_id) for row in results]
    if len(identities) != len(set(identities)):
        raise ValueError("Duplicate result identity in source manifest")
    legacy = json.loads(
        (ROOT / "docs/results-asb-legacy-coverage.json").read_text(encoding="utf-8")
    )
    # A newer review replaces older coverage of the same provider/team/season.
    # Importing both versions would update the same database row twice per run.
    coverage = {
        (row["provider"], row["team"], row["season"]): row
        for row in (
            asb["coverage"]
            + legacy
            + [row for manifest in manifests for row in manifest["coverage"]]
        )
    }
    return results, list(coverage.values()), asb["games"]


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


def match_projection(
    game: dict[str, object], team_id: str, competition_id: str, venue_id: str | None
) -> dict[str, object]:
    """The provider-owned fields only; publication/media/editor flags stay local."""
    status = asb_status(game)
    if status not in {"finished", "scheduled", "cancelled"}:
        raise ValueError("Only an explicit official match status can update a match")
    if not game.get("HasTime") or not game.get("GameTimeMsk"):
        raise ValueError("A match projection requires an official kickoff time")
    starts = datetime.strptime(f"{game['GameDate']} {game['GameTimeMsk']}", "%d.%m.%Y %H:%M")
    starts = starts.replace(tzinfo=timezone(timedelta(hours=3))).astimezone(UTC)
    finished = status == "finished"
    return {
        "team_id": team_id,
        "competition_id": competition_id,
        "venue_id": venue_id,
        "starts_at": starts.replace(tzinfo=None).isoformat(sep=" "),
        "club_side": "HOME" if str(game["TeamAid"]) == game["external_team_id"] else "AWAY",
        "home_team_name": game["TeamNameAru"],
        "away_team_name": game["TeamNameBru"],
        "home_score": game["ScoreA"] if finished else None,
        "away_score": game["ScoreB"] if finished else None,
        "status": status.upper(),
        "source": "asb",
        "external_id": str(game["GameID"]),
        "home_logo_url": game.get("TeamLogoA") or None,
        "away_logo_url": game.get("TeamLogoB") or None,
        "notes": game.get("CompNameRu"),
    }


def previous_projection(
    connection: sqlite3.Connection, game: dict[str, object] | None
) -> dict[str, object] | None:
    """Resolve an actual older source game; never use the edited row as a baseline."""
    if game is None:
        return None
    team = connection.execute("SELECT id FROM teams WHERE slug=?", (game["team"],)).fetchone()
    competition = connection.execute(
        "SELECT id FROM competitions WHERE source='asb' AND external_id=?",
        (str(game["season_comp_id"]),),
    ).fetchone()
    venue_id = None
    if game.get("ArenaId"):
        venue = connection.execute(
            "SELECT id FROM venues WHERE source='asb' AND external_id=?",
            (str(game["ArenaId"]),),
        ).fetchone()
        if venue is None:
            return None
        venue_id = venue["id"]
    if team is None or competition is None:
        return None
    try:
        return match_projection(game, team["id"], competition["id"], venue_id)
    except ValueError:
        return None


def same_match_value(field: str, current: object, previous: object) -> bool:
    # ORM/editor writes include microseconds, unlike the historical SQLite CLI.
    if field == "starts_at" and current is not None and previous is not None:
        return datetime.fromisoformat(str(current)) == datetime.fromisoformat(str(previous))
    return current == previous


def store_source_projection(
    connection: sqlite3.Connection, match_id: str, projection: dict[str, object], checked_at: str
) -> None:
    connection.execute(
        'INSERT INTO match_source_snapshots (match_id,source,external_id,"values",checked_at) '
        'VALUES (?,?,?,?,?) ON CONFLICT(match_id) DO UPDATE SET "values"=excluded."values",'
        "checked_at=excluded.checked_at",
        (match_id, "asb", projection["external_id"], json.dumps(projection), checked_at),
    )


def refresh_match(
    connection: sqlite3.Connection,
    existing: sqlite3.Row,
    values: dict[str, object],
    previous_game: dict[str, object] | None,
    now: str,
) -> str:
    # A deletion/draft is an editor publication decision, not a missing feed row.
    if existing["is_deleted"] or not existing["active"]:
        return "preserved"
    snapshot = connection.execute(
        "SELECT * FROM match_source_snapshots WHERE match_id=? AND source='asb' AND external_id=?",
        (existing["id"], existing["external_id"]),
    ).fetchone()
    before = (
        json.loads(snapshot["values"])
        if snapshot
        else previous_projection(connection, previous_game)
    )
    if before is None:
        # No trustworthy old projection exists on first adoption. Keep the row
        # intact rather than quietly treating an editor's values as ASB values.
        return "unverified_preserved"
    core_unchanged = all(
        field in before and same_match_value(field, existing[field], before[field])
        for field in RESULT_FIELDS
    )
    updates = {}
    for field, value in values.items():
        if field in RESULT_FIELDS and not core_unchanged:
            continue
        if field not in before or not same_match_value(field, existing[field], before[field]):
            continue
        if field in {"home_logo_url", "away_logo_url"}:
            if value is None or existing[field.replace("_url", "_asset_id")] is not None:
                continue
        if not same_match_value(field, existing[field], value):
            updates[field] = value
    store_source_projection(connection, existing["id"], values, now)
    if not updates:
        return "preserved"
    connection.execute(
        "UPDATE matches SET "
        + ",".join(field + "=?" for field in updates)
        + ",updated_at=?,version_id=version_id+1 WHERE id=?",
        (*updates.values(), now, existing["id"]),
    )
    return "updated"


def apply_import(
    database: Path,
    results: list[PublicResult],
    coverage: list[dict[str, object]],
    games: list[dict[str, object]],
    *,
    previous_games: list[dict[str, object]] | None = None,
) -> dict[str, int]:
    """Apply source data; previous_games is the manifest from BEFORE fetching.

    Existing pre-snapshot matches are deliberately left unchanged without that
    verified older source. An archive result alone cannot verify its kickoff
    time. New imported matches always record their own first source snapshot.
    """
    if database.resolve() == (ROOT / "data/predatory_beavers.db").resolve():
        raise ValueError("The preserved source database must never be modified")
    previous_by_id = {str(game["GameID"]): game for game in previous_games or []}
    counts = {
        "archive_created": 0,
        "archive_updated": 0,
        "archive_unchanged": 0,
        "matches_created": 0,
        "matches_updated": 0,
        "matches_preserved": 0,
        "matches_unverified_preserved": 0,
        "matches_unpublished_skipped": 0,
        "matches_unknown_time_skipped": 0,
        "matches_scheduled_archive_only": 0,
    }
    now = datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")
    with sqlite3.connect(database, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
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
                "status=excluded.status,source_url=excluded.source_url,note=excluded.note, "
                "updated_at=excluded.updated_at "
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
            status = asb_status(game)
            existing = connection.execute(
                "SELECT * FROM matches WHERE source='asb' AND external_id=?",
                (str(game["GameID"]),),
            ).fetchone()
            # New fixtures remain archive-only. A previously published result
            # can still receive an official cancellation or schedule correction.
            if status == "scheduled" and existing is None:
                counts["matches_scheduled_archive_only"] += 1
                continue
            if status not in {"finished", "scheduled", "cancelled"} or (
                status != "finished" and existing is None
            ):
                counts["matches_unpublished_skipped"] += 1
                continue
            # The match table requires a timestamp. Date-only fixtures remain in
            # archive_results, where their actual precision is retained, rather
            # than acquiring a fabricated 00:00 kickoff.
            if not game.get("HasTime") or not game.get("GameTimeMsk"):
                counts["matches_unknown_time_skipped"] += 1
                continue
            if existing and (existing["is_deleted"] or not existing["active"]):
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
            projection = match_projection(game, team_ids[str(game["team"])], comp_id[0], venue_id)
            if existing:
                outcome = refresh_match(
                    connection,
                    existing,
                    projection,
                    previous_by_id.get(str(game["GameID"])),
                    now,
                )
                if outcome == "updated":
                    counts["matches_updated"] += 1
                else:
                    counts["matches_preserved"] += 1
                    if outcome == "unverified_preserved":
                        counts["matches_unverified_preserved"] += 1
                continue
            values = {
                "id": stable_id("match", "asb/" + str(game["GameID"])),
                **projection,
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
            store_source_projection(connection, str(values["id"]), projection, now)
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
