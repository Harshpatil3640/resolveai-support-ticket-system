"""Verify that a pre-Alembic database matches the baseline migration.

Usage::

    python -m app.db.check_baseline                  # check resolveai.db
    python -m app.db.check_baseline --database PATH  # check another file
    python -m app.db.check_baseline --stamp          # check, then stamp

The check opens the database strictly read-only. It builds a temporary
database from the baseline revision and compares tables, columns,
primary keys, foreign keys, indexes, unique and check constraints.

``--stamp`` records the baseline revision only when the schema matches
exactly; any difference is reported and nothing is written.

Output never contains text read from the database: differences are
summarised with this code's own table and column names, counts and fixed
categories; recorded revisions are shown only when they equal one of this
code's revision IDs; database errors are described by their SQLite result
code only. There is deliberately no option to print raw database contents.

Exit codes:
    0  schema matches the baseline (and was stamped, with --stamp)
    1  schema differs from the baseline; differences are summarised
    2  not applicable: file missing, empty, or already managed by Alembic
    3  the database could not be read or stamped (safe diagnostic shown)
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from alembic import command
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

from app.db.database import DATABASE_FILE
from app.db.errors import describe_database_error
from app.db.migrations import (
    BASELINE_REVISION,
    MigrationDatabaseError,
    build_reference_schema,
    get_alembic_config,
    known_revision_ids,
    sqlite_url,
)
from app.db.safe_logging import sanitised_database_logs
from app.db.schema_inspection import (
    ALEMBIC_VERSION_TABLE,
    Schema,
    compare_schemas,
    describe_schema,
    read_only_engine,
    summarise_schema_differences,
)


EXIT_MATCH = 0
EXIT_MISMATCH = 1
EXIT_NOT_APPLICABLE = 2
EXIT_DATABASE_ERROR = 3


def baseline_schema() -> Schema:
    """Build a throwaway database at the baseline revision and describe it."""

    return build_reference_schema(BASELINE_REVISION)


def describe_recorded_revisions(recorded: Sequence[object]) -> str:
    """Summarise alembic_version rows without echoing stored values.

    Values equal to one of this code's revision IDs are shown, using the
    IDs from the migration scripts; any other value is only counted.
    """

    known = known_revision_ids()
    shown = sorted(revision for revision in known if revision in recorded)
    unrecognised = len({value for value in recorded if value not in known})

    parts = []

    if shown:
        parts.append(f"recorded revision: {', '.join(shown)}")

    if unrecognised:
        noun = "value" if unrecognised == 1 else "values"
        parts.append(
            f"{unrecognised} unrecognised revision {noun} (not shown)"
        )

    return "; ".join(parts) or "no revision recorded"


def check_database(database_file: Path) -> tuple[int, list[str]]:
    """Check a database file against the baseline without modifying it."""

    if not database_file.is_file():
        return EXIT_NOT_APPLICABLE, [
            f"Database not found: {database_file}",
            "For a brand-new database run: alembic upgrade head",
        ]

    engine = read_only_engine(database_file)

    try:
        table_names = set(inspect(engine).get_table_names())

        if ALEMBIC_VERSION_TABLE in table_names:
            with engine.connect() as connection:
                revisions = [
                    row[0]
                    for row in connection.exec_driver_sql(
                        f"SELECT version_num FROM {ALEMBIC_VERSION_TABLE}"
                    )
                ]

            return EXIT_NOT_APPLICABLE, [
                "Database is already managed by Alembic "
                f"({describe_recorded_revisions(revisions)}).",
                "Apply pending migrations with: alembic upgrade head",
            ]

        if not table_names:
            return EXIT_NOT_APPLICABLE, [
                "Database has no tables.",
                "Initialise it with: alembic upgrade head",
            ]

        actual = describe_schema(engine)

    except SQLAlchemyError as error:
        return EXIT_DATABASE_ERROR, [
            f"Could not read {database_file} "
            f"({describe_database_error(error)}).",
        ]

    finally:
        engine.dispose()

    expected = baseline_schema()

    # Detection is unchanged; only the safe summary is displayed.
    if compare_schemas(expected, actual):
        return EXIT_MISMATCH, summarise_schema_differences(expected, actual)

    return EXIT_MATCH, [
        f"Schema matches baseline revision {BASELINE_REVISION}.",
    ]


def stamp_baseline(database_file: Path) -> None:
    """Record the baseline revision without running any migration."""

    command.stamp(
        get_alembic_config(sqlite_url(database_file)),
        BASELINE_REVISION,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that a pre-Alembic ResolveAI database matches the "
            "baseline migration."
        ),
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DATABASE_FILE,
        help="SQLite database file (default: the application database).",
    )
    parser.add_argument(
        "--stamp",
        action="store_true",
        help="Stamp the baseline revision if, and only if, the schema matches.",
    )
    args = parser.parse_args(argv)

    # SQLAlchemy's own pool log records are sanitised for the whole run.
    with sanitised_database_logs():
        return _run(args.database, stamp=args.stamp)


def _run(database_file: Path, *, stamp: bool) -> int:
    status, messages = check_database(database_file)

    if status == EXIT_MISMATCH:
        print(
            "Schema does NOT match the baseline. Nothing was changed.",
            file=sys.stderr,
        )
        for message in messages:
            print(f"  - {message}", file=sys.stderr)
        return status

    if status == EXIT_DATABASE_ERROR:
        for message in messages:
            print(message, file=sys.stderr)
        return status

    for message in messages:
        print(message)

    if status == EXIT_MATCH and stamp:
        try:
            stamp_baseline(database_file)
        except MigrationDatabaseError as error:
            # Raised by alembic/env.py; its message is already safe text.
            print(
                f"Could not stamp the baseline revision. {error} Alembic "
                "stamps inside a transaction, so no revision was recorded.",
                file=sys.stderr,
            )
            return EXIT_DATABASE_ERROR
        except SQLAlchemyError as error:
            print(
                "Could not stamp the baseline revision "
                f"({describe_database_error(error)}). Alembic stamps inside "
                "a transaction, so no revision was recorded.",
                file=sys.stderr,
            )
            return EXIT_DATABASE_ERROR

        print(f"Stamped at revision {BASELINE_REVISION}.")
        print("Next step: alembic upgrade head")
    elif status == EXIT_MATCH:
        print(
            "To bring it under Alembic, back it up and re-run with --stamp "
            f"(or run: alembic stamp {BASELINE_REVISION}), "
            "then: alembic upgrade head"
        )

    return status


if __name__ == "__main__":
    raise SystemExit(main())
