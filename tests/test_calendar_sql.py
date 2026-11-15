"""Calendar date expressions preserve behavior and compile for both DB backends."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Date, DateTime, Integer, create_engine, literal, select
from sqlalchemy.dialects import postgresql, sqlite

from predatory_beavers.modules.calendar.repository import (
    CalendarRepository,
    _date_season,
    _declared_season,
)
from predatory_beavers.modules.calendar.sql import TimestampOrder, YearStartOrder


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026 / 2027", "2026-2027"),
        ("2025-2026", "2025-2026"),
        ("2026", None),
        ("202x-2027", None),
        ("2026-abcd", None),
        ("2026-2028", None),
        ("2026--027", None),
        ("2026-+027", None),
        ("2026- 202", None),
        ("２０２６-２０２７", None),
        (None, None),
    ],
)
def test_declared_season_requires_two_complete_consecutive_ascii_years(value, expected):
    with create_engine("sqlite://").connect() as connection:
        assert connection.scalar(select(_declared_season(literal(value)))) == expected


def test_sqlite_date_season_and_union_ordering_keep_moscow_and_source_precision():
    with create_engine("sqlite://").connect() as connection:
        for value, expected in [
            (datetime(2026, 6, 30, 20, 59, 59, tzinfo=UTC), "2025-2026"),
            (datetime(2026, 6, 30, 21, 0, tzinfo=UTC), "2026-2027"),
        ]:
            assert (
                connection.scalar(
                    select(_date_season(literal(value, DateTime(timezone=True)), moscow=True))
                )
                == expected
            )
        day = literal(date(2026, 7, 1), Date())
        assert connection.scalar(select(_date_season(day))) == "2026-2027"
        assert connection.scalar(select(_date_season(literal(None, Date())))) is None
        values = connection.execute(
            select(
                TimestampOrder(literal(datetime(2026, 1, 1, tzinfo=UTC), DateTime())),
                TimestampOrder(literal(date(2026, 1, 1), Date())),
                YearStartOrder(literal(2026, Integer())),
            )
        ).one()
        assert values[0] == values[1] == values[2]
        early = connection.scalar(
            select(TimestampOrder(literal(datetime(2026, 1, 1, 0, 0, 0, 1000), DateTime())))
        )
        late = connection.scalar(
            select(TimestampOrder(literal(datetime(2026, 1, 1, 0, 0, 0, 2000), DateTime())))
        )
        assert early < late


def test_complete_calendar_union_compiles_native_postgresql_dates_without_sqlite_functions():
    # _feed builds SQL without executing a session. Compilation covers both
    # source branches, dedup subquery, enums, declared seasons, and date fallbacks.
    repository = CalendarRepository(None)
    query = select(
        repository._feed(at=datetime(2026, 11, 15, tzinfo=UTC), team="men", discipline=None)
    )
    pg = str(query.compile(dialect=postgresql.dialect())).lower()
    local = str(query.compile(dialect=sqlite.dialect())).lower()
    for sqlite_only in [" glob ", "strftime(", "julianday("]:
        assert sqlite_only not in pg
    assert "extract(epoch from" in pg
    assert "make_date(" in pg
    assert "timezone('utc'" in pg
    assert "interval '3 hours'" in pg
    assert "exists (select" in pg
    assert "union all" in pg
    assert "julianday(" in local
