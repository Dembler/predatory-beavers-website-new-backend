"""Prepare a local, isolated database copy and optionally serve it on 127.0.0.1.

Run from the backend root: .venv/Scripts/python.exe scripts/setup_e2e.py --serve
Credentials are generated per run and stored only in gitignored data/e2e-credentials.json.
Existing E2E data is reused; the original database, media and passwords are never modified.
"""

import argparse
import asyncio
import json
import os
import secrets
import shutil
import sqlite3
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config

from predatory_beavers.db.session import create_engine, create_session_factory
from predatory_beavers.modules.auth.models import User, UserRole
from predatory_beavers.modules.auth.service import PasswordSecurity
from predatory_beavers.settings import Settings, get_settings

ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "data/e2e.db"
MEDIA = ROOT / "data/e2e-media"
CREDENTIALS = ROOT / "data/e2e-credentials.json"


async def add_test_admin(password: str) -> None:
    engine = create_engine(Settings())
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            session.add(
                User(
                    username="e2e-admin",
                    email="e2e-admin@example.test",
                    role=UserRole.ADMIN,
                    password_hash=PasswordSecurity().hash_password(password),
                    is_active=True,
                )
            )
            await session.commit()
    finally:
        await engine.dispose()


def prepare() -> None:
    os.chdir(ROOT)
    os.environ.update(
        {
            "APP_DATABASE_URL": f"sqlite+aiosqlite:///{DATABASE.as_posix()}",
            "APP_MEDIA_STORAGE_PATH": str(MEDIA),
            "APP_ENV": "test",
            "APP_COOKIE_SECURE": "false",
            "APP_RELOAD": "false",
            "APP_CORS_ORIGINS": '["http://localhost:5173","http://127.0.0.1:5173"]',
        }
    )
    get_settings.cache_clear()
    if CREDENTIALS.exists() and DATABASE.exists():
        runtime = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
        if "runtimeToken" not in runtime:
            runtime["runtimeToken"] = secrets.token_urlsafe(32)
            CREDENTIALS.write_text(json.dumps(runtime, indent=2), encoding="utf-8")
        os.environ["APP_E2E_RUNTIME_TOKEN"] = runtime["runtimeToken"]
        get_settings.cache_clear()
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        print("Isolated E2E data reused. Credentials: data/e2e-credentials.json")
        return
    if DATABASE.exists() or CREDENTIALS.exists() or MEDIA.exists():
        raise SystemExit("Partial E2E setup exists; inspect it before preparing a fresh copy")
    source_path = ROOT / "data/local.db"
    if not source_path.exists():
        source_path = ROOT / "data/predatory_beavers.db"
    if not source_path.exists():
        raise SystemExit("Source database is missing")
    with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(DATABASE) as target:
            source.backup(target)
    source_media = ROOT / "data/media"
    if source_media.exists():
        shutil.copytree(source_media, MEDIA)
    else:
        MEDIA.mkdir(parents=True)
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    password = secrets.token_urlsafe(24)
    runtime_token = secrets.token_urlsafe(32)
    asyncio.run(add_test_admin(password))
    CREDENTIALS.write_text(
        json.dumps(
            {
                "baseURL": "http://127.0.0.1:8000",
                "frontendURL": "http://localhost:5173",
                "username": "e2e-admin",
                "password": password,
                "runtimeToken": runtime_token,
                "databasePath": str(DATABASE),
                "mediaPath": str(MEDIA),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    os.environ["APP_E2E_RUNTIME_TOKEN"] = runtime_token
    get_settings.cache_clear()
    print("Isolated E2E database prepared. Credentials: data/e2e-credentials.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    prepare()
    if args.serve:
        uvicorn.run("predatory_beavers.api.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
