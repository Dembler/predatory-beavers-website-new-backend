import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from predatory_beavers.api.main import create_app
from predatory_beavers.db.base import Base
from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.auth.models import User, UserRole
from predatory_beavers.modules.auth.service import PasswordSecurity
from predatory_beavers.modules.club.models import Team, TeamCategory
from predatory_beavers.modules.standings.schemas import StandingsPublish
from predatory_beavers.settings import Settings


def _standing_rows(*, club_points: int = 4) -> list[dict[str, object]]:
    return [
        {
            "position": 2,
            "team_name": "Соперники",
            "played": 2,
            "wins": 1,
            "losses": 1,
            "draws": 0,
            "table_points": 3,
            "points_for": 145,
            "points_against": 147,
        },
        {
            "position": 1,
            "team_name": "Хищные Бобры",
            "external_team_id": "7433",
            "played": 2,
            "wins": 2,
            "losses": 0,
            "draws": 0,
            "table_points": club_points,
            "points_for": 164,
            "points_against": 139,
        },
    ]


def test_publish_standings_replaces_current_snapshot_and_keeps_history(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "standings.sqlite").as_posix()
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
                name="Хищные Бобры",
                category=TeamCategory.MEN,
                active=True,
            )
            user = User(
                username="standings-editor",
                email="standings@example.com",
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
            json={
                "username": "standings-editor",
                "password": "very-strong-password",
            },
        )
        assert login.status_code == 200
        csrf_token = client.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
        auth_headers = {settings.csrf_header_name: csrf_token}

        competition = client.post(
            "/api/v1/admin/competitions",
            json={"name": "Московская лига", "season": "2026-2027"},
            headers=auth_headers,
        )
        assert competition.status_code == 201
        competition_id = competition.json()["data"]["id"]

        payload = {
            "team_id": team_id,
            "competition_id": competition_id,
            "rows": _standing_rows(),
        }
        first = client.post(
            "/api/v1/admin/standings",
            json=payload,
            headers=auth_headers,
        )
        assert first.status_code == 201
        first_data = first.json()["data"]
        assert first_data["is_current"] is True
        assert [row["position"] for row in first_data["rows"]] == [1, 2]

        public = client.get("/api/v1/standings?team=men&season=2026-2027")
        assert public.status_code == 200
        assert public.json()["data"]["id"] == first_data["id"]
        assert public.json()["data"]["competition"]["name"] == "Московская лига"

        payload["rows"] = _standing_rows(club_points=5)
        second = client.post(
            "/api/v1/admin/standings",
            json=payload,
            headers=auth_headers,
        )
        assert second.status_code == 201
        second_data = second.json()["data"]
        assert second_data["id"] != first_data["id"]
        assert second_data["rows"][0]["table_points"] == 5

        archived = client.get("/api/v1/admin/standings?current=false")
        assert archived.status_code == 200
        assert archived.json()["total"] == 1
        assert archived.json()["items"][0]["id"] == first_data["id"]

        current = client.get("/api/v1/admin/standings?current=true")
        assert current.status_code == 200
        assert current.json()["total"] == 1
        assert current.json()["items"][0]["id"] == second_data["id"]

        public_after_replace = client.get("/api/v1/standings?team=men")
        assert public_after_replace.status_code == 200
        assert public_after_replace.json()["data"]["id"] == second_data["id"]

        deleted = client.delete(
            f"/api/v1/admin/standings/{second_data['id']}",
            headers=auth_headers,
        )
        assert deleted.status_code == 204
        assert client.get("/api/v1/standings?team=men").status_code == 404


def test_standings_payload_rejects_inconsistent_rows() -> None:
    invalid_results = _standing_rows()
    invalid_results[0]["played"] = 3
    with pytest.raises(ValidationError, match="add up to played"):
        StandingsPublish(
            team_id="6ba7b810-9dad-11d1-80b4-00c04fd430c8",  # type: ignore[arg-type]
            competition_id="6ba7b810-9dad-11d1-80b4-00c04fd430c9",  # type: ignore[arg-type]
            rows=invalid_results,  # type: ignore[arg-type]
        )

    invalid_positions = _standing_rows()
    invalid_positions[0]["position"] = 3
    with pytest.raises(ValidationError, match="contiguous"):
        StandingsPublish(
            team_id="6ba7b810-9dad-11d1-80b4-00c04fd430c8",  # type: ignore[arg-type]
            competition_id="6ba7b810-9dad-11d1-80b4-00c04fd430c9",  # type: ignore[arg-type]
            rows=invalid_positions,  # type: ignore[arg-type]
        )
