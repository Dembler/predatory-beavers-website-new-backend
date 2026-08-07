from typing import Protocol


class AsbClient(Protocol):
    """Port for the future allowlisted ASB/Infobasket adapter."""

    async def import_competition(
        self, *, competition_id: str, external_team_id: str, season: str
    ) -> None: ...
