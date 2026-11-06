import json
from io import BytesIO
from pathlib import Path

from PIL import Image

from predatory_beavers.modules.standings.logos import (
    LOGO_DIRECTORY,
    get_logo_path,
    logo_key,
    team_logo_path,
)


def test_bundled_emblems_decode_and_match_reviewed_identity():
    index = json.loads((LOGO_DIRECTORY / "index.json").read_text(encoding="utf-8"))
    for team_id, team in index["teams"].items():
        for name in team["names"]:
            path = team_logo_path(team_id, name)
            if team["key"] is None:
                assert path is None
            else:
                assert path == f"/api/v1/standings/logos/{team['key']}.webp"
                local = get_logo_path(team["key"])
                assert local is not None
                with Image.open(BytesIO(local.read_bytes())) as image:
                    image.load()
                    assert image.format == "WEBP"
                    assert image.width > 0 and image.height > 0


def test_emblem_resolution_does_not_confuse_same_abbreviations_or_gender():
    men_vgtu = team_logo_path("7432", "ВГТУ")
    women_vgtu = team_logo_path("7691", "ВГТУ")
    assert men_vgtu and women_vgtu and men_vgtu != women_vgtu
    assert team_logo_path("7432", "ВГУ") is None
    assert team_logo_path("non-asb-id", "ВГТУ") is None
    assert team_logo_path(None, "ВГТУ") is None
    # This historical team only has the official placeholder. Do not borrow
    # the university's first team emblem by abbreviation.
    assert team_logo_path("6870", "ВГТУ-Ф") is None


def test_emblem_paths_reject_unlisted_hashes_and_traversal():
    assert get_logo_path("../index.json") is None
    assert get_logo_path("a" * 64) is None
    assert get_logo_path(logo_key("https://example.com/logo.png")) is None
    assert get_logo_path(str(Path("/etc/passwd"))) is None
