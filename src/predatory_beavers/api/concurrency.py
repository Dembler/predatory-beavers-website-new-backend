"""Editor version preconditions; timestamps are precise database versions, not HTTP dates."""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from predatory_beavers.api.errors import ConflictError


class StaleRecordError(ConflictError):
    code = "stale_record"
    detail = "This record changed in another session. Reload it before saving or deleting."


class VersionedUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Exclude the precondition from persistence and audit changes.
    expected_updated_at: datetime | None = Field(default=None, exclude=True)


def require_version(actual: datetime, expected: datetime | None) -> None:
    if expected is None:
        return  # Backwards-compatible for existing API clients.
    # SQLite stores UTC as a naive datetime; API clients may send Z or +00:00.
    actual_utc = actual.replace(tzinfo=UTC) if actual.tzinfo is None else actual.astimezone(UTC)
    expected_utc = (
        expected.replace(tzinfo=UTC) if expected.tzinfo is None else expected.astimezone(UTC)
    )
    if actual_utc != expected_utc:
        raise StaleRecordError
