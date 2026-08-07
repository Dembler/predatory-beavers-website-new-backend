import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from predatory_beavers.api.main import create_app
from predatory_beavers.db.base import Base
from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.auth.models import User, UserRole
from predatory_beavers.modules.auth.service import PasswordSecurity
from predatory_beavers.modules.club.models import Team, TeamCategory
from predatory_beavers.settings import Settings


def test_live_health_and_security_headers() -> None:
    settings = Settings(
        env="test",
        database_url="sqlite+aiosqlite:///:memory:",
        cookie_secure=False,
        log_json=False,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/health/live", headers={"X-Request-ID": "test-request"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"] == "test-request"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_openapi_contains_club_auth_and_admin_routes() -> None:
    settings = Settings(
        env="test",
        database_url="sqlite+aiosqlite:///:memory:",
        cookie_secure=False,
        log_json=False,
    )
    with TestClient(create_app(settings)) as client:
        document = client.get("/openapi.json").json()

    paths = document["paths"]
    assert "/api/v1/auth/login" in paths
    assert "/api/v1/teams" in paths
    assert "/api/v1/players" in paths
    assert "/api/v1/admin/players" in paths


def test_admin_mutation_requires_session() -> None:
    settings = Settings(
        env="test",
        database_url="sqlite+aiosqlite:///:memory:",
        cookie_secure=False,
        log_json=False,
    )
    payload = {
        "team_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
        "full_name": "Тестовый Игрок",
    }
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/admin/players", json=payload)

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_session"
    assert response.json()["request_id"]


def test_unknown_route_uses_unified_error_shape() -> None:
    settings = Settings(
        env="test",
        database_url="sqlite+aiosqlite:///:memory:",
        cookie_secure=False,
        log_json=False,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/missing")

    assert response.status_code == 404
    assert response.json()["status"] == "error"
    assert response.json()["code"] == "http_404"
    assert response.json()["request_id"]


def test_login_csrf_admin_and_logout_flow(tmp_path: Path) -> None:
    database_path = (tmp_path / "api-flow.sqlite").as_posix()
    settings = Settings(
        env="test",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        cookie_secure=False,
        log_json=False,
    )

    async def seed_database() -> str:
        engine = create_engine(settings)
        session_factory = create_session_factory(engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            team = Team(
                slug="men",
                name="Хищные Бобры — мужская команда",
                category=TeamCategory.MEN,
                active=True,
            )
            user = User(
                username="editor",
                email="editor@example.com",
                password_hash=PasswordSecurity().hash_password("very-strong-password"),
                role=UserRole.EDITOR,
                is_active=True,
            )
            session.add_all([team, user])
            await session.commit()
            team_id = str(team.id)
        await engine.dispose()
        return team_id

    team_id = asyncio.run(seed_database())
    with TestClient(create_app(settings)) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "editor", "password": "very-strong-password"},
        )
        assert login.status_code == 200
        assert settings.session_cookie_name in login.cookies
        csrf_token = login.json()["data"]["csrf_token"]

        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["data"]["role"] == "EDITOR"

        player_payload = {"team_id": team_id, "full_name": "Новый Игрок"}
        no_csrf = client.post("/api/v1/admin/players", json=player_payload)
        assert no_csrf.status_code == 403
        assert no_csrf.json()["code"] == "invalid_csrf_token"

        created = client.post(
            "/api/v1/admin/players",
            json=player_payload,
            headers={settings.csrf_header_name: csrf_token},
        )
        assert created.status_code == 201
        assert created.json()["data"]["full_name"] == "Новый Игрок"

        logout = client.post(
            "/api/v1/auth/logout",
            headers={settings.csrf_header_name: csrf_token},
        )
        assert logout.status_code == 200
        assert client.get("/api/v1/auth/me").status_code == 401
