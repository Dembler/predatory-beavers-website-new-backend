from typing import Any

from sqlalchemy import ColumnElement, func, or_
from sqlalchemy.orm import InstrumentedAttribute


def text_search(
    query: str, *columns: InstrumentedAttribute[Any] | ColumnElement[Any]
) -> ColumnElement[bool]:
    """Literal Unicode case-insensitive search; %, _ and / are never wildcards."""
    return or_(
        *(
            func.casefold(column).contains(query.strip().casefold(), autoescape=True)
            for column in columns
        )
    )
