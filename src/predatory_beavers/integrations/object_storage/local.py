import asyncio
import os
from pathlib import Path, PurePosixPath
from uuid import uuid4


class ObjectNotFoundError(FileNotFoundError):
    pass


class LocalObjectStorage:
    """Filesystem adapter for development; keys remain portable to an S3 adapter."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    async def put(self, *, key: str, content: bytes, content_type: str) -> None:
        del content_type
        target = self._resolve(key)
        await asyncio.to_thread(self._atomic_write, target, content)

    async def read(self, *, key: str) -> bytes:
        target = self._resolve(key)
        try:
            return await asyncio.to_thread(target.read_bytes)
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(key) from exc

    async def delete(self, *, key: str) -> None:
        target = self._resolve(key)
        try:
            await asyncio.to_thread(target.unlink)
        except FileNotFoundError:
            return

    def _resolve(self, key: str) -> Path:
        if "\\" in key:
            raise ValueError("Object key must use forward slashes")
        relative = PurePosixPath(key)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError("Invalid object key")
        target = self._root.joinpath(*relative.parts).resolve()
        if not target.is_relative_to(self._root):
            raise ValueError("Object key escapes the storage root")
        return target

    @staticmethod
    def _atomic_write(target: Path, content: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
