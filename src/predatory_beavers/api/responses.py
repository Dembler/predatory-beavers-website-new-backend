from pydantic import BaseModel, Field


class ApiResponse[DataT](BaseModel):
    status: str = "success"
    message: str
    data: DataT | None = None


class PaginatedResponse[DataT](BaseModel):
    items: list[DataT]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total_pages: int = Field(ge=0)

    @classmethod
    def create(
        cls,
        *,
        items: list[DataT],
        total: int,
        page: int,
        page_size: int,
    ) -> "PaginatedResponse[DataT]":
        return cls(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=(total + page_size - 1) // page_size if total else 0,
        )


class ErrorBody(BaseModel):
    status: str = "error"
    code: str
    detail: str
    errors: list[dict[str, object]] = Field(default_factory=list)
    request_id: str | None = None
