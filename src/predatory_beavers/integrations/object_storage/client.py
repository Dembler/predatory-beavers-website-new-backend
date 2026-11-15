from typing import Protocol

from predatory_beavers.api.errors import ServiceUnavailableError


class ObjectStorageUnavailableError(ServiceUnavailableError):
    """A deliberately generic error: upstream responses may contain credentials."""

    code = "object_storage_unavailable"
    detail = "Media storage is temporarily unavailable"


class ObjectStorage(Protocol):
    """Portable keys backed by a local directory or private object storage."""

    async def put(self, *, key: str, content: bytes, content_type: str) -> None: ...

    async def read(self, *, key: str) -> bytes: ...

    async def delete(self, *, key: str) -> None: ...
