import re
from datetime import UTC, datetime

from predatory_beavers.integrations.asb.errors import AsbInvalidResponseError
from predatory_beavers.modules.matches.models import MatchStatus

_ASB_DATE_PATTERN = re.compile(r"^/Date\((?P<milliseconds>[0-9]+)(?:[+-][0-9]{4})?\)/$")


def parse_asb_datetime(value: str) -> datetime:
    match = _ASB_DATE_PATTERN.fullmatch(value)
    if match is None:
        raise AsbInvalidResponseError("ASB returned an invalid game datetime")
    milliseconds = int(match.group("milliseconds"))
    try:
        return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise AsbInvalidResponseError("ASB returned an invalid game datetime") from exc


def match_status(game_status: int, starts_at: datetime) -> MatchStatus:
    if game_status == 1:
        return MatchStatus.FINISHED
    if starts_at > datetime.now(UTC):
        return MatchStatus.SCHEDULED
    return MatchStatus.POSTPONED
