from predatory_beavers.integrations.asb.schemas import AsbGame
from scripts.backfill_local_match_logos import unambiguous_team_logos


def game(**overrides: object) -> AsbGame:
    payload: dict[str, object] = {
        "GameID": 1,
        "GameDateTimeMoscow": "/Date(1731947400000)/",
        "GameStatus": 1,
        "TeamAid": 7433,
        "TeamBid": 7432,
        "TeamNameAru": "ВГУ (Воронеж)",
        "TeamNameBru": "ВГТУ (Воронеж)",
        "TeamLogoA": "https://files.infobasket.su/Logos/17024.png",
        "TeamLogoB": "https://files.infobasket.su/Logos/830.png",
        "ScoreA": 41,
        "ScoreB": 48,
    }
    payload.update(overrides)
    return AsbGame.model_validate(payload)


def test_exact_name_mapping_requires_one_identity_and_official_url() -> None:
    logos = unambiguous_team_logos([game(), game(GameID=2)])
    assert logos == {
        "ВГУ (Воронеж)": "https://files.infobasket.su/Logos/17024.png",
        "ВГТУ (Воронеж)": "https://files.infobasket.su/Logos/830.png",
    }
    assert "ВГУ" not in logos
    assert "вгу (воронеж)" not in logos


def test_ambiguous_identity_or_logo_is_excluded() -> None:
    assert "ВГУ (Воронеж)" not in unambiguous_team_logos([game(), game(TeamAid=999)])
    assert "ВГУ (Воронеж)" not in unambiguous_team_logos(
        [game(), game(TeamAid=999, TeamLogoA=None)]
    )
    assert "ВГУ (Воронеж)" not in unambiguous_team_logos(
        [game(), game(TeamLogoA="https://files.infobasket.su/Logos/other.png")]
    )


def test_unknown_logo_is_never_guessed() -> None:
    logos = unambiguous_team_logos([game(TeamLogoA=None)])
    assert "ВГУ (Воронеж)" not in logos
    assert logos["ВГТУ (Воронеж)"] == "https://files.infobasket.su/Logos/830.png"
