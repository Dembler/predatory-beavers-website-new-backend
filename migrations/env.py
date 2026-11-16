import sys
from asyncio import SelectorEventLoop, run
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import JSON, cast, literal, literal_column, pool, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.schema import CreateSchema

from predatory_beavers.api.idempotency import AdminCreationKey  # noqa: F401
from predatory_beavers.db.base import Base
from predatory_beavers.db.session import database_connect_args
from predatory_beavers.modules.achievements import models as achievements_models  # noqa: F401,E402
from predatory_beavers.modules.audit import models as audit_models  # noqa: F401,E402

# Import every ORM module so Alembic can discover its tables.
from predatory_beavers.modules.auth import models as auth_models  # noqa: F401,E402
from predatory_beavers.modules.club import models as club_models  # noqa: F401,E402
from predatory_beavers.modules.content import models as content_models  # noqa: F401,E402
from predatory_beavers.modules.editorial import models as editorial_models  # noqa: F401,E402
from predatory_beavers.modules.imports import models as imports_models  # noqa: F401,E402
from predatory_beavers.modules.mascot import models as mascot_models  # noqa: F401,E402
from predatory_beavers.modules.matches import models as matches_models  # noqa: F401,E402
from predatory_beavers.modules.results import models as results_models  # noqa: F401,E402
from predatory_beavers.modules.roster import models as roster_models  # noqa: F401,E402
from predatory_beavers.modules.standings import models as standings_models  # noqa: F401,E402
from predatory_beavers.settings import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ConfigParser treats percent-encoded passwords as interpolation syntax unless escaped.
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
target_metadata = Base.metadata


def compare_server_default(
    migration_context: Any,
    inspected_column: Any,
    metadata_column: Any,
    inspected_default: str | None,
    metadata_default: Any,
    rendered_metadata_default: str | None,
) -> bool | None:
    del inspected_column, metadata_default
    if (
        migration_context.dialect.name == "postgresql"
        and isinstance(metadata_column.type, JSON)
        and inspected_default is not None
        and rendered_metadata_default is not None
    ):
        # PostgreSQL json has no equality operator; Alembic's default comparison
        # otherwise fails even when both JSON defaults are identical.
        return not migration_context.connection.scalar(
            select(
                cast(literal_column(inspected_default), JSONB)
                == cast(
                    literal(metadata_column.server_default.arg)
                    if isinstance(metadata_column.server_default.arg, str)
                    else literal_column(str(metadata_column.server_default.arg)),
                    JSONB,
                )
            )
        )
    return None


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    del obj, reflected, compare_to
    # A private search_path makes the qualified version table visible as an
    # unqualified reflected table too; it is never an application drop candidate.
    return not (type_ == "table" and name == "alembic_version")


def run_migrations_offline() -> None:
    is_postgres = settings.database_url.startswith("postgresql+")
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=compare_server_default,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=settings.database_schema if is_postgres else None,
    )
    with context.begin_transaction():
        if is_postgres:
            context.execute(CreateSchema(settings.database_schema, if_not_exists=True))
            context.execute(f'SET search_path TO "{settings.database_schema}"')
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    is_postgres = connection.dialect.name == "postgresql"
    if is_postgres:
        # One transaction keeps the lock and DDL on the same connection even
        # behind a transaction pooler. Failures release the lock automatically.
        with connection.begin():
            connection.execute(text("SELECT pg_advisory_xact_lock(739412601)"))
            connection.execute(CreateSchema(settings.database_schema, if_not_exists=True))
            migrate(connection, is_postgres=True)
    else:
        migrate(connection, is_postgres=False)


def migrate(connection: Connection, *, is_postgres: bool) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=compare_server_default,
        include_object=include_object,
        version_table_schema=settings.database_schema if is_postgres else None,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        hide_parameters=True,
        connect_args=database_connect_args(settings),
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    loop_factory = (
        SelectorEventLoop
        if sys.platform == "win32" and settings.database_url.startswith("postgresql+")
        else None
    )
    run(run_migrations_online(), loop_factory=loop_factory)
