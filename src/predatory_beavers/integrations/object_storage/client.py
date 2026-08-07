from typing import BinaryIO, Protocol


class ObjectStorage(Protocol):
    """Port for S3-compatible media storage."""

    async def put(self, *, key: str, content: BinaryIO, content_type: str) -> None: ...

    async def delete(self, *, key: str) -> None: ...
