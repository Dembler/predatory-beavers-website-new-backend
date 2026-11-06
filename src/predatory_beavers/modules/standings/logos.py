"""Reviewed official ASB emblems served from the bundled offline cache."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

LOGO_DIRECTORY = Path(__file__).resolve().parent / "assets" / "logos"
KEY_PATTERN = re.compile(r"^[a-f0-9]{64}$")


def logo_key(source_url: str) -> str:
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()


def normalized_team_name(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


@lru_cache(maxsize=1)
def _index() -> dict[str, Any]:
    path = LOGO_DIRECTORY / "index.json"
    if not path.is_file():
        return {}
    return dict(json.loads(path.read_text(encoding="utf-8")))


@lru_cache(maxsize=128)
def get_logo_path(key: str) -> Path | None:
    """Resolve only a named, checksummed cache entry; never fetch remote URLs."""
    if not KEY_PATTERN.fullmatch(key):
        return None
    entry = _index().get("images", {}).get(key)
    if not isinstance(entry, dict):
        return None
    path = LOGO_DIRECTORY / f"{key}.webp"
    if not path.is_file():
        return None
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry.get("sha256"):
        return None
    return path


def team_logo_path(external_team_id: str | None, team_name: str) -> str | None:
    """Use an exact ASB identity and reviewed name; unrelated abbreviations never match."""
    if external_team_id is None:
        return None
    entry = _index().get("teams", {}).get(str(external_team_id))
    if not isinstance(entry, dict):
        return None
    aliases = {normalized_team_name(name) for name in entry.get("names", [])}
    if normalized_team_name(team_name) not in aliases:
        return None
    key = entry.get("key")
    if not isinstance(key, str) or get_logo_path(key) is None:
        return None
    return f"/api/v1/standings/logos/{key}.webp"
