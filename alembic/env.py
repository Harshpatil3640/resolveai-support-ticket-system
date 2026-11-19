"""Alembic migration environment for ResolveAI."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, event, pool
from sqlalchemy.engine import Engine

# Importing the models package registers every table on Base.metadata,
# so autogenerate always sees the complete schema.
from app import models  # noqa: F401
from app.db.base import Base
from app.db.database import DATABASE_URL


config = context.config

if (
    config.config_file_name is not None
    and config.attributes.get("configure_logger", True)
):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def get_database_url() -> str:
    """Return the target URL, defaulting to the application database.

    Precedence: ``alembic -x db_url=...`` (e.g. to rehearse on a backup
    copy), then a programmatically set ``sqlalchemy.url``, then the
    application's DATABASE_URL.
    """

    cli_url = context.get_x_argument(as_dictionary=True).get("db_url")

    if cli_url:
        return cli_url

    configured_url = config.get_main_option("sqlalchemy.url")

    if configured_url:
        return configured_url

    return DATABASE_URL.render_as_string(hide_password=False)


def _configure_context(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        # SQLite cannot ALTER most column/constraint properties in place;
        # batch mode rebuilds tables safely when future migrations need it.
        render_as_batch=True,
        compare_type=True,
        compare_server_default=True,
        **kwargs,
    )


def make_transactional_sqlite_engine(url: str) -> Engine:
    """Create an engine whose SQLite transactions also cover DDL.

    Python's sqlite3 driver commits DDL implicitly, so a failed migration
    could leave a half-applied schema. Taking over transaction control
    (the documented SQLAlchemy recipe for pysqlite) makes each migration
    run atomically and roll back completely on error.

    Foreign-key enforcement is deliberately left at SQLite's default (off)
    during migrations so batch table rebuilds cannot trigger FK actions.
    """

    engine = create_engine(url, poolclass=pool.NullPool)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _disable_implicit_transactions(dbapi_connection, _record):
            dbapi_connection.isolation_level = None

        @event.listens_for(engine, "begin")
        def _emit_begin(connection):
            connection.exec_driver_sql("BEGIN")

    return engine


def run_migrations_offline() -> None:
    """Emit migration SQL without connecting to a database."""

    _configure_context(
        url=get_database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""

    engine = make_transactional_sqlite_engine(get_database_url())

    try:
        with engine.connect() as connection:
            _configure_context(
                connection=connection,
                transactional_ddl=True,
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
