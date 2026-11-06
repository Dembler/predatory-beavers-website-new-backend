import asyncio
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from predatory_beavers.api.main import create_app
from predatory_beavers.db.base import Base
from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.club.models import Team, TeamCategory
from predatory_beavers.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "public_results_import", ROOT / "scripts/import_public_results.py"
)
assert spec and spec.loader
importer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = importer
spec.loader.exec_module(importer)


@pytest.fixture
def results_db(tmp_path: Path) -> tuple[Path, Settings]:
    database = tmp_path / "archive.sqlite"
    settings = Settings(
        env="test",
        database_url="sqlite+aiosqlite:///" + database.as_posix(),
        media_storage_path=tmp_path / "media",
        log_json=False,
    )

    async def seed() -> None:
        engine = create_engine(settings)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with create_session_factory(engine)() as session:
            session.add_all(
                [
                    Team(slug="men", name="ВГУ", category=TeamCategory.MEN),
                    Team(slug="women", name="ВГУ", category=TeamCategory.WOMEN),
                ]
            )
            await session.commit()
        await engine.dispose()

    asyncio.run(seed())
    return database, settings


def test_import_is_idempotent_preserves_existing_match_edits_and_backs_up(
    results_db: tuple[Path, Settings],
) -> None:
    database, _ = results_db
    results, coverage, games = importer.load_sources()
    first = importer.apply_import(database, results, coverage, games)
    assert first["archive_created"] == len(results)
    finished_games = [game for game in games if game["GameStatus"] == 1]
    assert first["matches_created"] == len(finished_games)
    assert first["matches_unpublished_skipped"] == len(games) - len(finished_games)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE matches SET notes='Editor change',home_team_name='Edited name' "
            "WHERE external_id=?",
            (str(finished_games[0]["GameID"]),),
        )
        snapshot = connection.execute("SELECT * FROM matches ORDER BY id").fetchall()
    backup = importer.backup_database(database)
    second = importer.apply_import(database, results, coverage, games)
    assert second["archive_created"] == 0
    assert second["archive_updated"] == 0
    assert second["archive_unchanged"] == len(results)
    assert second["matches_created"] == 0
    assert second["matches_preserved"] == len(finished_games)
    with sqlite3.connect(database) as current, sqlite3.connect(backup) as copied:
        assert current.execute("SELECT * FROM matches ORDER BY id").fetchall() == snapshot
        assert copied.execute("SELECT * FROM matches ORDER BY id").fetchall() == snapshot


def test_public_archive_filters_paginates_and_exposes_real_source_coverage(
    results_db: tuple[Path, Settings],
) -> None:
    database, settings = results_db
    results, coverage, games = importer.load_sources()
    importer.apply_import(database, results, coverage, games)
    with TestClient(create_app(settings)) as client:
        page = client.get(
            "/api/v1/results-archive",
            params={
                "discipline": "3x3",
                "team": "women",
                "year": 2026,
                "page_size": 1,
            },
        )
        assert page.status_code == 200, page.text
        payload = page.json()
        assert payload["total"] >= 1
        assert len(payload["items"]) == 1
        row = payload["items"][0]
        assert row["external_id"] == "20726-women"
        assert row["place"] == 4
        assert row["event_date"] == "2026-10-03"
        assert row["source_url"] == "https://www.vsu.ru/ru/news/feed/2026/10/20726"
        assert not any("player" in key or "birth" in key for key in row)
        assert client.get("/api/v1/results-archive?discipline=bad").status_code == 422
        summary_response = client.get("/api/v1/results-archive/summary")
        assert summary_response.status_code == 200, summary_response.text
        summary = summary_response.json()["data"]
        assert summary["total"] == len(results)
        assert {"3x3", "5x5", "phygital"} == set(summary["disciplines"])
        fiba = [item for item in summary["coverage"] if item["provider"] == "fiba3x3"]
        assert len(fiba) == 2
        assert all(
            item["records"] == 0 and item["status"] == "upstream_unavailable" for item in fiba
        )
        legacy = [
            item
            for item in summary["coverage"]
            if item["provider"] == "asb"
            and item["season"] == "2018-2019"
            and item["team"] == "women"
        ]
        assert len(legacy) == 1
        assert legacy[0]["records"] == 0
        assert legacy[0]["status"] == "historical_identity_unverified"
        honours = client.get("/api/v1/results-archive/honours").json()["data"]
        assert honours["titles"] == 11
        assert honours["championships"] == 1
        assert honours["podiums"] == 33
        assert honours["silver"] == 10
        assert honours["bronze"] == 12
        assert honours["from_year"] == 2011
        women_honours = client.get("/api/v1/results-archive/honours?team=women").json()["data"]
        assert women_honours["titles"] == 2
        assert all(item["team"] == "women" for item in women_honours["items"])


def test_manifests_reject_personal_fields_and_do_not_mislabel_fallback_seasons() -> None:
    results, _, games = importer.load_sources()
    assert len({(row.source, row.external_id) for row in results}) == len(results)
    invalid = results[0].model_dump()
    invalid["player_birth_date"] = "2000-01-01"
    with pytest.raises(ValidationError):
        importer.PublicResult.model_validate(invalid)
    assert not any(game["season"] == "2026-2027" for game in games)
    unpublished = [row for row in results if row.source == "asb" and row.status == "unpublished"]
    assert len(unpublished) == 14
    assert all(row.club_score is None and row.opponent_score is None for row in unpublished)
    asb = json.loads((ROOT / "docs/results-asb.json").read_text(encoding="utf-8"))
    assert len(games) == 162
    assert all(item["records"] == 0 for item in asb["coverage"] if item["season"] == "2026-2027")


def test_reviewed_sources_correct_format_and_do_not_duplicate_academic_reports() -> None:
    rows, _, _ = importer.load_sources()
    by_id = {row.external_id: row for row in rows}
    marathon = by_id["sponsors-xlsx-8-men-2025"]
    assert marathon.discipline == "phygital"
    assert marathon.place == 1
    assert str(marathon.source_url) == "https://www.vsu.ru/ru/news/feed/2025/02/18927"
    assert by_id["sponsors-xlsx-8-men-2024"].discipline == "3x3"
    assk = by_id["sponsors-xlsx-10-men-2024"]
    assert str(assk.event_date) == "2024-11-30"
    assert str(assk.source_url).startswith("https://ssca.ru/")
    university = [
        row
        for row in rows
        if row.team == "men"
        and row.season == "2012-2013"
        and row.tournament == "Межвузовская универсиада Воронежской области"
    ]
    assert len(university) == 1
    assert university[0].year == 2012
    assert university[0].place == 2
    assert by_id["newspaper-2012-24-student-league-2012-men"].season == "2012"
    for key in ("20557-universiade-men", "20557-universiade-women"):
        assert by_id[key].event_date is None
        assert by_id[key].date_precision == "year"
        assert by_id[key].season == "2025-2026"
    rfb = by_id["russia-cup-2026-men-participation"]
    assert rfb.place is None and rfb.status == "participated"


def test_honours_api_checks_conflicting_nonpodium_place_before_counting(
    results_db: tuple[Path, Settings],
) -> None:
    database, settings = results_db
    results, coverage, games = importer.load_sources()
    importer.apply_import(database, results, coverage, games)
    with sqlite3.connect(database) as connection:
        # A second citation of the same event disagrees with the published bronze.
        original = connection.execute("SELECT * FROM archive_results WHERE external_id='20726-men'")
        columns = [item[0] for item in original.description]
        values = dict(zip(columns, original.fetchone(), strict=True))
        values.update(id=uuid4().hex, external_id="conflict-fixture", place=4)
        connection.execute(
            f"INSERT INTO archive_results ({','.join(values)}) "
            f"VALUES ({','.join('?' for _ in values)})",
            tuple(values.values()),
        )
    with TestClient(create_app(settings)) as client:
        honours = client.get("/api/v1/results-archive/honours").json()["data"]
        assert honours["podiums"] == 32
        assert honours["bronze"] == 11
