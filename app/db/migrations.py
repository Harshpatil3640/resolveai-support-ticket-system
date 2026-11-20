"""Programmatic helpers for running and checking Alembic migrations."""

import sqlite3
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionError
from alembic.util import CommandError
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.db.errors import (
    describe_database_error,
    describe_driver_error,
    describe_rollback_failure,
    rollback_failure_of,
    separate_rollback_failure,
)
from app.db.schema_inspection import (
    ALEMBIC_VERSION_TABLE,
    Schema,
    compare_schemas,
    describe_schema,
    read_only_connection,
    read_only_engine,
    summarise_schema_differences,
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


class MigrationDatabaseError(CommandError):
    """A migration command failed because of a database error.

    Raised by ``alembic/env.py`` in place of the SQLAlchemy error, without
    chaining it. The message is built only from fixed text and
    allow-listed diagnostics, so Alembic's command line can show it as its
    usual "FAILED: ..." line and exit non-zero without printing the SQL,
    parameters, the driver's message or a traceback.
    """


def migration_database_error(error: SQLAlchemyError) -> MigrationDatabaseError:
    """Build a safe ``MigrationDatabaseError`` for a failed migration run.

    The original failure is reported first, even if SQLAlchemy raised its
    own failed rollback in its place; a rollback failure follows.
    """

    primary, rollback_failure = separate_rollback_failure(error)
    rollback_failure = rollback_failure_of(primary) or rollback_failure

    message = (
        f"Database error during migration ({describe_database_error(primary)})."
    )

    if rollback_failure is not None:
        message += (
            " The rollback also failed "
            f"({describe_rollback_failure(rollback_failure)})."
        )

    return MigrationDatabaseError(
        f"{message} Details are hidden because database errors can contain "
        f"sensitive data (see {MIGRATION_GUIDE})."
    )


UNRESOLVED_REVISION_MESSAGE = (
    "Alembic could not resolve a recorded database revision. "
    "Revision details are hidden."
)


class MigrationRevisionError(CommandError):
    """A revision recorded in the database could not be resolved.

    Raised by ``alembic/env.py`` in place of Alembic's own revision error,
    without chaining it. Alembic quotes the unresolvable value verbatim
    ("Can't locate revision identified by '...'"), and a value read from
    ``alembic_version`` is database content that may contain secrets or
    terminal control characters, so only fixed text is shown.
    """


def migration_revision_error(
    error: CommandError,
    read_recorded_revisions: Callable[[], Iterable[object]],
    script: ScriptDirectory,
) -> MigrationRevisionError | None:
    """Return a safe replacement for a revision error, or ``None``.

    Alembic reports every revision problem as a ``CommandError`` chained
    from a ``RevisionError`` (``ResolutionError``, ``MultipleHeads``, ...),
    and its message may quote any revision involved, including the
    database's. ``None`` (keep Alembic's error) is returned only when the
    error is not a revision error, or when every recorded revision is
    exactly one of this code's revision IDs, so any revision text in the
    message came from the migration scripts or the operator's own command
    (for example a mistyped ``alembic upgrade <revision>``).

    The recorded revisions are read only here, after a failure: there is
    no check beforehand, so ``alembic stamp --purge`` can always replace an
    unknown value. If they cannot be read, the fixed-text error is used.
    """

    if not isinstance(error.__cause__, RevisionError):
        return None

    try:
        known = {revision.revision for revision in script.walk_revisions()}
        trusted = all(value in known for value in read_recorded_revisions())
    except Exception:  # noqa: BLE001 - any failure falls back to fixed text
        trusted = False

    if trusted:
        return None

    return MigrationRevisionError(
        f"{UNRESOLVED_REVISION_MESSAGE} See {MIGRATION_GUIDE}."
    )


def _script_directory() -> ScriptDirectory:
    """Load the migration scripts; no database is opened."""

    return ScriptDirectory.from_config(
        get_alembic_config(configure_logging=False)
    )


def known_revision_ids() -> set[str]:
    """Every revision ID defined by this code's migration scripts."""

    return {revision.revision for revision in _script_directory().walk_revisions()}


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


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def _check_recorded_revisions(
    database_file: Path,
    recorded: list[str] | None,
    script: ScriptDirectory,
) -> None:
    """Accept only version rows that are exactly the code's head(s).

    Messages never contain text read from the database. Revision values
    are shown only once they are known to equal one of this code's own
    revision IDs, and are then taken from the migration scripts; unknown
    or duplicated values are reported only as counts.
    """

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
            f"Database {database_file} records the same revision more than "
            f"once ({_count(len(recorded), 'row')} in its "
            f"{ALEMBIC_VERSION_TABLE} table, "
            f"{_count(len(duplicates), 'duplicated value')}); its migration "
            "records are corrupt. " + _DO_NOT_MIGRATE
        )

    known = {revision.revision for revision in script.walk_revisions()}
    unknown = set(recorded) - known

    if unknown:
        raise UnknownRevisionError(
            f"Database {database_file} records "
            f"{_count(len(unknown), 'revision value')} that this code does "
            "not know (it may have been migrated by a newer version). This "
            f"code requires revision {head_text}. Use code that matches the "
            "database; do not run migrations or provision users with this "
            f"code (see {MIGRATION_GUIDE})."
        )

    # Every recorded value is now one of this code's revision IDs. Display
    # the IDs from the migration scripts, never the strings read from the
    # database (they are equal, but only the scripts' copies are trusted).
    recorded_text = _join(revision for revision in known if revision in recorded)

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
            f"({recorded_text}), {reason}. Its migration records are "
            "inconsistent. " + _DO_NOT_MIGRATE
        )

    if set(recorded) != heads:
        raise PendingMigrationsError(
            f"Database {database_file} is at revision {recorded_text}, but "
            f"this code requires revision {head_text}. Back up the database "
            "and apply the pending migrations explicitly with "
            f"'alembic upgrade head' (see {MIGRATION_GUIDE})."
        )


def _check_schema_matches_heads(database_file: Path, head_text: str) -> None:
    """Compare the live schema with the schema the migrations produce.

    Detection uses ``compare_schemas`` (unchanged); the message and the
    exception's ``differences`` use ``summarise_schema_differences`` so
    that no database-provided names, definitions or SQL are shown.
    """

    engine = read_only_engine(database_file)

    try:
        actual = describe_schema(engine)
    except DBAPIError as error:
        raise DatabaseUnavailableError(
            f"Could not read the schema of {database_file} "
            f"({describe_driver_error(error.orig)})."
        ) from error
    finally:
        engine.dispose()

    expected = build_reference_schema("heads")

    if compare_schemas(expected, actual):
        differences = summarise_schema_differences(expected, actual)
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
            f"Could not read the migration state of {database_file} "
            f"({describe_driver_error(error)})."
        ) from error

    _check_recorded_revisions(database_file, recorded, script)
    _check_schema_matches_heads(database_file, _join(script.get_heads()))
