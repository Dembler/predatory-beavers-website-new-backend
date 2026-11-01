import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_content_media_admin import content_client as content_client

SECTIONS = ("contact_channels", "captains", "player_roles", "viewing_tips")


@pytest.mark.parametrize("section", SECTIONS)
def test_new_content_sections_crud_publication_and_filtering(
    content_client: tuple[TestClient, dict[str, str], str], section: str
) -> None:
    client, headers, _ = content_client
    endpoint = "/api/v1/admin/content"
    response = client.post(
        endpoint,
        headers=headers,
        json={
            "section": section,
            "title": "New section entry",
            "body": "Entry body",
            "subtitle": "  Section subtitle  ",
            "active": False,
        },
    )
    assert response.status_code == 201, response.text
    block = response.json()["data"]
    block_id = block["id"]
    assert block["subtitle"] == "Section subtitle"
    assert block["link_url"] is None
    assert block["link_label"] is None
    assert client.get(f"/api/v1/content/{section}").json()["data"]["items"] == []
    admin_list = client.get(endpoint, params={"section": section}).json()
    assert admin_list["total"] == 1
    assert [item["id"] for item in admin_list["items"]] == [block_id]
    assert client.get(endpoint, params={"section": "history"}).json()["total"] == 0

    updated = client.patch(
        f"{endpoint}/{block_id}",
        headers=headers,
        json={"active": True, "body": "Published body", "sort_order": 7},
    )
    assert updated.status_code == 200, updated.text
    public = client.get(f"/api/v1/content/{section}").json()["data"]["items"]
    assert len(public) == 1
    assert public[0]["body"] == "Published body"
    assert public[0]["subtitle"] == "Section subtitle"
    assert client.get(f"{endpoint}/{block_id}").json()["data"] == public[0]
    earlier = client.post(
        endpoint,
        headers=headers,
        json={"section": section, "title": "Earlier", "body": "Body", "sort_order": 2},
    )
    assert earlier.status_code == 201
    earlier_id = earlier.json()["data"]["id"]
    public = client.get(f"/api/v1/content/{section}").json()["data"]["items"]
    assert [item["id"] for item in public] == [earlier_id, block_id]
    assert client.delete(f"{endpoint}/{block_id}", headers=headers).status_code == 204
    assert client.get(f"{endpoint}/{block_id}").status_code == 404
    public = client.get(f"/api/v1/content/{section}").json()["data"]["items"]
    assert [item["id"] for item in public] == [earlier_id]
    assert client.get(endpoint, params={"section": section}).json()["total"] == 1


def test_content_link_persists_as_string_updates_and_clears_with_audit(
    content_client: tuple[TestClient, dict[str, str], str], tmp_path: Path
) -> None:
    client, headers, _ = content_client
    endpoint = "/api/v1/admin/content"
    response = client.post(
        endpoint,
        headers={**headers, "X-Request-ID": "content-link-create"},
        json={
            "section": "contact_channels",
            "title": "Telegram",
            "body": "Club channel",
            "subtitle": "  News and results  ",
            "link_url": "https://t.me/club?source=site",
            "link_label": "  Open channel  ",
        },
    )
    assert response.status_code == 201, response.text
    block_id = response.json()["data"]["id"]
    expected_url = "https://t.me/club?source=site"
    assert response.json()["data"]["link_url"] == expected_url
    with sqlite3.connect(tmp_path / "content.sqlite") as connection:
        assert connection.execute(
            "SELECT subtitle,link_url,link_label FROM content_blocks"
        ).fetchone() == ("News and results", expected_url, "Open channel")

    updated = client.patch(
        f"{endpoint}/{block_id}",
        headers={**headers, "X-Request-ID": "content-link-update"},
        json={"link_url": "http://example.test/updated", "link_label": " Read more "},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["link_url"] == "http://example.test/updated"
    assert updated.json()["data"]["subtitle"] == "News and results"
    cleared = client.patch(
        f"{endpoint}/{block_id}",
        headers={**headers, "X-Request-ID": "content-link-clear"},
        json={"subtitle": None, "link_url": None, "link_label": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert all(
        cleared.json()["data"][field] is None for field in ("subtitle", "link_url", "link_label")
    )
    public = client.get("/api/v1/content/contact_channels").json()["data"]["items"]
    assert public[0]["link_url"] is None
    with sqlite3.connect(tmp_path / "content.sqlite") as connection:
        assert connection.execute(
            "SELECT subtitle,link_url,link_label FROM content_blocks"
        ).fetchone() == (None, None, None)
        rows = connection.execute(
            'SELECT request_id,"before","after" FROM admin_audit_log '
            "WHERE entity_type='content_block' ORDER BY rowid"
        ).fetchall()
    audit = {
        request_id: (json.loads(before) if before else None, json.loads(after) if after else None)
        for request_id, before, after in rows
    }
    assert audit["content-link-create"][0] is None
    assert audit["content-link-create"][1]["link_url"] == expected_url
    assert audit["content-link-update"][0]["link_url"] == expected_url
    assert audit["content-link-update"][1]["link_url"] == "http://example.test/updated"
    assert audit["content-link-clear"][0]["link_url"] == "http://example.test/updated"
    assert audit["content-link-clear"][1]["link_url"] is None


def test_invalid_content_fields_never_mutate_existing_or_create_drafts(
    content_client: tuple[TestClient, dict[str, str], str],
    tmp_path: Path,
) -> None:
    client, headers, _ = content_client
    endpoint = "/api/v1/admin/content"
    payload = {"section": "captains", "title": "Captain", "body": "Historical captain"}
    response = client.post(endpoint, json=payload, headers=headers)
    assert response.status_code == 201
    block_id = response.json()["data"]["id"]
    original = client.get(f"{endpoint}/{block_id}").json()["data"]
    with sqlite3.connect(tmp_path / "content.sqlite") as connection:
        original_rows = connection.execute("SELECT * FROM content_blocks").fetchall()
        original_audit = connection.execute("SELECT * FROM admin_audit_log").fetchall()
    invalid = [
        {"link_url": "javascript:alert(1)"},
        {"link_url": "ftp://example.test/file"},
        {"link_url": "//example.test/path"},
        {"link_url": "https://example.test/" + "a" * 2048},
        {"link_url": "https://example.org/" + chr(1103) * 1000},
        {"subtitle": "s" * 201},
        {"link_label": "l" * 101},
        *({field: None} for field in ("title", "body", "section", "sort_order", "active")),
    ]
    for fields in invalid:
        draft = client.post(endpoint, json={**payload, "active": False, **fields}, headers=headers)
        assert draft.status_code == 422, (fields, draft.text)
        patched = client.patch(f"{endpoint}/{block_id}", json=fields, headers=headers)
        assert patched.status_code == 422, (fields, patched.text)
    assert client.get(endpoint).json()["total"] == 1
    assert client.get(f"{endpoint}/{block_id}").json()["data"] == original
    with sqlite3.connect(tmp_path / "content.sqlite") as connection:
        assert connection.execute("SELECT * FROM content_blocks").fetchall() == original_rows
        assert connection.execute("SELECT * FROM admin_audit_log").fetchall() == original_audit
    blank = client.patch(
        f"{endpoint}/{block_id}",
        json={"subtitle": " \n ", "link_label": "  "},
        headers=headers,
    )
    assert blank.status_code == 200
    assert blank.json()["data"]["subtitle"] is None
    assert blank.json()["data"]["link_label"] is None


def test_content_optional_fields_accept_exact_limits(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, _ = content_client
    prefix = "https://example.test/"
    url = prefix + "a" * (2048 - len(prefix))
    fields = {"subtitle": "s" * 200, "link_url": url, "link_label": "l" * 100}
    response = client.post(
        "/api/v1/admin/content",
        headers=headers,
        json={"section": "viewing_tips", "title": "Tip", "body": "Body", **fields},
    )
    assert response.status_code == 201, response.text
    block = response.json()["data"]
    assert {field: block[field] for field in fields} == fields
    public = client.get("/api/v1/content/viewing_tips").json()["data"]["items"]
    assert {field: public[0][field] for field in fields} == fields
