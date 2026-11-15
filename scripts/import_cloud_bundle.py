"""Verify and insert a private deployment bundle into an EMPTY Supabase project."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
from export_deployment import verify_bundle
from pydantic import SecretStr

from predatory_beavers.deployment.transfer import (
    assert_target_empty,
    insert_snapshot,
    lock_transfer,
    media_files,
    snapshot_rows,
    source_engine,
    target_engine,
    upload_media,
)
from predatory_beavers.integrations.object_storage.supabase import SupabaseObjectStorage
from predatory_beavers.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


def settings_environment(settings: Settings) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("APP_")}
    for field in type(settings).model_fields:
        value = getattr(settings, field)
        if value is None:
            continue
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        elif isinstance(value, (list, dict, bool)):
            value = json.dumps(value)
        environment["APP_" + field.upper()] = str(value)
    return environment


async def run_transfer(bundle: Path, settings: Settings, *, apply: bool) -> dict[str, object]:
    # Validate the remote target BEFORE migrations or uploads. An incomplete
    # configuration must never fall back to migrating the local default SQLite.
    if apply and (
        not settings.database_url.startswith("postgresql+psycopg://")
        or settings.database_schema == "public"
        or settings.media_storage_backend != "supabase"
    ):
        raise ValueError(
            "Cloud transfer requires PostgreSQL, a private schema and Supabase Storage"
        )
    verify_bundle(bundle)
    source = source_engine(bundle / "data/local.db")
    try:
        with source.connect() as connection:
            rows = snapshot_rows(connection)
    finally:
        source.dispose()
    files = media_files(bundle, rows)
    report: dict[str, object] = {
        "mode": "applied" if apply else "bundle_check_only",
        "tables": {name: len(records) for name, records in rows.items()},
        "media_objects": len(files),
        "sessions_transferred": 0,
    }
    if not apply:
        return report
    if settings.media_storage_backend != "supabase":
        raise ValueError("The cloud transfer requires private Supabase Storage")
    # Do not relay a driver traceback: it can contain a credential-bearing connection URL.
    migration = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "-x", "skip_content_seed=true", "upgrade", "head"],
        cwd=ROOT,
        env=settings_environment(settings),
        capture_output=True,
        timeout=180,
        check=False,
    )
    if migration.returncode:
        raise RuntimeError("Cloud schema migration failed; check connection settings")
    target = target_engine(settings)
    try:
        with target.begin() as connection:
            lock_transfer(connection)
            assert_target_empty(connection)
            assert settings.supabase_url is not None and settings.supabase_secret_key is not None
            async with httpx.AsyncClient(
                timeout=settings.media_storage_timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                storage = SupabaseObjectStorage(
                    settings.supabase_url,
                    settings.supabase_secret_key.get_secret_value(),
                    settings.supabase_storage_bucket,
                    client=client,
                )
                await storage.ensure_private_bucket(create=True)
                await upload_media(storage, files)
            insert_snapshot(connection, rows)
            # The cloud sync module imports the last successful source baselines and
            # portraits, so a first refresh retains editorial changes and cached photos.
            from predatory_beavers.worker.cloud_sync import bootstrap_runtime

            await bootstrap_runtime(connection, bundle / "data/sports-sync", settings)
    finally:
        target.dispose()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.render")
    parser.add_argument(
        "--apply", action="store_true", help="Create schema, upload private media and insert rows"
    )
    arguments = parser.parse_args()
    try:
        if arguments.apply and not arguments.env_file.is_file():
            raise ValueError("The populated cloud environment file must exist")
        # A bundle-only check never needs credentials and never contacts any cloud service.
        settings = (
            Settings(_env_file=arguments.env_file) if arguments.apply else Settings(_env_file=None)
        )
        report = asyncio.run(
            run_transfer(arguments.bundle.absolute(), settings, apply=arguments.apply)
        )
    except Exception as error:
        parser.exit(
            1, f"Cloud transfer failed ({type(error).__name__}); no credentials displayed.\n"
        )
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
