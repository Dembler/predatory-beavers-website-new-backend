"""Bundled, allowlisted roster portraits. No request-time network or remote proxy."""

import hashlib
import json
from pathlib import Path

PHOTO_DIRECTORY = Path(__file__).resolve().parent / "assets" / "photos"


def photo_key(source_url: str) -> str:
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()


def cached_photo(source_url: str | None) -> Path | None:
    if not source_url:
        return None
    index_file = PHOTO_DIRECTORY / "index.json"
    if not index_file.is_file():
        return None
    entries = json.loads(index_file.read_text(encoding="utf-8"))
    key = photo_key(source_url)
    entry = entries.get(key)
    # Bind the exact official source URL to a deterministic local filename.
    if not isinstance(entry, dict) or entry.get("source_url") != source_url:
        return None
    path = PHOTO_DIRECTORY / f"{key}.webp"
    if not path.is_file():
        return None
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry.get("sha256"):
        return None
    return path
