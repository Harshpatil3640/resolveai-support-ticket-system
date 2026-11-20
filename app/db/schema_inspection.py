"""Read-only helpers for describing and comparing SQLite schemas.

Shared by the baseline checker (``app.db.check_baseline``) and the head
revision check (``app.db.migrations``). This module deliberately imports
nothing from Alembic or the other ``app.db`` modules, so both can use it
without circular imports.
"""

import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine


ALEMBIC_VERSION_TABLE = "alembic_version"

Schema = dict[str, dict[str, Any]]


def read_only_connection(database_file: Path) -> sqlite3.Connection:
    """Open a SQLite file with mode=ro, so it can never be created or modified.

    Raises ``sqlite3.OperationalError`` if the file does not exist.
    """

    uri = database_file.resolve().as_uri() + "?mode=ro"

    return sqlite3.connect(uri, uri=True)


def read_only_engine(database_file: Path) -> Engine:
    """A SQLAlchemy engine whose every connection is opened with mode=ro."""

    return create_engine(
        "sqlite://",
        creator=lambda: read_only_connection(database_file),
    )


def _normalise_sql(sql: str | None) -> str | None:
    return " ".join(sql.split()) if sql else sql


def describe_schema(engine: Engine) -> Schema:
    """Return a comparable description of every application table.

    Covers tables, columns (type, nullability, default), primary keys,
    foreign keys, indexes, unique and check constraints. Alembic's version
    table is excluded, and SQLAlchemy's SQLite inspector never reports
    SQLite's internal ``sqlite_*`` tables.
    """

    inspector = inspect(engine)
    schema: Schema = {}

    for table in sorted(inspector.get_table_names()):
        if table == ALEMBIC_VERSION_TABLE:
            continue

        schema[table] = {
            "columns": {
                column["name"]: (
                    str(column["type"]).upper(),
                    bool(column["nullable"]),
                    _normalise_sql(column.get("default")),
                )
                for column in inspector.get_columns(table)
            },
            "primary_key": tuple(
                inspector.get_pk_constraint(table)["constrained_columns"]
            ),
            "foreign_keys": sorted(
                (
                    tuple(fk["constrained_columns"]),
                    fk["referred_table"],
                    tuple(fk["referred_columns"]),
                )
                for fk in inspector.get_foreign_keys(table)
            ),
            "indexes": sorted(
                (
                    index["name"],
                    tuple(index["column_names"]),
                    bool(index["unique"]),
                )
                for index in inspector.get_indexes(table)
            ),
            "unique_constraints": sorted(
                tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints(table)
            ),
            "check_constraints": sorted(
                (constraint["name"], _normalise_sql(constraint["sqltext"]))
                for constraint in inspector.get_check_constraints(table)
            ),
        }

    return schema


def compare_schemas(expected: Schema, actual: Schema) -> list[str]:
    """List every difference between two schema descriptions."""

    differences: list[str] = []

    for table in sorted(expected.keys() - actual.keys()):
        differences.append(f"missing table: {table}")

    for table in sorted(actual.keys() - expected.keys()):
        differences.append(f"unexpected table: {table}")

    for table in sorted(expected.keys() & actual.keys()):
        want, have = expected[table], actual[table]

        for column in sorted(want["columns"].keys() - have["columns"].keys()):
            differences.append(f"{table}: missing column {column}")

        for column in sorted(have["columns"].keys() - want["columns"].keys()):
            differences.append(f"{table}: unexpected column {column}")

        for column in sorted(want["columns"].keys() & have["columns"].keys()):
            if want["columns"][column] != have["columns"][column]:
                differences.append(
                    f"{table}.{column}: expected (type, nullable, default) "
                    f"{want['columns'][column]}, "
                    f"found {have['columns'][column]}"
                )

        for key in (
            "primary_key",
            "foreign_keys",
            "indexes",
            "unique_constraints",
            "check_constraints",
        ):
            if want[key] != have[key]:
                differences.append(
                    f"{table}: {key.replace('_', ' ')} differ; "
                    f"expected {want[key]}, found {have[key]}"
                )

    return differences


_SCHEMA_PARTS = (
    ("primary_key", "primary key"),
    ("foreign_keys", "foreign keys"),
    ("indexes", "indexes"),
    ("unique_constraints", "unique constraints"),
    ("check_constraints", "check constraints"),
)


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def summarise_schema_differences(expected: Schema, actual: Schema) -> list[str]:
    """Describe schema differences without any database-provided text.

    ``compare_schemas`` names everything (including live table and column
    names, defaults and CHECK-constraint SQL) and is used only to DETECT a
    difference. This summary is what may be DISPLAYED: only table and
    column names from ``expected`` (the schema built from this code's
    migrations) appear; objects that exist only in the live database are
    counted, never named; definitions (types, defaults, index contents,
    CHECK-constraint SQL) are reported only as "differ". It covers every
    category ``compare_schemas`` reports, so it is never empty when a
    difference exists.
    """

    differences = [
        f"missing table: {table}"
        for table in sorted(expected.keys() - actual.keys())
    ]

    unexpected_tables = len(actual.keys() - expected.keys())

    if unexpected_tables:
        differences.append(
            f"{_count(unexpected_tables, 'unexpected table')} "
            "(names not shown)"
        )

    for table in sorted(expected.keys() & actual.keys()):
        want, have = expected[table], actual[table]

        for column in sorted(want["columns"].keys() - have["columns"].keys()):
            differences.append(f"{table}: missing column {column}")

        unexpected_columns = len(have["columns"].keys() - want["columns"].keys())

        if unexpected_columns:
            differences.append(
                f"{table}: {_count(unexpected_columns, 'unexpected column')} "
                "(names not shown)"
            )

        for column in sorted(want["columns"].keys() & have["columns"].keys()):
            if want["columns"][column] != have["columns"][column]:
                differences.append(
                    f"{table}.{column}: column definition differs "
                    "(type, nullability or default)"
                )

        for key, label in _SCHEMA_PARTS:
            if want[key] != have[key]:
                differences.append(f"{table}: {label} differ")

    return differences
