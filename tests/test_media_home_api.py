import asyncio
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from predatory_beavers.api.main import create_app
from predatory_beavers.db.base import Base
from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.auth.models import User, UserRole
from predatory_beavers.modules.auth.service import PasswordSecurity
from predatory_beavers.modules.club.models import Team, TeamCategory
from predatory_beavers.settings import Settings


def _png_bytes(width: int = 320, height: int = 180) -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, height), color=(22, 56, 44)).save(output, format="PNG")
    return output.getvalue()


def test_media_achievement_and_home_flow(tmp_path: Path) -> None:
    database_path = (tmp_path / "media-home.sqlite").as_posix()
    settings = Settings(
        env="test",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        media_storage_path=tmp_path / "media",
        media_max_dimension=256,
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
                name="Хищные Бобры",
                category=TeamCategory.MEN,
                active=True,
            )
            user = User(
                username="content-editor",
                email="content@example.com",
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
            json={"username": "content-editor", "password": "very-strong-password"},
        )
        assert login.status_code == 200
        csrf_token = client.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
        auth_headers = {settings.csrf_header_name: csrf_token}

        uploaded = client.post(
            "/api/v1/admin/media",
            files={"file": ("achievement.png", _png_bytes(), "image/png")},
            data={"alt_text": "Кубок Хищных Бобров"},
            headers=auth_headers,
        )
        assert uploaded.status_code == 201
        media = uploaded.json()["data"]
        assert media["mime"] == "image/webp"
        assert media["width"] == 256
        assert media["height"] == 144
        assert "storage_key" not in media
        assert "checksum" not in media
        assert media["content_url"] == f"/media/{media['id']}/content"

        content = client.get(media["content_url"])
        assert content.status_code == 200
        assert content.headers["Content-Type"] == "image/webp"
        assert content.headers["Cache-Control"] == "public, max-age=31536000, immutable"
        assert content.headers["ETag"]
        assert content.content.startswith(b"RIFF")
        legacy_content = client.get(f"/api/v1/media/{media['id']}/content")
        assert legacy_content.status_code == 200

        achievement = client.post(
            "/api/v1/admin/achievements",
            json={
                "team_id": team_id,
                "title": "Победители сезона",
                "media_asset_id": media["id"],
                "achieved_at": "2026-05-20",
            },
            headers=auth_headers,
        )
        assert achievement.status_code == 201
        assert achievement.json()["data"]["media"]["content_url"] == media["content_url"]

        public_achievements = client.get("/api/v1/achievements?team=men")
        assert public_achievements.status_code == 200
        assert public_achievements.json()["total"] == 1

        competition = client.post(
            "/api/v1/admin/competitions",
            json={"name": "Московская лига", "season": "2026-2027"},
            headers=auth_headers,
        )
        assert competition.status_code == 201
        competition_id = competition.json()["data"]["id"]
        now = datetime.now(UTC)

        finished = client.post(
            "/api/v1/admin/matches",
            json={
                "team_id": team_id,
                "competition_id": competition_id,
                "starts_at": (now - timedelta(days=1)).isoformat(),
                "club_side": "home",
                "home_team_name": "Хищные Бобры",
                "away_team_name": "Соперники",
                "status": "finished",
                "home_score": 82,
                "away_score": 77,
            },
            headers=auth_headers,
        )
        assert finished.status_code == 201

        upcoming = client.post(
            "/api/v1/admin/matches",
            json={
                "team_id": team_id,
                "competition_id": competition_id,
                "starts_at": (now + timedelta(days=2)).isoformat(),
                "club_side": "away",
                "home_team_name": "Соперники",
                "away_team_name": "Хищные Бобры",
                "status": "scheduled",
            },
            headers=auth_headers,
        )
        assert upcoming.status_code == 201

        home = client.get("/api/v1/home")
        assert home.status_code == 200
        home_data = home.json()["data"]
        assert home_data["next_match"]["id"] == upcoming.json()["data"]["id"]
        assert home_data["recent_results"][0]["id"] == finished.json()["data"]["id"]
        assert [team["slug"] for team in home_data["teams"]] == ["men"]


def test_media_rejects_non_image(tmp_path: Path) -> None:
    database_path = (tmp_path / "bad-media.sqlite").as_posix()
    settings = Settings(
        env="test",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        media_storage_path=tmp_path / "media",
        cookie_secure=False,
        log_json=False,
    )

    async def seed_database() -> None:
        engine = create_engine(settings)
        session_factory = create_session_factory(engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            session.add(
                User(
                    username="editor",
                    email="editor@example.com",
                    password_hash=PasswordSecurity().hash_password("very-strong-password"),
                    role=UserRole.EDITOR,
                    is_active=True,
                )
            )
            await session.commit()
        await engine.dispose()

    asyncio.run(seed_database())
    with TestClient(create_app(settings)) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": "editor", "password": "very-strong-password"},
        )
        csrf_token = client.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
        response = client.post(
            "/api/v1/admin/media",
            files={"file": ("fake.png", b"not an image", "image/png")},
            headers={settings.csrf_header_name: csrf_token},
        )

    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_image"
