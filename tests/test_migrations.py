"""Alembic migration tests.

Each test uses its own SQLite file under pytest's ``tmp_path``; the
application database (resolveai.db) is never touched.
"""

import hashlib
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url

from app.db import database as app_database
from app.db.base import Base
from app.db.check_baseline import (
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


HEAD_REVISION = "0002"

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
    assert script.get_revision(HEAD_REVISION).down_revision == BASELINE_REVISION


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
        "revoked_tokens",
    }
    assert _current_revision(database_file) == [HEAD_REVISION]


def test_migrated_schema_matches_sqlalchemy_models(
    database_file: Path,
    alembic_config: Config,
) -> None:
    """Models and migrations must never drift apart."""

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
    assert _rows(legacy_database, "tickets") == tickets_before
    assert _rows(legacy_database, "revoked_tokens") == revoked_before

    with _connect(legacy_database) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_check_baseline_cli_stamps_matching_database(
    legacy_database: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = check_baseline_main(
        ["--database", str(legacy_database), "--stamp"]
    )

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
    assert "revoked_tokens, tickets, users" in message
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

    with pytest.raises(RuntimeError, match="already contains tables: notes"):
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

    assert "CREATE TABLE users" in capsys.readouterr().out
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
            "unexpected column token_version",
        ),
        (
            "unexpected table",
            lambda sql: sql + ["CREATE TABLE notes (id INTEGER PRIMARY KEY)"],
            "unexpected table: notes",
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
    # The revision says head, but migration 0002 was never actually applied.
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
            "is at revision 0001, but this code requires revision 0002",
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
            "records revision 9999, which this code does not know",
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
            "records revision 0002 more than once (0002, 0002)",
            False,
            id="duplicate-head-rows",
        ),
        pytest.param(
            _two_known_revisions,
            InvalidRevisionSetError,
            "records several revisions (0001, 0002)",
            False,
            id="two-known-revisions-linear-history",
        ),
        pytest.param(
            _foreign_database_at_head,
            SchemaMismatchError,
            "unexpected table: invoices",
            False,
            id="unrelated-database-at-0002",
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
            "unexpected table: notes",
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

    with pytest.raises(DatabaseUnavailableError, match="Could not read"):
        ensure_database_at_head(garbage)

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
    assert set(reference) == {"users", "tickets", "revoked_tokens"}
    assert reference["users"]["columns"]["token_version"] == (
        "INTEGER",
        False,
        "0",
    )
