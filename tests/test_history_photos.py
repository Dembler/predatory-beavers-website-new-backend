"""Reviewed history imports must be safe to preview and preserve editorial choices."""

import asyncio
import json
import sqlite3
import sys
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from PIL import Image

from predatory_beavers.db.base import Base
from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.auth import models as _auth_models  # noqa: F401
from predatory_beavers.modules.club.models import MediaAsset
from predatory_beavers.modules.content.models import ContentBlock
from predatory_beavers.modules.media.service import _process_image
from predatory_beavers.settings import Settings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from seed_history_photos import attach, reviewed_photos  # noqa: E402


@dataclass(frozen=True)
class HistoryDatabase:
    database: Path
    media: Path
    first: UUID
    second: UUID


@pytest.fixture
def history_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HistoryDatabase:
    database = tmp_path / "history.sqlite"
    media = tmp_path / "media"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_MEDIA_STORAGE_PATH", str(media))
    settings = Settings(database_url=f"sqlite+aiosqlite:///{database.as_posix()}")
    first, second = uuid4(), uuid4()

    async def seed() -> None:
        engine = create_engine(settings)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with create_session_factory(engine)() as session:
                session.add_all(
                    [
                        ContentBlock(
                            id=first, section="history", title="First event", body="Our history"
                        ),
                        ContentBlock(
                            id=second,
                            section="history",
                            title="Second event",
                            body="Edited history",
                            link_url="https://example.test/editor-source",
                            link_label="Editor source",
                        ),
                    ]
                )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(seed())
    return HistoryDatabase(database, media, first, second)


def write_manifest(tmp_path: Path, chapter_ids: list[UUID]) -> tuple[Path, Path]:
    assets = tmp_path / "reviewed-images"
    assets.mkdir(exist_ok=True)
    Image.new("RGB", (80, 60), "navy").save(assets / "event.webp", "WEBP")
    manifest = tmp_path / "history-photos.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {
                        "chapter_id": str(identity),
                        "file": "event.webp",
                        "source_url": f"https://example.test/events/{index}",
                        "image_url": "https://example.test/photos/event.webp",
                        "source_label": "Event report and photographs",
                        "alt": "Reviewed event photograph. Photo: University.",
                    }
                    for index, identity in enumerate(chapter_ids)
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest, assets


def table_rows(database: Path, table: str) -> list[dict[str, object]]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]


def test_history_preview_does_not_write_database_or_media(
    history_database: HistoryDatabase, tmp_path: Path
) -> None:
    database = history_database
    manifest, assets = write_manifest(tmp_path, [database.first])
    before = database.database.read_bytes()

    items = reviewed_photos(manifest, assets)
    assert asyncio.run(attach(database.database, items)) == 1

    assert database.database.read_bytes() == before
    assert not database.media.exists()
    assert table_rows(database.database, "media_assets") == []
    assert table_rows(database.database, "admin_audit_log") == []


def test_history_attachment_audits_reuses_media_and_is_idempotent(
    history_database: HistoryDatabase, tmp_path: Path
) -> None:
    database = history_database
    manifest, assets = write_manifest(tmp_path, [database.first, database.second])
    items = reviewed_photos(manifest, assets)

    assert asyncio.run(attach(database.database, items, apply=True)) == 2
    chapters = table_rows(database.database, "content_blocks")
    media = table_rows(database.database, "media_assets")
    audits = table_rows(database.database, "admin_audit_log")
    assert len(media) == 1
    assert {row["photo_asset_id"] for row in chapters} == {media[0]["id"]}
    assert chapters[0]["link_url"] == items[0]["source_url"]
    assert chapters[0]["link_label"] == "Event report and photographs"
    assert chapters[1]["link_url"] == "https://example.test/editor-source"
    assert chapters[1]["link_label"] == "Editor source"
    stored = database.media / str(media[0]["storage_key"])
    assert stored.read_bytes() == items[0]["image"].content
    assert media[0]["checksum"] == items[0]["image"].checksum
    assert len(list(database.media.rglob("*.webp"))) == 1
    assert len(audits) == 2
    assert {row["entity_id"] for row in audits} == {str(database.first), str(database.second)}
    for audit in audits:
        assert audit["actor_username"] == "system"
        assert audit["action"] == "UPDATE"
        assert audit["entity_type"] == "content_block"
        before, after = json.loads(str(audit["before"])), json.loads(str(audit["after"]))
        assert before["photo_asset_id"] is None
        assert after["photo_asset_id"] == str(UUID(str(media[0]["id"])))
        assert after["photo"]["id"] == after["photo_asset_id"]
        assert after["photo"]["alt_text"] == items[0]["alt"]
        assert after["photo"]["width"] == 80
        assert after["photo"]["height"] == 60
        assert before["body"] == after["body"]

    before_repeat = database.database.read_bytes()
    assert asyncio.run(attach(database.database, items, apply=True)) == 0
    assert database.database.read_bytes() == before_repeat
    assert table_rows(database.database, "admin_audit_log") == audits
    assert stored.read_bytes() == items[0]["image"].content


def test_history_attachment_preserves_photo_chosen_by_editor(
    history_database: HistoryDatabase, tmp_path: Path
) -> None:
    database = history_database
    output = BytesIO()
    Image.new("RGB", (30, 40), "red").save(output, "PNG")
    edited_image = _process_image(output.getvalue(), 1920, 88)
    editor_photo_id = uuid4()

    async def edit_photo() -> None:
        settings = Settings(database_url=f"sqlite+aiosqlite:///{database.database.as_posix()}")
        engine = create_engine(settings)
        try:
            async with create_session_factory(engine)() as session:
                asset = MediaAsset(
                    id=editor_photo_id,
                    storage_key="images/editor.webp",
                    mime="image/webp",
                    size=len(edited_image.content),
                    width=edited_image.width,
                    height=edited_image.height,
                    checksum=edited_image.checksum,
                    alt_text="Chosen by editor",
                )
                session.add(asset)
                await session.flush()
                chapter = await session.get(ContentBlock, database.second)
                assert chapter is not None
                chapter.photo, chapter.photo_asset_id = asset, asset.id
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(edit_photo())
    stored = database.media / "images/editor.webp"
    stored.parent.mkdir(parents=True)
    stored.write_bytes(edited_image.content)
    manifest, assets = write_manifest(tmp_path, [database.second])
    items = reviewed_photos(manifest, assets)
    before = database.database.read_bytes()

    assert asyncio.run(attach(database.database, items, apply=True)) == 0

    assert database.database.read_bytes() == before
    selected_photo = table_rows(database.database, "content_blocks")[1]["photo_asset_id"]
    assert selected_photo == editor_photo_id.hex
    assert len(table_rows(database.database, "media_assets")) == 1
    assert table_rows(database.database, "admin_audit_log") == []
    assert list(database.media.rglob("*.webp")) == [stored]
    assert stored.read_bytes() == edited_image.content


def test_history_manifest_rejects_duplicate_chapters_and_escaped_asset_paths(
    tmp_path: Path,
) -> None:
    identity = uuid4()
    manifest, assets = write_manifest(tmp_path, [identity, identity])
    with pytest.raises(ValueError, match="Duplicate chapter"):
        reviewed_photos(manifest, assets)

    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["items"] = document["items"][:1]
    document["items"][0]["file"] = "../outside.webp"
    Image.new("RGB", (20, 20)).save(tmp_path / "outside.webp", "WEBP")
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="inside the history assets directory"):
        reviewed_photos(manifest, assets)
