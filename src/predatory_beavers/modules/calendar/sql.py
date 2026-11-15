"""Small date expressions compiled explicitly for the supported databases."""

from typing import Any

from sqlalchemy import DateTime, Float
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.functions import FunctionElement


class MoscowTimestamp(FunctionElement[Any]):
    """Use the calendar's fixed UTC+3 rule, independently of DB session timezone."""

    type = DateTime()
    inherit_cache = True


@compiles(MoscowTimestamp, "sqlite")
def _moscow_sqlite(element: MoscowTimestamp, compiler: SQLCompiler, **kw: Any) -> str:
    return f"datetime({compiler.process(element.clauses, **kw)}, '+3 hours')"


@compiles(MoscowTimestamp, "postgresql")
def _moscow_postgresql(element: MoscowTimestamp, compiler: SQLCompiler, **kw: Any) -> str:
    value = compiler.process(element.clauses, **kw)
    return f"(timezone('UTC', {value}) + INTERVAL '3 hours')"


class TimestampOrder(FunctionElement[float]):
    """A common numeric scale for timestamp and date rows in the union feed."""

    type = Float()
    inherit_cache = True


@compiles(TimestampOrder, "sqlite")
def _order_sqlite(element: TimestampOrder, compiler: SQLCompiler, **kw: Any) -> str:
    # Retain the established SQLite ordering, including fractional seconds.
    return f"((julianday({compiler.process(element.clauses, **kw)}) - 2440587.5) * 86400.0)"


@compiles(TimestampOrder, "postgresql")
def _order_postgresql(element: TimestampOrder, compiler: SQLCompiler, **kw: Any) -> str:
    return f"EXTRACT(epoch FROM {compiler.process(element.clauses, **kw)})"


class YearStartOrder(FunctionElement[float]):
    type = Float()
    inherit_cache = True


@compiles(YearStartOrder, "sqlite")
def _year_sqlite(element: YearStartOrder, compiler: SQLCompiler, **kw: Any) -> str:
    year = compiler.process(element.clauses, **kw)
    return f"((julianday(CAST({year} AS TEXT) || '-01-01') - 2440587.5) * 86400.0)"


@compiles(YearStartOrder, "postgresql")
def _year_postgresql(element: YearStartOrder, compiler: SQLCompiler, **kw: Any) -> str:
    year = compiler.process(element.clauses, **kw)
    return f"EXTRACT(epoch FROM make_date({year}, 1, 1))"
