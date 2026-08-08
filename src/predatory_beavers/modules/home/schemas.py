from datetime import datetime

from pydantic import BaseModel

from predatory_beavers.modules.club.schemas import TeamRead
from predatory_beavers.modules.matches.schemas import MatchRead


class HomeData(BaseModel):
    generated_at: datetime
    next_match: MatchRead | None
    recent_results: list[MatchRead]
    teams: list[TeamRead]
