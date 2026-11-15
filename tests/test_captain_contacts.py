# ruff: noqa: F811
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_content_media_admin import content_client  # noqa: F401, F811


def test_captain_photo_publication_replacement_and_media_usage(content_client):
    client, headers, _ = content_client
    output = BytesIO()
    Image.new("RGB", (48, 64), "purple").save(output, "PNG")
    asset = client.post(
        "/api/v1/admin/media",
        headers=headers,
        files={"file": ("captain.png", output.getvalue(), "image/png")},
    ).json()["data"]
    payload = {
        "section": "captains",
        "title": "Captain",
        "body": "Men",
        "photo_asset_id": asset["id"],
        "active": False,
        "contacts": [{"label": "Phone", "value": "+7 900 1234567", "url": "tel:+79001234567"}],
    }
    response = client.post("/api/v1/admin/content", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    block = response.json()["data"]
    assert block["photo"]["id"] == asset["id"]
    endpoint = f"/api/v1/admin/content/{block['id']}"
    assert client.delete(f"/api/v1/admin/media/{asset['id']}", headers=headers).status_code == 409
    anonymous = TestClient(client.app)
    assert anonymous.get(asset["content_url"]).status_code == 404
    assert client.patch(endpoint, headers=headers, json={"active": True}).status_code == 200
    assert anonymous.get(asset["content_url"]).status_code == 200
    public = anonymous.get("/api/v1/content/captains").json()["data"]["items"][0]
    assert public["photo"]["content_url"] == asset["content_url"]
    assert public["contacts"] == payload["contacts"]
    cleared = client.patch(endpoint, headers=headers, json={"photo_asset_id": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"]["photo"] is None
    assert anonymous.get(asset["content_url"]).status_code == 404
    assert (
        client.patch(endpoint, headers=headers, json={"photo_asset_id": str(uuid4())}).status_code
        == 404
    )


def test_contacts_patch_omission_clear_and_safe_links(content_client):
    client, headers, _ = content_client
    contacts = [
        {"label": "Email", "value": "captain@example.test", "url": "mailto:captain@example.test"},
        {"label": "Telegram", "value": "@captain", "url": "https://t.me/captain"},
        {"label": "Office", "value": "Building 1", "url": None},
    ]
    response = client.post(
        "/api/v1/admin/content",
        headers=headers,
        json={
            "section": "contact_channels",
            "title": "Club",
            "body": "Contacts",
            "contacts": contacts,
            "link_url": "tel:+79001234567",
        },
    )
    assert response.status_code == 201, response.text
    endpoint = f"/api/v1/admin/content/{response.json()['data']['id']}"
    assert (
        client.patch(endpoint, headers=headers, json={"title": "Updated"}).json()["data"][
            "contacts"
        ]
        == contacts
    )
    assert client.patch(endpoint, headers=headers, json={"contacts": None}).status_code == 422
    for url in [
        "javascript:alert(1)",
        "data:text/html,test",
        "//evil.test",
        "https://user:secret@example.test",
        "mailto:a@example.test?body=%0aInjected",
        "https://evil.test/%0d%0a",
    ]:
        assert (
            client.patch(
                endpoint,
                headers=headers,
                json={"contacts": [{"label": "Bad", "value": "Bad", "url": url}]},
            ).status_code
            == 422
        )
    assert client.get(endpoint).json()["data"]["contacts"] == contacts
    assert (
        client.patch(endpoint, headers=headers, json={"contacts": []}).json()["data"]["contacts"]
        == []
    )


@pytest.mark.parametrize("field,value", [("label", "  "), ("value", " ")])
def test_blank_contacts_are_rejected(content_client, field, value):
    client, headers, _ = content_client
    contact = {"label": "Contact", "value": "Value", field: value}
    assert (
        client.post(
            "/api/v1/admin/content",
            headers=headers,
            json={"section": "captains", "title": "Captain", "body": "Team", "contacts": [contact]},
        ).status_code
        == 422
    )
