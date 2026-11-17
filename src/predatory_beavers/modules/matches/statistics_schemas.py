"""The public protocol never substitutes missing statistics with zero."""

from datetime import datetime
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlayerGameStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    points: int | None = Field(default=None, ge=0)
    rebounds: int | None = Field(default=None, ge=0)
    offensive_rebounds: int | None = Field(default=None, ge=0)
    defensive_rebounds: int | None = Field(default=None, ge=0)
    assists: int | None = Field(default=None, ge=0)
    steals: int | None = Field(default=None, ge=0)
    blocks: int | None = Field(default=None, ge=0)
    turnovers: int | None = Field(default=None, ge=0)
    fouls: int | None = Field(default=None, ge=0)
    minutes_seconds: int | None = Field(default=None, ge=0)
    free_throws_made: int | None = Field(default=None, ge=0)
    free_throws_attempted: int | None = Field(default=None, ge=0)
    two_points_made: int | None = Field(default=None, ge=0)
    two_points_attempted: int | None = Field(default=None, ge=0)
    three_points_made: int | None = Field(default=None, ge=0)
    three_points_attempted: int | None = Field(default=None, ge=0)
    plus_minus: int | None = None

    @model_validator(mode="after")
    def coherent_shots(self) -> "PlayerGameStats":
        for kind in ("free_throws", "two_points", "three_points"):
            made, attempts = getattr(self, kind + "_made"), getattr(self, kind + "_attempted")
            if made is not None and attempts is not None and made > attempts:
                raise ValueError("made shots exceed attempts")
        return self


class PeriodPlayerStatistics(BaseModel):
    period: int = Field(ge=1, le=20)
    statistics: PlayerGameStats


class PlayerBoxScore(BaseModel):
    external_player_id: str = Field(pattern=r"^[1-9][0-9]*$", max_length=128)
    full_name: str = Field(min_length=1, max_length=200)
    jersey_number: str | None = None
    team_side: Literal["home", "away"]
    is_starter: bool | None = None
    played: bool | None = None
    statistics: PlayerGameStats
    period_statistics: list[PeriodPlayerStatistics] = Field(default_factory=list, max_length=20)


class PeriodScore(BaseModel):
    number: int = Field(ge=1, le=20)
    kind: Literal["quarter", "overtime"]
    label: str
    home_score: int = Field(ge=0)
    away_score: int = Field(ge=0)


class ScoreByPeriod(BaseModel):
    """Compact card score, always oriented club/opponent."""

    number: int = Field(ge=1, le=20)
    label: str
    club_score: int = Field(ge=0)
    opponent_score: int = Field(ge=0)


class GameStatisticsRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["asb"] = "asb"
    external_id: str = Field(pattern=r"^[1-9][0-9]*$", max_length=200)
    team: Literal["men", "women"]
    season: str = Field(pattern=r"^\d{4}-\d{4}$")
    discipline: Literal["5x5"] = "5x5"
    club_side: Literal["home", "away"]
    home_team_name: str
    away_team_name: str
    home_external_team_id: str = Field(pattern=r"^[1-9][0-9]*$", max_length=128)
    away_external_team_id: str = Field(pattern=r"^[1-9][0-9]*$", max_length=128)
    home_score: int | None = Field(ge=0)
    away_score: int | None = Field(ge=0)
    source_url: str
    fetched_at: datetime
    source_label: str = "Официальный протокол АСБ"
    period_scores: list[PeriodScore] = Field(default_factory=list, max_length=20)
    box_score: list[PlayerBoxScore] = Field(default_factory=list, max_length=100)
    player_periods_status: Literal["available", "partial", "unavailable"] = "unavailable"
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent_protocol(self) -> "GameStatisticsRead":
        if self.fetched_at.utcoffset() is None:
            raise ValueError("fetched_at requires timezone")
        official = urlsplit(self.source_url)
        if (
            official.scheme != "https"
            or official.netloc != "asb.sportoteka.org"
            or official.path != "/api/abc/games/game"
            or official.fragment
            or parse_qs(official.query)
            != {
                "id": [self.external_id],
                "tag": ["asb"],
                "season": [self.season[-4:]],
                "lang": ["ru"],
            }
        ):
            raise ValueError("statistics require the exact official game source")
        club_id = "7433" if self.team == "men" else "7695"
        if self.home_external_team_id == self.away_external_team_id or club_id != (
            self.home_external_team_id if self.club_side == "home" else self.away_external_team_id
        ):
            raise ValueError("official team identity disagrees with club orientation")
        if self.period_scores:
            if [p.number for p in self.period_scores] != list(
                range(1, len(self.period_scores) + 1)
            ):
                raise ValueError("periods are not consecutive")
            for p in self.period_scores:
                if p.kind != ("quarter" if p.number <= 4 else "overtime"):
                    raise ValueError("incorrect period kind")
            if (
                sum(p.home_score for p in self.period_scores) != self.home_score
                or sum(p.away_score for p in self.period_scores) != self.away_score
            ):
                raise ValueError("period scores disagree with final score")
        identities = [p.external_player_id for p in self.box_score]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate box score player")
        for player in self.box_score:
            numbers = [v.period for v in player.period_statistics]
            if len(numbers) != len(set(numbers)) or not set(numbers) <= {
                v.number for v in self.period_scores
            }:
                raise ValueError("invalid player period")
            if player.period_statistics:
                if set(numbers) != {p.number for p in self.period_scores}:
                    raise ValueError("incomplete player period coverage")
                if player.played is False or self.player_periods_status == "unavailable":
                    raise ValueError("player periods have no verified participation scope")
                for field in PlayerGameStats.model_fields:
                    values = [getattr(v.statistics, field) for v in player.period_statistics]
                    if all(v is not None for v in values) and (
                        getattr(player.statistics, field) is None
                        or sum(v for v in values if v is not None)
                        != getattr(player.statistics, field)
                    ):
                        raise ValueError("player periods disagree with game statistics")
        if self.box_score:
            for side in ("home", "away"):
                scores = [p.statistics.points for p in self.box_score if p.team_side == side]
                if (
                    not scores
                    or any(v is None for v in scores)
                    or sum(v for v in scores if v is not None) != getattr(self, side + "_score")
                ):
                    raise ValueError("player totals disagree with final score")
        return self
