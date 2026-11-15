"""Render's Docker command: validate durable storage, migrate, then serve $PORT."""

from __future__ import annotations

import os
import sys

from alembic import command
from alembic.config import Config

from predatory_beavers.settings import get_settings


def main() -> None:
    # Render injects PORT. Normal local and Compose starts still use run.py.
    port = os.environ.get("PORT", "10000")
    if not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("PORT must be an integer between 1 and 65535")
    os.environ["APP_HOST"] = "0.0.0.0"
    os.environ["APP_PORT"] = port
    settings = get_settings()
    if (
        settings.env != "prod"
        or not settings.database_url.startswith("postgresql+psycopg://")
        or settings.database_schema == "public"
        or settings.media_storage_backend != "supabase"
        or settings.sports_sync_enabled
        or not settings.sports_sync_external_enabled
    ):
        raise ValueError(
            "Render requires production settings, a private PostgreSQL schema, "
            "Supabase media and the external cloud collector"
        )
    # The startup command is used because Free Render has no pre-deploy command.
    command.upgrade(Config("alembic.ini"), "head")
    os.execv(sys.executable, [sys.executable, "run.py"])


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Driver/configuration errors can include connection details; keep logs secret-free.
        print(
            f"Render startup failed ({type(error).__name__}); check configuration.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
