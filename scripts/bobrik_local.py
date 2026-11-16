"""Isolated local Bobrik runtime; never loads .env.render or the regular .env."""

import argparse
import os
import sqlite3
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "data" / "bobrik-local.db"
ENVIRONMENT = ROOT / ".env.bobrik"


def prepare_environment() -> None:
    os.chdir(ROOT)
    for key, value in dotenv_values(ENVIRONMENT).items():
        if value is not None:
            os.environ[key] = value
    # These cannot be redirected to production by an accidentally copied env file.
    os.environ.update(
        APP_ENV="dev",
        APP_CONFIG_FILE=str(ENVIRONMENT),
        APP_DATABASE_URL=f"sqlite+aiosqlite:///{DATABASE.as_posix()}",
        APP_COOKIE_SECURE="false",
        APP_COOKIE_SAMESITE="lax",
        APP_SESSION_COOKIE_NAME="pb_bobrik_dev",
        APP_MASCOT_SESSION_COOKIE="bobrik_local_session",
        APP_CORS_ORIGINS='["http://localhost:5176","http://127.0.0.1:5176"]',
        APP_SPORTS_SYNC_ENABLED="false",
        APP_SPORTS_SYNC_EXTERNAL_ENABLED="false",
        APP_MEDIA_STORAGE_BACKEND="local",
        APP_RELOAD="false",
    )


def initialize() -> None:
    if not DATABASE.exists():
        source = ROOT / "data" / "local.db"
        if not source.is_file():
            raise SystemExit("Create the regular local database first (scripts/setup_local.py).")
        # sqlite backup includes committed WAL data and leaves the source untouched.
        with sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True) as original:
            with sqlite3.connect(DATABASE) as target:
                original.backup(target)
        print("Created isolated data/bobrik-local.db from the local database.")
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("process", choices=["init", "api", "worker", "editorial-worker"])
    arguments = parser.parse_args()
    prepare_environment()
    if arguments.process == "init":
        initialize()
    elif not DATABASE.is_file():
        raise SystemExit("Run scripts/bobrik_local.py init first.")
    elif arguments.process == "api":
        import uvicorn

        uvicorn.run("predatory_beavers.api.main:app", host="127.0.0.1", port=8001)
    elif arguments.process == "editorial-worker":
        from predatory_beavers.modules.editorial.worker import main as run_editorial

        run_editorial()
    else:
        from predatory_beavers.modules.mascot.worker import main as run_worker

        run_worker()


if __name__ == "__main__":
    main()
