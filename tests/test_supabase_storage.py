"""Private REST storage never exposes bucket URLs, provider errors, or API secrets."""

import json
import traceback
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from predatory_beavers.api.dependency_injection import build_container
from predatory_beavers.api.errors import NotFoundError
from predatory_beavers.integrations.object_storage.client import (
    ObjectStorage,
    ObjectStorageUnavailableError,
)
from predatory_beavers.integrations.object_storage.local import (
    LocalObjectStorage,
    ObjectNotFoundError,
)
from predatory_beavers.integrations.object_storage.supabase import SupabaseObjectStorage
from predatory_beavers.modules.audit.provider import AuditProvider
from predatory_beavers.modules.media.provider import MediaProvider
from predatory_beavers.modules.media.service import MediaService
from predatory_beavers.settings import Settings

SECRET = "sb_secret_storage_test_only"
PROJECT = "https://project.supabase.co"
BUCKET = {"id": "club-media", "public": False}


def storage(client: httpx.AsyncClient) -> SupabaseObjectStorage:
    return SupabaseObjectStorage(PROJECT, SECRET, "club-media", client=client)


@pytest.mark.asyncio
async def test_authenticated_download_upload_and_delete_with_secret_apikey() -> None:
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["apikey"] == SECRET
        assert "authorization" not in request.headers
        assert request.url.host == "project.supabase.co"
        assert not request.url.query
        if request.url.path == "/storage/v1/bucket/club-media":
            return httpx.Response(200, json=BUCKET)
        if request.method == "GET":
            assert (
                request.url.path == "/storage/v1/object/authenticated/club-media/images/photo.webp"
            )
            return httpx.Response(200, content=b"private-photo")
        assert request.url.path == "/storage/v1/object/club-media/images/photo.webp"
        if request.method == "POST":
            assert request.content == b"private-photo"
            assert request.headers["content-type"] == "image/webp"
            assert request.headers["x-upsert"] == "true"
            assert request.headers["cache-control"] == "no-store"
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = storage(client)
        await adapter.put(
            key="images/photo.webp", content=b"private-photo", content_type="image/webp"
        )
        assert await adapter.read(key="images/photo.webp") == b"private-photo"
        await adapter.delete(key="images/photo.webp")
    assert [request.method for request in requests] == [
        "GET",
        "POST",
        "GET",
        "GET",
        "GET",
        "DELETE",
    ]
    assert all(
        "/public/" not in str(request.url) and "/sign/" not in str(request.url)
        for request in requests
    )


@pytest.mark.asyncio
async def test_explicit_bootstrap_creates_missing_bucket_private_and_checks_metadata() -> None:
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["apikey"] == SECRET
        if request.method == "POST":
            assert request.url.path == "/storage/v1/bucket"
            assert json.loads(request.content) == {
                "id": "club-media",
                "name": "club-media",
                "public": False,
            }
            return httpx.Response(200, json={"name": "club-media"})
        if len(requests) == 1:
            return httpx.Response(404, json={"message": "Bucket not found"})
        return httpx.Response(200, json=BUCKET)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await storage(client).ensure_private_bucket(create=True)
    assert [request.method for request in requests] == ["GET", "POST", "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"id": "club-media", "public": True},
        {"id": "club-media", "public": 0},
        {"id": "club-media"},
        {"id": "different", "public": False},
        [],
    ],
)
async def test_public_or_unverified_bucket_blocks_upload_and_download(metadata) -> None:
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=metadata)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = storage(client)
        for operation in [
            adapter.put(key="images/draft.webp", content=b"draft", content_type="image/webp"),
            adapter.read(key="images/draft.webp"),
            adapter.delete(key="images/draft.webp"),
        ]:
            with pytest.raises(ObjectStorageUnavailableError):
                await operation
    assert len(requests) == 3
    assert all(request.url.path == "/storage/v1/bucket/club-media" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 302])
async def test_bucket_errors_and_redirects_fail_closed_without_response_secrets(status) -> None:
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            status, text=SECRET, headers={"Location": "https://other.example/secret"}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=True
    ) as client:
        with pytest.raises(ObjectStorageUnavailableError) as error:
            await storage(client).read(key="images/photo.webp")
    assert SECRET not in str(error.value)
    assert SECRET not in repr(error.value)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [(404, {}), (400, {"statusCode": "404"})])
async def test_missing_object_maps_to_local_error_and_delete_is_idempotent(status, body) -> None:
    def handle(request):
        if request.url.path.startswith("/storage/v1/bucket/"):
            return httpx.Response(200, json=BUCKET)
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = storage(client)
        with pytest.raises(ObjectNotFoundError):
            await adapter.read(key="runtime/missing.json")
        await adapter.delete(key="runtime/missing.json")


@pytest.mark.asyncio
async def test_timeout_error_is_sanitized_and_not_chained() -> None:
    def handle(request):
        raise httpx.ReadTimeout(SECRET, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ObjectStorageUnavailableError) as error:
            await storage(client).read(key="images/photo.webp")
    rendered = "".join(traceback.format_exception(error.value))
    assert SECRET not in rendered
    assert error.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_internal_runtime_keys_stay_private_and_can_be_updated() -> None:
    objects = {}

    def handle(request):
        if request.url.path.startswith("/storage/v1/bucket/"):
            return httpx.Response(200, json=BUCKET)
        path = request.url.path.split("club-media/", 1)[1]
        if request.method == "POST":
            objects[path] = request.content
            return httpx.Response(200, json={})
        return httpx.Response(200, content=objects[path])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = storage(client)
        await adapter.put(
            key="runtime/portraits/player.webp", content=b"portrait", content_type="image/webp"
        )
        await adapter.put(
            key="runtime/sources.json",
            content=b'{"status":"partial"}',
            content_type="application/json",
        )
        assert await adapter.read(key="runtime/portraits/player.webp") == b"portrait"
        assert json.loads(await adapter.read(key="runtime/sources.json")) == {"status": "partial"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        "../private",
        "/private",
        "https://other/private",
        "images/../private",
        "images//photo",
        "x\\y",
        "x%2fy",
        "x?key",
        "x#key",
        "",
    ],
)
async def test_invalid_keys_never_make_a_request(key) -> None:
    def handle(request):
        raise AssertionError("An invalid key must not trigger HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ValueError, match="storage key"):
            await storage(client).read(key=key)


@pytest.mark.asyncio
async def test_storage_response_size_limit(monkeypatch) -> None:
    monkeypatch.setattr(
        "predatory_beavers.integrations.object_storage.supabase.MAX_OBJECT_BYTES", 4
    )

    def handle(request):
        if request.url.path.startswith("/storage/v1/bucket/"):
            return httpx.Response(200, json=BUCKET)
        return httpx.Response(200, content=b"oversized-photo")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ObjectStorageUnavailableError):
            await storage(client).read(key="images/photo.webp")


@pytest.mark.asyncio
async def test_private_media_authorization_runs_before_storage_read() -> None:
    class Repository:
        async def get(self, asset_id):
            return SimpleNamespace(storage_key="images/draft.webp", legacy_public=False)

        async def is_publicly_referenced(self, asset_id):
            return False

    class Storage:
        async def read(self, *, key):
            raise AssertionError("Unauthorized draft must never be downloaded from Supabase")

    service = MediaService(Repository(), Storage(), None, Settings(_env_file=None))
    with pytest.raises(NotFoundError):
        await service.get_content(uuid4())


@pytest.mark.asyncio
async def test_provider_preserves_local_and_closes_supabase_client(
    tmp_path: Path, monkeypatch
) -> None:
    local_settings = Settings(_env_file=None, media_storage_path=tmp_path / "media")
    container = build_container(local_settings, MediaProvider(), AuditProvider())
    local = await container.get(ObjectStorage)
    assert isinstance(local, LocalObjectStorage)
    await local.put(key="images/photo.webp", content=b"photo", content_type="image/webp")
    assert await local.read(key="images/photo.webp") == b"photo"
    await container.close()

    created = []
    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        client = real_client(transport=httpx.MockTransport(lambda _: httpx.Response(200)), **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(
        "predatory_beavers.modules.media.provider.httpx.AsyncClient", client_factory
    )
    settings = Settings(
        _env_file=None,
        media_storage_backend="supabase",
        supabase_url=PROJECT,
        supabase_secret_key=SECRET,
    )
    container = build_container(settings, MediaProvider(), AuditProvider())
    assert isinstance(await container.get(ObjectStorage), SupabaseObjectStorage)
    assert len(created) == 1 and not created[0].is_closed
    await container.close()
    assert created[0].is_closed


def test_settings_require_server_secret_and_hide_secret_in_repr_and_errors() -> None:
    settings = Settings(
        _env_file=None,
        media_storage_backend="supabase",
        supabase_url=PROJECT,
        supabase_secret_key=SECRET,
    )
    assert SECRET not in repr(settings)
    assert SECRET not in settings.model_dump_json()
    for changes in [
        {"supabase_url": None},
        {"supabase_secret_key": "sb_publishable_not_private"},
        {"supabase_url": PROJECT + "/storage/v1"},
        {"supabase_url": "http://project.supabase.co"},
        {"supabase_storage_bucket": "../public"},
    ]:
        arguments = {
            "media_storage_backend": "supabase",
            "supabase_url": PROJECT,
            "supabase_secret_key": SECRET,
        } | changes
        with pytest.raises(ValidationError) as error:
            Settings(_env_file=None, **arguments)
        assert SECRET not in str(error.value)
