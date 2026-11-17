"""Independent, repeatable sports jobs with last-good data and bounded backups."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger(__name__)
TEAMS = {"men": 7433, "women": 7695}
MODERN_BASE = "https://asb.sportoteka.org/api/abc"


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def working_tree(database: Path) -> Path:
    runtime = database.parent / "sports-sync" / "working"
    documents = runtime / "docs"
    documents.mkdir(parents=True, exist_ok=True)
    for source in (ROOT / "docs").glob("*.json"):
        target = documents / source.name
        if not target.exists():
            shutil.copyfile(source, target)
    return runtime


def daily_backup(database: Path) -> Path:
    directory = database.parent / "backups" / "sports-sync"
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory / f"{database.stem}-{datetime.now(UTC):%Y-%m-%d}.db"
    if not backup.exists():
        temporary = backup.with_suffix(".db.tmp")
        with (
            closing(sqlite3.connect(database)) as source,
            closing(sqlite3.connect(temporary)) as target,
        ):
            source.backup(target)
        temporary.replace(backup)
    for old in sorted(directory.glob(f"{database.stem}-????-??-??.db"))[:-7]:
        old.unlink()
    return backup


def migrate_if_needed(database: Path) -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from import_public_results import migrate

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()
    with closing(sqlite3.connect(database)) as connection:
        has_version = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        current = (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()
            if has_version
            else None
        )
    if not current or current[0] != head:
        migrate(database)


def fetch_json(client: httpx.Client, url: str) -> dict[str, Any]:
    response = client.get(url)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Unexpected source response")
    return payload


def season_now() -> str:
    now = datetime.now(timezone(timedelta(hours=3)))
    end = now.year + (now.month >= 7)
    return f"{end - 1}-{end}"


def fetch_games(team: str, team_id: int, previous: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from collect_asb_archive import modern_game

    season = season_now()
    end = int(season.split("-")[1])
    url = (
        f"{MODERN_BASE}/teams/games?teamId={team_id}&tag=asb&season={end}"
        "&lang=ru&maxResultCount=1000"
    )
    with httpx.Client(timeout=25, trust_env=False, follow_redirects=False) as client:
        data = fetch_json(client, url)
        if data.get("status") != "Ok" or not isinstance(data.get("items"), list):
            raise ValueError("Invalid official team calendar")
        if data.get("totalCount") != len(data["items"]):
            raise ValueError("Incomplete official team calendar")
        old = [row for row in previous if row["team"] == team and row["season"] == season]
        root_id = int(old[0]["season_comp_id"]) if old else None
        if root_id is None and data["items"]:
            comps = fetch_json(
                client, f"{MODERN_BASE}/teams/comps?teamId={team_id}&season={end}&lang=ru"
            )
            if comps.get("status") != "Ok" or not comps.get("items"):
                raise ValueError("Cannot confirm current competition identity")
            root_id = int(comps["items"][0]["compId"])
        records = [
            modern_game(item, team, team_id, season, root_id or 0, url) for item in data["items"]
        ]
    if any(record is None for record in records):
        raise ValueError("Calendar contains a different season")
    ids = [str(record["GameID"]) for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate official game identity")
    missing = {str(row["GameID"]) for row in old} - set(ids)
    if missing:
        raise ValueError("Previously published games disappeared; keeping last-good calendar")
    return records


def sync_games(database: Path, runtime: Path, *, team: str, preview: bool) -> dict[str, Any]:
    import import_public_results as importer

    manifest = runtime / "docs/results-asb.json"
    document = load_json(manifest)
    previous = document["games"]
    records = fetch_games(team, TEAMS[team], previous)
    results = [importer.asb_result(game) for game in records]
    counts = (
        {"received": len(records), "preview": True}
        if preview
        else importer.apply_import(database, results, [], records, previous_games=previous)
    )
    if preview:
        return counts
    document["games"] = [
        row for row in previous if (row["team"], row["season"]) != (team, season_now())
    ] + records
    document["fetched_at"] = datetime.now(UTC).isoformat()
    atomic_json(manifest, document)
    return counts


def sync_match_statistics(
    database: Path, runtime: Path, *, team: str, preview: bool
) -> dict[str, Any]:
    import collect_asb_match_statistics as protocols

    games = load_json(runtime / "docs/results-asb.json")["games"]
    selected = [g for g in games if g["team"] == team and g["season"] == season_now()]
    payload = protocols.collect(selected, season=season_now())
    coverage = payload["coverage"]
    # Apply good independent protocols while retaining missing/rejected last-good rows.
    counts = protocols.apply_statistics(database, payload, apply=not preview)
    if not preview:
        atomic_json(runtime / f"docs/asb-match-statistics-{team}.json", payload)
    return {
        "status": "partial" if any(r["status"] != "collected" for r in coverage) else "succeeded",
        "received": len(coverage),
        "collected": len(payload["statistics"]),
        "counts": counts,
        "preview": preview,
    }


def sync_standings(database: Path, runtime: Path, *, team: str, preview: bool) -> dict[str, Any]:
    import collect_asb_standings as collector
    import import_asb_standings as importer

    async def fetch() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        async with httpx.AsyncClient(timeout=25, trust_env=False) as client:

            async def get(url: str) -> Any:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()

            original = collector.TEAMS
            try:
                collector.TEAMS = {team: TEAMS[team]}
                return await collector.collect_modern(get)
            finally:
                collector.TEAMS = original

    tables, _ = asyncio.run(fetch())
    now = datetime.now(UTC)
    parsed = [importer.OfficialTable.model_validate(table) for table in tables]
    manifest = runtime / "docs/asb-standings.json"
    document = load_json(manifest)
    existing = {
        row["competition_id"]
        for row in document["tables"]
        if row["team"] == team and row["season"] == season_now()
    }
    if existing - {row["competition_id"] for row in tables}:
        raise ValueError("Published standings stages disappeared; keeping last-good tables")
    counts = (
        {"tables": len(parsed), "preview": True}
        if preview
        else importer.apply_tables(database, parsed, now)
    )
    if preview:
        return counts
    old = {(row["team"], row["competition_id"]): row for row in document["tables"]}
    old.update({(row["team"], row["competition_id"]): row for row in tables})
    document.update(tables=list(old.values()), fetched_at=now.isoformat())
    atomic_json(manifest, document)
    return counts


def sync_roster(database: Path, runtime: Path, *, team: str, preview: bool) -> dict[str, Any]:
    import sync_asb_roster as roster

    # Optional historical endpoints must not block fresh current statistics.
    # Reuse only previously verified local bundles for the same external IDs.
    original = roster.historical_statistics
    roster.historical_statistics = lambda client, members, team_id, season: members
    try:
        snapshots = roster.collect(teams=[(team, TEAMS[team])])
    finally:
        roster.historical_statistics = original
    manifest = runtime / "docs/asb-roster.json"
    document = load_json(manifest)
    previous = {(row["team"], row["season"]): row for row in document["snapshots"]}
    for snapshot in snapshots:
        old = previous.get((snapshot.team, snapshot.season)) or {}
        players = {row["external_player_id"]: row for row in old.get("players", [])}
        if old.get("is_published") and not snapshot.is_published:
            raise ValueError("Published roster disappeared; keeping last-good roster")
        merged = []
        for member in snapshot.players:
            prior = players.get(member.external_player_id)
            if prior and prior.get("statistics_season") == snapshot.season:
                if member.statistics_season != snapshot.season or (
                    (member.games_played or 0) < (prior.get("games_played") or 0)
                ):
                    raise ValueError("Current player statistics regressed; keeping last-good data")
                if any(
                    prior.get(field) is not None and getattr(member, field) is None
                    for field in roster.TOTAL_FIELDS
                ):
                    raise ValueError("Published player totals disappeared; keeping last-good data")
            if member.statistics_season is None and prior and prior.get("statistics_season"):
                fields = [
                    "games_played",
                    *roster.TOTAL_FIELDS,
                    "statistics_season",
                    "statistics_source_url",
                ]
                member = roster.RosterMemberInput.model_validate(
                    {**member.model_dump(), **{key: prior.get(key) for key in fields}}
                )
            merged.append(member)
        snapshot.players = merged
    counts = (
        {"players": sum(len(row.players) for row in snapshots), "preview": True}
        if preview
        else (roster.apply_sync(database, snapshots))
    )
    if preview:
        return counts
    for snapshot in snapshots:
        previous[(snapshot.team, snapshot.season)] = snapshot.model_dump(mode="json")
    atomic_json(manifest, {"snapshots": list(previous.values())})
    return counts


def sync_portraits(database: Path, runtime: Path, *, preview: bool) -> dict[str, Any]:
    import cache_asb_roster_photos as photos
    import sync_asb_roster as roster

    from predatory_beavers.modules.roster.photos import PHOTO_DIRECTORY

    if preview:
        return {"status": "succeeded", "preview": True}
    target = database.parent / "sports-sync" / "roster-photos"
    if not target.exists():
        shutil.copytree(PHOTO_DIRECTORY, target)
    original_directory, original_loader = photos.PHOTO_DIRECTORY, photos.load_manifest
    try:
        photos.PHOTO_DIRECTORY = target
        photos.load_manifest = lambda: roster.load_manifest(runtime / "docs/asb-roster.json")
        return photos.cache_photos(fetch=True)
    finally:
        photos.PHOTO_DIRECTORY, photos.load_manifest = original_directory, original_loader


def sync_fiba(
    database: Path, runtime: Path, *, preview: bool, api_key: str | None = None
) -> dict[str, Any]:
    import collect_fiba_games as collector
    import import_public_results as importer

    from predatory_beavers.settings import get_settings

    watchlist = database.parent / "sports-sync" / "fiba-watchlist.json"
    if not watchlist.exists():
        shutil.copyfile(ROOT / "docs/fiba-watchlist.json", watchlist)
    document = collector.collect_watched(
        watchlist, api_key=api_key if api_key is not None else get_settings().fiba_api_key or ""
    )
    rows = [importer.PublicResult.model_validate(row) for row in document.get("results", [])]
    manifest = runtime / "docs/results-fiba3x3.json"
    previous = load_json(manifest)
    previous_registrations = {
        row["registration_id"]: row for row in previous.get("registrations", [])
    }
    for registration in document.get("registrations", []):
        if registration.get("status") != "ok":
            continue
        old = previous_registrations.get(registration["registration_id"], {})
        if set(old.get("external_ids", [])) - set(registration.get("external_ids", [])):
            raise ValueError("Published FIBA games disappeared; keeping last-good registration")
        previous_registrations[registration["registration_id"]] = registration
    keyed = {(row["source"], row["external_id"]): row for row in previous["results"]}
    keyed.update({(row.source, row.external_id): row.model_dump(mode="json") for row in rows})
    verified = {
        (row["team"], row["season"])
        for row in document.get("registrations", [])
        if row.get("status") == "ok"
    }
    coverage = [
        {
            **row,
            "records": sum(
                item["team"] == row["team"] and item["season"] == row["season"]
                for item in keyed.values()
            ),
        }
        for row in document.get("coverage", [])
        if (row["team"], row["season"]) in verified
    ]
    counts = (
        {}
        if preview or not (rows or coverage)
        else importer.apply_import(database, rows, coverage, [])
    )
    if (rows or coverage) and not preview:
        previous["results"] = list(keyed.values())
        previous["registrations"] = list(previous_registrations.values())
        existing_coverage = {
            (row["provider"], row["team"], row["season"]): row
            for row in previous.get("coverage", [])
        }
        existing_coverage.update(
            {(row["provider"], row["team"], row["season"]): row for row in coverage}
        )
        previous["coverage"] = list(existing_coverage.values())
        atomic_json(manifest, previous)
    return {**document, "results": len(rows), "counts": counts}


def run_refresh(database: Path, *, preview: bool = False) -> dict[str, Any]:
    database = database.resolve()
    if database == (ROOT / "data/predatory_beavers.db").resolve() or not database.is_file():
        raise ValueError("Only an existing working database can be refreshed")
    runtime = working_tree(database)
    if not preview:
        daily_backup(database)
        migrate_if_needed(database)
    jobs = (
        [
            (
                f"asb_games_{team}",
                lambda team=team: sync_games(database, runtime, team=team, preview=preview),
            )
            for team in TEAMS
        ]
        + [
            (
                f"asb_match_statistics_{team}",
                lambda team=team: sync_match_statistics(
                    database, runtime, team=team, preview=preview
                ),
            )
            for team in TEAMS
        ]
        + [
            (
                f"asb_standings_{team}",
                lambda team=team: sync_standings(database, runtime, team=team, preview=preview),
            )
            for team in TEAMS
        ]
        + [
            (
                f"asb_roster_{team}",
                lambda team=team: sync_roster(database, runtime, team=team, preview=preview),
            )
            for team in TEAMS
        ]
        + [
            ("asb_portraits", lambda: sync_portraits(database, runtime, preview=preview)),
            ("fiba3x3", lambda: sync_fiba(database, runtime, preview=preview)),
        ]
    )
    state_path = database.parent / "sports-sync" / "sources.json"
    previous = load_json(state_path) if state_path.is_file() else {}
    states = {}
    for name, job in jobs:
        started = datetime.now(UTC).isoformat()
        old = previous.get(name, {})
        from predatory_beavers.settings import get_settings

        settings = get_settings()
        interval = (
            settings.sports_sync_fiba_interval_seconds
            if name == "fiba3x3"
            else settings.sports_sync_interval_seconds
        )
        due_at = old.get("next_attempt_at")
        if not preview and due_at and datetime.fromisoformat(due_at) > datetime.now(UTC):
            states[name] = old
            continue
        try:
            result = job()
            status = result.get("status", "succeeded")
            if status in {"ok", "success", "imported"}:
                status = "succeeded"
            state = {
                "status": status,
                "started_at": started,
                "finished_at": datetime.now(UTC).isoformat(),
                "result": result,
                "last_success_at": old.get("last_success_at"),
            }
            if status == "succeeded":
                state["last_success_at"] = state["finished_at"]
        except Exception as exc:
            state = {
                "status": "failed",
                "started_at": started,
                "finished_at": datetime.now(UTC).isoformat(),
                "last_success_at": old.get("last_success_at"),
                "error": type(exc).__name__,
            }
            logger.warning("Sports source refresh failed: %s (%s)", name, type(exc).__name__)
        states[name] = state
        state["interval_seconds"] = interval
        state["next_attempt_at"] = (datetime.now(UTC) + timedelta(seconds=interval)).isoformat()
        if not preview:
            atomic_json(state_path, {**previous, **states})
    successes = sum(state["status"] == "succeeded" for state in states.values())
    status = "succeeded" if successes == len(states) else "partial" if successes else "failed"
    return {
        "status": status,
        "finished_at": datetime.now(UTC).isoformat(),
        "preview": preview,
        "sources": states,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data/local.db")
    parser.add_argument("--status-json", type=Path)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--lock-owned", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    from predatory_beavers.worker.sports_sync import ProcessLock

    lock = None
    if not args.lock_owned:
        database = args.database.resolve()
        lock = ProcessLock(database.with_name(database.name + ".sports-sync.lock"))
        if not lock.acquire():
            print(json.dumps({"status": "skipped", "reason": "another_refresh_is_running"}))
            return
    try:
        try:
            report = run_refresh(args.database, preview=args.preview)
        except Exception as exc:
            report = {"status": "failed", "error": type(exc).__name__, "sources": {}}
    finally:
        if lock is not None:
            lock.release()
    if args.status_json:
        atomic_json(args.status_json, report)
    print(json.dumps(report, ensure_ascii=True))
    if report["status"] == "failed":
        raise SystemExit(1)
