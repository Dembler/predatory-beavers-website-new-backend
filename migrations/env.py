from asyncio import run
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from predatory_beavers.db.base import Base
from predatory_beavers.modules.achievements import models as achievements_models  # noqa: F401,E402
from predatory_beavers.modules.audit import models as audit_models  # noqa: F401,E402

# Import every ORM module so Alembic can discover its tables.
from predatory_beavers.modules.auth import models as auth_models  # noqa: F401,E402
from predatory_beavers.modules.club import models as club_models  # noqa: F401,E402
from predatory_beavers.modules.imports import models as imports_models  # noqa: F401,E402
from predatory_beavers.modules.matches import models as matches_models  # noqa: F401,E402
from predatory_beavers.modules.standings import models as standings_models  # noqa: F401,E402
from predatory_beavers.settings import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run(run_migrations_online())
