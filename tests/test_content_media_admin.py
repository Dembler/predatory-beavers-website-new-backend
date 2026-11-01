import asyncio
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from predatory_beavers.api.main import create_app
from predatory_beavers.db.base import Base
from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.auth.models import User, UserRole
from predatory_beavers.modules.auth.service import PasswordSecurity
from predatory_beavers.modules.club.models import Team, TeamCategory
from predatory_beavers.settings import Settings


@pytest.fixture
def content_client(tmp_path: Path) -> tuple[TestClient, dict[str, str], str]:
    settings = Settings(
        env="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'content.sqlite').as_posix()}",
        media_storage_path=tmp_path / "media",
        cookie_secure=False,
        log_json=False,
    )

    async def seed() -> str:
        engine = create_engine(settings)
        factory = create_session_factory(engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            team = Team(slug="men", name="Beavers", category=TeamCategory.MEN)
            session.add_all(
                [
                    team,
                    User(
                        username="editor",
                        email="editor@example.test",
                        role=UserRole.EDITOR,
                        password_hash=PasswordSecurity().hash_password("strong-test-password"),
                        is_active=True,
                    ),
                ]
            )
            await session.commit()
            team_id = str(team.id)
        await engine.dispose()
        return team_id

    team_id = asyncio.run(seed())
    with TestClient(create_app(settings)) as client:
        assert (
            client.post(
                "/api/v1/auth/login",
                json={
                    "username": "editor",
                    "password": "strong-test-password",
                },
            ).status_code
            == 200
        )
        csrf = client.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
        yield client, {settings.csrf_header_name: csrf}, team_id


def test_content_crud_public_visibility_order_and_csrf(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, _ = content_client
    endpoint = "/api/v1/admin/content"
    payload = {
        "section": "history",
        "title": "  Our club  ",
        "body": "First line\n\n<script>window.__e2eUnsafe = true</script>",
    }
    assert client.post(endpoint, json=payload).status_code == 403
    first = client.post(endpoint, json=payload, headers=headers)
    assert first.status_code == 201
    first_id = first.json()["data"]["id"]
    assert first.json()["data"]["title"] == "Our club"
    assert first.json()["data"]["body"] == payload["body"]
    assert client.get(f"{endpoint}/{first_id}").json()["data"]["title"] == "Our club"
    second = client.post(
        endpoint, json={**payload, "title": "Draft", "active": False}, headers=headers
    )
    assert second.status_code == 201
    second_id = second.json()["data"]["id"]
    rule = client.post(endpoint, json={**payload, "section": "rules"}, headers=headers)
    assert rule.status_code == 201
    assert client.get(endpoint + "?section=history&page_size=1").json()["total"] == 2
    public = client.get("/api/v1/content/history").json()["data"]["items"]
    assert [item["id"] for item in public] == [first_id]

    assert (
        client.patch(
            f"{endpoint}/{second_id}",
            json={
                "active": True,
                "sort_order": 0,
            },
            headers=headers,
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"{endpoint}/{first_id}",
            json={
                "sort_order": 2,
                "body": "Updated history",
            },
            headers=headers,
        ).status_code
        == 200
    )
    public = client.get("/api/v1/content/history").json()["data"]["items"]
    assert [item["id"] for item in public] == [second_id, first_id]
    assert public[1]["body"] == "Updated history"
    for invalid in ({"title": "  "}, {"body": None}, {"active": None}, {"section": "bad"}):
        assert (
            client.patch(f"{endpoint}/{first_id}", json=invalid, headers=headers).status_code == 422
        )
    assert client.get("/api/v1/content/bad").status_code == 422
    assert client.delete(f"{endpoint}/{first_id}", headers=headers).status_code == 204
    assert client.get(f"{endpoint}/{first_id}").status_code == 404
    assert client.delete(f"{endpoint}/{first_id}", headers=headers).status_code == 404
    assert client.get(endpoint + "?section=history").json()["total"] == 1
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
    assert client.get(endpoint).status_code == 401
    assert client.get("/api/v1/content/history").status_code == 200


def test_media_library_references_and_reupload(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, team_id = content_client
    output = BytesIO()
    Image.new("RGB", (60, 100), "green").save(output, "PNG")
    content = output.getvalue()

    def upload() -> dict[str, object]:
        response = client.post(
            "/api/v1/admin/media",
            headers=headers,
            files={"file": ("photo.png", content, "image/png")},
        )
        assert response.status_code == 201
        return response.json()["data"]

    media = upload()
    media_id = media["id"]
    endpoint = f"/api/v1/admin/media/{media_id}"
    assert client.get("/api/v1/admin/media?page_size=1").json()["total"] == 1
    assert upload()["id"] == media_id
    assert (
        client.patch(endpoint, json={"alt_text": "  Player portrait  "}, headers=headers).json()[
            "data"
        ]["alt_text"]
        == "Player portrait"
    )
    assert client.delete(endpoint).status_code == 403
    player = client.post(
        "/api/v1/admin/players",
        headers=headers,
        json={
            "team_id": team_id,
            "full_name": "Test Player",
            "photo_asset_id": media_id,
        },
    )
    assert player.status_code == 201
    player_id = player.json()["data"]["id"]
    assert client.delete(endpoint, headers=headers).status_code == 409
    assert client.get(str(media["content_url"])).status_code == 200
    assert (
        client.patch(
            f"/api/v1/admin/players/{player_id}", headers=headers, json={"photo_asset_id": None}
        ).status_code
        == 200
    )
    assert client.delete(endpoint, headers=headers).status_code == 204
    assert client.get(endpoint).status_code == 404
    assert client.get(str(media["content_url"])).status_code == 404
    assert client.get("/api/v1/admin/media").json()["total"] == 0
    # A deleted checksum must restore the immutable image rather than violate UNIQUE.
    assert upload()["id"] == media_id
    assert client.get(str(media["content_url"])).status_code == 200
    assert client.get("/api/v1/admin/media").json()["total"] == 1

    competition = client.post(
        "/api/v1/admin/competitions",
        headers=headers,
        json={
            "name": "Test League",
            "season": "2026-2027",
        },
    ).json()["data"]
    match = client.post(
        "/api/v1/admin/matches",
        headers=headers,
        json={
            "team_id": team_id,
            "competition_id": competition["id"],
            "starts_at": "2026-11-20T10:00:00Z",
            "club_side": "home",
            "home_team_name": "Beavers",
            "away_team_name": "Visitors",
            "home_logo_asset_id": media_id,
        },
    )
    assert match.status_code == 201
    assert client.delete(endpoint, headers=headers).status_code == 409
    match_id = match.json()["data"]["id"]
    assert (
        client.patch(
            f"/api/v1/admin/matches/{match_id}",
            headers=headers,
            json={
                "home_logo_asset_id": None,
                "away_logo_asset_id": media_id,
            },
        ).status_code
        == 200
    )
    assert client.delete(endpoint, headers=headers).status_code == 409
    assert client.delete(f"/api/v1/admin/matches/{match_id}", headers=headers).status_code == 204
    assert client.delete(endpoint, headers=headers).status_code == 204


def test_concurrent_duplicate_uploads_reuse_one_asset(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, _ = content_client
    output = BytesIO()
    Image.new("RGB", (300, 500), "orange").save(output, "PNG")
    content = output.getvalue()
    barrier = Barrier(4)

    def upload(_: int) -> tuple[int, str]:
        barrier.wait(timeout=10)
        response = client.post(
            "/api/v1/admin/media",
            headers=headers,
            files={"file": ("same.png", content, "image/png")},
        )
        return response.status_code, response.json().get("data", {}).get("id", "")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(upload, range(4)))
    assert all(status == 201 for status, _ in results)
    assert len({asset_id for _, asset_id in results}) == 1
    assert client.get("/api/v1/admin/media").json()["total"] == 1


def test_upload_auth_precedes_parsing_and_transport_is_bounded(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, _ = content_client
    endpoint = "/api/v1/admin/media"
    # Invalid multipart must not reach parsing before the CSRF check.
    assert (
        client.post(
            endpoint,
            content=b"malformed multipart",
            headers={
                "Content-Type": "multipart/form-data",
            },
        ).status_code
        == 403
    )
    client.app.state.settings.media_max_upload_bytes = 1024
    response = client.post(
        endpoint, headers=headers, files={"file": ("big.png", b"x" * 1025, "image/png")}
    )
    assert response.status_code == 413
    assert response.json()["code"] == "media_too_large"
    response = client.post(
        endpoint, headers=headers, files={"file": ("huge.png", b"x" * (70 * 1024), "image/png")}
    )
    assert response.status_code == 413
    assert response.json()["code"] == "media_too_large"
    output = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(output, "PNG")
    content = output.getvalue()
    assert (
        client.post(
            endpoint,
            headers=headers,
            files=[
                ("file", ("one.png", content, "image/png")),
                ("second", ("two.png", content, "image/png")),
            ],
        ).status_code
        == 422
    )
    assert (
        client.post(
            endpoint,
            headers=headers,
            files={"file": ("one.png", content, "image/png")},
            data={"alt_text": "x" * 501},
        ).status_code
        == 422
    )
    assert client.get(endpoint).json()["total"] == 0
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
    assert (
        client.post(
            endpoint,
            content=b"malformed multipart",
            headers={
                "Content-Type": "multipart/form-data",
            },
        ).status_code
        == 401
    )


def test_player_sort_order_rejects_overflow_without_changing_the_record(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, team_id = content_client
    endpoint = "/api/v1/admin/players"
    payload = {"team_id": team_id, "full_name": "Sort Order Test", "sort_order": 2**100}
    assert client.post(endpoint, json=payload, headers=headers).status_code == 422
    assert client.get(endpoint).json()["total"] == 0
    created = client.post(endpoint, json={**payload, "sort_order": 7}, headers=headers)
    assert created.status_code == 201
    player_id = created.json()["data"]["id"]
    assert (
        client.patch(
            f"{endpoint}/{player_id}", json={"sort_order": 2**100}, headers=headers
        ).status_code
        == 422
    )
    assert client.get(f"{endpoint}/{player_id}").json()["data"]["sort_order"] == 7
