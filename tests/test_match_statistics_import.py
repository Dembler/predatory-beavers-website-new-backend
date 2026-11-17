"""Exercise atomic protocol revisions and the editor's parent-game precedence."""

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from predatory_beavers.integrations.asb.statistics import normalize_game_statistics

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from collect_asb_match_statistics import apply_statistics  # noqa: E402


@pytest.fixture
def revision():
    raw = json.loads((ROOT / "tests/fixtures/asb-game-1138965.json").read_text(encoding="utf-8"))
    data = normalize_game_statistics(
        raw,
        external_id="1138965",
        team="men",
        season="2026-2027",
        fetched_at=datetime(2026, 11, 17, tzinfo=UTC),
    )
    assert data is not None
    return data.model_dump(mode="json")


@pytest.fixture
def database(tmp_path):
    target = tmp_path / "working.db"
    with sqlite3.connect(target) as db:
        db.executescript("""
            CREATE TABLE archive_results (
                source,external_id,team,season,discipline,status,club_score,opponent_score);
            CREATE TABLE teams (id,category,active,is_deleted);
            CREATE TABLE competitions (id,season,active,is_deleted);
            CREATE TABLE matches (id,source,external_id,team_id,competition_id,
                club_side,status,home_score,away_score,active,is_deleted);
            CREATE TABLE match_statistics (id,created_at,updated_at,source,external_id,
                team,season,discipline,payload,fetched_at);
            INSERT INTO archive_results VALUES (
                'asb','1138965','men','2026-2027','5x5','finished',60,51);
            INSERT INTO teams VALUES ('t','MEN',1,0);
            INSERT INTO competitions VALUES ('c','2026-2027',1,0);
        """)
    return target


def read_payload(database):
    with sqlite3.connect(database) as db:
        value = db.execute("SELECT payload FROM match_statistics").fetchone()
    return json.loads(value[0]) if value else None


def test_preview_is_read_only_and_apply_is_idempotent(database, revision):
    before = database.read_bytes()
    assert apply_statistics(database, {"statistics": [revision]})["created"] == 1
    assert database.read_bytes() == before
    assert apply_statistics(database, {"statistics": [revision]}, apply=True)["created"] == 1
    assert apply_statistics(database, {"statistics": [revision]}, apply=True)["unchanged"] == 1
    assert read_payload(database) == revision


def test_empty_or_degraded_protocol_preserves_last_good_revision(database, revision):
    apply_statistics(database, {"statistics": [revision]}, apply=True)
    assert apply_statistics(database, {"statistics": []}, apply=True)["created"] == 0
    degraded = dict(revision, box_score=[], player_periods_status="unavailable")
    assert apply_statistics(database, {"statistics": [degraded]}, apply=True)["regressed"] == 1
    assert read_payload(database) == revision


@pytest.mark.parametrize("field,value", [("active", 0), ("home_score", 59), ("club_side", "AWAY")])
def test_manual_match_overrides_archive_copy(database, revision, field, value):
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO matches VALUES ('m','asb','1138965','t','c','HOME','FINISHED',60,51,1,0)"
        )
        db.execute(f"UPDATE matches SET {field}=?", (value,))
    assert apply_statistics(database, {"statistics": [revision]}, apply=True)["stale"] == 1
    assert read_payload(database) is None


def test_match_without_archive_is_supported_and_duplicate_bundle_is_atomic(database, revision):
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM archive_results")
        db.execute(
            "INSERT INTO matches VALUES ('m','asb','1138965','t','c','HOME','FINISHED',60,51,1,0)"
        )
    with pytest.raises(ValueError, match="duplicate"):
        apply_statistics(database, {"statistics": [revision, revision]}, apply=True)
    assert read_payload(database) is None
    assert apply_statistics(database, {"statistics": [revision]}, apply=True)["created"] == 1


def test_source_database_is_never_a_target(tmp_path, revision):
    with pytest.raises(ValueError, match="regular database"):
        apply_statistics(tmp_path / "predatory_beavers.db", {"statistics": [revision]}, apply=True)


def test_refresh_imports_only_current_team_and_preview_does_not_write(
    database, revision, tmp_path, monkeypatch
):
    import collect_asb_match_statistics as collector
    import sports_refresh_runtime as refresh

    runtime = tmp_path / "runtime"
    records = [
        {"team": "men", "season": "2026-2027", "GameID": 1138965, "GameStatus": 1},
        {"team": "women", "season": "2026-2027", "GameID": 1138973, "GameStatus": 1},
        {"team": "men", "season": "2025-2026", "GameID": 999, "GameStatus": 1},
    ]
    refresh.atomic_json(runtime / "docs/results-asb.json", {"games": records})
    monkeypatch.setattr(refresh, "season_now", lambda: "2026-2027")

    def collect(games, *, season):
        assert games == records[:1]
        assert season == "2026-2027"
        return {"statistics": [revision], "coverage": [{"status": "collected"}]}

    monkeypatch.setattr(collector, "collect", collect)
    before = database.read_bytes()
    result = refresh.sync_match_statistics(database, runtime, team="men", preview=True)
    assert result["counts"]["created"] == 1
    assert database.read_bytes() == before
    assert not (runtime / "docs/asb-match-statistics-men.json").exists()
    result = refresh.sync_match_statistics(database, runtime, team="men", preview=False)
    assert result["status"] == "succeeded"
    assert read_payload(database) == revision
    assert (runtime / "docs/asb-match-statistics-men.json").is_file()
