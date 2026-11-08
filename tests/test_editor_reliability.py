import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm.exc import StaleDataError

from predatory_beavers.api.main import create_app
from predatory_beavers.db.base import Base
from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.club.models import Team, TeamCategory
from predatory_beavers.settings import Settings, get_settings

pytest_plugins = ["test_content_media_admin"]


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "https://*.example.test",
        "http://example.test",
        "https://example.test/path",
        "https://example.test/",
        "https://user@example.test",
        "https://example.test?query=1",
    ],
)
def test_production_rejects_non_origin_cors_values(origin: str) -> None:
    with pytest.raises(ValidationError, match="exact HTTPS origins"):
        Settings(
            env="prod",
            reload=False,
            cookie_secure=True,
            session_cookie_name="__Host-pb_session",
            cors_origins=[origin],
        )


def test_cross_origin_editor_can_send_idempotency_key() -> None:
    settings = Settings(env="test", cors_origins=["https://club.example.test"])
    with TestClient(create_app(settings)) as client:
        response = client.options(
            "/api/v1/admin/players",
            headers={
                "Origin": "https://club.example.test",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-csrf-token,idempotency-key",
            },
        )
        assert response.status_code == 200
        assert response.headers["Access-Control-Allow-Origin"] == "https://club.example.test"


def test_creation_replay_survives_lost_response_relogin_and_changed_payload(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, team_id = content_client
    endpoint = "/api/v1/admin/players"
    payload = {"team_id": team_id, "full_name": "One player"}
    replay_headers = {**headers, "Idempotency-Key": str(uuid4())}
    # The first request commits; simulate losing its response by retrying the
    # same draft after a fresh login, with a different session and CSRF token.
    committed = client.post(endpoint, headers=replay_headers, json=payload)
    assert committed.status_code == 201
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
    login = client.post(
        "/api/v1/auth/login",
        json={
            "username": "editor",
            "password": "strong-test-password",
        },
    )
    assert login.status_code == 200
    csrf = client.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
    replay_headers.update({"X-CSRF-Token": csrf})
    replay = client.post(endpoint, headers=replay_headers, json=payload)
    assert replay.status_code == 201
    assert replay.json()["data"]["id"] == committed.json()["data"]["id"]
    assert client.get(endpoint).json()["total"] == 1
    conflict = client.post(
        endpoint, headers=replay_headers, json={**payload, "full_name": "Different"}
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"
    assert (
        client.post(
            endpoint, headers={**replay_headers, "X-CSRF-Token": "wrong"}, json=payload
        ).status_code
        == 403
    )
    assert (
        client.post(
            endpoint, headers={**replay_headers, "Idempotency-Key": "bad"}, json=payload
        ).status_code
        == 400
    )


def test_all_editor_creation_resources_honor_replay_keys(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, team_id = content_client
    competition = client.post(
        "/api/v1/admin/competitions",
        headers=headers,
        json={"name": "Replay league", "season": "2026"},
    ).json()["data"]
    image = BytesIO()
    Image.new("RGB", (12, 12), "yellow").save(image, "PNG")
    asset = client.post(
        "/api/v1/admin/media",
        headers=headers,
        files={"file": ("honour.png", image.getvalue(), "image/png")},
    ).json()["data"]
    cases = (
        ("content", {"section": "history", "title": "Replay text", "body": "Text"}),
        ("competitions", {"name": "Other replay league", "season": "2026"}),
        ("venues", {"name": "Replay gym", "address": "Gym address"}),
        (
            "matches",
            {
                "team_id": team_id,
                "competition_id": competition["id"],
                "starts_at": "2026-12-01T12:00:00Z",
                "club_side": "home",
                "home_team_name": "Club",
                "away_team_name": "Visitors",
            },
        ),
        (
            "achievements",
            {"team_id": team_id, "title": "Replay honour", "media_asset_id": asset["id"]},
        ),
    )
    for resource, payload in cases:
        endpoint = f"/api/v1/admin/{resource}"
        replay_headers = {**headers, "Idempotency-Key": str(uuid4())}
        first = client.post(endpoint, headers=replay_headers, json=payload)
        second = client.post(endpoint, headers=replay_headers, json=payload)
        assert first.status_code == second.status_code == 201, (resource, first.text, second.text)
        assert first.json()["data"]["id"] == second.json()["data"]["id"]


def test_concurrent_creation_replay_and_failed_creation_do_not_duplicate(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, team_id = content_client
    endpoint = "/api/v1/admin/players"
    replay_headers = {**headers, "Idempotency-Key": str(uuid4())}
    payload = {"team_id": team_id, "full_name": "Concurrent player"}
    # A failed transaction must roll back its reservation, allowing the same
    # draft/key to be corrected and saved subsequently.
    assert (
        client.post(
            endpoint, headers=replay_headers, json={**payload, "team_id": str(uuid4())}
        ).status_code
        == 404
    )
    barrier = Barrier(3)

    def create(_: int) -> tuple[int, str]:
        barrier.wait(timeout=10)
        response = client.post(endpoint, headers=replay_headers, json=payload)
        return response.status_code, response.json().get("data", {}).get("id", "")

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(create, range(3)))
    assert all(code == 201 for code, _ in results)
    assert len({entity_id for _, entity_id in results}) == 1
    assert client.get(endpoint).json()["total"] == 1


def test_incomplete_creation_ledger_returns_a_safe_error_without_creating_a_duplicate(
    content_client: tuple[TestClient, dict[str, str], str],
    tmp_path: Path,
) -> None:
    client, headers, team_id = content_client
    key = uuid4()
    with sqlite3.connect(tmp_path / "content.sqlite") as connection:
        actor_id = connection.execute("SELECT id FROM users WHERE username='editor'").fetchone()[0]
        connection.execute(
            "INSERT INTO admin_creation_keys "
            "(actor_id,resource,key,request_hash,entity_id,created_at) VALUES (?,?,?,?,NULL,?)",
            (actor_id, "players", key.hex, "0" * 64, "2026-11-07 12:00:00"),
        )
    for _ in range(2):
        response = client.post(
            "/api/v1/admin/players",
            headers={**headers, "Idempotency-Key": str(key)},
            json={"team_id": team_id, "full_name": "Unconfirmed save"},
        )
        assert response.status_code == 503
        assert response.json()["code"] == "idempotency_incomplete"
    assert client.get("/api/v1/admin/players").json()["total"] == 0


def test_deleted_external_identities_remain_reserved(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, team_id = content_client
    competition_id = client.post(
        "/api/v1/admin/competitions",
        headers=headers,
        json={"name": "Match dependency", "season": "2026"},
    ).json()["data"]["id"]
    cases = (
        ("competitions", {"name": "Reserved league", "season": "2026"}),
        ("venues", {"name": "Reserved gym", "address": "Gym address"}),
        (
            "matches",
            {
                "team_id": team_id,
                "competition_id": competition_id,
                "starts_at": "2026-12-01T12:00:00Z",
                "club_side": "home",
                "home_team_name": "Club",
                "away_team_name": "Visitors",
            },
        ),
    )
    for resource, fields in cases:
        endpoint = f"/api/v1/admin/{resource}"
        payload = {**fields, "source": "asb", "external_id": "reserved-record"}
        created = client.post(endpoint, headers=headers, json=payload)
        assert created.status_code == 201
        record = created.json()["data"]
        assert client.delete(f"{endpoint}/{record['id']}", headers=headers).status_code == 204
        repeated = client.post(endpoint, headers=headers, json=payload)
        assert repeated.status_code == 409
        assert client.get(f"{endpoint}/{record['id']}").status_code == 404


def test_stale_editor_cannot_update_or_delete_and_public_birth_date_is_private(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, team_id = content_client
    created = client.post(
        "/api/v1/admin/players",
        headers=headers,
        json={"team_id": team_id, "full_name": "Initial", "birth_date": "2000-01-02"},
    ).json()["data"]
    path = f"/api/v1/admin/players/{created['id']}"
    version = created["updated_at"]
    changed = client.patch(
        path,
        headers=headers,
        json={
            "full_name": "First editor",
            "expected_updated_at": version,
        },
    )
    assert changed.status_code == 200
    assert changed.json()["data"]["updated_at"] != version
    for response in (
        client.patch(path, headers=headers, json={"fact": "stale", "expected_updated_at": version}),
        client.delete(path, headers=headers, params={"expected_updated_at": version}),
    ):
        assert response.status_code == 409
        assert response.json()["code"] == "stale_record"
    assert client.get(path).json()["data"]["full_name"] == "First editor"
    assert client.get(path).json()["data"]["birth_date"] == "2000-01-02"
    public = client.get(f"/api/v1/players/{created['id']}").json()["data"]
    assert "birth_date" not in public
    assert "birth_date" not in client.get("/api/v1/players").json()["items"][0]


@pytest.mark.asyncio
async def test_atomic_version_check_rejects_two_overlapping_transactions(tmp_path: Path) -> None:
    settings = Settings(env="test", database_url=f"sqlite+aiosqlite:///{tmp_path / 'overlap.db'}")
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        team = Team(slug="overlap", name="Initial", category=TeamCategory.MEN)
        session.add(team)
        await session.commit()
        team_id = team.id
    both_loaded, first_committed = asyncio.Event(), asyncio.Event()
    readers = 0

    async def editor(name: str, first: bool) -> None:
        nonlocal readers
        async with factory() as session:
            record = await session.get(Team, team_id)
            assert record is not None
            # Both editors read the same version before either writes.
            readers += 1
            if readers == 2:
                both_loaded.set()
            await both_loaded.wait()
            if not first:
                await first_committed.wait()
            record.name = name
            if first:
                await session.commit()
                first_committed.set()
            else:
                with pytest.raises(StaleDataError):
                    await session.commit()
                await session.rollback()

    try:
        await asyncio.wait_for(asyncio.gather(editor("First", True), editor("Second", False)), 10)
        async with factory() as session:
            assert await session.scalar(select(Team.name).where(Team.id == team_id)) == "First"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_iso_timestamp_format_can_still_be_edited(tmp_path: Path) -> None:
    settings = Settings(
        env="test", database_url=f"sqlite+aiosqlite:///{tmp_path / 'legacy-edit.db'}"
    )
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        team = Team(slug="legacy", name="Imported", category=TeamCategory.MEN)
        session.add(team)
        await session.commit()
        team_id = team.id
    # Existing migration/import scripts persisted valid ISO timestamps with T/Z,
    # while SQLAlchemy's DateTime binder uses a space and six decimal places.
    with sqlite3.connect(tmp_path / "legacy-edit.db") as connection:
        connection.execute("UPDATE teams SET updated_at='2026-11-01T12:00:00.123456+00:00'")
    try:
        async with factory() as session:
            imported = await session.get(Team, team_id)
            assert imported is not None
            imported.name = "Edited import"
            await session.commit()
            assert imported.version_id == 2
    finally:
        await engine.dispose()


def test_unicode_search_is_literal_filtered_and_paginated(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, team_id = content_client
    endpoint = "/api/v1/admin/players"
    for name, active in (("Ёжик 100%", True), ("ЁЖИК 100% запас", False), ("ежик 1000", True)):
        assert (
            client.post(
                endpoint,
                headers=headers,
                json={
                    "team_id": team_id,
                    "full_name": name,
                    "active": active,
                },
            ).status_code
            == 201
        )
    result = client.get(endpoint, params={"q": "ёжик 100%", "page_size": 1}).json()
    assert result["total"] == 2
    assert len(result["items"]) == 1
    assert client.get(endpoint, params={"q": "ЁЖИК", "active": False}).json()["total"] == 1
    assert client.get(endpoint, params={"q": "_"}).json()["total"] == 0


def test_match_search_finds_the_complete_visible_title(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, team_id = content_client
    competition = client.post(
        "/api/v1/admin/competitions",
        headers=headers,
        json={"name": "Search league", "season": "2026"},
    ).json()["data"]
    created = client.post(
        "/api/v1/admin/matches",
        headers=headers,
        json={
            "team_id": team_id,
            "competition_id": competition["id"],
            "starts_at": "2026-12-01T12:00:00Z",
            "club_side": "home",
            "home_team_name": "Хищные Бобры",
            "away_team_name": "Соперник",
        },
    )
    assert created.status_code == 201
    result = client.get(
        "/api/v1/admin/matches", params={"q": "хищные бобры — соперник", "page_size": 1}
    )
    assert result.status_code == 200
    assert result.json()["total"] == 1
    assert result.json()["items"][0]["id"] == created.json()["data"]["id"]


def test_admin_matches_start_with_the_latest_date_while_public_schedule_is_chronological(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, team_id = content_client
    competition_id = client.post(
        "/api/v1/admin/competitions",
        headers=headers,
        json={"name": "Date ordering", "season": "2026"},
    ).json()["data"]["id"]
    ids = []
    for starts_at in ("2020-01-01T12:00:00Z", "2030-01-01T12:00:00Z"):
        response = client.post(
            "/api/v1/admin/matches",
            headers=headers,
            json={
                "team_id": team_id,
                "competition_id": competition_id,
                "starts_at": starts_at,
                "club_side": "home",
                "home_team_name": "Club",
                "away_team_name": "Visitors",
            },
        )
        assert response.status_code == 201
        ids.append(response.json()["data"]["id"])
    assert [row["id"] for row in client.get("/api/v1/admin/matches").json()["items"]] == ids[::-1]
    assert [row["id"] for row in client.get("/api/v1/matches").json()["items"]] == ids


def test_media_draft_privacy_usage_reuse_and_publication(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, team_id = content_client
    image = BytesIO()
    Image.new("RGB", (24, 32), "purple").save(image, "PNG")

    def upload(alt: str) -> dict[str, object]:
        response = client.post(
            "/api/v1/admin/media",
            headers=headers,
            files={"file": ("image.png", image.getvalue(), "image/png")},
            data={"alt_text": alt},
        )
        assert response.status_code == 201
        return response.json()["data"]

    asset = upload("Original description")
    assert asset["reused"] is False
    reused = upload("New description")
    assert reused["reused"] is True
    assert reused["id"] == asset["id"]
    assert reused["alt_text"] == "Original description"
    content_url = str(asset["content_url"])
    assert client.get(content_url).headers["cache-control"] == "private, no-store"
    cookies = dict(client.cookies)

    def anonymous_status() -> int:
        client.cookies.clear()
        try:
            return client.get(content_url).status_code
        finally:
            client.cookies.update(cookies)

    assert anonymous_status() == 404
    player = client.post(
        "/api/v1/admin/players",
        headers=headers,
        json={
            "team_id": team_id,
            "full_name": "Draft",
            "active": False,
            "photo_asset_id": asset["id"],
        },
    ).json()["data"]
    assert anonymous_status() == 404
    usage = client.get(f"/api/v1/admin/media/{asset['id']}/usage").json()["data"]
    assert usage[0]["admin_url"] == f"/admin?tab=players&edit={player['id']}"
    assert (
        client.delete(f"/api/v1/admin/media/{asset['id']}", headers=headers).json()["errors"]
        == usage
    )
    assert (
        client.patch(
            f"/api/v1/admin/players/{player['id']}", headers=headers, json={"active": True}
        ).status_code
        == 200
    )
    assert anonymous_status() == 200
    assert client.get(content_url).headers["cache-control"] == "public, max-age=0, must-revalidate"
    assert (
        client.patch(
            f"/api/v1/admin/players/{player['id']}", headers=headers, json={"active": False}
        ).status_code
        == 200
    )
    assert anonymous_status() == 404
    assert client.get(content_url).headers["cache-control"] == "private, no-store"


def test_team_editor_coordinates_and_competition_impact(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, team_id = content_client
    team_path = f"/api/v1/admin/teams/{team_id}"
    team = client.get(team_path).json()["data"]
    assert (
        client.patch(
            team_path,
            headers=headers,
            json={
                "name": "Updated club",
                "expected_updated_at": team["updated_at"],
            },
        ).status_code
        == 200
    )
    assert client.get("/api/v1/teams").json()["items"][0]["name"] == "Updated club"
    assert (
        client.patch(
            team_path,
            headers=headers,
            json={
                "name": "Old",
                "expected_updated_at": team["updated_at"],
            },
        ).status_code
        == 409
    )
    venue = client.post(
        "/api/v1/admin/venues",
        headers=headers,
        json={
            "name": "Gym",
            "address": "Old address",
            "latitude": 55.5,
            "longitude": 37.5,
        },
    ).json()["data"]
    moved = client.patch(
        f"/api/v1/admin/venues/{venue['id']}", headers=headers, json={"address": "New address"}
    ).json()["data"]
    assert moved["latitude"] is None and moved["longitude"] is None
    competition = client.post(
        "/api/v1/admin/competitions", headers=headers, json={"name": "League", "season": "2026"}
    ).json()["data"]
    assert (
        client.post(
            "/api/v1/admin/matches",
            headers=headers,
            json={
                "team_id": team_id,
                "competition_id": competition["id"],
                "starts_at": "2026-12-01T12:00:00Z",
                "club_side": "home",
                "home_team_name": "Updated club",
                "away_team_name": "Rivals",
            },
        ).status_code
        == 201
    )
    impact = client.get(f"/api/v1/admin/competitions/{competition['id']}/impact").json()["data"]
    assert impact == {"matches_count": 1, "published_matches_count": 1}
    assert (
        client.get("/api/v1/admin/matches", params={"competition_id": competition["id"]}).json()[
            "total"
        ]
        == 1
    )


def test_edited_team_logo_reaches_match_player_achievement_and_standings_summaries(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, team_id = content_client
    image = BytesIO()
    Image.new("RGB", (20, 20), "orange").save(image, "PNG")
    asset = client.post(
        "/api/v1/admin/media",
        headers=headers,
        files={"file": ("logo.png", image.getvalue(), "image/png")},
    ).json()["data"]
    assert (
        client.patch(
            f"/api/v1/admin/teams/{team_id}", headers=headers, json={"logo_asset_id": asset["id"]}
        ).status_code
        == 200
    )
    competition = client.post(
        "/api/v1/admin/competitions",
        headers=headers,
        json={"name": "Logo league", "season": "2026"},
    ).json()["data"]
    match = client.post(
        "/api/v1/admin/matches",
        headers=headers,
        json={
            "team_id": team_id,
            "competition_id": competition["id"],
            "starts_at": "2026-12-01T12:00:00Z",
            "club_side": "home",
            "home_team_name": "Club",
            "away_team_name": "Visitors",
        },
    )
    player = client.post(
        "/api/v1/admin/players",
        headers=headers,
        json={"team_id": team_id, "full_name": "Logo player"},
    )
    achievement = client.post(
        "/api/v1/admin/achievements",
        headers=headers,
        json={"team_id": team_id, "title": "Logo honour", "media_asset_id": asset["id"]},
    )
    for response in (match, player, achievement):
        assert response.status_code == 201
        assert response.json()["data"]["team"]["logo"]["id"] == asset["id"]
    for endpoint in ("matches", "players", "achievements"):
        item = client.get(f"/api/v1/{endpoint}").json()["items"][0]
        assert item["team"]["logo"]["content_url"] == asset["content_url"]
    assert (
        client.get("/api/v1/home").json()["data"]["next_match"]["team"]["logo"]["id"] == asset["id"]
    )


def test_media_migration_preserves_old_urls_without_publicizing_future_inserts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "legacy.db"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    try:
        command.upgrade(config, "0014")
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO media_assets "
                "(id,storage_key,mime,size,checksum,created_at,updated_at) "
                "VALUES ('a','old.webp','image/webp',1,'old','2026-01-01','2026-01-01')"
            )
        command.upgrade(config, "head")
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT legacy_public FROM media_assets WHERE id='a'"
            ).fetchone() == (1,)
            connection.execute(
                "INSERT INTO media_assets "
                "(id,storage_key,mime,size,checksum,created_at,updated_at) "
                "VALUES ('b','new.webp','image/webp',1,'new','2026-01-01','2026-01-01')"
            )
            assert connection.execute(
                "SELECT legacy_public FROM media_assets WHERE id='b'"
            ).fetchone() == (0,)
    finally:
        get_settings.cache_clear()
