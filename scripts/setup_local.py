"""Inspect local database readiness; --apply backs it up before migration.

No existing user passwords are changed. If there is no active administrator,
create one with a random password in gitignored data/local-admin-credentials.json.
Use --apply --serve to start the normal local API after migration.
"""

import argparse
import asyncio
import json
import os
import secrets
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.auth.models import User, UserRole
from predatory_beavers.modules.auth.service import PasswordSecurity
from predatory_beavers.settings import Settings, get_settings

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DATABASE = ROOT / "data/predatory_beavers.db"
DATABASE = ROOT / "data/local.db"
CREDENTIALS = ROOT / "data/local-admin-credentials.json"


async def ensure_admin() -> None:
    engine = create_engine(Settings())
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            count = await session.scalar(
                select(func.count(User.id)).where(
                    User.role == UserRole.ADMIN,
                    User.is_active.is_(True),
                )
            )
            if count:
                print("Existing active administrator preserved; password unchanged")
                return
            username = "local-admin-" + secrets.token_hex(3)
            password = secrets.token_urlsafe(24)
            session.add(
                User(
                    username=username,
                    email=f"{username}@example.test",
                    role=UserRole.ADMIN,
                    password_hash=PasswordSecurity().hash_password(password),
                    is_active=True,
                )
            )
            await session.commit()
            CREDENTIALS.write_text(
                json.dumps(
                    {
                        "baseURL": "http://127.0.0.1:8000",
                        "frontendURL": "http://localhost:5173",
                        "username": username,
                        "password": password,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            print("Local administrator created. Credentials: data/local-admin-credentials.json")
    finally:
        await engine.dispose()


def inspect() -> None:
    inspected = DATABASE if DATABASE.exists() else SOURCE_DATABASE
    if not inspected.exists():
        raise SystemExit("Local database is missing; no changes made")
    if inspected != DATABASE:
        print("Source snapshot only; --apply creates ignored data/local.db")
    with sqlite3.connect(inspected.as_uri() + "?mode=ro", uri=True) as source:
        version = source.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        admins = source.execute(
            "SELECT count(*) FROM users WHERE role = 'ADMIN' AND is_active = 1",
        ).fetchone()[0]
        players = source.execute("SELECT count(*) FROM players").fetchone()[0]
        matches = source.execute("SELECT count(*) FROM matches").fetchone()[0]
    print(
        f"Schema: {version}; active administrators: {admins}; "
        f"players: {players}; matches: {matches}"
    )


def apply() -> None:
    if not DATABASE.exists():
        if not SOURCE_DATABASE.exists():
            raise SystemExit("Source snapshot is missing; no changes made")
        DATABASE.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(SOURCE_DATABASE.as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(DATABASE) as target:
                source.backup(target)
        print("Created ignored working database: data/local.db")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = DATABASE.with_name(f"{DATABASE.stem}.before-admin-{timestamp}-{uuid4().hex[:6]}.db")
    with sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(backup) as target:
            source.backup(target)
    print(f"Consistent backup created: data/{backup.name}")
    os.environ.update(
        {
            "APP_DATABASE_URL": f"sqlite+aiosqlite:///{DATABASE.as_posix()}",
            "APP_MEDIA_STORAGE_PATH": str(ROOT / "data/media"),
            "APP_ENV": "dev",
            "APP_COOKIE_SECURE": "false",
            "APP_RELOAD": "false",
            "APP_CORS_ORIGINS": '["http://localhost:5173","http://127.0.0.1:5173"]',
        }
    )
    os.environ.pop("APP_E2E_RUNTIME_TOKEN", None)
    get_settings.cache_clear()
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    asyncio.run(ensure_admin())
    # Append reviewed history on every setup; stable IDs preserve all editor changes.
    for manifest in ("legacy-content-manifest.json", "club-history-manifest.json"):
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/import_legacy_content.py"),
                "--database",
                str(DATABASE),
                "--manifest",
                str(ROOT / "docs" / manifest),
                "--apply",
            ],
            check=True,
        )
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/import_public_results.py"),
            "--database",
            str(DATABASE),
            "--apply",
        ],
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/sync_asb_roster.py"),
            "--database",
            str(DATABASE),
            "--apply",
        ],
        check=True,
    )
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/cache_asb_roster_photos.py")],
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/import_asb_standings.py"),
            "--database",
            str(DATABASE),
            "--apply",
        ],
        check=True,
    )
    inspect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.serve and not args.apply:
        parser.error("--serve requires --apply")
    inspect()
    if args.apply:
        apply()
        if args.serve:
            uvicorn.run("predatory_beavers.api.main:app", host="127.0.0.1", port=8000, reload=False)
    else:
        print("Preview only; run with --apply to back up and migrate the local database")


if __name__ == "__main__":
    main()
