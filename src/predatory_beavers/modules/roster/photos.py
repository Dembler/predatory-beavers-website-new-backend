"""Bundled, allowlisted roster portraits. No request-time network or remote proxy."""

import hashlib
import json
import re
from pathlib import Path

from sqlalchemy.engine import make_url

from predatory_beavers.settings import get_settings

PHOTO_DIRECTORY = Path(__file__).resolve().parent / "assets" / "photos"


def photo_key(source_url: str) -> str:
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()


def cached_photo(source_url: str | None) -> Path | None:
    if not source_url:
        return None
    settings = get_settings()
    url = make_url(settings.database_url)
    cache = (
        Path(url.database or "data/local.db").parent / "sports-sync"
        if url.get_backend_name() == "sqlite"
        else settings.sports_sync_cache_path
    )
    for directory in [cache / "roster-photos", PHOTO_DIRECTORY]:
        path = _cached_photo_in(directory, source_url)
        if path is not None:
            return path
    return None


def _cached_photo_in(directory: Path, source_url: str) -> Path | None:
    index_file = directory / "index.json"
    if not index_file.is_file():
        return None
    try:
        entries = json.loads(index_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    key = photo_key(source_url)
    entry = entries.get(key)
    # Bind the exact official source URL to a deterministic local filename.
    if not isinstance(entry, dict) or entry.get("source_url") != source_url:
        return None
    checksum = entry.get("sha256")
    if not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
        return None
    # Replica caches use immutable content-addressed files, so a failed refresh
    # cannot overwrite a portrait still referenced by the last complete index.
    path = directory / "objects" / f"{checksum}.webp"
    if not path.is_file():
        path = directory / f"{key}.webp"
    if not path.is_file():
        return None
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry.get("sha256"):
        return None
    return path
