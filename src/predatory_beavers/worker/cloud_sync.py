"""Bootstrap durable sports control documents from a verified private bundle."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import insert, select
from sqlalchemy.engine import Connection

from predatory_beavers.integrations.object_storage.supabase import SupabaseObjectStorage
from predatory_beavers.modules.imports.models import CloudSportsState
from predatory_beavers.settings import Settings
from predatory_beavers.worker.cloud_state import MANIFESTS, PORTRAIT_PREFIX, portrait_entries


async def bootstrap_runtime(connection: Connection, runtime_root: Path, settings: Settings) -> None:
    """Run inside first-transfer transaction after verified media upload.

    Existing SQL state is preserved. Immutable portrait objects are uploaded
    first; their SQL index is published only after every hash has been checked.
    """
    if not await asyncio.to_thread(runtime_root.is_dir):
        return

    def seed(key: str, value: dict[str, Any]) -> None:
        if not connection.scalar(select(CloudSportsState.key).where(CloudSportsState.key == key)):
            connection.execute(
                insert(CloudSportsState).values(key=key, value=value, updated_at=datetime.now(UTC))
            )

    for name in MANIFESTS:
        path = runtime_root / (name if name == "fiba-watchlist.json" else f"working/docs/{name}")
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Invalid bootstrap sports document")
            seed("document/" + name, value)
    status = runtime_root / "status.json"
    if status.is_file():
        value = json.loads(status.read_text(encoding="utf-8"))
        seed(
            "status",
            {
                key: value.get(key)
                for key in (
                    "state",
                    "sources",
                    "attempt_started_at",
                    "attempt_finished_at",
                    "next_attempt_at",
                    "last_success_at",
                    "last_error",
                )
            },
        )
    photos = runtime_root / "roster-photos"
    if not (photos / "index.json").is_file():
        return
    document = portrait_entries(json.loads((photos / "index.json").read_text(encoding="utf-8")))
    assert settings.supabase_url and settings.supabase_secret_key
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
        storage = SupabaseObjectStorage(
            settings.supabase_url,
            settings.supabase_secret_key.get_secret_value(),
            settings.supabase_storage_bucket,
            client=client,
        )
        for key, entry in document.items():
            content = (photos / f"{key}.webp").read_bytes()
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise ValueError("Invalid bootstrap portrait")
            await storage.put(
                key=PORTRAIT_PREFIX + entry["sha256"] + ".webp",
                content=content,
                content_type="image/webp",
            )
    seed("portraits", document)
