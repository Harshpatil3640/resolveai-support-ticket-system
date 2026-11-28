import os
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.settings import (
    DEFAULT_DATABASE_FILE,
    PROJECT_ROOT,
    is_sqlite_url,
    load_environment,
    resolve_database_url,
)


# The database URL comes from DATABASE_URL (in the environment or the
# project .env file); without it, the SQLite file resolveai.db in the
# project root is used. Only DATABASE_URL is read here, so this module and
# Alembic (whose env.py imports DATABASE_URL from here) do not require
# SECRET_KEY. Configuring another dialect does not make it supported:
# ResolveAI currently supports SQLite only.
load_environment()

DATABASE_URL: URL = resolve_database_url(os.environ)


def sqlite_database_file(url: URL) -> Path | None:
    """The file of a SQLite URL, or None (in-memory or another dialect)."""

    if not is_sqlite_url(url):
        return None

    database = url.database

    if not database or database == ":memory:" or database.startswith("file:"):
        return None

    return Path(database)


# The SQLite database file the application uses, or None when DATABASE_URL
# is not a SQLite file. Used as the default by the database tools.
DATABASE_FILE: Path | None = sqlite_database_file(DATABASE_URL)


def engine_options(url: URL) -> dict:
    """Keyword arguments for ``create_engine`` for this database URL.

    hide_parameters keeps bound values (such as password hashes) out of
    exception messages and SQL logs; statements are still shown.
    ``check_same_thread=False`` is a pysqlite argument, needed because
    FastAPI runs requests on a thread pool; it is passed only to SQLite.
    """

    options: dict = {"hide_parameters": True}

    if is_sqlite_url(url):
        options["connect_args"] = {"check_same_thread": False}

    return options


def enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    """SQLite enforces foreign keys only when asked to, per connection."""

    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_database_engine(url: URL) -> Engine:
    """Create the application engine for ``url``.

    SQLite connections get ``check_same_thread=False`` and
    ``PRAGMA foreign_keys=ON``; no SQLite-specific argument or statement
    is applied to any other dialect.
    """

    engine = create_engine(url, **engine_options(url))

    if is_sqlite_url(url):
        event.listen(engine, "connect", enable_sqlite_foreign_keys)

    return engine


# Create the SQLAlchemy engine.
engine = create_database_engine(DATABASE_URL)

# Create a session factory
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


# Provide a database session to application functions
def get_db() -> Generator[Session, None, None]:

    db = SessionLocal()

    try:
        yield db

    finally:
        db.close()


__all__ = [
    "DATABASE_FILE",
    "DATABASE_URL",
    "DEFAULT_DATABASE_FILE",
    "PROJECT_ROOT",
    "SessionLocal",
    "create_database_engine",
    "enable_sqlite_foreign_keys",
    "engine",
    "engine_options",
    "get_db",
    "sqlite_database_file",
]
