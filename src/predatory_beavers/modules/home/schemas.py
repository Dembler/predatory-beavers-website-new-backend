from datetime import datetime

from pydantic import BaseModel

from predatory_beavers.modules.club.schemas import TeamRead
from predatory_beavers.modules.matches.schemas import MatchRead
from predatory_beavers.modules.results.router import ArchiveResultRead


class HomeData(BaseModel):
    generated_at: datetime
    next_match: MatchRead | None
    next_result: ArchiveResultRead | None = None
    recent_results: list[MatchRead]
    teams: list[TeamRead]
