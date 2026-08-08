import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from predatory_beavers.api.main import create_app
from predatory_beavers.db.base import Base
from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.auth.models import User, UserRole
from predatory_beavers.modules.auth.service import PasswordSecurity
from predatory_beavers.modules.club.models import Team, TeamCategory
from predatory_beavers.settings import Settings


def test_admin_audit_log_captures_mutations_and_denies_editor(tmp_path: Path) -> None:
    database_path = (tmp_path / "audit.sqlite").as_posix()
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
        password_security = PasswordSecurity()
        async with session_factory() as session:
            team = Team(
                slug="men",
                name="Хищные Бобры",
                category=TeamCategory.MEN,
                active=True,
            )
            admin = User(
                username="audit-admin",
                email="audit-admin@example.com",
                password_hash=password_security.hash_password("very-strong-password"),
                role=UserRole.ADMIN,
                is_active=True,
            )
            editor = User(
                username="audit-editor",
                email="audit-editor@example.com",
                password_hash=password_security.hash_password("very-strong-password"),
                role=UserRole.EDITOR,
                is_active=True,
            )
            session.add_all([team, admin, editor])
            await session.commit()
            team_id = str(team.id)
        await engine.dispose()
        return team_id

    team_id = asyncio.run(seed_database())
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/admin/audit-log").status_code == 401

        login = client.post(
            "/api/v1/auth/login",
            json={"username": "audit-admin", "password": "very-strong-password"},
        )
        assert login.status_code == 200
        csrf_token = client.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
        auth_headers = {settings.csrf_header_name: csrf_token}

        created = client.post(
            "/api/v1/admin/players",
            json={"team_id": team_id, "full_name": "Первое Имя"},
            headers={**auth_headers, "X-Request-ID": "audit-create"},
        )
        assert created.status_code == 201
        player_id = created.json()["data"]["id"]

        updated = client.patch(
            f"/api/v1/admin/players/{player_id}",
            json={"full_name": "Новое Имя"},
            headers={**auth_headers, "X-Request-ID": "audit-update"},
        )
        assert updated.status_code == 200

        deleted = client.delete(
            f"/api/v1/admin/players/{player_id}",
            headers={**auth_headers, "X-Request-ID": "audit-delete"},
        )
        assert deleted.status_code == 204

        audit = client.get(f"/api/v1/admin/audit-log?entity_type=player&entity_id={player_id}")
        assert audit.status_code == 200
        body = audit.json()
        assert body["total"] == 3
        entries = {entry["action"]: entry for entry in body["items"]}

        create_entry = entries["create"]
        assert create_entry["actor_username"] == "audit-admin"
        assert create_entry["actor_role"] == "ADMIN"
        assert create_entry["before"] is None
        assert create_entry["after"]["full_name"] == "Первое Имя"
        assert create_entry["request_id"] == "audit-create"

        update_entry = entries["update"]
        assert update_entry["before"]["full_name"] == "Первое Имя"
        assert update_entry["after"]["full_name"] == "Новое Имя"
        assert update_entry["request_id"] == "audit-update"

        delete_entry = entries["delete"]
        assert delete_entry["before"]["full_name"] == "Новое Имя"
        assert delete_entry["after"] is None
        assert delete_entry["request_id"] == "audit-delete"
        assert "password" not in json.dumps(body).lower()

        import_payload = {
            "team": "men",
            "competition_id": "48154",
            "standings_competition_id": "48637",
            "external_team_id": "7433",
            "season": "2024-2025",
        }
        disabled_import = client.post(
            "/api/v1/admin/imports/asb",
            json=import_payload,
            headers=auth_headers,
        )
        assert disabled_import.status_code == 503
        assert disabled_import.json()["code"] == "asb_disabled"
        import_jobs = client.get("/api/v1/admin/imports")
        assert import_jobs.status_code == 200
        assert import_jobs.json()["total"] == 1
        assert import_jobs.json()["items"][0]["status"] == "failed"
        assert import_jobs.json()["items"][0]["error_code"] == "asb_disabled"

        logged_out = client.post(
            "/api/v1/auth/logout",
            headers=auth_headers,
        )
        assert logged_out.status_code == 200
        editor_login = client.post(
            "/api/v1/auth/login",
            json={"username": "audit-editor", "password": "very-strong-password"},
        )
        assert editor_login.status_code == 200
        forbidden = client.get("/api/v1/admin/audit-log")
        assert forbidden.status_code == 403
        editor_csrf = client.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
        forbidden_import = client.post(
            "/api/v1/admin/imports/asb",
            json=import_payload,
            headers={settings.csrf_header_name: editor_csrf},
        )
        assert forbidden_import.status_code == 403
