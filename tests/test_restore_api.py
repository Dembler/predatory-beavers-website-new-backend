import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.club.models import Team, TeamCategory
from predatory_beavers.settings import Settings

pytest_plugins = ["test_content_media_admin"]


@pytest.mark.parametrize(
    ("entity", "parent"),
    [
        ("players", "teams"),
        ("achievements", "teams"),
        ("matches", "teams"),
        ("matches", "competitions"),
        ("matches", "venues"),
    ],
)
def test_active_records_cannot_be_created_or_moved_beneath_hidden_parents(
    content_client: tuple[TestClient, dict[str, str], str],
    tmp_path: Path,
    entity: str,
    parent: str,
) -> None:
    client, headers, team_id = content_client

    async def hidden_team() -> str:
        settings = Settings(
            env="test",
            database_url=f"sqlite+aiosqlite:///{(tmp_path / 'content.sqlite').as_posix()}",
        )
        engine = create_engine(settings)
        factory = create_session_factory(engine)
        async with factory() as session:
            team = Team(slug="women", name="Hidden team", category=TeamCategory.WOMEN, active=False)
            session.add(team)
            await session.commit()
            result = str(team.id)
        await engine.dispose()
        return result

    hidden_team_id = asyncio.run(hidden_team())
    competition = client.post(
        "/api/v1/admin/competitions",
        headers=headers,
        json={"name": "Published league", "season": "2026"},
    ).json()["data"]
    hidden_competition = client.post(
        "/api/v1/admin/competitions",
        headers=headers,
        json={"name": "Hidden league", "season": "2026", "active": False},
    ).json()["data"]
    venue = client.post(
        "/api/v1/admin/venues",
        headers=headers,
        json={"name": "Published gym", "address": "Address"},
    ).json()["data"]
    hidden_venue = client.post(
        "/api/v1/admin/venues",
        headers=headers,
        json={"name": "Hidden gym", "address": "Address", "active": False},
    ).json()["data"]
    image = BytesIO()
    Image.new("RGB", (10, 10), "orange").save(image, "PNG")
    asset = client.post(
        "/api/v1/admin/media",
        headers=headers,
        files={"file": ("photo.png", image.getvalue(), "image/png")},
    ).json()["data"]
    payloads = {
        "players": {"full_name": "Published player", "team_id": team_id},
        "achievements": {
            "title": "Published achievement",
            "team_id": team_id,
            "media_asset_id": asset["id"],
        },
        "matches": {
            "team_id": team_id,
            "competition_id": competition["id"],
            "venue_id": venue["id"],
            "starts_at": "2026-12-01T12:00:00Z",
            "club_side": "home",
            "home_team_name": "Club",
            "away_team_name": "Visitors",
        },
    }
    field, public_parent_id, hidden_parent_id = {
        "teams": ("team_id", team_id, hidden_team_id),
        "competitions": ("competition_id", competition["id"], hidden_competition["id"]),
        "venues": ("venue_id", venue["id"], hidden_venue["id"]),
    }[parent]
    endpoint = f"/api/v1/admin/{entity}"
    public_payload = payloads[entity]
    source = client.post(endpoint, headers=headers, json=public_payload).json()["data"]
    hidden_payload = {**public_payload, field: hidden_parent_id}
    for explicit_active in ({}, {"active": True}):
        blocked = client.post(endpoint, headers=headers, json={**hidden_payload, **explicit_active})
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["code"] == "publication_dependency"
    assert client.get(endpoint).json()["total"] == 1
    # Existing active records retain effective publication when PATCH omits active.
    source_url = f"{endpoint}/{source['id']}"
    blocked = client.patch(source_url, headers=headers, json={field: hidden_parent_id})
    assert blocked.status_code == 409 and blocked.json()["code"] == "publication_dependency"
    assert client.get(source_url).json()["data"][field] == public_parent_id
    # Both new hidden drafts and an explicit unpublish-and-move are allowed.
    draft = client.post(endpoint, headers=headers, json={**hidden_payload, "active": False})
    assert draft.status_code == 201, draft.text
    assert draft.json()["data"]["active"] is False
    assert (
        client.patch(
            source_url, headers=headers, json={field: hidden_parent_id, "active": False}
        ).status_code
        == 200
    )
    text_field = {"players": "position", "achievements": "title", "matches": "notes"}[entity]
    assert (
        client.patch(source_url, headers=headers, json={text_field: "Draft edit"}).status_code
        == 200
    )
    blocked = client.patch(source_url, headers=headers, json={"active": True})
    assert blocked.status_code == 409 and blocked.json()["code"] == "publication_dependency"
    # A hidden team/competition still blocks editing a published child. An
    # already associated venue may be hidden without unpublishing the match.
    another = client.post(endpoint, headers=headers, json=public_payload).json()["data"]
    assert (
        client.patch(
            f"/api/v1/admin/{parent}/{public_parent_id}", headers=headers, json={"active": False}
        ).status_code
        == 200
    )
    another_url = f"{endpoint}/{another['id']}"
    blocked = client.patch(
        another_url, headers=headers, json={text_field: "Keep active implicitly"}
    )
    if parent == "venues":
        assert blocked.status_code == 200, blocked.text
        assert blocked.json()["data"]["active"] is True
        assert blocked.json()["data"]["venue_id"] == public_parent_id
    else:
        assert blocked.status_code == 409 and blocked.json()["code"] == "publication_dependency"
    assert (
        client.patch(
            another_url, headers=headers, json={text_field: "Hidden edit", "active": False}
        ).status_code
        == 200
    )


def _delete_and_version(
    client: TestClient, headers: dict[str, str], entity: str, record_id: str
) -> str:
    assert client.delete(f"/api/v1/admin/{entity}/{record_id}", headers=headers).status_code == 204
    trash = client.get(f"/api/v1/admin/trash/{entity}")
    assert trash.status_code == 200, trash.text
    return next(item["updated_at"] for item in trash.json()["items"] if item["id"] == record_id)


@pytest.mark.parametrize(
    "entity", ["players", "content", "competitions", "venues", "matches", "achievements", "media"]
)
def test_restore_keeps_identity_unpublishes_and_retries_safely(
    content_client: tuple[TestClient, dict[str, str], str],
    entity: str,
) -> None:
    client, headers, team_id = content_client
    competition = client.post(
        "/api/v1/admin/competitions",
        headers=headers,
        json={"name": "Restore league", "season": "2026"},
    ).json()["data"]
    image = BytesIO()
    Image.new("RGB", (10, 10), "purple").save(image, "PNG")
    asset = client.post(
        "/api/v1/admin/media",
        headers=headers,
        files={"file": ("photo.png", image.getvalue(), "image/png")},
    ).json()["data"]
    payloads = {
        "players": {"full_name": "Игрок в корзине", "team_id": team_id},
        "content": {
            "section": "history",
            "title": "История из корзины",
            "body": "Preserved content",
        },
        "competitions": {"name": "Удалённый турнир", "season": "2026"},
        "venues": {"name": "Удалённый зал", "address": "Preserved address"},
        "matches": {
            "team_id": team_id,
            "competition_id": competition["id"],
            "starts_at": "2026-12-01T12:00:00Z",
            "club_side": "home",
            "home_team_name": "Клуб",
            "away_team_name": "Гости",
        },
        "achievements": {
            "team_id": team_id,
            "title": "Фото достижения",
            "media_asset_id": asset["id"],
        },
    }
    record = (
        asset
        if entity == "media"
        else client.post(f"/api/v1/admin/{entity}", headers=headers, json=payloads[entity]).json()[
            "data"
        ]
    )
    record_id = record["id"]
    version = _delete_and_version(client, headers, entity, record_id)
    restore_url = f"/api/v1/admin/trash/{entity}/{record_id}/restore"
    payload = {"expected_updated_at": version}
    assert client.post(restore_url, json=payload).status_code == 403
    assert client.post(restore_url, headers=headers, json={}).status_code == 422
    assert (
        client.post(
            restore_url, headers=headers, json={"expected_updated_at": "2000-01-01T00:00:00Z"}
        ).status_code
        == 409
    )
    restored = client.post(restore_url, headers=headers, json=payload)
    assert restored.status_code == 200, restored.text
    assert restored.json()["data"]["id"] == record_id
    assert restored.json()["data"]["restored"] is True
    saved = client.get(f"/api/v1/admin/{entity}/{record_id}").json()["data"]
    if entity != "media":
        assert saved["active"] is False
    if entity == "matches":
        assert client.get(f"/api/v1/matches/{record_id}").status_code == 404
    replay = client.post(restore_url, headers=headers, json=payload)
    assert replay.status_code == 200
    assert replay.json()["data"]["restored"] is False
    assert client.get(f"/api/v1/admin/trash/{entity}").json()["total"] == 0
    if entity != "media":
        assert (
            client.patch(
                f"/api/v1/admin/{entity}/{record_id}", headers=headers, json={"active": True}
            ).status_code
            == 200
        )
        # A late retry must not hide a record that someone has since published.
        assert client.post(restore_url, headers=headers, json=payload).status_code == 409
        assert client.get(f"/api/v1/admin/{entity}/{record_id}").json()["data"]["active"] is True
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
    assert client.get(f"/api/v1/admin/trash/{entity}").status_code == 401
    if entity == "media":
        assert client.get(f"/media/{record_id}/content").status_code == 404


def test_trash_search_pagination_section_and_missing_records(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, _ = content_client
    for section in ("history", "rules"):
        for suffix in ("1", "2"):
            record = client.post(
                "/api/v1/admin/content",
                headers=headers,
                json={"section": section, "title": "КОРЗИНА " + suffix, "body": "Text"},
            ).json()["data"]
            _delete_and_version(client, headers, "content", record["id"])
    first = client.get("/api/v1/admin/trash/content?section=history&q=корзина&page_size=1").json()
    second = client.get(
        "/api/v1/admin/trash/content?section=history&q=корзина&page_size=1&page=2"
    ).json()
    assert first["total"] == 2 and first["total_pages"] == 2
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert client.get("/api/v1/admin/content").json()["total"] == 0
    assert client.get("/api/v1/content/history").json()["data"]["items"] == []
    assert client.get("/api/v1/admin/trash/teams").status_code == 422
    assert (
        client.post(
            f"/api/v1/admin/trash/content/{uuid4()}/restore",
            headers=headers,
            json={"expected_updated_at": "2026-01-01T00:00:00Z"},
        ).status_code
        == 404
    )


def test_trash_match_search_finds_complete_title_and_treats_punctuation_literally(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, team_id = content_client
    competition_id = client.post(
        "/api/v1/admin/competitions",
        headers=headers,
        json={"name": "Search league", "season": "2026"},
    ).json()["data"]["id"]
    ids = []
    for opponent in ("Соперник 100%", "Соперник 1000"):
        created = client.post(
            "/api/v1/admin/matches",
            headers=headers,
            json={
                "team_id": team_id,
                "competition_id": competition_id,
                "starts_at": "2026-12-01T12:00:00Z",
                "club_side": "home",
                "home_team_name": "Хищные Бобры",
                "away_team_name": opponent,
            },
        )
        assert created.status_code == 201, created.text
        match_id = created.json()["data"]["id"]
        ids.append(match_id)
        _delete_and_version(client, headers, "matches", match_id)
    result = client.get(
        "/api/v1/admin/trash/matches",
        params={"q": "хищные бобры — соперник 100%", "page_size": 1},
    )
    assert result.status_code == 200, result.text
    assert result.json()["total"] == 1
    assert result.json()["items"][0]["id"] == ids[0]
    assert client.get("/api/v1/admin/trash/matches", params={"q": "_"}).json()["total"] == 0


def test_competition_recovery_does_not_republish_matches_and_blocks_hidden_parent(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, team_id = content_client
    competition = client.post(
        "/api/v1/admin/competitions", headers=headers, json={"name": "League", "season": "2026"}
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
    ).json()["data"]
    version = _delete_and_version(client, headers, "matches", match["id"])
    competition_version = _delete_and_version(client, headers, "competitions", competition["id"])
    restore_match = f"/api/v1/admin/trash/matches/{match['id']}/restore"
    blocked = client.post(restore_match, headers=headers, json={"expected_updated_at": version})
    assert blocked.status_code == 409 and blocked.json()["code"] == "restore_dependency"
    assert (
        client.post(
            f"/api/v1/admin/trash/competitions/{competition['id']}/restore",
            headers=headers,
            json={"expected_updated_at": competition_version},
        ).status_code
        == 200
    )
    assert (
        client.post(
            restore_match, headers=headers, json={"expected_updated_at": version}
        ).status_code
        == 200
    )
    publish_url = f"/api/v1/admin/matches/{match['id']}"
    blocked = client.patch(publish_url, headers=headers, json={"active": True})
    assert blocked.status_code == 409 and blocked.json()["code"] == "publication_dependency"
    assert (
        client.patch(
            f"/api/v1/admin/competitions/{competition['id']}",
            headers=headers,
            json={"active": True},
        ).status_code
        == 200
    )
    assert client.get(f"/api/v1/matches/{match['id']}").status_code == 404
    assert client.patch(publish_url, headers=headers, json={"active": True}).status_code == 200
    assert client.get(f"/api/v1/matches/{match['id']}").status_code == 200


def test_media_file_and_deleted_reference_must_be_recovered_first(
    content_client: tuple[TestClient, dict[str, str], str],
    tmp_path: Path,
) -> None:
    client, headers, team_id = content_client
    image = BytesIO()
    Image.new("RGB", (10, 10), "blue").save(image, "PNG")
    asset = client.post(
        "/api/v1/admin/media",
        headers=headers,
        files={"file": ("photo.png", image.getvalue(), "image/png")},
    ).json()["data"]
    player = client.post(
        "/api/v1/admin/players",
        headers=headers,
        json={"full_name": "With image", "team_id": team_id, "photo_asset_id": asset["id"]},
    ).json()["data"]
    player_version = _delete_and_version(client, headers, "players", player["id"])
    media_version = _delete_and_version(client, headers, "media", asset["id"])
    restore_player = f"/api/v1/admin/trash/players/{player['id']}/restore"
    assert (
        client.post(
            restore_player, headers=headers, json={"expected_updated_at": player_version}
        ).json()["code"]
        == "restore_dependency"
    )
    with sqlite3.connect(tmp_path / "content.sqlite") as connection:
        storage_key = connection.execute(
            "SELECT storage_key FROM media_assets WHERE id = ?", (asset["id"].replace("-", ""),)
        ).fetchone()[0]
    image_path = tmp_path / "media" / storage_key
    original = image_path.read_bytes()
    image_path.unlink()
    restore_asset = f"/api/v1/admin/trash/media/{asset['id']}/restore"
    blocked = client.post(
        restore_asset, headers=headers, json={"expected_updated_at": media_version}
    )
    assert blocked.status_code == 409 and blocked.json()["code"] == "restore_dependency"
    image_path.write_bytes(original)
    assert (
        client.post(
            restore_asset, headers=headers, json={"expected_updated_at": media_version}
        ).status_code
        == 200
    )
    assert (
        client.post(
            restore_player, headers=headers, json={"expected_updated_at": player_version}
        ).status_code
        == 200
    )


def test_concurrent_restore_writes_one_audit_entry_and_user_role_cannot_recover(
    content_client: tuple[TestClient, dict[str, str], str],
    tmp_path: Path,
) -> None:
    client, headers, _ = content_client
    record = client.post(
        "/api/v1/admin/content",
        headers=headers,
        json={"section": "history", "title": "Concurrent recovery", "body": "Preserve me"},
    ).json()["data"]
    version = _delete_and_version(client, headers, "content", record["id"])
    restore_url = f"/api/v1/admin/trash/content/{record['id']}/restore"
    barrier = Barrier(2)

    def recover(_: int) -> int:
        barrier.wait(timeout=10)
        return client.post(
            restore_url, headers=headers, json={"expected_updated_at": version}
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(recover, range(2)))
    assert 200 in statuses and all(code in (200, 409) for code in statuses)
    assert client.get(f"/api/v1/admin/content/{record['id']}").json()["data"]["active"] is False
    with sqlite3.connect(tmp_path / "content.sqlite") as connection:
        logs = connection.execute(
            "SELECT after FROM admin_audit_log WHERE entity_id = ? AND action = 'UPDATE'",
            (record["id"],),
        ).fetchall()
        assert len(logs) == 1 and '"operation": "restore"' in logs[0][0]
        connection.execute("UPDATE users SET role = 'USER' WHERE username = 'editor'")
    assert client.get("/api/v1/admin/trash/content").status_code == 403
    assert (
        client.post(restore_url, headers=headers, json={"expected_updated_at": version}).status_code
        == 403
    )
