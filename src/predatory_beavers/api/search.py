from typing import Any

from sqlalchemy import ColumnElement, String, literal, or_
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.functions import GenericFunction


class _SearchCasefold(GenericFunction[str]):
    """SQLite's Unicode UDF, or PostgreSQL's collation-aware lower()."""

    name = "casefold"
    type = String()
    inherit_cache = True


@compiles(_SearchCasefold, "postgresql")
def _postgresql_casefold(element: _SearchCasefold, compiler: SQLCompiler, **kwargs: Any) -> str:
    return f"lower({compiler.process(element.clauses, **kwargs)})"


def text_search(
    query: str, *columns: InstrumentedAttribute[Any] | ColumnElement[Any]
) -> ColumnElement[bool]:
    """Literal Unicode case-insensitive search; %, _ and / are never wildcards."""
    term = query.strip().replace("/", "//").replace("%", "/%").replace("_", "/_")
    return or_(
        *(
            _SearchCasefold(column).contains(_SearchCasefold(literal(term)), escape="/")
            for column in columns
        )
    )
