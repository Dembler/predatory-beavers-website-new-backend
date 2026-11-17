"""Normalize official Sportoteka protocols without retaining personal profile fields.

Primary schemas checked against /api/abc/games/game and the public
/api/basket/stats/plays dictionary (Goal1..Miss3, IDs 1..6).
Period points/shots are reconstructed only when every player's full-game
counts and every official period reconcile. Other period metrics remain unknown.
"""

import re
from collections import defaultdict
from datetime import datetime
from typing import Any, Literal

from predatory_beavers.modules.matches.statistics_schemas import (
    GameStatisticsRead,
    PeriodPlayerStatistics,
    PeriodScore,
    PlayerBoxScore,
    PlayerGameStats,
)

BASE = "https://asb.sportoteka.org/api/abc"
TEAM_IDS = {"men": "7433", "women": "7695"}
FINISHED = {"Result", "ResultConfirmed"}
SHOT_FIELDS = (
    "points",
    "free_throws_made",
    "free_throws_attempted",
    "two_points_made",
    "two_points_attempted",
    "three_points_made",
    "three_points_attempted",
)
STATS_FIELDS = {
    "points": "points",
    "offensive_rebounds": "offReb",
    "defensive_rebounds": "defReb",
    "assists": "assist",
    "steals": "steal",
    "blocks": "block",
    "turnovers": "turnover",
    "fouls": "foul",
    "minutes_seconds": "second",
    "free_throws_made": "goal1",
    "free_throws_attempted": "shot1",
    "two_points_made": "goal2",
    "two_points_attempted": "shot2",
    "three_points_made": "goal3",
    "three_points_attempted": "shot3",
    "plus_minus": "plusMinus",
}


def game_url(external_id: str, season: str) -> str:
    if not external_id.isdigit() or not re.fullmatch(r"\d{4}-\d{4}", season):
        raise ValueError("invalid ASB game identity")
    return f"{BASE}/games/game?id={external_id}&tag=asb&season={season[-4:]}&lang=ru"


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _stats(raw: dict[str, Any]) -> PlayerGameStats:
    values = {key: _integer(raw.get(source)) for key, source in STATS_FIELDS.items()}
    offensive, defensive = values["offensive_rebounds"], values["defensive_rebounds"]
    values["rebounds"] = (
        offensive + defensive if offensive is not None and defensive is not None else None
    )
    return PlayerGameStats.model_validate(values)


def _period_scores(game: dict[str, Any]) -> list[PeriodScore]:
    full = game.get("fullScore")
    count = _integer(game.get("periods"))
    if not full:
        return []
    if not isinstance(full, str) or not re.fullmatch(r"\d+:\d+(?:,\d+:\d+){3,19}", full):
        raise ValueError("unrecognized period scores")
    pairs = [tuple(map(int, pair.split(":"))) for pair in full.split(",")]
    if count != len(pairs):
        raise ValueError("period count disagrees with score list")
    if sum(p[0] for p in pairs) != game.get("score1") or sum(p[1] for p in pairs) != game.get(
        "score2"
    ):
        # Never guess cumulative semantics or manufacture the fourth quarter.
        raise ValueError("period scores disagree with final score")
    return [
        PeriodScore(
            number=i,
            kind="quarter" if i <= 4 else "overtime",
            label=f"{i}-я четверть" if i <= 4 else f"ОТ{i - 4}",
            home_score=pair[0],
            away_score=pair[1],
        )
        for i, pair in enumerate(pairs, 1)
    ]


def _add_period_shots(
    result: GameStatisticsRead,
    data: dict[str, Any],
    starts: dict[int, PlayerBoxScore],
) -> None:
    plays = data.get("plays")
    if not result.period_scores or not isinstance(plays, list) or not plays:
        return
    # Counts are zero only after completeness has been proved by reconciliation.
    counts: dict[tuple[int, int], dict[str, int]] = defaultdict(
        lambda: dict.fromkeys(SHOT_FIELDS, 0)
    )
    seen: set[int] = set()
    for parent in plays:
        if not isinstance(parent, dict):
            return
        period = _integer(parent.get("period"))
        children = parent.get("children") or []
        if not isinstance(children, list):
            return
        for event in [parent, *children]:
            if not isinstance(event, dict):
                return
            num = _integer(event.get("num"))
            if num is None or num in seen:
                return
            seen.add(num)
            code = _integer(event.get("play"))
            if code not in {1, 2, 3, 4, 5, 6}:
                continue
            start = _integer(event.get("startNum"))
            if (
                start is None
                or period is None
                or start not in starts
                or period not in {p.number for p in result.period_scores}
            ):
                return
            row = counts[(start, period)]
            shot_kind = {1: "free_throws", 2: "two_points", 3: "three_points"}[(code - 1) % 3 + 1]
            row[shot_kind + "_attempted"] += 1
            if code <= 3:
                row[shot_kind + "_made"] += 1
                row["points"] += code
    for start, player in starts.items():
        for field in SHOT_FIELDS:
            total = getattr(player.statistics, field)
            if (
                total is None
                or sum(counts[(start, p.number)][field] for p in result.period_scores) != total
            ):
                result.warnings.append(
                    "Поквартальные показатели игроков не прошли сверку протокола."
                )
                return
    for period_score in result.period_scores:
        for side in ("home", "away"):
            points = sum(
                counts[(start, period_score.number)]["points"]
                for start, player in starts.items()
                if player.team_side == side
            )
            if points != getattr(period_score, side + "_score"):
                return
    for start, player in starts.items():
        if player.played is False:
            continue
        player.period_statistics = [
            PeriodPlayerStatistics(
                period=p.number,
                statistics=PlayerGameStats.model_validate(counts[(start, p.number)]),
            )
            for p in result.period_scores
        ]
    result.player_periods_status = "partial"
    result.warnings.append(
        "По четвертям подтверждены очки и броски; остальные показатели доступны за матч."
    )


def normalize_game_statistics(
    envelope: object,
    *,
    external_id: str,
    team: Literal["men", "women"],
    season: str,
    fetched_at: datetime,
) -> GameStatisticsRead | None:
    """None is an unavailable protocol, not an authoritative deletion."""
    url = game_url(external_id, season)
    if not isinstance(envelope, dict) or envelope.get("status") != "Ok":
        return None
    data = envelope.get("result")
    if not isinstance(data, dict) or not data:
        return None
    game = data.get("game")
    if not isinstance(game, dict) or str(game.get("id")) != external_id:
        raise ValueError("ASB returned another game")
    if game.get("gameStatus") not in FINISHED:
        return None  # Includes Forfeit: no fabricated playing periods or player totals.
    stamp = datetime.fromisoformat(game["defaultZoneDateTime"])
    start, end = map(int, season.split("-"))
    if (
        stamp.utcoffset() is None
        or end != start + 1
        or not (datetime(start, 7, 1).date() <= stamp.date() < datetime(end, 7, 1).date())
    ):
        raise ValueError("ASB returned another season")
    live = data.get("live") or {}
    if live.get("playersOnCourt") not in {None, 5} or live.get("periodCount") not in {None, 4}:
        raise ValueError("protocol is not conventional 5x5")
    first, second = data.get("team1"), data.get("team2")
    if not isinstance(first, dict) or not isinstance(second, dict):
        raise ValueError("missing official team identities")
    home, away = str(first.get("teamId")), str(second.get("teamId"))
    if home == away or TEAM_IDS[team] not in {home, away}:
        raise ValueError("ASB returned another team")
    result = GameStatisticsRead(
        external_id=external_id,
        team=team,
        season=season,
        club_side="home" if TEAM_IDS[team] == home else "away",
        home_team_name=first["name"],
        away_team_name=second["name"],
        home_external_team_id=home,
        away_external_team_id=away,
        home_score=game.get("score1"),
        away_score=game.get("score2"),
        source_url=url,
        fetched_at=fetched_at,
    )
    try:
        result.period_scores = _period_scores(game)
    except ValueError:
        result.warnings.append("Счёт по четвертям не согласуется с итогом; подробности скрыты.")
    rows = data.get("teams") or []
    starts: dict[int, PlayerBoxScore] = {}
    if not isinstance(rows, list):
        raise ValueError("invalid team box score")
    for row in rows:
        if row.get("teamNumber") not in {1, 2}:
            continue
        number = row["teamNumber"]
        if str(row.get("teamId")) != (home if number == 1 else away):
            raise ValueError("box score team orientation mismatch")
        for person in row.get("starts") or []:
            if person.get("startRole") != "Player" or not person.get("personId"):
                continue
            raw = person.get("stats") or {}
            stats = _stats(raw)
            start_num = person["startNum"]
            if start_num in starts:
                raise ValueError("duplicate protocol start identity")
            played: bool | None = None
            if stats.minutes_seconds is not None and stats.minutes_seconds > 0:
                played = True
            elif (
                any(
                    (getattr(stats, f) or 0) > 0
                    for f in PlayerGameStats.model_fields
                    if f != "plus_minus"
                )
                or raw.get("isStart") is True
            ):
                played = True
            elif (
                stats.minutes_seconds == 0
                and all(
                    getattr(stats, f) == 0
                    for f in PlayerGameStats.model_fields
                    if f != "plus_minus"
                )
                and raw.get("isStart") is False
            ):
                played = False
            player = PlayerBoxScore(
                external_player_id=str(person["personId"]),
                full_name=" ".join(
                    v.strip()
                    for k in ("lastName", "firstName", "secondName")
                    if isinstance((v := person.get(k)), str) and v.strip()
                ),
                jersey_number=(
                    str(person["displayNumber"])
                    if person.get("displayNumber") not in {None, ""}
                    else None
                ),
                team_side="home" if number == 1 else "away",
                is_starter=raw.get("isStart") if isinstance(raw.get("isStart"), bool) else None,
                played=played,
                statistics=stats,
            )
            starts[start_num] = player
            result.box_score.append(player)
    # A partial team protocol cannot establish a complete box score or PBP scope.
    for side, final in (("home", result.home_score), ("away", result.away_score)):
        points = [p.statistics.points for p in result.box_score if p.team_side == side]
        if not points or any(v is None for v in points) or sum(v or 0 for v in points) != final:
            result.box_score = []
            result.warnings.append(
                "Индивидуальная статистика пока не подтверждена полным протоколом."
            )
            break
    if result.box_score:
        _add_period_shots(result, data, starts)
    if not result.period_scores and not result.box_score:
        return None
    return GameStatisticsRead.model_validate(result.model_dump())
