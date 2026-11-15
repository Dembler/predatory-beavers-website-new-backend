"""Automatic sync status is admin-only, bounded and reflects actual source schedules."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from predatory_beavers.api.main import create_app
from predatory_beavers.db.base import Base
from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.auth.models import User, UserRole
from predatory_beavers.modules.auth.service import PasswordSecurity
from predatory_beavers.settings import Settings

ENDPOINT = "/api/v1/admin/imports/sports-sync"
PASSWORD = "sync-status-test-password"
PRIVATE_KEY = "fiba-private-test-key-never-return-this"


@pytest.fixture
def sync_api(tmp_path: Path):
    settings = Settings(
        env="test",
        database_url="sqlite+aiosqlite:///" + (tmp_path / "status.sqlite").as_posix(),
        media_storage_path=tmp_path / "media",
        sports_sync_backend_root=tmp_path,
        sports_sync_enabled=True,
        sports_sync_interval_seconds=3600,
        sports_sync_fiba_interval_seconds=86_400,
        sports_sync_timeout_seconds=600,
        fiba_api_key=PRIVATE_KEY,
        cookie_secure=False,
        log_json=False,
    )

    async def seed() -> None:
        engine = create_engine(settings)
        session_factory = create_session_factory(engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        security = PasswordSecurity()
        password_hash = security.hash_password(PASSWORD)
        async with session_factory() as session:
            session.add_all(
                [
                    User(
                        username=f"sync-{role.value.lower()}",
                        email=f"sync-{role.value.lower()}@example.com",
                        password_hash=password_hash,
                        role=role,
                        is_active=True,
                    )
                    for role in [UserRole.ADMIN, UserRole.EDITOR]
                ]
            )
            await session.commit()
        await engine.dispose()

    asyncio.run(seed())
    with TestClient(create_app(settings)) as client:
        yield client, settings, tmp_path / "sports-sync" / "status.json"


def login(client: TestClient, role: str) -> None:
    response = client.post(
        "/api/v1/auth/login", json={"username": f"sync-{role}", "password": PASSWORD}
    )
    assert response.status_code == 200


def test_sync_status_rejects_unauthenticated_and_editor_sessions(sync_api):
    client, _, state_path = sync_api
    assert client.get(ENDPOINT).status_code == 401
    login(client, "editor")
    assert client.get(ENDPOINT).status_code == 403
    assert not state_path.exists()  # Test API startup never starts a real network importer.


def test_admin_sees_real_partial_state_and_separate_schedules_without_private_configuration(
    sync_api,
):
    client, settings, state_path = sync_api
    state = {
        "state": "partial",
        "attempt_started_at": "2026-11-15T09:00:00+00:00",
        "attempt_finished_at": "2026-11-15T09:01:00+00:00",
        "next_attempt_at": "2026-11-15T10:01:00+00:00",
        "last_success_at": "2026-11-14T08:00:00+00:00",
        "last_error": "Some official sources could not be refreshed",
        "sources": {
            "asb_games_men": {
                "status": "succeeded",
                "interval_seconds": 3600,
                "last_success_at": "2026-11-15T09:01:00+00:00",
                "next_attempt_at": "2026-11-15T10:01:00+00:00",
                "result": {"created": 1, "updated": 2},
            },
            "fiba3x3": {
                "status": "auth_required",
                "interval_seconds": 86_400,
                "last_success_at": None,
                "next_attempt_at": "2026-11-16T09:01:00+00:00",
            },
        },
        # The API must select public status fields instead of returning the entire file.
        "database": settings.database_url,
        "backend_root": str(settings.sports_sync_backend_root),
        "fiba_api_key": PRIVATE_KEY,
        "command": "private invocation details",
    }
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    login(client, "admin")
    response = client.get(ENDPOINT)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["enabled"] is True
    assert data["interval_seconds"] == 3600
    assert data["fiba_interval_seconds"] == 86_400
    assert data["timeout_seconds"] == 600
    for field in [
        "state",
        "attempt_started_at",
        "attempt_finished_at",
        "next_attempt_at",
        "last_success_at",
        "last_error",
        "sources",
    ]:
        assert data[field] == state[field]
    assert PRIVATE_KEY not in response.text
    assert str(settings.sports_sync_backend_root) not in response.text
    assert settings.sports_sync_backend_root.name not in response.text
    assert "status.sqlite" not in response.text
    assert "private invocation details" not in response.text
    assert "password" not in response.text.lower()


@pytest.mark.parametrize(
    "contents",
    [
        None,
        b"{not-json",
        b"[]",
        b"\xff",
        json.dumps({"state": "running", "sources": {}, "padding": "x" * 262_145}).encode(),
    ],
    ids=["missing", "malformed", "nonobject", "invalid-utf8", "oversized"],
)
def test_missing_malformed_nonobject_or_oversized_state_is_safe_idle(sync_api, contents):
    client, _, state_path = sync_api
    if contents is not None:
        state_path.parent.mkdir(parents=True)
        state_path.write_bytes(contents)
    login(client, "admin")
    response = client.get(ENDPOINT)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["state"] == "idle"
    assert data["sources"] == {}
    assert data["last_success_at"] is None
    assert data["attempt_started_at"] is None
    assert data["last_error"] is None
    assert data["interval_seconds"] == 3600
    assert data["fiba_interval_seconds"] == 86_400
    assert PRIVATE_KEY not in response.text
