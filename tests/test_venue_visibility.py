from datetime import UTC, datetime, timedelta
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

pytest_plugins = ["test_content_media_admin"]


def test_venue_lifecycle_redacts_details_without_unpublishing_matches_or_logos(
    content_client: tuple[TestClient, dict[str, str], str],
) -> None:
    client, headers, team_id = content_client
    competition = client.post(
        "/api/v1/admin/competitions",
        headers=headers,
        json={"name": "Venue lifecycle league", "season": "2026-2027"},
    ).json()["data"]
    venue = client.post(
        "/api/v1/admin/venues",
        headers=headers,
        json={"name": "Private venue identity", "address": "Private venue address 491"},
    ).json()["data"]
    venue_url = f"/api/v1/admin/venues/{venue['id']}"
    logos = []
    for color in ("orange", "blue"):
        image = BytesIO()
        Image.new("RGB", (10, 10), color).save(image, "PNG")
        uploaded = client.post(
            "/api/v1/admin/media",
            headers=headers,
            files={"file": (f"{color}.png", image.getvalue(), "image/png")},
        )
        assert uploaded.status_code == 201, uploaded.text
        logos.append(uploaded.json()["data"])
    now = datetime.now(UTC)
    matches = []
    for finished in (False, True):
        response = client.post(
            "/api/v1/admin/matches",
            headers=headers,
            json={
                "team_id": team_id,
                "competition_id": competition["id"],
                "venue_id": venue["id"],
                "starts_at": (now + timedelta(days=-1 if finished else 1)).isoformat(),
                "club_side": "home",
                "home_team_name": "Beavers",
                "away_team_name": "Previous opponent" if finished else "Next opponent",
                "status": "finished" if finished else "scheduled",
                "home_score": 80 if finished else None,
                "away_score": 70 if finished else None,
                "home_logo_asset_id": logos[0]["id"],
                "away_logo_asset_id": logos[1]["id"],
                "featured": not finished,
            },
        )
        assert response.status_code == 201, response.text
        matches.append(response.json()["data"])
    upcoming, finished = matches
    match_ids = {match["id"] for match in matches}
    cookies = dict(client.cookies)

    def check_surfaces(*, published_venue: bool) -> None:
        client.cookies.clear()
        try:
            responses = [client.get("/api/v1/matches")]
            assert responses[0].status_code == 200
            rows = responses[0].json()["items"]
            assert {row["id"] for row in rows} == match_ids
            for match in matches:
                detail = client.get(f"/api/v1/matches/{match['id']}")
                assert detail.status_code == 200, detail.text
                rows.append(detail.json()["data"])
                responses.append(detail)
            home = client.get("/api/v1/home")
            assert home.status_code == 200, home.text
            home_data = home.json()["data"]
            assert home_data["next_match"]["id"] == upcoming["id"]
            assert [row["id"] for row in home_data["recent_results"]] == [finished["id"]]
            rows.extend([home_data["next_match"], *home_data["recent_results"]])
            calendar = client.get("/api/v1/calendar", params={"season": "all"})
            assert calendar.status_code == 200, calendar.text
            assert calendar.json()["total"] == 2
            calendar_matches = [entry["match"] for entry in calendar.json()["items"]]
            assert {row["id"] for row in calendar_matches} == match_ids
            rows.extend(calendar_matches)
            responses.extend([home, calendar])
            for row in rows:
                assert row["active"] is True
                if published_venue:
                    assert row["venue_id"] == venue["id"]
                    assert row["venue"]["name"] == venue["name"]
                    assert row["venue"]["address"] == venue["address"]
                else:
                    assert row["venue_id"] is None
                    assert row["venue"] is None
            if not published_venue:
                for response in responses:
                    for private_detail in (venue["id"], venue["name"], venue["address"]):
                        assert private_detail not in response.text
            visible_venues = client.get("/api/v1/venues").json()["items"]
            assert (venue["id"] in {item["id"] for item in visible_venues}) == published_venue
            for logo in logos:
                for path in (logo["content_url"], f"/api/v1/media/{logo['id']}/content"):
                    content = client.get(path)
                    assert content.status_code == 200, content.text
                    assert content.headers["cache-control"] == "public, max-age=0, must-revalidate"
        finally:
            client.cookies.update(cookies)
        # Public serialization must leave the editor's original association and
        # publication decisions intact, including while the venue is deleted.
        admin_rows = client.get("/api/v1/admin/matches").json()["items"]
        assert {row["id"] for row in admin_rows} == match_ids
        for row in admin_rows:
            assert row["active"] is True
            assert row["featured"] == (row["id"] == upcoming["id"])
            assert row["venue_id"] == venue["id"]
            assert row["venue"]["name"] == venue["name"]
            assert row["venue"]["address"] == venue["address"]
            detail = client.get(f"/api/v1/admin/matches/{row['id']}").json()["data"]
            assert detail["venue_id"] == venue["id"]
            assert detail["venue"] == row["venue"]

    check_surfaces(published_venue=True)
    hidden = client.patch(venue_url, headers=headers, json={"active": False})
    assert hidden.status_code == 200, hidden.text
    check_surfaces(published_venue=False)
    notes = client.patch(
        f"/api/v1/admin/matches/{upcoming['id']}",
        headers=headers,
        json={"notes": "Still published with a hidden venue"},
    )
    assert notes.status_code == 200, notes.text
    assert notes.json()["data"]["venue_id"] == venue["id"]

    deleted = client.delete(venue_url, headers=headers)
    assert deleted.status_code == 204, deleted.text
    check_surfaces(published_venue=False)
    score = client.patch(
        f"/api/v1/admin/matches/{finished['id']}",
        headers=headers,
        json={"home_score": 91, "away_score": 82, "venue_id": venue["id"]},
    )
    assert score.status_code == 200, score.text
    assert score.json()["data"]["active"] is True
    assert score.json()["data"]["venue_id"] == venue["id"]
    assert client.get(f"/api/v1/matches/{finished['id']}").json()["data"]["home_score"] == 91

    trash = client.get("/api/v1/admin/trash/venues").json()["items"]
    tombstone = next(item for item in trash if item["id"] == venue["id"])
    restored = client.post(
        f"/api/v1/admin/trash/venues/{venue['id']}/restore",
        headers=headers,
        json={"expected_updated_at": tombstone["updated_at"]},
    )
    assert restored.status_code == 200, restored.text
    assert client.get(venue_url).json()["data"]["active"] is False
    check_surfaces(published_venue=False)
    republished = client.patch(venue_url, headers=headers, json={"active": True})
    assert republished.status_code == 200, republished.text
    check_surfaces(published_venue=True)

    for match in matches:
        assert (
            client.patch(
                f"/api/v1/admin/matches/{match['id']}", headers=headers, json={"active": False}
            ).status_code
            == 200
        )
    client.cookies.clear()
    try:
        for logo in logos:
            assert client.get(logo["content_url"]).status_code == 404
    finally:
        client.cookies.update(cookies)
