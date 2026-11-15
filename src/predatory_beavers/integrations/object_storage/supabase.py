"""Private Supabase Storage REST adapter; content authorization stays in the API."""

import json
import re
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit

import httpx

from predatory_beavers.integrations.object_storage.client import ObjectStorageUnavailableError
from predatory_beavers.integrations.object_storage.local import ObjectNotFoundError

MAX_OBJECT_BYTES = 32 * 1024 * 1024
MAX_CONTROL_BYTES = 64 * 1024


class SupabaseObjectStorage:
    """Use a server-side sb_secret key in apikey, never a public or signed URL."""

    def __init__(
        self,
        project_url: str,
        secret_key: str,
        bucket: str,
        *,
        client: httpx.AsyncClient,
    ) -> None:
        parsed = urlsplit(project_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Supabase project URL must be a root HTTPS URL")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", bucket):
            raise ValueError("Invalid Supabase Storage bucket name")
        if not secret_key.startswith("sb_secret_") or any(char.isspace() for char in secret_key):
            raise ValueError("Supabase Storage requires a server-side sb_secret key")
        self._url = project_url.rstrip("/") + "/storage/v1"
        self._key = secret_key
        self._bucket = bucket
        self._client = client

    async def ensure_private_bucket(self, *, create: bool = False) -> None:
        """Verify existing metadata; only an explicit bootstrap may create a private bucket."""
        try:
            payload = await self._request(
                "GET", f"/bucket/{self._bucket}", missing_key=self._bucket
            )
        except ObjectNotFoundError:
            if not create:
                raise ObjectStorageUnavailableError from None
            payload = json.dumps(
                {"id": self._bucket, "name": self._bucket, "public": False}
            ).encode()
            await self._request("POST", "/bucket", content=payload, content_type="application/json")
            # Creation responses omit visibility, so verify the actual metadata.
            await self.ensure_private_bucket()
            return
        try:
            bucket = json.loads(payload)
        except (ValueError, UnicodeError):
            raise ObjectStorageUnavailableError from None
        if (
            not isinstance(bucket, dict)
            or bucket.get("id") != self._bucket
            or bucket.get("public") is not False
        ):
            # Missing visibility metadata must fail closed, too.
            raise ObjectStorageUnavailableError

    async def put(self, *, key: str, content: bytes, content_type: str) -> None:
        path = self._object_path(key)
        if len(content) > MAX_OBJECT_BYTES:
            raise ValueError("Object exceeds the storage size limit")
        await self.ensure_private_bucket()
        await self._request("POST", path, content=content, content_type=content_type, upsert=True)

    async def read(self, *, key: str) -> bytes:
        path = self._object_path(key, authenticated=True)
        await self.ensure_private_bucket()
        return await self._request("GET", path, missing_key=key, max_bytes=MAX_OBJECT_BYTES)

    async def delete(self, *, key: str) -> None:
        path = self._object_path(key)
        await self.ensure_private_bucket()
        try:
            await self._request("DELETE", path, missing_key=key)
        except ObjectNotFoundError:
            pass

    def _object_path(self, key: str, *, authenticated: bool = False) -> str:
        relative = PurePosixPath(key)
        if (
            not key
            or len(key) > 512
            or relative.is_absolute()
            or relative.as_posix() != key
            or any(part in {".", ".."} for part in relative.parts)
            or any(char in key for char in "\\%?#:")
            or any(ord(char) < 32 or ord(char) == 127 for char in key)
        ):
            raise ValueError("Invalid object storage key")
        prefix = "/object/authenticated" if authenticated else "/object"
        return f"{prefix}/{self._bucket}/{quote(key, safe='/')}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
        content_type: str | None = None,
        upsert: bool = False,
        missing_key: str | None = None,
        max_bytes: int = MAX_CONTROL_BYTES,
    ) -> bytes:
        headers = {"apikey": self._key}
        if content_type is not None:
            headers["Content-Type"] = content_type
            headers["Cache-Control"] = "no-store"
        if upsert:
            headers["x-upsert"] = "true"
        try:
            async with self._client.stream(
                method,
                self._url + path,
                headers=headers,
                content=content,
                follow_redirects=False,
            ) as response:
                if response.status_code == 404 and missing_key is not None:
                    raise ObjectNotFoundError(missing_key)
                if not 200 <= response.status_code < 300:
                    if response.status_code == 400 and missing_key is not None:
                        # Storage can return HTTP 400 with statusCode="404" for a missing object.
                        body = await self._read_bounded(response, MAX_CONTROL_BYTES)
                        try:
                            error = json.loads(body)
                        except (ValueError, UnicodeError):
                            error = None
                        if isinstance(error, dict) and str(error.get("statusCode")) == "404":
                            raise ObjectNotFoundError(missing_key)
                    raise ObjectStorageUnavailableError
                return await self._read_bounded(response, max_bytes)
        except httpx.HTTPError:
            # Avoid a chained request exception or arbitrary upstream body in app logs.
            raise ObjectStorageUnavailableError from None

    @staticmethod
    async def _read_bounded(response: httpx.Response, limit: int) -> bytes:
        content = bytearray()
        async for chunk in response.aiter_bytes():
            if len(content) + len(chunk) > limit:
                raise ObjectStorageUnavailableError
            content.extend(chunk)
        return bytes(content)
