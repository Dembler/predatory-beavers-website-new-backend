from typing import Protocol


class ObjectStorage(Protocol):
    """Port for S3-compatible media storage."""

    async def put(self, *, key: str, content: bytes, content_type: str) -> None: ...

    async def read(self, *, key: str) -> bytes: ...

    async def delete(self, *, key: str) -> None: ...
