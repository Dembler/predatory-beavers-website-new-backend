"""Attach reviewed event photographs to history without replacing editor choices.

Preview validates local originals and chapter identities without writing anything.
--apply backs up the working database and uses the regular image processor/audit.
Source URLs are provenance only: this command never downloads remote content.
"""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID

from import_public_results import backup_database, migrate, stable_id
from pydantic import HttpUrl, TypeAdapter
from sqlalchemy import select

from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.audit.models import AdminAuditLog, AuditAction
from predatory_beavers.modules.club.models import MediaAsset
from predatory_beavers.modules.content.models import ContentBlock
from predatory_beavers.modules.content.schemas import ContentRead
from predatory_beavers.modules.media.service import _process_image
from predatory_beavers.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src/predatory_beavers/modules/content/assets/history"


def reviewed_photos(manifest: Path, assets: Path = ASSETS) -> list[dict]:
    document = json.loads(manifest.read_text(encoding="utf-8"))
    if document.get("version") != 1 or not document.get("items"):
        raise ValueError("Unsupported or empty reviewed history manifest")
    items = []
    identities = set()
    for record in document["items"]:
        identity = UUID(record["chapter_id"])
        if identity in identities:
            raise ValueError("Duplicate chapter identity in history manifest")
        identities.add(identity)
        path = (assets / record["file"]).resolve(strict=True)
        if not path.is_relative_to(assets.resolve()) or not path.is_file():
            raise ValueError("Photo must be a local file inside the history assets directory")
        for field in ("source_url", "image_url"):
            TypeAdapter(HttpUrl).validate_python(record[field])
        caption = record["alt"].strip()
        if not caption or len(caption) > 500:
            raise ValueError("A reviewed photo caption is required (up to 500 characters)")
        image = _process_image(path.read_bytes(), 1920, 88)
        items.append({**record, "chapter_id": identity, "image": image, "alt": caption})
    return items


async def attach(database: Path, items: list[dict], *, apply: bool = False) -> int:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{database.as_posix()}")
    engine = create_engine(settings)
    count = 0
    try:
        async with create_session_factory(engine)() as session:
            chapters = []
            for record in items:
                block = await session.scalar(
                    select(ContentBlock).where(
                        ContentBlock.id == record["chapter_id"],
                        ContentBlock.section == "history",
                        ContentBlock.is_deleted.is_(False),
                    )
                )
                if block is None:
                    raise ValueError(f"Reviewed history chapter missing: {record['chapter_id']}")
                chapters.append((record, block))
            for record, block in chapters:
                if block.photo_asset_id is not None:
                    continue
                image = record["image"]
                asset = await session.scalar(
                    select(MediaAsset).where(MediaAsset.checksum == image.checksum)
                )
                if asset is not None and asset.is_deleted:
                    continue
                count += 1
                if not apply:
                    continue
                before = ContentRead.model_validate(block).model_dump(mode="json")
                if asset is None:
                    key = f"images/history-{image.checksum}.webp"
                    target = settings.media_storage_path / key
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(image.content)
                    asset = MediaAsset(
                        id=UUID(stable_id("history-photo", image.checksum)),
                        storage_key=key,
                        mime="image/webp",
                        size=len(image.content),
                        width=image.width,
                        height=image.height,
                        checksum=image.checksum,
                        alt_text=record["alt"],
                    )
                    session.add(asset)
                    await session.flush()
                block.photo, block.photo_asset_id = asset, asset.id
                if not block.link_url:
                    block.link_url = record["source_url"]
                    block.link_label = record.get("source_label", "ВГУ · фото и итоги события")
                await session.flush()
                session.add(
                    AdminAuditLog(
                        actor_username="system",
                        action=AuditAction.UPDATE,
                        entity_type="content_block",
                        entity_id=str(block.id),
                        before=before,
                        after=ContentRead.model_validate(block).model_dump(mode="json"),
                    )
                )
            if apply:
                await session.commit()
    finally:
        await engine.dispose()
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data/local.db")
    parser.add_argument("--manifest", type=Path, default=ROOT / "docs/history-photos.json")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    database = args.database.resolve(strict=True)
    if database == (ROOT / "data/predatory_beavers.db").resolve():
        raise SystemExit("The source snapshot is not an import target")
    items = reviewed_photos(args.manifest)
    print("Reviewed event photographs:", len(items))
    print("Empty chapter photos to fill:", asyncio.run(attach(database, items)))
    if args.apply:
        print("Backup:", backup_database(database))
        migrate(database)
        print("History photographs attached:", asyncio.run(attach(database, items, apply=True)))


if __name__ == "__main__":
    main()
