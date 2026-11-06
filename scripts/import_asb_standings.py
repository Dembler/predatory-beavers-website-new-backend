"""Import reviewed official ASB tables without deriving standings from matches.

Preview is read-only. --apply backs up and migrates the working database first.
The preserved data/predatory_beavers.db is always rejected as a write target.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from import_public_results import migrate, stable_id
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationInfo,
    field_validator,
    model_validator,
)

from predatory_beavers.integrations.asb.schemas import AsbStanding
from predatory_beavers.modules.standings.schemas import StandingRow, StandingsGame

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/asb-standings.json"
STAT_FIELDS = (
    "position",
    "team_name",
    "external_team_id",
    "played",
    "wins",
    "losses",
    "draws",
    "table_points",
    "points_for",
    "points_against",
)


class OfficialTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team: Literal["men", "women"]
    external_team_id: str
    season: str = Field(pattern=r"^20\d{2}-20\d{2}$")
    competition_id: str = Field(pattern=r"^[1-9]\d*$")
    name: str = Field(min_length=1, max_length=200)
    path: list[str] = Field(min_length=1)
    source_url: HttpUrl
    tournament_id: str | None = Field(default=None, pattern=r"^[1-9]\d*$")
    tournament_name: str | None = Field(default=None, max_length=200)
    stage_name: str | None = Field(default=None, max_length=200)
    stage_kind: Literal["group", "playoff", "classification", "other"] = "group"
    stage_order: int = Field(default=0, ge=0)
    last_game_at: datetime | None = None
    last_game_precision: Literal["day", "time"] | None = None
    rows: list[StandingRow] = Field(default_factory=list, max_length=100)
    games: list[StandingsGame] = Field(default_factory=list, max_length=1000)

    @field_validator("rows", mode="before")
    @classmethod
    def official_rows(cls, value: list[dict], info: ValidationInfo) -> list[dict]:
        normalized = []
        for source in value:
            if "CompID" not in source:
                normalized.append(source)
                continue
            # Null Standings fields are registration/playoff lists, not tables.
            # AsbStanding rejects them rather than manufacturing zero statistics.
            row = AsbStanding.model_validate(source)
            if str(row.competition_id) != info.data.get("competition_id"):
                raise ValueError("Official row belongs to another competition")
            normalized.append(
                dict(
                    position=row.position,
                    team_name=row.team_name.name,
                    external_team_id=str(row.team_id),
                    team_logo_url=source.get("team_logo_url"),
                    played=row.stats.played,
                    wins=row.stats.wins,
                    losses=row.stats.losses,
                    draws=row.stats.draws or 0,
                    table_points=row.stats.table_points,
                    points_for=row.stats.points_for,
                    points_against=row.stats.points_against,
                )
            )
        return normalized

    @model_validator(mode="after")
    def validate_identity(self) -> OfficialTable:
        expected = "7433" if self.team == "men" else "7695"
        if self.external_team_id != expected:
            raise ValueError("Official table has the wrong VGU team identity")
        game_teams = {
            team_id for game in self.games for team_id in [game.home_team_id, game.away_team_id]
        }
        if expected not in {row.external_team_id for row in self.rows} | game_teams:
            raise ValueError("VGU is absent from the official table")
        if not self.rows and (self.stage_kind != "playoff" or not self.games):
            raise ValueError("A stage requires a numeric official table or actual playoff games")
        if self.games and self.stage_kind != "playoff":
            raise ValueError("Official bracket games require a playoff stage")
        if len({game.external_id for game in self.games}) != len(self.games):
            raise ValueError("Playoff game identities must be unique")
        positions = [row.position for row in self.rows]
        identities = [row.external_team_id for row in self.rows]
        if len(set(positions)) != len(positions) or len(set(identities)) != len(identities):
            raise ValueError("Official positions and team identities must be unique")
        if any(identity is None for identity in identities):
            raise ValueError("Official tables require external team identities")
        start, end = map(int, self.season.split("-"))
        if end != start + 1:
            raise ValueError("Invalid consecutive academic season")
        url = urlsplit(str(self.source_url))
        if (
            url.scheme != "https"
            or url.hostname != "asb.infobasket.su"
            or url.path
            != f"/Widget/{'PlayOff' if self.games else 'CompTeamResults'}/{self.competition_id}"
        ):
            raise ValueError("Source URL must identify this official ASB competition")
        if self.last_game_at is not None:
            if self.last_game_at.utcoffset() is None:
                raise ValueError("Official game date must have a timezone")
            if (
                not datetime(start, 7, 1, tzinfo=UTC)
                <= self.last_game_at
                < datetime(end, 7, 1, tzinfo=UTC)
            ):
                raise ValueError("Last game belongs to another season")
        for game in self.games:
            if str(game.source_url) != str(self.source_url):
                raise ValueError("Playoff games must identify the exact official bracket source")
            if game.starts_at is not None and (
                game.starts_at.utcoffset() is None
                or not datetime(start, 7, 1, tzinfo=UTC)
                <= game.starts_at
                < datetime(end, 7, 1, tzinfo=UTC)
            ):
                raise ValueError("Playoff game belongs to another season or lacks timezone")
        return self


def load_manifest(path: Path = MANIFEST) -> tuple[list[OfficialTable], datetime]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("version") != 1:
        raise ValueError("Unsupported official standings manifest")
    fetched_at = datetime.fromisoformat(document["fetched_at"])
    if fetched_at.utcoffset() is None:
        raise ValueError("Manifest fetch time must have a timezone")
    tables = [OfficialTable.model_validate(row) for row in document["tables"]]
    identities = [(row.team, row.competition_id) for row in tables]
    if len(identities) != len(set(identities)):
        raise ValueError("Duplicate official team/stage table")
    return tables, fetched_at


def normalized_rows(table: OfficialTable) -> list[dict]:
    return [
        row.model_dump(mode="json") for row in sorted(table.rows, key=lambda item: item.position)
    ]


def same_statistics(before: list[dict], after: list[dict]) -> bool:
    def projection(rows: list[dict]) -> list[dict]:
        return sorted(
            [{field: row.get(field) for field in STAT_FIELDS} for row in rows],
            key=lambda row: row["position"],
        )

    return projection(before) == projection(after)


def audit(connection: sqlite3.Connection, entity_id: str, before: dict | None, after: dict) -> None:
    now = datetime.now(UTC).isoformat()
    connection.execute(
        "INSERT INTO admin_audit_log "
        '(id,actor_id,actor_username,actor_role,action,entity_type,entity_id,"before",'
        '"after",request_id,created_at) '
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            stable_id("standings-audit", entity_id + now),
            None,
            "asb-standings-sync",
            None,
            "IMPORT",
            "standings_snapshot",
            entity_id,
            json.dumps(before, ensure_ascii=False) if before else None,
            json.dumps(after, ensure_ascii=False),
            None,
            now,
        ),
    )


def apply_tables(
    database: Path, tables: list[OfficialTable], fetched_at: datetime
) -> dict[str, int]:
    if database.resolve() == (ROOT / "data/predatory_beavers.db").resolve():
        raise ValueError("The preserved source database must never be modified")
    counts = dict(
        competitions_created=0,
        snapshots_created=0,
        snapshots_updated=0,
        snapshots_relinked=0,
        snapshots_unchanged=0,
        editor_snapshots_preserved=0,
    )
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for table in tables:
            team = connection.execute(
                "SELECT id FROM teams WHERE slug=? AND active=1 AND is_deleted=0", (table.team,)
            ).fetchone()
            if team is None:
                raise ValueError(f"Missing active club team: {table.team}")
            competition = connection.execute(
                "SELECT * FROM competitions WHERE source='asb' AND external_id=?",
                (table.competition_id,),
            ).fetchone()
            if competition is None:
                competition_id = stable_id("asb-standings-competition", table.competition_id)
                connection.execute(
                    "INSERT INTO competitions (id,name,season,source,external_id,active,"
                    "created_at,updated_at,is_deleted,deleted_at) "
                    "VALUES (?,?,?,?,?,1,?,?,0,NULL)",
                    (
                        competition_id,
                        table.name,
                        table.season,
                        "asb",
                        table.competition_id,
                        now,
                        now,
                    ),
                )
                counts["competitions_created"] += 1
            else:
                if competition["season"] != table.season:
                    raise ValueError("ASB competition identity belongs to another season")
                if not competition["active"] or competition["is_deleted"]:
                    raise ValueError("Existing competition was unpublished by an editor")
                competition_id = competition["id"]

            current = connection.execute(
                "SELECT * FROM standings_snapshots WHERE team_id=? AND competition_id=? "
                "AND is_current=1 AND is_deleted=0",
                (team["id"], competition_id),
            ).fetchone()
            imported_id = stable_id(
                "asb-standings-snapshot", f"{table.team}/{table.competition_id}"
            )
            removed = connection.execute(
                "SELECT id FROM standings_snapshots WHERE id=? AND is_deleted=1", (imported_id,)
            ).fetchone()
            if removed:
                counts["editor_snapshots_preserved"] += 1
                continue
            if current and current["id"] != imported_id and current["source"] != "asb":
                counts["editor_snapshots_preserved"] += 1
                continue
            rows = normalized_rows(table)
            # The initial importer attached one regional table to the season-wide
            # match competition. Relink that exact public table without modifying
            # its calendar competition or any match, team, venue or player row.
            legacy = None
            if current is None:
                legacy = connection.execute(
                    "SELECT * FROM standings_snapshots WHERE team_id=? AND source='asb' "
                    "AND source_reference=? AND is_current=1 AND is_deleted=0",
                    (team["id"], table.competition_id),
                ).fetchone()
                if legacy is not None and not same_statistics(json.loads(legacy["rows"]), rows):
                    raise ValueError(
                        "Legacy standings differ: preserve and review before relinking"
                    )
                current = legacy
            if current is not None:
                old_rows = json.loads(current["rows"])
                old_by_id = {str(row.get("external_team_id")): row for row in old_rows}
                for row in rows:
                    if not row.get("team_logo_url"):
                        row["team_logo_url"] = old_by_id.get(str(row["external_team_id"]), {}).get(
                            "team_logo_url"
                        )
            values = dict(
                team_id=team["id"],
                competition_id=competition_id,
                rows=json.dumps(rows, ensure_ascii=False),
                source="asb",
                source_reference=table.competition_id,
                source_url=str(table.source_url),
                last_game_at=table.last_game_at.astimezone(UTC).isoformat()
                if table.last_game_at
                else None,
                stage_order=table.stage_order,
                tournament_id=table.tournament_id,
                tournament_name=table.tournament_name,
                stage_name=table.stage_name,
                stage_kind=table.stage_kind,
                games=json.dumps(
                    [game.model_dump(mode="json") for game in table.games], ensure_ascii=False
                ),
                is_current=1,
                is_deleted=0,
                deleted_at=None,
            )
            if current is not None:
                if current["source"] != "asb":
                    counts["editor_snapshots_preserved"] += 1
                    continue
                changed = any(
                    (
                        json.loads(current[key]) != json.loads(value)
                        if key in {"rows", "games"}
                        else current[key] != value
                    )
                    for key, value in values.items()
                )
                if not changed:
                    counts["snapshots_unchanged"] += 1
                    continue
                snapshot_id = current["id"]
                connection.execute(
                    "UPDATE standings_snapshots SET "
                    + ",".join(key + "=?" for key in values)
                    + ",fetched_at=?,updated_at=? WHERE id=?",
                    (*values.values(), fetched_at.astimezone(UTC).isoformat(), now, snapshot_id),
                )
                counts["snapshots_relinked" if legacy else "snapshots_updated"] += 1
                audit(connection, snapshot_id, dict(current), values)
            else:
                snapshot_id = imported_id
                values.update(
                    id=snapshot_id,
                    fetched_at=fetched_at.astimezone(UTC).isoformat(),
                    created_at=now,
                    updated_at=now,
                )
                connection.execute(
                    f"INSERT INTO standings_snapshots ({','.join(values)}) "
                    f"VALUES ({','.join('?' for _ in values)})",
                    tuple(values.values()),
                )
                counts["snapshots_created"] += 1
                audit(connection, snapshot_id, None, values)
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Foreign key check failed")
    return counts


def backup(database: Path) -> Path:
    path = database.with_name(
        f"{database.stem}.before-asb-standings-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}.db"
    )
    with sqlite3.connect(database) as source, sqlite3.connect(path) as destination:
        source.backup(destination)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--database", type=Path, default=ROOT / "data/local.db")
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    args = parser.parse_args()
    tables, fetched_at = load_manifest(args.manifest)
    print(
        json.dumps(
            {
                "tables": len(tables),
                "rows": sum(len(item.rows) for item in tables),
                "playoff_stages": sum(item.stage_kind == "playoff" for item in tables),
                "games": sum(len(item.games) for item in tables),
                "apply": args.apply,
            }
        )
    )
    if not args.apply:
        return
    database = args.database.resolve()
    if database == (ROOT / "data/predatory_beavers.db").resolve() or not database.is_file():
        raise SystemExit("Only an existing working database can be modified")
    print("Backup:", backup(database))
    migrate(database)
    print(json.dumps(apply_tables(database, tables, fetched_at)))


if __name__ == "__main__":
    main()
