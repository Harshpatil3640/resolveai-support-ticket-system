"""Alembic migration tests.

Each test uses its own SQLite file under pytest's ``tmp_path``; the
application database (resolveai.db) is never touched.
"""

import functools
import hashlib
import importlib.util
import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.db import check_baseline
from app.db import database as app_database
from app.db.base import Base
from app.db.check_baseline import (
    EXIT_DATABASE_ERROR,
    EXIT_MATCH,
    EXIT_MISMATCH,
    EXIT_NOT_APPLICABLE,
    check_database,
    main as check_baseline_main,
)
from app.db.migrations import (
    BASELINE_REVISION,
    DatabaseNotAtHeadError,
    DatabaseUnavailableError,
    InconsistentMigrationRecordsError,
    InvalidRevisionSetError,
    PendingMigrationsError,
    SchemaMismatchError,
    UnknownRevisionError,
    UnmanagedDatabaseError,
    build_reference_schema,
    ensure_database_at_head,
    get_alembic_config,
    read_database_revisions,
    sqlite_url,
)
from app.db.schema_inspection import compare_schemas, describe_schema


HEAD_REVISION = "0005"
MESSAGES_REVISION = "0004"
ASSIGNMENT_REVISION = "0003"
TOKEN_VERSION_REVISION = "0002"


@pytest.fixture(autouse=True)
def check_baseline_does_not_configure_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the check_baseline CLI in-process without Alembic logging config.

    In production, ``stamp_baseline()`` loads Alembic's logging config
    (``fileConfig``), which is correct for a CLI process. Inside the test
    process, ``fileConfig`` would close every registered logging handler
    and attach a handler to the current (soon closed) capture stream,
    affecting later tests. The tests therefore use the existing
    ``configure_logging=False`` option; production behaviour is unchanged.
    """

    monkeypatch.setattr(
        check_baseline,
        "get_alembic_config",
        functools.partial(get_alembic_config, configure_logging=False),
    )

# Schema of a database created by Base.metadata.create_all before Alembic
# was introduced, captured verbatim from an existing resolveai.db.
LEGACY_SCHEMA_SQL = [
    """CREATE TABLE users (
	id INTEGER NOT NULL,
	name VARCHAR(100) NOT NULL,
	email VARCHAR(255) NOT NULL,
	password_hash VARCHAR(255) NOT NULL,
	role VARCHAR(30) NOT NULL,
	is_active BOOLEAN NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
)""",
    "CREATE UNIQUE INDEX ix_users_email ON users (email)",
    """CREATE TABLE tickets (
	id INTEGER NOT NULL,
	title VARCHAR(200) NOT NULL,
	description TEXT NOT NULL,
	requester_name VARCHAR(100),
	predicted_category VARCHAR(100),
	category VARCHAR(100),
	prediction_confidence FLOAT,
	priority VARCHAR(20) NOT NULL,
	status VARCHAR(20) NOT NULL,
	created_by_user_id INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_tickets_priority CHECK (priority IN ('Low', 'Medium', 'High')),
	CONSTRAINT ck_tickets_status CHECK (status IN ('Open', 'In Progress', 'Resolved')),
	FOREIGN KEY(created_by_user_id) REFERENCES users (id)
)""",
    "CREATE INDEX ix_tickets_created_by_user_id ON tickets (created_by_user_id)",
    """CREATE TABLE revoked_tokens (
	jti VARCHAR(128) NOT NULL,
	expires_at DATETIME NOT NULL,
	revoked_at DATETIME NOT NULL,
	PRIMARY KEY (jti)
)""",
    "CREATE INDEX ix_revoked_tokens_expires_at ON revoked_tokens (expires_at)",
]

LEGACY_DATA_SQL = [
    "INSERT INTO users (id, name, email, password_hash, role, is_active, created_at) "
    "VALUES (1, 'Legacy Agent', 'agent@example.test', 'hash-1', 'agent', 1, "
    "'2026-11-01 09:00:00.000000')",
    "INSERT INTO users (id, name, email, password_hash, role, is_active, created_at) "
    "VALUES (2, 'Legacy Admin', 'admin@example.test', 'hash-2', 'admin', 0, "
    "'2026-11-02 09:00:00.000000')",
    "INSERT INTO tickets (id, title, description, requester_name, predicted_category, "
    "category, prediction_confidence, priority, status, created_by_user_id, "
    "created_at, updated_at) VALUES (1, 'Payment failed', 'Card declined.', "
    "'Jane', 'PAYMENT', 'PAYMENT', 0.97, 'High', 'Open', 1, "
    "'2026-11-03 10:00:00.000000', '2026-11-03 10:00:00.000000')",
    "INSERT INTO tickets (id, title, description, requester_name, predicted_category, "
    "category, prediction_confidence, priority, status, created_by_user_id, "
    "created_at, updated_at) VALUES (2, 'Late order', 'Order is late.', NULL, "
    "NULL, NULL, NULL, 'Low', 'Resolved', 2, "
    "'2026-11-04 10:00:00.000000', '2026-11-05 10:00:00.000000')",
    "INSERT INTO revoked_tokens (jti, expires_at, revoked_at) VALUES "
    "('legacy-jti', '2026-11-06 10:00:00.000000', '2026-11-06 09:30:00.000000')",
]


# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------


def _connect(path: Path) -> closing[sqlite3.Connection]:
    return closing(sqlite3.connect(path))


def _rows(path: Path, table: str) -> list[tuple]:
    with _connect(path) as connection:
        return connection.execute(
            f"SELECT * FROM {table} ORDER BY 1"
        ).fetchall()


def _table_names(path: Path) -> set[str]:
    with _connect(path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }


def _current_revision(path: Path) -> list[str]:
    with _connect(path) as connection:
        return [
            row[0]
            for row in connection.execute(
                "SELECT version_num FROM alembic_version"
            )
        ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def database_file(tmp_path: Path) -> Path:
    return tmp_path / "migration_test.db"


@pytest.fixture
def alembic_config(database_file: Path) -> Config:
    return get_alembic_config(
        sqlite_url(database_file),
        configure_logging=False,
    )


@pytest.fixture
def make_legacy_database(
    tmp_path: Path,
) -> Callable[..., Path]:
    """Create a pre-Alembic database, optionally with data or alterations."""

    def _make(
        *,
        with_data: bool = True,
        schema_sql: list[str] | None = None,
        name: str = "legacy.db",
    ) -> Path:
        path = tmp_path / name

        with _connect(path) as connection:
            for statement in schema_sql or LEGACY_SCHEMA_SQL:
                connection.execute(statement)

            if with_data:
                for statement in LEGACY_DATA_SQL:
                    connection.execute(statement)

            connection.commit()

        return path

    return _make


@pytest.fixture
def legacy_database(make_legacy_database: Callable[..., Path]) -> Path:
    return make_legacy_database()


def _config_for(path: Path) -> Config:
    return get_alembic_config(sqlite_url(path), configure_logging=False)


# ---------------------------------------------------------------------------
# Revision history
# ---------------------------------------------------------------------------


def test_revision_history_is_linear_baseline_to_head() -> None:
    script = ScriptDirectory.from_config(
        get_alembic_config(configure_logging=False)
    )

    assert script.get_bases() == [BASELINE_REVISION]
    assert script.get_heads() == [HEAD_REVISION]
    assert (
        script.get_revision(HEAD_REVISION).down_revision
        == MESSAGES_REVISION
    )
    assert (
        script.get_revision(MESSAGES_REVISION).down_revision
        == ASSIGNMENT_REVISION
    )
    assert (
        script.get_revision(ASSIGNMENT_REVISION).down_revision
        == TOKEN_VERSION_REVISION
    )
    assert (
        script.get_revision(TOKEN_VERSION_REVISION).down_revision
        == BASELINE_REVISION
    )


# ---------------------------------------------------------------------------
# Brand-new database
# ---------------------------------------------------------------------------


def test_upgrade_head_initialises_empty_database(
    database_file: Path,
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "head")

    assert _table_names(database_file) == {
        "alembic_version",
        "users",
        "tickets",
        "ticket_events",
        "ticket_messages",
        "notifications",
        "revoked_tokens",
    }
    assert _current_revision(database_file) == [HEAD_REVISION]


def test_migrated_schema_matches_sqlalchemy_models(
    database_file: Path,
    alembic_config: Config,
) -> None:
    """Models and migrations must never drift apart."""

    # Every model table is compared, including the ticket history.
    assert set(Base.metadata.tables) == {
        "users",
        "tickets",
        "ticket_events",
        "ticket_messages",
        "notifications",
        "revoked_tokens",
    }

    command.upgrade(alembic_config, "head")

    engine = create_engine(sqlite_url(database_file))

    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "compare_server_default": True},
            )
            differences = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert differences == []


def test_token_version_column_definition(
    database_file: Path,
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "head")

    engine = create_engine(sqlite_url(database_file))

    try:
        columns = {
            column["name"]: column
            for column in inspect(engine).get_columns("users")
        }
    finally:
        engine.dispose()

    token_version = columns["token_version"]

    assert str(token_version["type"]) == "INTEGER"
    assert token_version["nullable"] is False
    assert token_version["default"] == "0"


def test_new_rows_default_to_token_version_zero(
    database_file: Path,
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "head")

    with _connect(database_file) as connection:
        connection.execute(
            "INSERT INTO users (name, email, password_hash, role, is_active, "
            "created_at) VALUES ('New', 'new@example.test', 'h', 'agent', 1, "
            "'2026-11-18 00:00:00')"
        )
        connection.commit()

        version = connection.execute(
            "SELECT token_version FROM users WHERE email = 'new@example.test'"
        ).fetchone()[0]

    assert version == 0


def test_token_version_rejects_null(
    database_file: Path,
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "head")

    with _connect(database_file) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO users (name, email, password_hash, role, "
                "is_active, created_at, token_version) VALUES ('N', "
                "'n@example.test', 'h', 'agent', 1, '2026-11-18', NULL)"
            )


# ---------------------------------------------------------------------------
# Existing (pre-Alembic) database
# ---------------------------------------------------------------------------


def test_legacy_database_matches_baseline(legacy_database: Path) -> None:
    status, messages = check_database(legacy_database)

    assert status == EXIT_MATCH, messages


def test_baseline_check_does_not_modify_database(
    legacy_database: Path,
) -> None:
    before = _sha256(legacy_database)

    check_database(legacy_database)

    assert _sha256(legacy_database) == before
    assert "alembic_version" not in _table_names(legacy_database)


def test_existing_database_stamp_and_upgrade_preserves_data(
    legacy_database: Path,
) -> None:
    users_before = _rows(legacy_database, "users")
    tickets_before = _rows(legacy_database, "tickets")
    revoked_before = _rows(legacy_database, "revoked_tokens")

    config = _config_for(legacy_database)

    command.stamp(config, BASELINE_REVISION)
    assert _current_revision(legacy_database) == [BASELINE_REVISION]

    command.upgrade(config, "head")
    assert _current_revision(legacy_database) == [HEAD_REVISION]

    # Every existing user is unchanged and receives token_version 0.
    assert _rows(legacy_database, "users") == [
        row + (0,) for row in users_before
    ]
    # Every existing ticket is unchanged: unassigned, at version 1.
    assert _rows(legacy_database, "tickets") == [
        row + (None, None, 1) for row in tickets_before
    ]
    assert _rows(legacy_database, "revoked_tokens") == revoked_before

    with _connect(legacy_database) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_check_baseline_cli_stamps_matching_database(
    legacy_database: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _logging_state() -> list[tuple]:
        return [
            (name, list(logger.handlers), logger.level, logger.propagate)
            for name in ("", "alembic", "sqlalchemy.engine")
            for logger in [logging.getLogger(name)]
        ]

    before = _logging_state()

    exit_code = check_baseline_main(
        ["--database", str(legacy_database), "--stamp"]
    )

    # The in-process stamp must not have reconfigured logging.
    assert _logging_state() == before

    assert exit_code == EXIT_MATCH
    assert _current_revision(legacy_database) == [BASELINE_REVISION]
    assert "Stamped at revision 0001" in capsys.readouterr().out

    command.upgrade(_config_for(legacy_database), "head")

    assert _current_revision(legacy_database) == [HEAD_REVISION]


def test_baseline_migration_refuses_to_recreate_existing_tables(
    legacy_database: Path,
) -> None:
    """Running upgrade on an unstamped legacy DB fails and changes nothing."""

    before = _sha256(legacy_database)
    users_before = _rows(legacy_database, "users")
    tickets_before = _rows(legacy_database, "tickets")

    with pytest.raises(RuntimeError) as error:
        command.upgrade(_config_for(legacy_database), "head")

    message = str(error.value)
    assert "already contains 3 tables (names not shown)" in message

    for table in ("revoked_tokens", "tickets", "users"):
        assert table not in message
    assert "python -m app.db.check_baseline --stamp" in message
    assert "Nothing was changed" in message

    # The failed migration was rolled back entirely, including the
    # alembic_version table Alembic creates before running migrations.
    assert "alembic_version" not in _table_names(legacy_database)
    assert _sha256(legacy_database) == before
    assert _rows(legacy_database, "users") == users_before
    assert _rows(legacy_database, "tickets") == tickets_before


def test_baseline_migration_refuses_unrelated_existing_table(
    database_file: Path,
    alembic_config: Config,
) -> None:
    """Any application table blocks the baseline, not just known ones."""

    with _connect(database_file) as connection:
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
        connection.execute("INSERT INTO notes (body) VALUES ('keep me')")
        connection.commit()

    before = _sha256(database_file)

    with pytest.raises(
        RuntimeError,
        match=r"already contains 1 table \(names not shown\)",
    ):
        command.upgrade(alembic_config, "head")

    assert _table_names(database_file) == {"notes"}
    assert _rows(database_file, "notes") == [(1, "keep me")]
    assert _sha256(database_file) == before


def test_baseline_migration_accepts_existing_empty_file(
    database_file: Path,
    alembic_config: Config,
) -> None:
    with _connect(database_file):
        pass

    assert database_file.is_file()

    command.upgrade(alembic_config, "head")

    assert _current_revision(database_file) == [HEAD_REVISION]


def test_baseline_migration_ignores_sqlite_internal_tables(
    database_file: Path,
    alembic_config: Config,
) -> None:
    # ANALYZE creates sqlite_stat1, which outlives the table it describes.
    with _connect(database_file) as connection:
        connection.execute("CREATE TABLE scratch (value INTEGER)")
        connection.execute("CREATE INDEX ix_scratch_value ON scratch (value)")
        connection.execute("INSERT INTO scratch VALUES (1)")
        connection.execute("ANALYZE")
        connection.execute("DROP TABLE scratch")
        connection.commit()

    assert _table_names(database_file) == set()
    with _connect(database_file) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall() == [("sqlite_stat1",)]

    command.upgrade(alembic_config, "head")

    assert _current_revision(database_file) == [HEAD_REVISION]


def test_baseline_migration_accepts_existing_empty_version_table(
    database_file: Path,
    alembic_config: Config,
) -> None:
    """Alembic's own version table (no revision recorded) is allowed."""

    with _connect(database_file) as connection:
        connection.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        )
        connection.commit()

    command.upgrade(alembic_config, "head")

    assert _current_revision(database_file) == [HEAD_REVISION]


def test_offline_sql_generation_does_not_touch_any_database(
    database_file: Path,
    alembic_config: Config,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--sql cannot run the live guard; it never connects at all."""

    command.upgrade(alembic_config, "head", sql=True)

    output = capsys.readouterr().out

    assert "CREATE TABLE users" in output
    # 0003 rebuilds tickets from an explicit definition, which needs no
    # connection, and creates the history table.
    assert "CREATE TABLE ticket_events" in output
    assert not database_file.exists()


@pytest.mark.parametrize(
    ("description", "transform", "expected_message"),
    [
        (
            "missing index",
            lambda sql: [s for s in sql if "ix_users_email" not in s],
            "indexes differ",
        ),
        (
            "missing column",
            lambda sql: [
                s.replace("\tprediction_confidence FLOAT,\n", "") for s in sql
            ],
            "missing column prediction_confidence",
        ),
        (
            "changed column type",
            lambda sql: [s.replace("role VARCHAR(30)", "role VARCHAR(10)") for s in sql],
            "users.role",
        ),
        (
            "missing check constraint",
            lambda sql: [
                s.replace(
                    "\tCONSTRAINT ck_tickets_status CHECK "
                    "(status IN ('Open', 'In Progress', 'Resolved')),\n",
                    "",
                )
                for s in sql
            ],
            "check constraints differ",
        ),
        (
            "column already added",
            lambda sql: [
                s.replace(
                    "\tcreated_at DATETIME NOT NULL,\n\tPRIMARY KEY (id)\n)",
                    "\tcreated_at DATETIME NOT NULL,\n"
                    "\ttoken_version INTEGER DEFAULT 0 NOT NULL,\n"
                    "\tPRIMARY KEY (id)\n)",
                )
                if s.startswith("CREATE TABLE users")
                else s
                for s in sql
            ],
            "users: 1 unexpected column (names not shown)",
        ),
        (
            "unexpected table",
            lambda sql: sql + ["CREATE TABLE notes (id INTEGER PRIMARY KEY)"],
            "1 unexpected table (names not shown)",
        ),
    ],
)
def test_schema_mismatch_is_reported_and_never_stamped(
    make_legacy_database: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
    description: str,
    transform: Callable[[list[str]], list[str]],
    expected_message: str,
) -> None:
    schema_sql = transform(list(LEGACY_SCHEMA_SQL))
    assert schema_sql != LEGACY_SCHEMA_SQL, description

    path = make_legacy_database(with_data=False, schema_sql=schema_sql)
    before = _sha256(path)

    status, messages = check_database(path)

    assert status == EXIT_MISMATCH
    assert any(expected_message in message for message in messages), messages

    # --stamp must refuse and leave the file untouched.
    assert check_baseline_main(["--database", str(path), "--stamp"]) == EXIT_MISMATCH
    assert "does NOT match" in capsys.readouterr().err
    assert _sha256(path) == before
    assert "alembic_version" not in _table_names(path)


def test_check_baseline_reports_missing_database(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"

    status, _ = check_database(missing)

    assert status == EXIT_NOT_APPLICABLE
    assert not missing.exists()


def test_check_baseline_reports_empty_database(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    with _connect(empty):
        pass

    status, messages = check_database(empty)

    assert status == EXIT_NOT_APPLICABLE
    assert any("alembic upgrade head" in message for message in messages)


def test_check_baseline_reports_managed_database(
    database_file: Path,
    alembic_config: Config,
) -> None:
    command.upgrade(alembic_config, "head")

    status, messages = check_database(database_file)

    assert status == EXIT_NOT_APPLICABLE
    assert any(HEAD_REVISION in message for message in messages)


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------


def test_downgrade_to_baseline_removes_column_and_keeps_data(
    legacy_database: Path,
) -> None:
    users_before = _rows(legacy_database, "users")
    tickets_before = _rows(legacy_database, "tickets")

    config = _config_for(legacy_database)
    command.stamp(config, BASELINE_REVISION)
    command.upgrade(config, "head")

    command.downgrade(config, BASELINE_REVISION)

    assert _current_revision(legacy_database) == [BASELINE_REVISION]
    assert _rows(legacy_database, "users") == users_before
    assert _rows(legacy_database, "tickets") == tickets_before

    # After downgrade the schema is the baseline again.
    with _connect(legacy_database) as connection:
        connection.execute("DROP TABLE alembic_version")
        connection.commit()

    status, messages = check_database(legacy_database)

    assert status == EXIT_MATCH, messages


# ---------------------------------------------------------------------------
# Read-only "is the database usable at head?" check
# ---------------------------------------------------------------------------


def _run_sql(path: Path, *statements: str) -> None:
    with _connect(path) as connection:
        for statement in statements:
            connection.execute(statement)
        connection.commit()


def _at_head(path: Path) -> None:
    command.upgrade(_config_for(path), "head")


def _pending(path: Path) -> None:
    command.upgrade(_config_for(path), BASELINE_REVISION)


def _unmanaged(path: Path) -> None:
    _run_sql(path, *LEGACY_SCHEMA_SQL)


def _unknown_revision(path: Path) -> None:
    _at_head(path)
    _run_sql(path, "UPDATE alembic_version SET version_num = '9999'")


def _empty_version_table(path: Path) -> None:
    _at_head(path)
    _run_sql(path, "DELETE FROM alembic_version")


def _duplicate_head_rows(path: Path) -> None:
    # Alembic's own table has a primary key; a hand-made one may not.
    _at_head(path)
    _run_sql(
        path,
        "DROP TABLE alembic_version",
        "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)",
        f"INSERT INTO alembic_version VALUES ('{HEAD_REVISION}')",
        f"INSERT INTO alembic_version VALUES ('{HEAD_REVISION}')",
    )


def _two_known_revisions(path: Path) -> None:
    _at_head(path)
    _run_sql(path, f"INSERT INTO alembic_version VALUES ('{BASELINE_REVISION}')")


def _foreign_database_at_head(path: Path) -> None:
    _run_sql(
        path,
        "CREATE TABLE invoices (id INTEGER PRIMARY KEY, total FLOAT)",
        "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, "
        "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))",
        f"INSERT INTO alembic_version VALUES ('{HEAD_REVISION}')",
    )


def _missing_application_tables(path: Path) -> None:
    _at_head(path)
    _run_sql(path, "DROP TABLE revoked_tokens", "DROP TABLE tickets")


def _users_without_token_version(path: Path) -> None:
    _at_head(path)
    _run_sql(path, "ALTER TABLE users DROP COLUMN token_version")


def _baseline_stamped_as_head(path: Path) -> None:
    # The revision says head, but the later migrations were never applied.
    _pending(path)
    _run_sql(
        path,
        f"UPDATE alembic_version SET version_num = '{HEAD_REVISION}'",
    )


def _unexpected_application_table(path: Path) -> None:
    _at_head(path)
    _run_sql(path, "CREATE TABLE notes (id INTEGER PRIMARY KEY)")


def _missing_index(path: Path) -> None:
    _at_head(path)
    _run_sql(path, "DROP INDEX ix_users_email")


@pytest.mark.parametrize(
    ("setup", "error_type", "expected_fragment", "advises_upgrade"),
    [
        pytest.param(
            _pending,
            PendingMigrationsError,
            f"is at revision 0001, but this code requires revision {HEAD_REVISION}",
            True,
            id="pending-earlier-revision",
        ),
        pytest.param(
            _unmanaged,
            UnmanagedDatabaseError,
            "no Alembic revision recorded",
            True,
            id="unmanaged-pre-alembic",
        ),
        pytest.param(
            _unknown_revision,
            UnknownRevisionError,
            "records 1 revision value that this code does not know",
            False,
            id="unknown-revision",
        ),
        pytest.param(
            _empty_version_table,
            InconsistentMigrationRecordsError,
            "table with no rows",
            False,
            id="empty-version-table",
        ),
        pytest.param(
            _duplicate_head_rows,
            InconsistentMigrationRecordsError,
            "records the same revision more than once (2 rows in its "
            "alembic_version table, 1 duplicated value)",
            False,
            id="duplicate-head-rows",
        ),
        pytest.param(
            _two_known_revisions,
            InvalidRevisionSetError,
            f"records several revisions (0001, {HEAD_REVISION})",
            False,
            id="two-known-revisions-linear-history",
        ),
        pytest.param(
            _foreign_database_at_head,
            SchemaMismatchError,
            "1 unexpected table (names not shown)",
            False,
            id="unrelated-database-at-head",
        ),
        pytest.param(
            _missing_application_tables,
            SchemaMismatchError,
            "missing table: revoked_tokens",
            False,
            id="missing-application-tables",
        ),
        pytest.param(
            _users_without_token_version,
            SchemaMismatchError,
            "users: missing column token_version",
            False,
            id="users-missing-token-version",
        ),
        pytest.param(
            _baseline_stamped_as_head,
            SchemaMismatchError,
            "users: missing column token_version",
            False,
            id="baseline-schema-stamped-as-head",
        ),
        pytest.param(
            _unexpected_application_table,
            SchemaMismatchError,
            "1 unexpected table (names not shown)",
            False,
            id="unexpected-application-table",
        ),
        pytest.param(
            _missing_index,
            SchemaMismatchError,
            "users: indexes differ",
            False,
            id="missing-index",
        ),
    ],
)
def test_ensure_database_at_head_refuses_and_changes_nothing(
    database_file: Path,
    setup: Callable[[Path], None],
    error_type: type[DatabaseNotAtHeadError],
    expected_fragment: str,
    advises_upgrade: bool,
) -> None:
    setup(database_file)
    before = _sha256(database_file)

    with pytest.raises(DatabaseNotAtHeadError) as error_info:
        ensure_database_at_head(database_file)

    error = error_info.value
    message = str(error)

    assert type(error) is error_type
    assert expected_fragment in message

    # 'alembic upgrade head' is recommended only where it is safe; in every
    # other case it may appear only as an explicit prohibition.
    if advises_upgrade:
        assert "'alembic upgrade head'" in message
        assert "Do not run" not in message
    else:
        assert "alembic upgrade head" not in message.replace(
            "Do not run 'alembic upgrade head'", ""
        )

    if isinstance(error, SchemaMismatchError):
        assert any(expected_fragment in item for item in error.differences)

    assert _sha256(database_file) == before


def test_ensure_database_at_head_accepts_new_database_at_head(
    database_file: Path,
) -> None:
    _at_head(database_file)
    before = _sha256(database_file)

    ensure_database_at_head(database_file)

    assert _sha256(database_file) == before


def test_ensure_database_at_head_accepts_upgraded_legacy_database(
    legacy_database: Path,
) -> None:
    """The documented stamp-then-upgrade path yields an accepted database."""

    config = _config_for(legacy_database)
    command.stamp(config, BASELINE_REVISION)
    command.upgrade(config, "head")
    before = _sha256(legacy_database)

    ensure_database_at_head(legacy_database)

    assert _sha256(legacy_database) == before


def test_ensure_database_at_head_does_not_create_missing_file(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.db"

    with pytest.raises(DatabaseUnavailableError, match="Database not found"):
        ensure_database_at_head(missing)

    assert not missing.exists()


def test_ensure_database_at_head_rejects_unreadable_file(
    tmp_path: Path,
) -> None:
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a SQLite database " * 64)
    before = _sha256(garbage)

    with pytest.raises(DatabaseUnavailableError, match="Could not read") as error:
        ensure_database_at_head(garbage)

    # Only the fixed description is shown, never SQLite's raw message
    # ("file is not a database").
    message = str(error.value)
    assert "(the file is not a SQLite database [SQLITE_NOTADB])" in message
    assert "file is not a database" not in message

    assert _sha256(garbage) == before


def test_read_database_revisions_preserves_duplicate_rows(
    database_file: Path,
) -> None:
    _duplicate_head_rows(database_file)
    before = _sha256(database_file)

    assert read_database_revisions(database_file) == [
        HEAD_REVISION,
        HEAD_REVISION,
    ]
    assert _sha256(database_file) == before


def test_read_database_revisions_without_version_table(
    legacy_database: Path,
) -> None:
    assert read_database_revisions(legacy_database) is None


def test_reference_schema_is_built_from_migrations_in_a_temporary_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reference never falls back to the application database."""

    # Redirect the application's default URL to a sentinel file. If the
    # reference build ever used the default, the sentinel would be created.
    sentinel = tmp_path / "application-database-sentinel.db"
    monkeypatch.setattr(
        app_database,
        "DATABASE_URL",
        make_url(sqlite_url(sentinel)),
    )

    reference = build_reference_schema("heads")

    assert not sentinel.exists()
    assert set(reference) == {
        "users",
        "tickets",
        "ticket_events",
        "ticket_messages",
        "notifications",
        "revoked_tokens",
    }
    assert reference["users"]["columns"]["token_version"] == (
        "INTEGER",
        False,
        "0",
    )


# ---------------------------------------------------------------------------
# Readiness diagnostics never echo database-provided text (A7)
# ---------------------------------------------------------------------------


SECRET_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2VjcmV0LXNhbHQ$c2VjcmV0LWhhc2g"
HOSTILE_REVISION = (
    f"9999'; DROP TABLE users; -- {SECRET_HASH}\x1b[31m\nINJECTED"
)
SECRET_DEFAULT = f"stored-default {SECRET_HASH}"
SECRET_SQL = "SELECT password_hash FROM users"
HOSTILE_TABLE = f"exfil_{SECRET_HASH}\x1b[2J"
HOSTILE_COLUMN = f"exfil_col_{SECRET_HASH}"

# Text that may only come from the database, so must never be displayed.
DATABASE_ONLY_TEXT = (
    SECRET_HASH,
    "$argon2",
    "DROP TABLE",
    "INJECTED",
    "9999",
    "\x1b",
    "stored-default",
    SECRET_SQL,
    "password_hash",
    "exfil_",
    "IN ('Open'",
)


def _assert_no_database_text(text: str) -> None:
    for fragment in DATABASE_ONLY_TEXT:
        assert fragment not in text, f"diagnostic exposed {fragment!r}"


def _set_version_rows(
    path: Path,
    values: list[str],
    *,
    primary_key: bool = True,
) -> None:
    """Replace alembic_version's rows, binding values as parameters."""

    constraint = (
        ", CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)"
        if primary_key
        else ""
    )

    with _connect(path) as connection:
        connection.execute("DROP TABLE alembic_version")
        connection.execute(
            "CREATE TABLE alembic_version "
            f"(version_num VARCHAR(32) NOT NULL{constraint})"
        )
        connection.executemany(
            "INSERT INTO alembic_version VALUES (?)",
            [(value,) for value in values],
        )
        connection.commit()


@pytest.mark.parametrize(
    ("values", "primary_key", "error_type", "expected_fragment"),
    [
        pytest.param(
            [HOSTILE_REVISION],
            True,
            UnknownRevisionError,
            "records 1 revision value that this code does not know",
            id="hostile-unknown-revision",
        ),
        pytest.param(
            [HOSTILE_REVISION, HOSTILE_REVISION],
            False,
            InconsistentMigrationRecordsError,
            "(2 rows in its alembic_version table, 1 duplicated value)",
            id="hostile-duplicated-revision",
        ),
        pytest.param(
            [HEAD_REVISION, HOSTILE_REVISION],
            True,
            UnknownRevisionError,
            "records 1 revision value that this code does not know",
            id="head-plus-hostile-revision",
        ),
    ],
)
def test_revision_diagnostics_never_echo_stored_values(
    database_file: Path,
    values: list[str],
    primary_key: bool,
    error_type: type[DatabaseNotAtHeadError],
    expected_fragment: str,
) -> None:
    _at_head(database_file)
    _set_version_rows(database_file, values, primary_key=primary_key)
    before = _sha256(database_file)

    with pytest.raises(DatabaseNotAtHeadError) as error_info:
        ensure_database_at_head(database_file)

    message = str(error_info.value)

    assert type(error_info.value) is error_type
    assert expected_fragment in message
    _assert_no_database_text(message)
    assert _sha256(database_file) == before


def test_known_revisions_are_shown_from_the_code_not_the_database(
    database_file: Path,
) -> None:
    _at_head(database_file)
    _set_version_rows(database_file, [BASELINE_REVISION, HEAD_REVISION])

    with pytest.raises(InvalidRevisionSetError) as error_info:
        ensure_database_at_head(database_file)

    # Both values equal this code's revision IDs, so they may be shown.
    assert f"records several revisions (0001, {HEAD_REVISION})" in str(
        error_info.value
    )


def _hostile_schema(path: Path) -> None:
    """A database at the head revision whose schema text carries secrets and SQL."""

    schema_sql = [
        statement.replace(
            "role VARCHAR(30) NOT NULL",
            f"role VARCHAR(30) NOT NULL DEFAULT '{SECRET_DEFAULT}'",
        ).replace(
            "CHECK (status IN ('Open', 'In Progress', 'Resolved'))",
            "CHECK (status IN ('Open', 'In Progress', 'Resolved') "
            f"AND title <> '{SECRET_SQL}')",
        )
        for statement in LEGACY_SCHEMA_SQL
    ]

    _run_sql(
        path,
        *schema_sql,
        "ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0",
        f'ALTER TABLE users ADD COLUMN "{HOSTILE_COLUMN}" TEXT '
        f"DEFAULT '{SECRET_DEFAULT}'",
        f'CREATE TABLE "{HOSTILE_TABLE}" (id INTEGER PRIMARY KEY, '
        f"note TEXT CHECK (note <> '{SECRET_SQL}'))",
        "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, "
        "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))",
        f"INSERT INTO alembic_version VALUES ('{HEAD_REVISION}')",
    )


def test_schema_diagnostics_never_echo_definitions_or_sql(
    database_file: Path,
) -> None:
    _hostile_schema(database_file)
    before = _sha256(database_file)

    with pytest.raises(SchemaMismatchError) as error_info:
        ensure_database_at_head(database_file)

    error = error_info.value
    message = str(error)

    # Still detected, and still useful: fixed categories, code-defined
    # names and counts.
    assert "users.role: column definition differs" in message
    assert "users: 1 unexpected column (names not shown)" in message
    assert "tickets: check constraints differ" in message
    assert "1 unexpected table (names not shown)" in message

    _assert_no_database_text(message)

    for difference in error.differences:
        _assert_no_database_text(difference)

    assert _sha256(database_file) == before


# ---------------------------------------------------------------------------
# The check_baseline CLI never echoes database-provided text (A)
# ---------------------------------------------------------------------------


def _hostile_legacy_schema(path: Path) -> None:
    """A pre-Alembic database whose schema text carries secrets and SQL."""

    schema_sql = [
        statement.replace(
            "role VARCHAR(30) NOT NULL",
            f"role VARCHAR(30) NOT NULL DEFAULT '{SECRET_DEFAULT}'",
        ).replace(
            "CHECK (status IN ('Open', 'In Progress', 'Resolved'))",
            "CHECK (status IN ('Open', 'In Progress', 'Resolved') "
            f"AND title <> '{SECRET_SQL}')",
        )
        for statement in LEGACY_SCHEMA_SQL
    ]

    _run_sql(
        path,
        *schema_sql,
        f'ALTER TABLE users ADD COLUMN "{HOSTILE_COLUMN}" TEXT '
        f"DEFAULT '{SECRET_DEFAULT}'",
        f'CREATE TABLE "{HOSTILE_TABLE}" (id INTEGER PRIMARY KEY, '
        f"note TEXT CHECK (note <> '{SECRET_SQL}'))",
    )


def test_check_baseline_summarises_hostile_schema_safely(
    database_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _hostile_legacy_schema(database_file)
    before = _sha256(database_file)

    status, messages = check_database(database_file)

    assert status == EXIT_MISMATCH

    # Detection and diagnostic value are preserved.
    assert "users.role: column definition differs (type, nullability or default)" in messages
    assert "users: 1 unexpected column (names not shown)" in messages
    assert "tickets: check constraints differ" in messages
    assert "1 unexpected table (names not shown)" in messages

    for message in messages:
        _assert_no_database_text(message)

    # The CLI (even with --stamp) prints only the summary and stamps nothing.
    assert check_baseline_main(["--database", str(database_file), "--stamp"]) == (
        EXIT_MISMATCH
    )

    captured = capsys.readouterr()

    for stream in (captured.out, captured.err):
        _assert_no_database_text(stream)
        assert "Traceback" not in stream

    assert "1 unexpected table (names not shown)" in captured.err
    assert _sha256(database_file) == before
    assert "alembic_version" not in _table_names(database_file)


@pytest.mark.parametrize(
    ("values", "primary_key", "expected_fragment"),
    [
        pytest.param(
            [HOSTILE_REVISION],
            True,
            "(1 unrecognised revision value (not shown))",
            id="hostile-revision",
        ),
        pytest.param(
            [HOSTILE_REVISION, HOSTILE_REVISION],
            False,
            "(1 unrecognised revision value (not shown))",
            id="hostile-duplicates",
        ),
        pytest.param(
            [HEAD_REVISION, HOSTILE_REVISION],
            True,
            f"(recorded revision: {HEAD_REVISION}; 1 unrecognised revision value "
            "(not shown))",
            id="head-plus-hostile",
        ),
        pytest.param(
            [],
            True,
            "(no revision recorded)",
            id="empty-version-table",
        ),
        pytest.param(
            [BASELINE_REVISION],
            True,
            "(recorded revision: 0001)",
            id="known-revision",
        ),
    ],
)
def test_check_baseline_never_echoes_recorded_revisions(
    database_file: Path,
    capsys: pytest.CaptureFixture[str],
    values: list[str],
    primary_key: bool,
    expected_fragment: str,
) -> None:
    _at_head(database_file)
    _set_version_rows(database_file, values, primary_key=primary_key)
    before = _sha256(database_file)

    status, messages = check_database(database_file)

    assert status == EXIT_NOT_APPLICABLE
    assert expected_fragment in messages[0]

    for message in messages:
        _assert_no_database_text(message)

    assert check_baseline_main(["--database", str(database_file)]) == (
        EXIT_NOT_APPLICABLE
    )

    captured = capsys.readouterr()

    for stream in (captured.out, captured.err):
        _assert_no_database_text(stream)

    assert _sha256(database_file) == before


def test_check_baseline_reports_unreadable_file_safely(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a SQLite database " * 64)
    before = _sha256(garbage)

    assert check_baseline_main(["--database", str(garbage)]) == EXIT_DATABASE_ERROR

    captured = capsys.readouterr()

    assert (
        "(DatabaseError: the file is not a SQLite database [SQLITE_NOTADB])"
        in captured.err
    )

    for stream in (captured.out, captured.err):
        assert "file is not a database" not in stream  # the raw driver text
        assert "PRAGMA" not in stream
        assert "Traceback" not in stream

    assert _sha256(garbage) == before


def test_check_baseline_reports_stamp_failure_safely(
    legacy_database: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _failing_stamp(*_args, **_kwargs):
        driver_error = sqlite3.OperationalError(f"stamp detail {SECRET_HASH}")
        driver_error.sqlite_errorname = "SQLITE_BUSY"
        raise OperationalError(
            "INSERT INTO alembic_version (version_num) VALUES (?)",
            (SECRET_HASH,),
            driver_error,
        )

    monkeypatch.setattr(command, "stamp", _failing_stamp)

    exit_code = check_baseline_main(
        ["--database", str(legacy_database), "--stamp"]
    )

    captured = capsys.readouterr()

    assert exit_code == EXIT_DATABASE_ERROR
    assert (
        "Could not stamp the baseline revision (OperationalError: the "
        "database is locked [SQLITE_BUSY])"
    ) in captured.err

    for stream in (captured.out, captured.err):
        _assert_no_database_text(stream)
        assert "stamp detail" not in stream
        assert "INSERT INTO" not in stream
        assert "Traceback" not in stream

    assert "alembic_version" not in _table_names(legacy_database)


# ---------------------------------------------------------------------------
# The baseline migration's refusal never names existing tables (FIX 1)
# ---------------------------------------------------------------------------


HOSTILE_TABLE_NAMES = (
    f"leak_{SECRET_HASH}\x1b[31m",
    f"drop\nTABLE users; -- {SECRET_HASH}",
)


def test_baseline_refusal_never_names_existing_tables(
    database_file: Path,
    alembic_config: Config,
) -> None:
    _run_sql(
        database_file,
        *(
            f'CREATE TABLE "{name}" (id INTEGER PRIMARY KEY)'
            for name in HOSTILE_TABLE_NAMES
        ),
    )
    tables_before = _table_names(database_file)
    before = _sha256(database_file)

    assert tables_before == set(HOSTILE_TABLE_NAMES)

    with pytest.raises(RuntimeError) as error_info:
        command.upgrade(alembic_config, "head")

    message = str(error_info.value)

    # Still refused, with a count instead of the names.
    assert "already contains 2 tables (names not shown)" in message
    assert "Nothing was changed" in message

    for fragment in (SECRET_HASH, "$argon2", "\x1b", "leak_", "drop\n", "DROP"):
        assert fragment not in message

    assert _table_names(database_file) == tables_before
    assert _sha256(database_file) == before


# ---------------------------------------------------------------------------
# Revision 0003: ticket assignment columns and ticket_events
# ---------------------------------------------------------------------------

MIGRATION_0003 = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0003_ticket_assignment_and_events.py"
)

# Data inserted at revision 0002. Ticket 2 is "In Progress" with nobody
# assigned (assignment does not exist yet); 0003 must leave it as it is.
DATA_0002_SQL = [
    "INSERT INTO users (id, name, email, password_hash, role, is_active, "
    "created_at, token_version) VALUES (1, 'Agent One', 'agent1@example.test', "
    "'hash-1', 'agent', 1, '2026-11-01 09:00:00.000000', 0)",
    "INSERT INTO users (id, name, email, password_hash, role, is_active, "
    "created_at, token_version) VALUES (2, 'Old Admin', 'admin@example.test', "
    "'hash-2', 'admin', 0, '2026-11-02 09:00:00.000000', 3)",
    "INSERT INTO users (id, name, email, password_hash, role, is_active, "
    "created_at, token_version) VALUES (3, 'Customer', 'customer@example.test', "
    "'hash-3', 'customer', 1, '2026-11-02 10:00:00.000000', 1)",
    "INSERT INTO tickets (id, title, description, requester_name, "
    "predicted_category, category, prediction_confidence, priority, status, "
    "created_by_user_id, created_at, updated_at) VALUES (1, 'Payment failed', "
    "'Card declined.', 'Jane', 'PAYMENT', 'PAYMENT', 0.97, 'High', 'Open', 1, "
    "'2026-11-03 10:00:00.000000', '2026-11-03 10:00:00.000000')",
    "INSERT INTO tickets (id, title, description, requester_name, "
    "predicted_category, category, prediction_confidence, priority, status, "
    "created_by_user_id, created_at, updated_at) VALUES (2, 'Refund pending', "
    "'Refund not received.', NULL, 'REFUND', NULL, 0.61, 'Medium', "
    "'In Progress', 3, '2026-11-04 10:00:00.000000', "
    "'2026-11-05 11:00:00.000000')",
    "INSERT INTO tickets (id, title, description, requester_name, "
    "predicted_category, category, prediction_confidence, priority, status, "
    "created_by_user_id, created_at, updated_at) VALUES (3, 'Late order', "
    "'Order is late.', NULL, NULL, NULL, NULL, 'Low', 'Resolved', 2, "
    "'2026-11-06 10:00:00.000000', '2026-11-07 10:00:00.000000')",
    "INSERT INTO revoked_tokens (jti, expires_at, revoked_at) VALUES "
    "('jti-0002', '2026-11-06 10:00:00.000000', '2026-11-06 09:30:00.000000')",
]

TICKET_EVENT_COLUMNS = (
    "ticket_id, event_type, actor_user_id, from_assignee_id, to_assignee_id, "
    "from_status, to_status, reason, created_at"
)


@pytest.fixture
def database_0002(tmp_path: Path) -> Path:
    """A genuine revision-0002 database (built by the migrations) with data."""

    path = tmp_path / "at_0002.db"
    command.upgrade(_config_for(path), TOKEN_VERSION_REVISION)
    _run_sql(path, *DATA_0002_SQL)

    assert _current_revision(path) == [TOKEN_VERSION_REVISION]

    return path


def _enforcing_connection(path: Path) -> closing[sqlite3.Connection]:
    """A connection with foreign keys enforced, as the application uses."""

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")

    return closing(connection)


def _table_sql(path: Path, table: str) -> str:
    with _connect(path) as connection:
        return connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()[0]


def _foreign_key_list(path: Path, table: str) -> set[tuple]:
    """(column, referred table, referred column, ON DELETE) per foreign key."""

    with _connect(path) as connection:
        return {
            (row[3], row[2], row[4], row[6])
            for row in connection.execute(f"PRAGMA foreign_key_list({table})")
        }


def _index_names(path: Path, table: str) -> set[str]:
    with _connect(path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = ? AND name NOT LIKE 'sqlite_%'",
                (table,),
            )
        }


def _describe(path: Path) -> dict:
    engine = create_engine(sqlite_url(path))

    try:
        return describe_schema(engine)
    finally:
        engine.dispose()


def _check_constraint_names(path: Path, table: str) -> set[str]:
    engine = create_engine(sqlite_url(path))

    try:
        return {
            constraint["name"]
            for constraint in inspect(engine).get_check_constraints(table)
        }
    finally:
        engine.dispose()


def _insert_event(connection: sqlite3.Connection, **overrides) -> None:
    values = {
        "ticket_id": 1,
        "event_type": "created",
        "actor_user_id": 1,
        "from_assignee_id": None,
        "to_assignee_id": None,
        "from_status": None,
        "to_status": None,
        "reason": None,
        "created_at": "2026-11-23 10:00:00.000000",
        **overrides,
    }
    connection.execute(
        f"INSERT INTO ticket_events ({TICKET_EVENT_COLUMNS}) VALUES "
        "(:ticket_id, :event_type, :actor_user_id, :from_assignee_id, "
        ":to_assignee_id, :from_status, :to_status, :reason, :created_at)",
        values,
    )


def test_0003_upgrade_preserves_existing_rows(database_0002: Path) -> None:
    users_before = _rows(database_0002, "users")
    tickets_before = _rows(database_0002, "tickets")
    revoked_before = _rows(database_0002, "revoked_tokens")

    command.upgrade(_config_for(database_0002), "head")

    assert _current_revision(database_0002) == [HEAD_REVISION]
    assert _rows(database_0002, "users") == users_before
    assert _rows(database_0002, "revoked_tokens") == revoked_before
    # Every ticket keeps every value; the new columns are NULL, NULL, 1.
    assert _rows(database_0002, "tickets") == [
        row + (None, None, 1) for row in tickets_before
    ]
    # No history is generated for existing tickets.
    assert _rows(database_0002, "ticket_events") == []

    with _connect(database_0002) as connection:
        in_progress = connection.execute(
            "SELECT status, assigned_to_user_id, assigned_at, version "
            "FROM tickets WHERE id = 2"
        ).fetchone()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    # The unassigned "In Progress" ticket is not assigned or moved to Open.
    assert in_progress == ("In Progress", None, None, 1)


def test_0003_schema_has_assignee_foreign_key_indexes_and_history_table(
    database_0002: Path,
) -> None:
    command.upgrade(_config_for(database_0002), "head")

    # SQLite itself records the assignee foreign key (not only the model).
    assert _foreign_key_list(database_0002, "tickets") == {
        ("created_by_user_id", "users", "id", "NO ACTION"),
        ("assigned_to_user_id", "users", "id", "NO ACTION"),
    }
    assert (
        "CONSTRAINT fk_tickets_assigned_to_user_id_users FOREIGN KEY"
        "(assigned_to_user_id) REFERENCES users (id)"
        in _table_sql(database_0002, "tickets")
    )
    assert _index_names(database_0002, "tickets") == {
        "ix_tickets_created_by_user_id",
        "ix_tickets_assigned_to_user_id",
    }
    assert _check_constraint_names(database_0002, "tickets") == {
        "ck_tickets_priority",
        "ck_tickets_status",
    }

    columns = _describe(database_0002)["tickets"]["columns"]

    assert columns["assigned_to_user_id"] == ("INTEGER", True, None)
    assert columns["assigned_at"] == ("DATETIME", True, None)
    assert columns["version"] == ("INTEGER", False, "1")

    assert _foreign_key_list(database_0002, "ticket_events") == {
        ("ticket_id", "tickets", "id", "CASCADE"),
        ("actor_user_id", "users", "id", "NO ACTION"),
        ("from_assignee_id", "users", "id", "NO ACTION"),
        ("to_assignee_id", "users", "id", "NO ACTION"),
    }
    events_sql = _table_sql(database_0002, "ticket_events")

    for name in (
        "fk_ticket_events_ticket_id_tickets",
        "fk_ticket_events_actor_user_id_users",
        "fk_ticket_events_from_assignee_id_users",
        "fk_ticket_events_to_assignee_id_users",
    ):
        assert f"CONSTRAINT {name} FOREIGN KEY" in events_sql

    assert _index_names(database_0002, "ticket_events") == {
        "ix_ticket_events_ticket_id_created_at_id",
    }
    assert _check_constraint_names(database_0002, "ticket_events") == {
        "ck_ticket_events_event_type",
        "ck_ticket_events_from_status",
        "ck_ticket_events_to_status",
        "ck_ticket_events_reason",
    }

    with _connect(database_0002) as connection:
        index_columns = [
            row[2]
            for row in connection.execute(
                "PRAGMA index_info(ix_ticket_events_ticket_id_created_at_id)"
            )
        ]

    assert index_columns == ["ticket_id", "created_at", "id"]

    # Upgrading an existing database gives exactly the schema a new one gets.
    assert compare_schemas(
        build_reference_schema("head"), _describe(database_0002)
    ) == []


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE tickets SET priority = 'Urgent' WHERE id = 1",
        "UPDATE tickets SET status = 'Closed' WHERE id = 1",
        "UPDATE tickets SET assigned_to_user_id = 99 WHERE id = 1",
        "UPDATE tickets SET version = NULL WHERE id = 1",
    ],
    ids=["invalid priority", "invalid status", "missing assignee", "null version"],
)
def test_0003_ticket_constraints_are_enforced(
    database_0002: Path,
    statement: str,
) -> None:
    command.upgrade(_config_for(database_0002), "head")
    tickets_before = _rows(database_0002, "tickets")

    with _enforcing_connection(database_0002) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement)

    assert _rows(database_0002, "tickets") == tickets_before


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_type": "deleted"},
        {"event_type": None},
        {"from_status": "Closed"},
        {"to_status": "open"},
        {"reason": "something_else"},
        {"ticket_id": 99},
        {"actor_user_id": 99},
        {"actor_user_id": None},
        {"from_assignee_id": 99},
        {"to_assignee_id": 99},
        {"created_at": None},
    ],
    ids=lambda overrides: "-".join(f"{k}={v}" for k, v in overrides.items()),
)
def test_0003_ticket_event_constraints_are_enforced(
    database_0002: Path,
    overrides: dict,
) -> None:
    command.upgrade(_config_for(database_0002), "head")

    with _enforcing_connection(database_0002) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_event(connection, **overrides)

    assert _rows(database_0002, "ticket_events") == []


def test_0003_valid_events_are_accepted_and_cascade_with_their_ticket(
    database_0002: Path,
) -> None:
    command.upgrade(_config_for(database_0002), "head")

    with _enforcing_connection(database_0002) as connection:
        _insert_event(connection)
        _insert_event(connection, event_type="assigned", to_assignee_id=1)
        _insert_event(
            connection,
            event_type="status_changed",
            from_status="Open",
            to_status="In Progress",
        )
        _insert_event(
            connection,
            event_type="unassigned",
            from_assignee_id=1,
            reason="assignee_deactivated",
        )
        _insert_event(connection, ticket_id=2, actor_user_id=3)
        connection.commit()

        # Deleting a ticket deletes its events, and only its events.
        connection.execute("DELETE FROM tickets WHERE id = 1")
        connection.commit()

        remaining = connection.execute(
            "SELECT ticket_id FROM ticket_events"
        ).fetchall()

    assert remaining == [(2,)]


def test_0003_downgrade_restores_0002_schema_and_data(database_0002: Path) -> None:
    users_before = _rows(database_0002, "users")
    tickets_before = _rows(database_0002, "tickets")
    config = _config_for(database_0002)
    reference_0002 = build_reference_schema(TOKEN_VERSION_REVISION)

    command.upgrade(config, "head")

    # Use the new schema before downgrading: an assignment and history.
    with _enforcing_connection(database_0002) as connection:
        connection.execute(
            "UPDATE tickets SET assigned_to_user_id = 1, "
            "assigned_at = '2026-11-23 10:00:00.000000', version = 2 "
            "WHERE id = 1"
        )
        _insert_event(connection, event_type="assigned", to_assignee_id=1)
        connection.commit()

    command.downgrade(config, TOKEN_VERSION_REVISION)

    assert _current_revision(database_0002) == [TOKEN_VERSION_REVISION]
    assert "ticket_events" not in _table_names(database_0002)
    assert _rows(database_0002, "users") == users_before
    assert _rows(database_0002, "tickets") == tickets_before
    # Columns, foreign keys, indexes and CHECK constraints are exactly 0002.
    assert compare_schemas(reference_0002, _describe(database_0002)) == []
    assert _foreign_key_list(database_0002, "tickets") == {
        ("created_by_user_id", "users", "id", "NO ACTION"),
    }
    assert _index_names(database_0002, "tickets") == {
        "ix_tickets_created_by_user_id",
    }
    assert "fk_tickets_assigned_to_user_id_users" not in _table_sql(
        database_0002, "tickets"
    )

    with _enforcing_connection(database_0002) as connection:
        for statement in (
            "UPDATE tickets SET status = 'Closed' WHERE id = 1",
            "UPDATE tickets SET priority = 'Urgent' WHERE id = 1",
            "UPDATE tickets SET created_by_user_id = 99 WHERE id = 1",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)

        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    assert _rows(database_0002, "tickets") == tickets_before


def test_0003_refuses_unexpected_ticket_columns_and_changes_nothing(
    database_0002: Path,
) -> None:
    """The rebuild copies known columns only, so it must not drop others."""

    _run_sql(
        database_0002,
        "ALTER TABLE tickets ADD COLUMN secret_notes_column TEXT",
        "UPDATE tickets SET secret_notes_column = 'keep me' WHERE id = 1",
    )
    before = _sha256(database_0002)

    with pytest.raises(RuntimeError) as error:
        command.upgrade(_config_for(database_0002), "head")

    message = str(error.value)

    assert "0 missing, 1 unexpected; names not shown" in message
    assert "Nothing was changed" in message
    assert "secret_notes_column" not in message
    assert _sha256(database_0002) == before
    assert _current_revision(database_0002) == [TOKEN_VERSION_REVISION]
    assert "ticket_events" not in _table_names(database_0002)


def test_0003_runs_with_foreign_keys_enforced(database_0002: Path) -> None:
    """alembic/env.py turns enforcement off; the migration must not need that.

    The migration's own upgrade and downgrade are run on a connection that
    enforces foreign keys: tickets is rebuilt before ticket_events exists,
    and ticket_events is dropped before tickets is rebuilt again.
    """

    spec = importlib.util.spec_from_file_location("migration_0003", MIGRATION_0003)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    tickets_before = _rows(database_0002, "tickets")
    engine = create_engine(sqlite_url(database_0002))

    @event.listens_for(engine, "connect")
    def _enforce(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    try:
        with engine.begin() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1

            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()

        with _enforcing_connection(database_0002) as connection:
            _insert_event(connection)
            connection.commit()

        assert _rows(database_0002, "tickets") == [
            row + (None, None, 1) for row in tickets_before
        ]

        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
    finally:
        engine.dispose()

    assert _rows(database_0002, "tickets") == tickets_before
    assert "ticket_events" not in _table_names(database_0002)

    with _connect(database_0002) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


# ---------------------------------------------------------------------------
# Revision 0004: ticket_messages conversation table
# ---------------------------------------------------------------------------

MIGRATION_0004 = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0004_ticket_messages.py"
)

# Phase 7 data at revision 0003, on top of DATA_0002_SQL: an assigned
# ticket with history, an unassigned one and a resolved one.
DATA_0003_SQL = [
    "UPDATE tickets SET assigned_to_user_id = 1, "
    "assigned_at = '2026-11-23 09:00:00.000000', version = 3 WHERE id = 1",
    "UPDATE tickets SET version = 2 WHERE id = 3",
    f"INSERT INTO ticket_events ({TICKET_EVENT_COLUMNS}) VALUES "
    "(1, 'created', 1, NULL, NULL, NULL, NULL, NULL, "
    "'2026-11-23 08:00:00.000000')",
    f"INSERT INTO ticket_events ({TICKET_EVENT_COLUMNS}) VALUES "
    "(1, 'assigned', 1, NULL, 1, NULL, NULL, NULL, "
    "'2026-11-23 09:00:00.000000')",
    f"INSERT INTO ticket_events ({TICKET_EVENT_COLUMNS}) VALUES "
    "(3, 'unassigned', 2, 1, NULL, 'In Progress', 'Open', "
    "'assignee_deactivated', '2026-11-23 09:30:00.000000')",
]

TICKET_MESSAGE_COLUMNS = (
    "ticket_id, sender_user_id, sender_role, content, created_at"
)

PHASE_7_TABLES = ("users", "tickets", "ticket_events", "revoked_tokens")


def _load_migration_0004():
    spec = importlib.util.spec_from_file_location("migration_0004", MIGRATION_0004)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    return migration


@pytest.fixture
def database_0003(tmp_path: Path) -> Path:
    """A genuine revision-0003 database (built by the migrations) with data."""

    path = tmp_path / "at_0003.db"
    command.upgrade(_config_for(path), ASSIGNMENT_REVISION)
    _run_sql(path, *DATA_0002_SQL, *DATA_0003_SQL)

    assert _current_revision(path) == [ASSIGNMENT_REVISION]

    return path


def _phase_7_rows(path: Path) -> dict[str, list[tuple]]:
    return {table: _rows(path, table) for table in PHASE_7_TABLES}


def _insert_message(connection: sqlite3.Connection, **overrides) -> None:
    values = {
        "ticket_id": 1,
        "sender_user_id": 3,
        "sender_role": "customer",
        "content": "Hello, I still need help.",
        "created_at": "2026-11-25 10:00:00.000000",
        **overrides,
    }
    connection.execute(
        f"INSERT INTO ticket_messages ({TICKET_MESSAGE_COLUMNS}) VALUES "
        "(:ticket_id, :sender_user_id, :sender_role, :content, :created_at)",
        values,
    )


def test_0004_upgrade_preserves_every_phase_7_row(database_0003: Path) -> None:
    before = _phase_7_rows(database_0003)

    assert before["ticket_events"]  # the fixture really has history

    command.upgrade(_config_for(database_0003), "head")

    assert _current_revision(database_0003) == [HEAD_REVISION]
    # Users, tickets (assignment and versions included), history and
    # revoked tokens keep exactly the same rows.
    assert _phase_7_rows(database_0003) == before
    assert _rows(database_0003, "ticket_messages") == []

    with _enforcing_connection(database_0003) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_0004_schema_has_foreign_keys_checks_and_index(database_0003: Path) -> None:
    command.upgrade(_config_for(database_0003), "head")

    assert _foreign_key_list(database_0003, "ticket_messages") == {
        ("ticket_id", "tickets", "id", "CASCADE"),
        ("sender_user_id", "users", "id", "NO ACTION"),
    }
    messages_sql = _table_sql(database_0003, "ticket_messages")

    for name in (
        "fk_ticket_messages_ticket_id_tickets",
        "fk_ticket_messages_sender_user_id_users",
    ):
        assert f"CONSTRAINT {name} FOREIGN KEY" in messages_sql

    assert _check_constraint_names(database_0003, "ticket_messages") == {
        "ck_ticket_messages_sender_role",
        "ck_ticket_messages_content_not_blank",
        "ck_ticket_messages_content_length",
    }
    assert _index_names(database_0003, "ticket_messages") == {
        "ix_ticket_messages_ticket_id_id",
    }

    with _connect(database_0003) as connection:
        index_columns = [
            row[2]
            for row in connection.execute(
                "PRAGMA index_info(ix_ticket_messages_ticket_id_id)"
            )
        ]

    assert index_columns == ["ticket_id", "id"]

    columns = _describe(database_0003)["ticket_messages"]["columns"]

    assert columns["ticket_id"] == ("INTEGER", False, None)
    assert columns["sender_user_id"] == ("INTEGER", False, None)
    assert columns["sender_role"] == ("VARCHAR(30)", False, None)
    assert columns["content"] == ("TEXT", False, None)
    assert columns["created_at"] == ("DATETIME", False, None)

    # Upgrading an existing database gives exactly the schema a new one gets.
    assert compare_schemas(
        build_reference_schema("head"), _describe(database_0003)
    ) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"sender_role": "superuser"},
        {"sender_role": "Admin"},
        {"sender_role": None},
        {"content": ""},
        {"content": "   "},
        {"content": " \t\r\n "},
        {"content": "x" * 10_001},
        {"content": None},
        {"ticket_id": 99},
        {"ticket_id": None},
        {"sender_user_id": 99},
        {"sender_user_id": None},
        {"created_at": None},
    ],
    ids=[
        "unknown-role",
        "role-wrong-case",
        "null-role",
        "empty-content",
        "spaces-only",
        "mixed-whitespace-only",
        "content-too-long",
        "null-content",
        "missing-ticket",
        "null-ticket",
        "missing-sender",
        "null-sender",
        "null-created-at",
    ],
)
def test_0004_message_constraints_are_enforced(
    database_0003: Path,
    overrides: dict,
) -> None:
    command.upgrade(_config_for(database_0003), "head")

    with _enforcing_connection(database_0003) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_message(connection, **overrides)

    assert _rows(database_0003, "ticket_messages") == []


def test_0004_valid_messages_are_accepted_and_cascade_with_their_ticket(
    database_0003: Path,
) -> None:
    command.upgrade(_config_for(database_0003), "head")

    with _enforcing_connection(database_0003) as connection:
        _insert_message(connection)
        _insert_message(connection, sender_user_id=1, sender_role="agent")
        _insert_message(connection, sender_user_id=2, sender_role="admin")
        _insert_message(connection, content="x" * 10_000)
        _insert_message(connection, ticket_id=2, content="  padded  ")
        # User 4 is referenced by nothing but a message on ticket 2.
        connection.execute(
            "INSERT INTO users (id, name, email, password_hash, role, "
            "is_active, created_at, token_version) VALUES (4, 'Agent Four', "
            "'agent4@example.test', 'hash-4', 'agent', 1, "
            "'2026-11-24 09:00:00.000000', 0)"
        )
        _insert_message(
            connection, ticket_id=2, sender_user_id=4, sender_role="agent"
        )
        connection.commit()

        # Deleting a ticket deletes its messages (and only its messages,
        # together with its events); the senders are kept.
        connection.execute("DELETE FROM tickets WHERE id = 1")
        connection.commit()

        remaining = connection.execute(
            "SELECT ticket_id FROM ticket_messages"
        ).fetchall()
        users = connection.execute("SELECT count(*) FROM users").fetchone()

        # A user referenced by a message cannot be deleted (no cascade
        # from messages to users).
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM users WHERE id = 4")

    assert remaining == [(2,), (2,)]
    assert users == (4,)


def test_0004_downgrade_restores_0003_and_upgrades_again(
    database_0003: Path,
) -> None:
    before = _phase_7_rows(database_0003)
    config = _config_for(database_0003)
    reference_0003 = build_reference_schema(ASSIGNMENT_REVISION)

    command.upgrade(config, "head")

    with _enforcing_connection(database_0003) as connection:
        _insert_message(connection)
        _insert_message(
            connection, ticket_id=3, sender_user_id=2, sender_role="admin"
        )
        connection.commit()

    command.downgrade(config, ASSIGNMENT_REVISION)

    assert _current_revision(database_0003) == [ASSIGNMENT_REVISION]
    assert "ticket_messages" not in _table_names(database_0003)
    assert _phase_7_rows(database_0003) == before
    assert compare_schemas(reference_0003, _describe(database_0003)) == []

    with _connect(database_0003) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master "
            "WHERE name = 'ix_ticket_messages_ticket_id_id'"
        ).fetchone() == (0,)

    # Re-upgrading works and starts with an empty conversation table.
    command.upgrade(config, "head")

    assert _current_revision(database_0003) == [HEAD_REVISION]
    assert _phase_7_rows(database_0003) == before
    assert _rows(database_0003, "ticket_messages") == []
    assert compare_schemas(
        build_reference_schema("head"), _describe(database_0003)
    ) == []

    with _connect(database_0003) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_0004_runs_with_foreign_keys_enforced(database_0003: Path) -> None:
    """The migration does not rely on env.py turning enforcement off."""

    migration = _load_migration_0004()
    before = _phase_7_rows(database_0003)
    engine = create_engine(sqlite_url(database_0003))

    @event.listens_for(engine, "connect")
    def _enforce(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    try:
        with engine.begin() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1

            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()

        with _enforcing_connection(database_0003) as connection:
            _insert_message(connection)
            connection.commit()

        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
    finally:
        engine.dispose()

    assert _phase_7_rows(database_0003) == before
    assert "ticket_messages" not in _table_names(database_0003)

    with _connect(database_0003) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_0004_model_check_constraints_match_the_migration() -> None:
    """The model's named CHECK constraints are the migration's, verbatim.

    Alembic's autogenerate comparison ignores CHECK constraints, so the
    model and the migration texts are compared directly.
    """

    from sqlalchemy import CheckConstraint

    from app.models import TicketMessage
    from app.models.ticket_message import MAX_MESSAGE_LENGTH

    migration = _load_migration_0004()
    source = MIGRATION_0004.read_text(encoding="utf-8")

    model_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in TicketMessage.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert set(model_checks) == {
        "ck_ticket_messages_sender_role",
        "ck_ticket_messages_content_not_blank",
        "ck_ticket_messages_content_length",
    }
    assert migration.MAX_MESSAGE_LENGTH == MAX_MESSAGE_LENGTH
    assert model_checks["ck_ticket_messages_content_length"] == (
        f"length(content) <= {migration.MAX_MESSAGE_LENGTH}"
    )

    for name, text in model_checks.items():
        if name != "ck_ticket_messages_content_length":
            assert f'"{text}"' in source


# ---------------------------------------------------------------------------
# Revision 0005: notifications table
# ---------------------------------------------------------------------------

MIGRATION_0005 = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0005_notifications.py"
)

# Phase 9 data at revision 0004, on top of DATA_0002_SQL and
# DATA_0003_SQL: conversations on all three tickets (customer, agent and
# administrator senders).
DATA_0004_SQL = [
    f"INSERT INTO ticket_messages (id, {TICKET_MESSAGE_COLUMNS}) VALUES "
    "(1, 2, 3, 'customer', 'My refund is still missing.', "
    "'2026-11-25 10:00:00.000000')",
    f"INSERT INTO ticket_messages (id, {TICKET_MESSAGE_COLUMNS}) VALUES "
    "(2, 2, 1, 'agent', 'Looking into it now.', "
    "'2026-11-25 10:05:00.000000')",
    f"INSERT INTO ticket_messages (id, {TICKET_MESSAGE_COLUMNS}) VALUES "
    "(3, 1, 1, 'agent', 'Card issuer contacted.', "
    "'2026-11-25 11:00:00.000000')",
    f"INSERT INTO ticket_messages (id, {TICKET_MESSAGE_COLUMNS}) VALUES "
    "(4, 3, 2, 'admin', 'Closing this one.', "
    "'2026-11-25 12:00:00.000000')",
]

NOTIFICATION_COLUMNS = (
    "recipient_user_id, actor_user_id, ticket_id, message_id, "
    "notification_type, created_at, read_at"
)

PHASE_9_TABLES = (*PHASE_7_TABLES, "ticket_messages")

NOTIFICATION_INDEXES = {
    "ix_notifications_recipient_user_id_id": ["recipient_user_id", "id"],
    "ix_notifications_recipient_user_id_read_at": ["recipient_user_id", "read_at"],
    "ix_notifications_ticket_id": ["ticket_id"],
}


def _load_migration_0005():
    spec = importlib.util.spec_from_file_location("migration_0005", MIGRATION_0005)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    return migration


@pytest.fixture
def database_0004(tmp_path: Path) -> Path:
    """A genuine revision-0004 database (built by the migrations) with data."""

    path = tmp_path / "at_0004.db"
    command.upgrade(_config_for(path), MESSAGES_REVISION)
    _run_sql(path, *DATA_0002_SQL, *DATA_0003_SQL, *DATA_0004_SQL)

    assert _current_revision(path) == [MESSAGES_REVISION]

    return path


def _phase_9_rows(path: Path) -> dict[str, list[tuple]]:
    return {table: _rows(path, table) for table in PHASE_9_TABLES}


def _insert_notification(connection: sqlite3.Connection, **overrides) -> None:
    """By default: agent 1 notified of customer 3's message 1 on ticket 2."""

    values = {
        "recipient_user_id": 1,
        "actor_user_id": 3,
        "ticket_id": 2,
        "message_id": 1,
        "notification_type": "ticket_message",
        "created_at": "2026-11-26 10:00:00.000000",
        "read_at": None,
        **overrides,
    }
    connection.execute(
        f"INSERT INTO notifications ({NOTIFICATION_COLUMNS}) VALUES "
        "(:recipient_user_id, :actor_user_id, :ticket_id, :message_id, "
        ":notification_type, :created_at, :read_at)",
        values,
    )


def test_0005_upgrade_preserves_every_phase_9_row(database_0004: Path) -> None:
    before = _phase_9_rows(database_0004)

    assert len(before["ticket_messages"]) == 4  # the fixture really has messages

    command.upgrade(_config_for(database_0004), "head")

    assert _current_revision(database_0004) == [HEAD_REVISION]
    # Users, tickets, history, messages and revoked tokens keep exactly
    # the same rows.
    assert _phase_9_rows(database_0004) == before
    # Existing messages are not backfilled into notifications.
    assert _rows(database_0004, "notifications") == []

    with _enforcing_connection(database_0004) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_0005_schema_has_foreign_keys_checks_unique_and_indexes(
    database_0004: Path,
) -> None:
    command.upgrade(_config_for(database_0004), "head")

    assert _foreign_key_list(database_0004, "notifications") == {
        ("recipient_user_id", "users", "id", "NO ACTION"),
        ("actor_user_id", "users", "id", "NO ACTION"),
        ("ticket_id", "tickets", "id", "CASCADE"),
        ("message_id", "ticket_messages", "id", "CASCADE"),
    }
    notifications_sql = _table_sql(database_0004, "notifications")

    for name in (
        "fk_notifications_recipient_user_id_users",
        "fk_notifications_actor_user_id_users",
        "fk_notifications_ticket_id_tickets",
        "fk_notifications_message_id_ticket_messages",
    ):
        assert f"CONSTRAINT {name} FOREIGN KEY" in notifications_sql

    assert (
        "CONSTRAINT uq_notifications_message_id_recipient_user_id "
        "UNIQUE (message_id, recipient_user_id)" in notifications_sql
    )
    assert _check_constraint_names(database_0004, "notifications") == {
        "ck_notifications_notification_type",
        "ck_notifications_message_required",
        "ck_notifications_not_self",
    }

    engine = create_engine(sqlite_url(database_0004))

    try:
        unique_constraints = [
            (constraint["name"], constraint["column_names"])
            for constraint in inspect(engine).get_unique_constraints(
                "notifications"
            )
        ]
    finally:
        engine.dispose()

    assert unique_constraints == [
        (
            "uq_notifications_message_id_recipient_user_id",
            ["message_id", "recipient_user_id"],
        )
    ]
    assert _index_names(database_0004, "notifications") == set(NOTIFICATION_INDEXES)

    with _connect(database_0004) as connection:
        index_list = {
            row[1]: row[2]
            for row in connection.execute("PRAGMA index_list(notifications)")
        }

        for index, expected_columns in NOTIFICATION_INDEXES.items():
            columns = [
                row[2] for row in connection.execute(f"PRAGMA index_info({index})")
            ]

            assert columns == expected_columns
            assert index_list[index] == 0  # not unique

    columns = _describe(database_0004)["notifications"]["columns"]

    assert columns == {
        "id": ("INTEGER", False, None),
        "recipient_user_id": ("INTEGER", False, None),
        "actor_user_id": ("INTEGER", False, None),
        "ticket_id": ("INTEGER", False, None),
        "message_id": ("INTEGER", True, None),
        "notification_type": ("VARCHAR(40)", False, None),
        "created_at": ("DATETIME", False, None),
        "read_at": ("DATETIME", True, None),
    }

    # Upgrading an existing database gives exactly the schema a new one gets.
    assert compare_schemas(
        build_reference_schema("head"), _describe(database_0004)
    ) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"notification_type": "status_changed"},
        {"notification_type": "Ticket_Message"},
        {"notification_type": ""},
        {"notification_type": None},
        {"message_id": None},
        {"recipient_user_id": 3},
        {"recipient_user_id": 99},
        {"recipient_user_id": None},
        {"actor_user_id": 99},
        {"actor_user_id": None},
        {"ticket_id": 99},
        {"ticket_id": None},
        {"message_id": 99},
        {"created_at": None},
    ],
    ids=[
        "unknown-type",
        "type-wrong-case",
        "empty-type",
        "null-type",
        "ticket-message-without-message",
        "self-notification",
        "missing-recipient",
        "null-recipient",
        "missing-actor",
        "null-actor",
        "missing-ticket",
        "null-ticket",
        "missing-message",
        "null-created-at",
    ],
)
def test_0005_notification_constraints_are_enforced(
    database_0004: Path,
    overrides: dict,
) -> None:
    command.upgrade(_config_for(database_0004), "head")

    with _enforcing_connection(database_0004) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_notification(connection, **overrides)

    assert _rows(database_0004, "notifications") == []


def test_0005_duplicate_message_recipient_pair_is_rejected(
    database_0004: Path,
) -> None:
    command.upgrade(_config_for(database_0004), "head")

    with _enforcing_connection(database_0004) as connection:
        _insert_notification(connection)
        connection.commit()

        # Same message and recipient, even with other values: refused.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_notification(
                connection,
                created_at="2026-11-26 11:00:00.000000",
                read_at="2026-11-26 11:30:00.000000",
            )

    assert len(_rows(database_0004, "notifications")) == 1


def test_0005_valid_notifications_are_accepted_and_cascade(
    database_0004: Path,
) -> None:
    command.upgrade(_config_for(database_0004), "head")

    with _enforcing_connection(database_0004) as connection:
        # Message 1 (customer 3 on ticket 2) notifies agent 1 and admin 2.
        _insert_notification(connection)
        _insert_notification(connection, recipient_user_id=2)
        # Message 2 (agent 1 on ticket 2) notifies customer 3, already read.
        _insert_notification(
            connection,
            recipient_user_id=3,
            actor_user_id=1,
            message_id=2,
            read_at="2026-11-26 10:30:00.000000",
        )
        # Message 3 (agent 1 on ticket 1) notifies customer 3.
        _insert_notification(
            connection,
            recipient_user_id=3,
            actor_user_id=1,
            ticket_id=1,
            message_id=3,
        )
        connection.commit()

        # Deleting a message deletes only its notifications.
        connection.execute("DELETE FROM ticket_messages WHERE id = 1")
        connection.commit()
        after_message_delete = connection.execute(
            "SELECT message_id FROM notifications ORDER BY message_id"
        ).fetchall()

        # Deleting a ticket deletes its messages and their notifications.
        connection.execute("DELETE FROM tickets WHERE id = 2")
        connection.commit()
        after_ticket_delete = connection.execute(
            "SELECT ticket_id, message_id FROM notifications"
        ).fetchall()
        users = connection.execute("SELECT count(*) FROM users").fetchone()

        # Users referenced by a notification cannot be deleted (no cascade
        # from notifications to users): user 3 is the remaining recipient,
        # user 1 the remaining actor.
        for user_id in (3, 1):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM users WHERE id = ?", (user_id,))

        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    assert after_message_delete == [(2,), (3,)]
    assert after_ticket_delete == [(1, 3)]
    assert users == (3,)


def test_0005_downgrade_restores_0004_and_upgrades_again(
    database_0004: Path,
) -> None:
    before = _phase_9_rows(database_0004)
    config = _config_for(database_0004)
    reference_0004 = build_reference_schema(MESSAGES_REVISION)

    command.upgrade(config, "head")

    with _enforcing_connection(database_0004) as connection:
        _insert_notification(connection)
        _insert_notification(
            connection, recipient_user_id=3, actor_user_id=1, message_id=2
        )
        connection.commit()

    command.downgrade(config, MESSAGES_REVISION)

    assert _current_revision(database_0004) == [MESSAGES_REVISION]
    assert "notifications" not in _table_names(database_0004)
    assert _phase_9_rows(database_0004) == before
    assert compare_schemas(reference_0004, _describe(database_0004)) == []

    with _connect(database_0004) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name LIKE 'ix_notifications_%'"
        ).fetchone() == (0,)

    # Re-upgrading works and starts with an empty notifications table.
    command.upgrade(config, "head")

    assert _current_revision(database_0004) == [HEAD_REVISION]
    assert _phase_9_rows(database_0004) == before
    assert _rows(database_0004, "notifications") == []
    assert compare_schemas(
        build_reference_schema("head"), _describe(database_0004)
    ) == []

    with _connect(database_0004) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_0005_runs_with_foreign_keys_enforced(database_0004: Path) -> None:
    """The migration does not rely on env.py turning enforcement off."""

    migration = _load_migration_0005()
    before = _phase_9_rows(database_0004)
    engine = create_engine(sqlite_url(database_0004))

    @event.listens_for(engine, "connect")
    def _enforce(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    try:
        with engine.begin() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1

            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()

        with _enforcing_connection(database_0004) as connection:
            _insert_notification(connection)
            connection.commit()

        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
    finally:
        engine.dispose()

    assert _phase_9_rows(database_0004) == before
    assert "notifications" not in _table_names(database_0004)

    with _connect(database_0004) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_0005_model_check_constraints_match_the_migration() -> None:
    """The model's named CHECK constraints are the migration's, verbatim.

    Alembic's autogenerate comparison ignores CHECK constraints, so the
    model and the migration texts are compared directly. The unique
    constraint and index names are compared too.
    """

    from sqlalchemy import CheckConstraint, UniqueConstraint

    from app.models import Notification

    migration = _load_migration_0005()
    source = MIGRATION_0005.read_text(encoding="utf-8")

    assert migration.revision == HEAD_REVISION
    assert migration.down_revision == MESSAGES_REVISION

    model_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in Notification.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert set(model_checks) == {
        "ck_notifications_notification_type",
        "ck_notifications_message_required",
        "ck_notifications_not_self",
    }

    for text in model_checks.values():
        assert f'"{text}"' in source

    [unique] = [
        constraint
        for constraint in Notification.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    ]

    assert unique.name == "uq_notifications_message_id_recipient_user_id"
    assert f'name="{unique.name}"' in source
    assert {
        migration.RECIPIENT_INDEX,
        migration.UNREAD_INDEX,
        migration.TICKET_INDEX,
    } == {index.name for index in Notification.__table__.indexes}
