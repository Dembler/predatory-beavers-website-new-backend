from pydantic import BaseModel, ConfigDict, Field


class AsbSchema(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class AsbGame(AsbSchema):
    game_id: int = Field(alias="GameID", gt=0)
    game_datetime_moscow: str = Field(alias="GameDateTimeMoscow", min_length=1, max_length=64)
    game_status: int = Field(alias="GameStatus", ge=0)
    home_team_id: int = Field(alias="TeamAid", gt=0)
    away_team_id: int = Field(alias="TeamBid", gt=0)
    home_team_name: str = Field(alias="TeamNameAru", min_length=1, max_length=200)
    away_team_name: str = Field(alias="TeamNameBru", min_length=1, max_length=200)
    home_score: int = Field(alias="ScoreA", ge=0)
    away_score: int = Field(alias="ScoreB", ge=0)
    arena_id: int | None = Field(default=None, alias="ArenaId", ge=0)
    arena_name: str | None = Field(default=None, alias="ArenaRu", max_length=200)
    phase_name: str | None = Field(default=None, alias="CompNameRu", max_length=200)


class AsbTeamName(AsbSchema):
    name: str = Field(alias="CompTeamNameRu", min_length=1, max_length=200)


class AsbStandingStats(AsbSchema):
    played: int = Field(alias="StandingGame", ge=0)
    wins: int = Field(alias="StandingWin", ge=0)
    draws: int | None = Field(default=None, alias="StandingDraw", ge=0)
    losses: int = Field(alias="StandingLose", ge=0)
    table_points: int = Field(alias="StandingPoints", ge=0)
    points_for: int = Field(alias="StandingGoalPlus", ge=0)
    points_against: int = Field(alias="StandingGoalMinus", ge=0)


class AsbStanding(AsbSchema):
    competition_id: int = Field(alias="CompID", gt=0)
    team_id: int = Field(alias="TeamID", gt=0)
    position: int = Field(alias="Place", gt=0)
    team_name: AsbTeamName = Field(alias="CompTeamName")
    stats: AsbStandingStats = Field(alias="Standings")
