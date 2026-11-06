"""Fetch allowlisted public ASB roster fields; offline preview by default.

--fetch refreshes the reviewed manifest from the public source. --apply backs up the
working database, migrates and synchronizes ASB-owned snapshots in one transaction.
Editor player rows and historical roster snapshots are never deleted or overwritten.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
from import_public_results import backup_database, migrate, stable_id

from predatory_beavers.modules.roster.router import current_season
from predatory_beavers.modules.roster.schemas import RosterMemberInput, RosterSnapshotInput

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/asb-roster.json"
BASE = "https://asb.sportoteka.org/api/abc/teams"
LEGACY_BASE = "https://asb.infobasket.su/Widget"
TOTAL_FIELDS = ("points_total", "assists_total", "rebounds_total", "seconds_total")


def select_statistics(
    members: list[RosterMemberInput], rows: list[dict], season: str, source_url: str
) -> list[RosterMemberInput]:
    """Choose one published team-season bundle per current external player ID.

    A sparse newer bundle wins as a whole: missing metrics are never backfilled
    from an older season. Explicit zero totals are published values, not missing.
    """
    by_id = {str(row["external_player_id"]): row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError("Duplicate team-season statistics; cannot safely aggregate")
    selected = []
    for member in members:
        row = by_id.get(member.external_player_id)
        if (
            row
            and row.get("games_played", 0)
            and row["games_played"] > 0
            and any(row.get(field) is not None for field in TOTAL_FIELDS)
            and (member.statistics_season is None or member.statistics_season < season)
        ):
            member = RosterMemberInput.model_validate(
                {
                    **member.model_dump(),
                    "games_played": row["games_played"],
                    **{field: row.get(field) for field in TOTAL_FIELDS},
                    "statistics_season": season,
                    "statistics_source_url": source_url,
                }
            )
        selected.append(member)
    return selected


def normalize_members(
    roster: dict, stats: dict, *, season: str, source_url: str
) -> list[RosterMemberInput]:
    """Allowlisted current members and team-season totals, never career averages."""
    stat_rows = stats.get("players") or []
    by_id = {str(row["personId"]): row for row in stat_rows}
    if len(by_id) != len(stat_rows):
        raise ValueError("Duplicate team-season statistics; cannot safely aggregate")
    team_id = (roster.get("team") or {}).get("id")
    if any((row.get("team") or {}).get("id") != team_id for row in stat_rows):
        raise ValueError("Statistics contain a different team")
    members = []
    for player in roster.get("players") or []:
        if (
            player.get("personType") != "Player"
            or player.get("accessStatus") != "Access"
            or player.get("leaveTeam") is not False
        ):
            continue
        person_id = str(player["personId"])
        name = " ".join(
            str(player.get(field) or "").strip()
            for field in ["lastName", "firstName", "secondName"]
        ).strip()
        members.append(
            RosterMemberInput(
                external_player_id=person_id,
                full_name=name,
                photo_url=player.get("photo") or None,
                games_played=None,
                points_total=None,
                assists_total=None,
                rebounds_total=None,
                seconds_total=None,
            )
        )
    rows = [
        {
            "external_player_id": str(row["personId"]),
            "games_played": row.get("games"),
            **{
                target: (row.get("stats") or {}).get(source)
                for target, source in zip(
                    TOTAL_FIELDS, ["points", "assist", "rebound", "second"], strict=True
                )
            },
        }
        for row in stat_rows
    ]
    return select_statistics(
        sorted(members, key=lambda member: member.full_name), rows, season, source_url
    )


def historical_statistics(
    client: httpx.Client, members: list[RosterMemberInput], team_id: int, roster_season: str
) -> list[RosterMemberInput]:
    """Verified official season roots include every ASB competition for this team.

    The modern 2025/26 men's index has both regional league and Belov League.
    Using the legacy full-season root prevents discarding the second competition
    or double-counting shared totals. Historical membership is never imported.
    """
    if all(member.statistics_season == roster_season for member in members):
        return members
    response = client.get(f"{LEGACY_BASE}/GetTeamSeasons/{team_id}")
    response.raise_for_status()
    seasons = response.json()
    if not isinstance(seasons, list):
        raise ValueError("Invalid official ASB season index")
    years = [row["Year"] for row in seasons]
    if len(years) != len(set(years)):
        raise ValueError("Duplicate official ASB season roots")
    for row in sorted(seasons, key=lambda row: row["Year"], reverse=True):
        end_year = int(row["Year"])
        season = f"{end_year - 1}-{end_year}"
        if row.get("SeasonName") != f"Сезон {end_year - 1}/{end_year}":
            raise ValueError("ASB season label does not match its year")
        if season >= roster_season:
            continue
        if all(member.statistics_season is not None for member in members):
            break
        source_url = f"{LEGACY_BASE}/TeamStats/{team_id}?compId={int(row['CompID'])}&format=json"
        response = client.get(source_url)
        response.raise_for_status()
        totals = response.json()
        if totals.get("TeamID") != team_id:
            raise ValueError("Historical statistics contain a different team")
        # Only numeric totals and external IDs survive this projection. No old
        # names, photos, birthdays, or other response fields enter the manifest.
        stat_rows = [
            {
                "external_player_id": str(player["PersonID"]),
                "games_played": player.get("InGameCount"),
                **{
                    target: player.get(source)
                    for target, source in zip(
                        TOTAL_FIELDS, ["Points", "Assist", "Rebound", "Seconds"], strict=True
                    )
                },
            }
            for player in totals.get("Players") or []
        ]
        members = select_statistics(members, stat_rows, season, source_url)
    return members


def collect() -> list[RosterSnapshotInput]:
    now = datetime.now(UTC)
    snapshots = []
    season = current_season()
    end_year = int(season.split("-")[1])
    with httpx.Client(timeout=35, trust_env=False, follow_redirects=False) as client:
        for slug, team_id in [("men", 7433), ("women", 7695)]:
            comps_url = f"{BASE}/comps?teamId={team_id}&season={end_year}&lang=ru"
            response = client.get(comps_url)
            response.raise_for_status()
            comps = response.json().get("items") or []
            # Currently both VGU teams have exactly one regional competition. Never
            # average averages or merge overlapping league totals without game IDs.
            if len(comps) > 1:
                raise ValueError("Multiple competitions require verified non-overlapping totals")
            comp = comps[0] if comps else {}
            tag = comp.get("tag", "")
            query = f"id={team_id}&season={end_year}&tag={tag}&lang=ru"
            roster_url, stats_url = f"{BASE}/roster?{query}", f"{BASE}/stats?{query}"
            info_response = client.get(f"{BASE}/info?{query}")
            roster_response = client.get(roster_url)
            stats_response = client.get(stats_url)
            for response in [info_response, roster_response, stats_response]:
                response.raise_for_status()
            info = info_response.json().get("result") or {}
            roster = roster_response.json().get("result") or {}
            stats = stats_response.json().get("result") or {}
            confirmed = bool(
                comp
                and info.get("id") == team_id
                and info.get("selectedSeason") == end_year
                and info.get("selectedTag") == tag
                and any(
                    row.get("id") == comp.get("compId")
                    and row.get("season") == end_year
                    and row.get("isActual") is True
                    for row in info.get("comps") or []
                )
            )
            if roster.get("players") and not confirmed:
                raise ValueError("Roster season/competition is not confirmed by the team index")
            if confirmed and (roster.get("team") or {}).get("id") != team_id:
                raise ValueError("Roster contains a different team")
            members = (
                normalize_members(roster, stats, season=season, source_url=stats_url)
                if confirmed
                else []
            )
            if members:
                members = historical_statistics(client, members, team_id, season)
            snapshots.append(
                RosterSnapshotInput(
                    team=slug,
                    season=season,
                    external_team_id=str(team_id),
                    competition_id=str(comp.get("compId", 0)),
                    is_published=bool(members),
                    source_url=roster_url,
                    stats_source_url=stats_url,
                    checked_at=now,
                    players=members,
                )
            )
    return snapshots


def load_manifest(path: Path = MANIFEST) -> list[RosterSnapshotInput]:
    data = json.loads(path.read_text(encoding="utf-8"))
    snapshots = [RosterSnapshotInput.model_validate(row) for row in data["snapshots"]]
    identities = {(row.team, row.season) for row in snapshots}
    if len(identities) != len(snapshots):
        raise ValueError("Duplicate team-season snapshot")
    return snapshots


def apply_sync(database: Path, snapshots: list[RosterSnapshotInput]) -> dict[str, int]:
    counts = {
        "snapshots_created": 0,
        "members_created": 0,
        "members_updated": 0,
        "members_unchanged": 0,
        "members_withdrawn": 0,
    }
    now = datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        for snapshot in snapshots:
            snapshot_id = stable_id("roster-snapshot", f"{snapshot.team}/{snapshot.season}")
            values = snapshot.model_dump(mode="json", exclude={"players"})
            values["checked_at"] = snapshot.checked_at.replace(tzinfo=None).isoformat(sep=" ")
            existing = connection.execute(
                "SELECT id FROM asb_roster_snapshots WHERE team=? AND season=?",
                (snapshot.team, snapshot.season),
            ).fetchone()
            if existing:
                snapshot_id = existing["id"]
                connection.execute(
                    "UPDATE asb_roster_snapshots SET "
                    + ",".join(key + "=?" for key in values)
                    + " WHERE id=?",
                    (*values.values(), snapshot_id),
                )
            else:
                values.update(id=snapshot_id, created_at=now, updated_at=now)
                connection.execute(
                    f"INSERT INTO asb_roster_snapshots ({','.join(values)}) "
                    f"VALUES ({','.join('?' for _ in values)})",
                    tuple(values.values()),
                )
                counts["snapshots_created"] += 1
            members_now = set()
            for member in snapshot.players:
                members_now.add(member.external_player_id)
                values = member.model_dump(mode="json")
                values.update(snapshot_id=snapshot_id, is_listed=1)
                existing = connection.execute(
                    "SELECT * FROM asb_roster_members WHERE snapshot_id=? AND external_player_id=?",
                    (snapshot_id, member.external_player_id),
                ).fetchone()
                if existing is None:
                    values.update(
                        id=stable_id(
                            "roster-member", snapshot_id + "/" + member.external_player_id
                        ),
                        created_at=now,
                        updated_at=now,
                    )
                    connection.execute(
                        f"INSERT INTO asb_roster_members ({','.join(values)}) "
                        f"VALUES ({','.join('?' for _ in values)})",
                        tuple(values.values()),
                    )
                    counts["members_created"] += 1
                elif any(existing[key] != value for key, value in values.items()):
                    connection.execute(
                        "UPDATE asb_roster_members SET "
                        + ",".join(key + "=?" for key in values)
                        + ",updated_at=? WHERE id=?",
                        (*values.values(), now, existing["id"]),
                    )
                    counts["members_updated"] += 1
                else:
                    counts["members_unchanged"] += 1
            for row in connection.execute(
                "SELECT id,external_player_id FROM asb_roster_members "
                "WHERE snapshot_id=? AND is_listed=1",
                (snapshot_id,),
            ).fetchall():
                if row["external_player_id"] not in members_now:
                    connection.execute(
                        "UPDATE asb_roster_members SET is_listed=0,updated_at=? WHERE id=?",
                        (now, row["id"]),
                    )
                    counts["members_withdrawn"] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--database", type=Path, default=ROOT / "data/local.db")
    args = parser.parse_args()
    if args.fetch:
        snapshots = collect()
        MANIFEST.write_text(
            json.dumps(
                {"snapshots": [row.model_dump(mode="json") for row in snapshots]},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    snapshots = load_manifest()
    print(
        json.dumps(
            {
                "snapshots": len(snapshots),
                "members": sum(len(row.players) for row in snapshots),
                "apply": args.apply,
            }
        )
    )
    if not args.apply:
        return
    database = args.database.resolve()
    if database == (ROOT / "data/predatory_beavers.db").resolve():
        raise SystemExit("The preserved source database must never be modified")
    if not database.is_file():
        raise SystemExit("Working database must already exist")
    print("Backup:", backup_database(database))
    migrate(database)
    print(json.dumps(apply_sync(database, snapshots)))


if __name__ == "__main__":
    main()
