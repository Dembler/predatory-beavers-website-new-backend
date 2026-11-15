"""Attach reviewed ASB portraits to existing captains, preserving editor choices.

Preview is read-only. --apply backs up and migrates the working database first.
Images come from the verified offline roster cache, never a guessed player name.
"""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID

from import_public_results import backup_database, migrate, stable_id
from sqlalchemy import select

from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.audit.models import AdminAuditLog, AuditAction
from predatory_beavers.modules.club.models import MediaAsset
from predatory_beavers.modules.content.models import ContentBlock
from predatory_beavers.modules.content.schemas import ContentRead
from predatory_beavers.modules.media.service import _process_image
from predatory_beavers.modules.roster.photos import cached_photo
from predatory_beavers.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
REVIEWED = {"56903": "Леонид Юдин", "442828": "Дарья Черевко"}


def portraits() -> list[tuple[str, Path]]:
    roster = json.loads((ROOT / "docs/asb-roster.json").read_text(encoding="utf8"))
    result = []
    for snapshot in roster["snapshots"]:
        for player in snapshot["players"]:
            name = REVIEWED.get(player["external_player_id"])
            if name:
                if not set(name.split()) <= set(player["full_name"].split()):
                    raise ValueError(
                        "Reviewed captain identity no longer matches the official roster"
                    )
                path = cached_photo(player["photo_url"])
                if path is None:
                    raise ValueError(f"Verified offline portrait missing: {name}")
                result.append((name, path))
    if len(result) != len(REVIEWED):
        raise ValueError("Reviewed captain portrait identities are missing or duplicated")
    return result


async def seed(database: Path) -> int:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{database.as_posix()}")
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    count = 0
    try:
        async with factory() as session:
            for name, portrait in portraits():
                block = await session.scalar(
                    select(ContentBlock).where(
                        ContentBlock.section == "captains",
                        ContentBlock.title == name,
                        ContentBlock.is_deleted.is_(False),
                    )
                )
                if block is None or block.photo_asset_id is not None:
                    continue
                before = ContentRead.model_validate(block).model_dump(mode="json")
                image = _process_image(portrait.read_bytes(), 4096, 85)
                asset = await session.scalar(
                    select(MediaAsset).where(MediaAsset.checksum == image.checksum)
                )
                if asset is not None and asset.is_deleted:
                    continue
                if asset is None:
                    key = f"images/captain-{image.checksum}.webp"
                    target = ROOT / "data/media" / key
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(image.content)
                    asset = MediaAsset(
                        id=UUID(stable_id("captain-photo", image.checksum)),
                        storage_key=key,
                        mime="image/webp",
                        size=len(image.content),
                        width=image.width,
                        height=image.height,
                        checksum=image.checksum,
                        alt_text=name,
                    )
                    session.add(asset)
                    await session.flush()
                block.photo, block.photo_asset_id = asset, asset.id
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
                count += 1
            await session.commit()
    finally:
        await engine.dispose()
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data/local.db")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    database = args.database.resolve(strict=True)
    if database == (ROOT / "data/predatory_beavers.db").resolve():
        raise SystemExit("The source snapshot is not an import target")
    print("Reviewed portraits:", len(portraits()))
    if args.apply:
        print("Backup:", backup_database(database))
        migrate(database)
        print("Captain portraits attached:", asyncio.run(seed(database)))


if __name__ == "__main__":
    main()
