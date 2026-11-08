from sqlalchemy.sql.elements import ColumnElement

from predatory_beavers.modules.club.models import Team
from predatory_beavers.modules.matches.models import Competition, Match


def public_match_conditions() -> list[ColumnElement[bool]]:
    """A venue's publication state does not determine its matches' visibility."""
    return [
        Match.active.is_(True),
        Match.is_deleted.is_(False),
        Team.active.is_(True),
        Team.is_deleted.is_(False),
        Competition.active.is_(True),
        Competition.is_deleted.is_(False),
    ]
