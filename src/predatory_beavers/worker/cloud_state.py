"""Private persistent scheduler state and a bounded replica portrait cache."""

import asyncio
import hashlib
import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from predatory_beavers.integrations.object_storage.client import ObjectStorage
from predatory_beavers.modules.imports.models import CloudSportsState

logger = logging.getLogger(__name__)
PORTRAIT_PREFIX = "runtime/portraits/"
MANIFESTS = frozenset(
    {
        "results-asb.json",
        "results-fiba3x3.json",
        "asb-roster.json",
        "asb-standings.json",
        "fiba-watchlist.json",
    }
)


async def read_cloud_state(engine: AsyncEngine, key: str) -> dict[str, Any]:
    async with engine.connect() as connection:
        value = await connection.scalar(
            select(CloudSportsState.value).where(CloudSportsState.key == key)
        )
    return value if isinstance(value, dict) else {}


def portrait_entries(document: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Accept only bounded content-addressed portraits bound to their source URL."""
    if len(document) > 1000:
        raise ValueError("Too many portrait cache entries")
    accepted = {}
    for key, entry in document.items():
        if (
            not re.fullmatch(r"[a-f0-9]{64}", key)
            or not isinstance(entry, dict)
            or not isinstance(entry.get("source_url"), str)
            or hashlib.sha256(entry["source_url"].encode()).hexdigest() != key
            or not re.fullmatch(r"[a-f0-9]{64}", entry.get("sha256", ""))
        ):
            raise ValueError("Invalid portrait cache entry")
        accepted[key] = {"source_url": entry["source_url"], "sha256": entry["sha256"]}
    return accepted


def replace_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


async def pull_portrait_cache(engine: AsyncEngine, storage: ObjectStorage, directory: Path) -> None:
    document = portrait_entries(await read_cloud_state(engine, "portraits"))
    # Each immutable object is written before publishing the index. A failed
    # download leaves the previous complete index available to public requests.
    for entry in document.values():
        path = directory / "objects" / f"{entry['sha256']}.webp"
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]:
            continue
        content = await storage.read(key=PORTRAIT_PREFIX + entry["sha256"] + ".webp")
        if len(content) > 8 * 1024 * 1024 or hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise ValueError("Invalid portrait object")
        replace_bytes(path, content)
    replace_bytes(directory / "index.json", json.dumps(document).encode())


async def refresh_portrait_cache(
    engine: AsyncEngine, storage: ObjectStorage, directory: Path, *, interval: int = 300
) -> None:
    while True:
        try:
            await pull_portrait_cache(engine, storage, directory)
        except Exception as exc:
            logger.warning("Cloud portrait cache refresh failed (%s)", type(exc).__name__)
        await asyncio.sleep(interval)
