from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

RosterSelection = Literal["current", "latest_published"]


class RosterMemberInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_player_id: str = Field(min_length=1, max_length=128)
    full_name: str = Field(min_length=1, max_length=200)
    photo_url: HttpUrl | None
    games_played: int | None = Field(ge=0)
    points_total: int | None = Field(ge=0)
    assists_total: int | None = Field(ge=0)
    rebounds_total: int | None = Field(ge=0)
    seconds_total: int | None = Field(ge=0)
    statistics_season: str | None = Field(default=None, pattern=r"^20\d{2}-20\d{2}$")
    statistics_source_url: HttpUrl | None = None

    @model_validator(mode="after")
    def attributed_statistics(self) -> "RosterMemberInput":
        metrics = [self.points_total, self.assists_total, self.rebounds_total, self.seconds_total]
        has_statistics = bool(self.games_played and any(value is not None for value in metrics))
        if has_statistics != bool(self.statistics_season and self.statistics_source_url):
            raise ValueError("Published statistics require one explicit season and source")
        if not has_statistics and (
            self.games_played is not None
            or any(value is not None for value in metrics)
            or self.statistics_season is not None
            or self.statistics_source_url is not None
        ):
            raise ValueError("Unavailable statistics must remain null")
        if self.statistics_source_url and (
            self.statistics_source_url.scheme != "https"
            or self.statistics_source_url.host not in {"asb.infobasket.su", "asb.sportoteka.org"}
        ):
            raise ValueError("Statistics source must be the public ASB API")
        return self

    @field_validator("photo_url")
    @classmethod
    def official_photo(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value and (
            value.scheme != "https"
            or value.host not in {"img.infobasket.su", "files.infobasket.su"}
        ):
            raise ValueError("Only official public ASB image hosts are accepted")
        return value


class RosterSnapshotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team: Literal["men", "women"]
    season: str = Field(pattern=r"^20\d{2}-20\d{2}$")
    external_team_id: str
    competition_id: str
    is_published: bool
    source_url: HttpUrl
    stats_source_url: HttpUrl
    checked_at: datetime
    players: list[RosterMemberInput]

    @model_validator(mode="after")
    def verified_membership(self) -> "RosterSnapshotInput":
        if self.is_published != bool(self.players):
            raise ValueError("A published roster must contain verified team members")
        ids = [player.external_player_id for player in self.players]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate player in a team-season roster")
        if any(
            player.statistics_season and player.statistics_season > self.season
            for player in self.players
        ):
            raise ValueError("Statistics cannot come from a future season")
        if any(
            url.host not in {"asb.infobasket.su", "asb.sportoteka.org"} or url.scheme != "https"
            for url in [self.source_url, self.stats_source_url]
        ):
            raise ValueError("Roster sources must be the public ASB API")
        return self


class RosterPlayerRead(BaseModel):
    id: UUID
    external_player_id: str
    full_name: str
    photo_url: str | None
    games_played: int | None
    points_per_game: float | None
    assists_per_game: float | None
    rebounds_per_game: float | None
    minutes_per_game: float | None
    statistics_season: str | None
    statistics_source_url: str | None


class RosterContext(BaseModel):
    team: str
    current_season: str
    season: str | None
    latest_published_season: str | None
    is_current_season: bool
    status: Literal["published", "not_published"]
    selection: RosterSelection
    notice: str | None
    source_url: str | None
    stats_source_url: str | None
    fetched_at: datetime | None


class RosterRead(RosterContext):
    players: list[RosterPlayerRead]


class RosterPlayerDetail(RosterContext):
    player: RosterPlayerRead
