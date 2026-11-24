"""Shared serialised-transaction helper (app.db.locking).

The helper is exercised directly here: the statement it issues, the lock
it holds, that it leaves the transaction to its caller, and how it refuses
a transaction that has already written. The end-to-end protections it
provides to the admin API (concurrent changes, lock time-outs, rollback)
are covered by tests/test_admin_users_api.py and are not repeated.

Every database is a temporary file or in memory; resolveai.db is never
used.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from app.db import locking
from app.db.base import Base
from app.db.locking import TransactionAlreadyStartedError, begin_serialised_change
from app.services import user_service
from app.services.user_service import UserServiceError


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sqlite_file(tmp_path):
    """A temporary SQLite file with the application schema."""

    path = tmp_path / "locking.db"
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)

    yield engine, path

    engine.dispose()


def _statements(engine) -> list[str]:
    """Record every SQL statement the engine sends to the driver."""

    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    return statements


def _write_lock_is_held(path: Path) -> bool:
    """Whether another connection is refused SQLite's write lock."""

    other = sqlite3.connect(path, timeout=0)

    try:
        other.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        return True
    finally:
        other.close()

    return False


def _users(path: Path) -> list[str]:
    """Names stored in the file, read through an independent connection."""

    reader = sqlite3.connect(path)

    try:
        return [row[0] for row in reader.execute("SELECT name FROM users")]
    finally:
        reader.close()


def _insert_user(db: Session, name: str) -> None:
    db.execute(
        text(
            "INSERT INTO users (name, email, password_hash, role, is_active, "
            "token_version, created_at) VALUES (:name, :email, 'unused', "
            "'agent', 1, 0, CURRENT_TIMESTAMP)"
        ),
        {"name": name, "email": f"{name}@example.test"},
    )


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


def test_sqlite_issues_begin_immediate_and_holds_the_write_lock(sqlite_file) -> None:
    engine, path = sqlite_file
    statements = _statements(engine)

    with Session(engine) as db:
        assert not _write_lock_is_held(path)

        begin_serialised_change(db)

        assert statements == ["BEGIN IMMEDIATE"]
        assert db.connection().connection.driver_connection.in_transaction
        # Taken before anything is read: another writer is refused now.
        assert _write_lock_is_held(path)

        db.rollback()

    assert not _write_lock_is_held(path)


def test_helper_leaves_commit_and_rollback_to_the_caller(sqlite_file) -> None:
    engine, path = sqlite_file

    with Session(engine) as db:
        begin_serialised_change(db)
        _insert_user(db, "rolled-back")

        # Nothing was committed by the helper.
        assert _users(path) == []

        db.rollback()

    assert _users(path) == []
    assert not _write_lock_is_held(path)

    with Session(engine) as db:
        begin_serialised_change(db)
        _insert_user(db, "committed")
        db.commit()

    assert _users(path) == ["committed"]
    assert not _write_lock_is_held(path)


def test_transaction_that_already_wrote_is_refused_and_left_untouched(
    sqlite_file,
) -> None:
    engine, path = sqlite_file

    with Session(engine) as db:
        _insert_user(db, "pending")
        statements = _statements(engine)

        with pytest.raises(TransactionAlreadyStartedError) as raised:
            begin_serialised_change(db)

        assert str(raised.value) == (
            "A serialised change must start a new transaction."
        )
        # No BEGIN was attempted, and the caller's transaction (with its
        # uncommitted write) is neither committed nor rolled back.
        assert statements == []
        assert db.connection().connection.driver_connection.in_transaction
        assert db.execute(text("SELECT name FROM users")).scalars().all() == [
            "pending"
        ]
        assert _users(path) == []

        db.rollback()

    assert _users(path) == []


def test_reads_alone_do_not_count_as_a_started_transaction(sqlite_file) -> None:
    """Authorisation reads the user first; the change must still lock."""

    engine, path = sqlite_file

    with Session(engine) as db:
        db.execute(text("SELECT count(*) FROM users")).scalar_one()

        begin_serialised_change(db)

        assert _write_lock_is_held(path)

        db.rollback()


# ---------------------------------------------------------------------------
# Other dialects
# ---------------------------------------------------------------------------


class _FakeConnection:
    def __init__(self, dialect_name: str) -> None:
        self.dialect = type("Dialect", (), {"name": dialect_name})()
        self.executed: list[str] = []

    def exec_driver_sql(self, statement: str) -> None:
        self.executed.append(statement)

    @property
    def connection(self):  # pragma: no cover - must not be reached
        raise AssertionError("the driver connection must not be inspected")


class _FakeSession:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def connection(self) -> _FakeConnection:
        return self._connection


@pytest.mark.parametrize("dialect_name", ["postgresql", "mysql"])
def test_other_dialects_execute_nothing(dialect_name: str) -> None:
    """Row locks (SELECT ... FOR UPDATE) are taken by the caller's reads."""

    connection = _FakeConnection(dialect_name)

    begin_serialised_change(_FakeSession(connection))

    assert connection.executed == []


# ---------------------------------------------------------------------------
# Compatibility of the user service
# ---------------------------------------------------------------------------


def test_user_service_error_keeps_its_hierarchy() -> None:
    error_type = user_service.TransactionAlreadyStartedError

    assert issubclass(error_type, UserServiceError)
    assert issubclass(error_type, RuntimeError)
    assert issubclass(error_type, locking.TransactionAlreadyStartedError)
    assert issubclass(locking.TransactionAlreadyStartedError, RuntimeError)


def test_user_service_wrapper_raises_its_own_error_and_message(
    sqlite_file,
) -> None:
    engine, _path = sqlite_file

    with Session(engine) as db:
        _insert_user(db, "pending")

        with pytest.raises(user_service.TransactionAlreadyStartedError) as raised:
            user_service._begin_serialised_change(db)

        assert str(raised.value) == (
            "A role or status change must start a new transaction."
        )
        # Not chained to the shared helper's error.
        assert raised.value.__cause__ is None
        assert raised.value.__suppress_context__ is True

        db.rollback()


def test_user_service_wrapper_delegates_to_the_shared_helper(
    sqlite_file,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, path = sqlite_file
    calls: list[Session] = []
    original = locking.begin_serialised_change

    def recording(db: Session) -> None:
        calls.append(db)
        original(db)

    monkeypatch.setattr(locking, "begin_serialised_change", recording)

    with Session(engine) as db:
        user_service._begin_serialised_change(db)

        assert calls == [db]
        assert _write_lock_is_held(path)

        db.rollback()


def test_locking_module_imports_without_application_modules() -> None:
    """A fresh interpreter can import it, and it pulls in no services."""

    script = (
        "import sys\n"
        "import app.db.locking\n"
        "loaded = sorted(m for m in sys.modules "
        "if m == 'app' or m.startswith('app.'))\n"
        "print(loaded)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.strip() == "['app', 'app.db', 'app.db.locking']"


def test_both_import_paths_work() -> None:
    from app.db.locking import TransactionAlreadyStartedError as shared
    from app.services.user_service import (
        TransactionAlreadyStartedError as from_user_service,
    )

    assert shared is locking.TransactionAlreadyStartedError
    assert issubclass(from_user_service, shared)
