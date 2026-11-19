"""Programmatic helpers for running and checking Alembic migrations."""

import sqlite3
import tempfile
from collections import Counter
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from app.db.schema_inspection import (
    ALEMBIC_VERSION_TABLE,
    Schema,
    compare_schemas,
    describe_schema,
    read_only_connection,
    read_only_engine,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]

ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"

# The revision describing the schema that existed before Alembic was
# introduced. Pre-existing databases are verified against it and stamped.
BASELINE_REVISION = "0001"

MIGRATION_GUIDE = "docs/database-migrations.md"

# Advice for states where running migrations could make things worse.
_DO_NOT_MIGRATE = (
    "Do not run 'alembic upgrade head' or provision users against this "
    "database. Restore it from a backup, or investigate it as described in "
    f"{MIGRATION_GUIDE}."
)


# ---------------------------------------------------------------------------
# Errors raised by ensure_database_at_head()
# ---------------------------------------------------------------------------


class DatabaseNotAtHeadError(RuntimeError):
    """Base class: the database cannot be used by code at the head revision."""


class DatabaseUnavailableError(DatabaseNotAtHeadError):
    """The database file is missing or cannot be read."""


class UnmanagedDatabaseError(DatabaseNotAtHeadError):
    """The database has no Alembic version table (it predates Alembic)."""


class PendingMigrationsError(DatabaseNotAtHeadError):
    """The database is at an earlier, known revision."""


class UnknownRevisionError(DatabaseNotAtHeadError):
    """The database records a revision this code does not know."""


class InconsistentMigrationRecordsError(DatabaseNotAtHeadError):
    """The version table is corrupt: empty, or with duplicate rows."""


class InvalidRevisionSetError(DatabaseNotAtHeadError):
    """Several distinct revisions are recorded in an invalid combination."""


class SchemaMismatchError(DatabaseNotAtHeadError):
    """The revision is correct but the schema is not the expected one."""

    def __init__(self, message: str, differences: list[str]) -> None:
        super().__init__(message)
        self.differences = differences


# ---------------------------------------------------------------------------
# Configuration and running migrations
# ---------------------------------------------------------------------------


def sqlite_url(database_file: Path) -> str:
    """Build a SQLAlchemy URL string for a SQLite database file."""

    return URL.create(
        drivername="sqlite",
        database=str(database_file),
    ).render_as_string(hide_password=False)


def get_alembic_config(
    database_url: str | None = None,
    *,
    configure_logging: bool = True,
) -> Config:
    """Load the project's Alembic configuration.

    ``database_url`` overrides the application database (used by tests
    and the baseline checker). ``configure_logging=False`` stops Alembic
    from reconfiguring Python logging, which matters inside test runs.
    """

    config = Config(str(ALEMBIC_INI))
    config.set_main_option(
        "script_location",
        str(PROJECT_ROOT / "alembic"),
    )

    if database_url is not None:
        # ConfigParser treats "%" as interpolation syntax.
        config.set_main_option(
            "sqlalchemy.url",
            database_url.replace("%", "%%"),
        )

    config.attributes["configure_logger"] = configure_logging

    return config


def upgrade_to_head(
    database_url: str | None = None,
    *,
    configure_logging: bool = True,
) -> None:
    """Apply every pending migration."""

    command.upgrade(
        get_alembic_config(
            database_url,
            configure_logging=configure_logging,
        ),
        "head",
    )


def _script_directory() -> ScriptDirectory:
    """Load the migration scripts; no database is opened."""

    return ScriptDirectory.from_config(
        get_alembic_config(configure_logging=False)
    )


# ---------------------------------------------------------------------------
# Reference schema and read-only inspection
# ---------------------------------------------------------------------------


def build_reference_schema(revision: str) -> Schema:
    """Describe the schema the project's migrations produce at ``revision``.

    Runs the real migrations in ``alembic/versions`` against a throwaway
    SQLite file in a new temporary directory, describes it, and deletes
    it. The URL is always passed explicitly, so the application database
    is never used as the reference.
    """

    with tempfile.TemporaryDirectory() as directory:
        database_file = Path(directory) / "reference.db"

        command.upgrade(
            get_alembic_config(
                sqlite_url(database_file),
                configure_logging=False,
            ),
            revision,
        )

        engine = create_engine(sqlite_url(database_file))

        try:
            return describe_schema(engine)
        finally:
            engine.dispose()


def read_database_revisions(database_file: Path) -> list[str] | None:
    """Return every row of the Alembic version table, read-only.

    Duplicate rows are preserved so that a corrupt table can be detected.
    Returns ``None`` when the database has no Alembic version table. The
    file is opened with ``mode=ro``, so it is never created or modified.
    Raises ``sqlite3.Error`` if the file is missing or unreadable.
    """

    with closing(read_only_connection(database_file)) as connection:
        has_version_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (ALEMBIC_VERSION_TABLE,),
        ).fetchone()

        if has_version_table is None:
            return None

        return [
            row[0]
            for row in connection.execute(
                f"SELECT version_num FROM {ALEMBIC_VERSION_TABLE} "
                "ORDER BY version_num"
            )
        ]


def _join(revisions: Iterable[str]) -> str:
    return ", ".join(sorted(revisions))


# ---------------------------------------------------------------------------
# Head check
# ---------------------------------------------------------------------------


def _check_recorded_revisions(
    database_file: Path,
    recorded: list[str] | None,
    script: ScriptDirectory,
) -> None:
    """Accept only version rows that are exactly the code's head(s)."""

    heads = set(script.get_heads())
    head_text = _join(heads)

    if recorded is None:
        raise UnmanagedDatabaseError(
            f"Database {database_file} has no Alembic revision recorded. If "
            "it was created before Alembic, back it up, verify and stamp it "
            "with 'python -m app.db.check_baseline --stamp', then run "
            f"'alembic upgrade head' (see {MIGRATION_GUIDE})."
        )

    if not recorded:
        raise InconsistentMigrationRecordsError(
            f"Database {database_file} has an {ALEMBIC_VERSION_TABLE} table "
            "with no rows, so its migration state is unknown. "
            + _DO_NOT_MIGRATE
        )

    duplicates = sorted(
        revision for revision, count in Counter(recorded).items() if count > 1
    )

    if duplicates:
        raise InconsistentMigrationRecordsError(
            f"Database {database_file} records revision {_join(duplicates)} "
            f"more than once ({', '.join(recorded)}); its migration records "
            "are corrupt. " + _DO_NOT_MIGRATE
        )

    known = {revision.revision for revision in script.walk_revisions()}
    unknown = sorted(set(recorded) - known)

    if unknown:
        raise UnknownRevisionError(
            f"Database {database_file} records revision {_join(unknown)}, "
            "which this code does not know (it may have been migrated by a "
            f"newer version). This code requires revision {head_text}. Use "
            "code that matches the database; do not run migrations or "
            f"provision users with this code (see {MIGRATION_GUIDE})."
        )

    if len(recorded) > 1 and set(recorded) != heads:
        if len(heads) == 1:
            reason = (
                f"but the migration history has a single linear head "
                f"({head_text}), so exactly one revision must be recorded"
            )
        else:
            reason = f"which is not this code's set of heads ({head_text})"

        raise InvalidRevisionSetError(
            f"Database {database_file} records several revisions "
            f"({_join(recorded)}), {reason}. Its migration records are "
            "inconsistent. " + _DO_NOT_MIGRATE
        )

    if set(recorded) != heads:
        raise PendingMigrationsError(
            f"Database {database_file} is at revision {_join(recorded)}, but "
            f"this code requires revision {head_text}. Back up the database "
            "and apply the pending migrations explicitly with "
            f"'alembic upgrade head' (see {MIGRATION_GUIDE})."
        )


def _check_schema_matches_heads(database_file: Path, head_text: str) -> None:
    """Compare the live schema with the schema the migrations produce."""

    engine = read_only_engine(database_file)

    try:
        actual = describe_schema(engine)
    except DBAPIError as error:
        raise DatabaseUnavailableError(
            f"Could not read the schema of {database_file}: {error.orig}."
        ) from error
    finally:
        engine.dispose()

    differences = compare_schemas(build_reference_schema("heads"), actual)

    if differences:
        listing = "\n".join(f"  - {difference}" for difference in differences)

        raise SchemaMismatchError(
            f"Database {database_file} records revision {head_text}, but "
            "its schema does not match the ResolveAI schema at that "
            "revision. It may not be a ResolveAI database, or it was "
            "changed outside the migrations. " + _DO_NOT_MIGRATE
            + f"\nDifferences:\n{listing}",
            differences,
        )


def ensure_database_at_head(database_file: Path) -> None:
    """Verify, without modifying it, that a database is usable at head.

    1. The file exists and can be read (opened with ``mode=ro``).
    2. The Alembic version table records exactly the code's head
       revision(s): no missing, empty, duplicate, unknown or invalid
       combination of rows.
    3. The schema (tables, columns, types, nullability, defaults, keys,
       indexes and constraints) matches the schema the project's own
       migrations produce at head, built in a temporary database.

    Raises a ``DatabaseNotAtHeadError`` subclass describing the problem and
    what the operator should do. Nothing is migrated, stamped or written.
    """

    script = _script_directory()

    if not database_file.is_file():
        raise DatabaseUnavailableError(
            f"Database not found: {database_file}. Create it explicitly "
            f"with 'alembic upgrade head' (see {MIGRATION_GUIDE})."
        )

    try:
        recorded = read_database_revisions(database_file)
    except sqlite3.Error as error:
        raise DatabaseUnavailableError(
            f"Could not read the migration state of {database_file}: {error}."
        ) from error

    _check_recorded_revisions(database_file, recorded, script)
    _check_schema_matches_heads(database_file, _join(script.get_heads()))
