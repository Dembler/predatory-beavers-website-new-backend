"""Pure security helpers for the mascot prototype.

The opaque session token is stored only as a SHA-256 hash; the client-readable
CSRF token is a domain-separated hash of that token, mirroring the existing
auth module. ``session_id`` is never a URL token and never grants access by
itself.
"""

from __future__ import annotations

import hashlib
import secrets
from urllib.parse import urlsplit

_CSRF_DOMAIN = "mascot-csrf-v1"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_opaque_token() -> str:
    return secrets.token_urlsafe(32)


def mascot_csrf_token(session_token: str) -> str:
    return hashlib.sha256((_CSRF_DOMAIN + ":" + session_token).encode("utf-8")).hexdigest()


def origin_allowed(origin: str | None, allowed_origins: list[str]) -> bool:
    """CSRF origin gate: an explicit Origin must match the configured allowlist.

    A missing Origin header (same-origin fetch, CLI, tests) is accepted; this is
    the conservative prototype posture documented in the plan (§18.5).
    """
    if not origin:
        return True
    normalized = origin.rstrip("/")
    return any(_normalize_origin(allowed) == normalized for allowed in allowed_origins)


def _normalize_origin(origin: str) -> str:
    # Compare scheme/host/port exactly; ignore a trailing slash.
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return origin.rstrip("/")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}".rstrip("/")
