"""Database errors in the API process never leak driver text (B).

Uses the real FastAPI application (its lifespan and exception handler) and
a real sqlite3 driver connection that fails on demand, with a fake Argon2
hash inside the driver's own error message. Every database is a temporary
file; resolveai.db is never used.
"""

import asyncio
import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.database import get_db
from app.db.safe_logging import POOL_LOGGER_NAMES, DatabaseLogSanitiser
from app.main import DATABASE_ERROR_DETAIL, app, database_error_handler


FAKE_HASH = "$argon2id$v=19$m=65536,t=3,p=4$ZmFrZS1zYWx0$ZmFrZS1oYXNoLXZhbHVl"
RAW_DRIVER_TEXT = "driver detail leaked"


def _driver_error(message: str, code: str) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError(message)
    error.sqlite_errorname = code

    return error


def _sanitisers(name: str) -> list[logging.Filter]:
    return [
        log_filter
        for log_filter in logging.getLogger(name).filters
        if isinstance(log_filter, DatabaseLogSanitiser)
    ]


def _handler_state() -> list[tuple]:
    """Handlers, levels, propagation of loggers the app must not alter."""

    names = (
        "",
        "sqlalchemy",
        "sqlalchemy.pool",
        "sqlalchemy.engine",
        "uvicorn",
        "uvicorn.error",
        "app",
        "app.main",
        *POOL_LOGGER_NAMES,
    )

    return [
        (
            name,
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
            logging.getLogger(name).disabled,
        )
        for name in names
    ]


def _assert_no_secrets(text_value: str) -> None:
    assert FAKE_HASH not in text_value
    assert "$argon2" not in text_value
    assert RAW_DRIVER_TEXT not in text_value
    assert "Exception during reset" not in text_value


@pytest.fixture
def failing_driver(tmp_path):
    """A file database whose real sqlite3 execute/rollback fail on demand."""

    class FailingCursor(sqlite3.Cursor):
        def execute(self, sql, parameters=()):
            if FailingConnection.fail_execute:
                raise _driver_error(
                    f"{RAW_DRIVER_TEXT}: {FAKE_HASH}",
                    "SQLITE_IOERR_READ",
                )

            return super().execute(sql, parameters)

    class FailingConnection(sqlite3.Connection):
        fail_execute = False
        fail_rollback = False

        def cursor(self, factory=FailingCursor):
            return super().cursor(factory)

        def rollback(self) -> None:
            if type(self).fail_rollback:
                raise _driver_error(
                    f"{RAW_DRIVER_TEXT} on rollback: {FAKE_HASH}",
                    "SQLITE_BUSY",
                )

            super().rollback()

    engine = create_engine(
        f"sqlite:///{tmp_path / 'api.db'}",
        connect_args={"factory": FailingConnection, "check_same_thread": False},
    )
    Base.metadata.create_all(engine)

    yield engine, FailingConnection

    FailingConnection.fail_execute = False
    FailingConnection.fail_rollback = False
    engine.dispose()


# ---------------------------------------------------------------------------
# Application-lifetime setup
# ---------------------------------------------------------------------------


def test_lifespan_attaches_sanitiser_and_removes_it_on_shutdown() -> None:
    state_before = _handler_state()

    assert not any(_sanitisers(name) for name in POOL_LOGGER_NAMES)

    for _restart in range(2):
        with TestClient(app):
            # Exactly one sanitiser per pool logger, even after a restart.
            for name in POOL_LOGGER_NAMES:
                assert len(_sanitisers(name)) == 1

            # No handler, level or propagation setting was touched.
            assert _handler_state() == state_before

        assert not any(_sanitisers(name) for name in POOL_LOGGER_NAMES)
        assert _handler_state() == state_before


# ---------------------------------------------------------------------------
# SQLAlchemy's own pool logging while the API runs
# ---------------------------------------------------------------------------


def test_pool_reset_failure_during_api_lifetime_is_sanitised(
    failing_driver,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, connection_class = failing_driver
    unrelated_logger = logging.getLogger("resolveai.tests.unrelated")

    # Make ordinary SQLAlchemy pool records visible as well (pytest
    # restores this level itself at teardown).
    caplog.set_level(logging.DEBUG, logger="sqlalchemy.pool")
    state_before = _handler_state()

    with TestClient(app):
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.commit()

            # Returning the (committed) connection resets it with a
            # rollback, which fails in the real driver. Nothing is raised:
            # SQLAlchemy's pool only logs it, with the driver error
            # attached, which is exactly the path that must be sanitised.
            connection_class.fail_rollback = True

        connection_class.fail_rollback = False

        try:
            raise ValueError("unrelated failure")
        except ValueError:
            unrelated_logger.exception("Unrelated application error")

    captured = capsys.readouterr()
    pool_records = [r for r in caplog.records if r.name.startswith("sqlalchemy.pool")]

    # The failure was reported, sanitised, through the existing handlers.
    errors = [r for r in pool_records if r.levelno >= logging.ERROR]

    assert [r.getMessage() for r in errors] == [
        "Database connection problem reported by SQLAlchemy (the database "
        "is locked [SQLITE_BUSY]); details hidden."
    ]
    assert all(r.exc_info is None for r in errors)

    # Ordinary SQLAlchemy records are untouched...
    assert any("checked out from pool" in r.getMessage() for r in pool_records)

    # ...and so are unrelated application records, traceback included.
    [unrelated] = [r for r in caplog.records if r.name == unrelated_logger.name]

    assert unrelated.getMessage() == "Unrelated application error"
    assert unrelated.exc_info is not None

    # No handler can render a secret or a traceback from the pool records.
    for record in pool_records:
        formatted = logging.Formatter().format(record)
        _assert_no_secrets(formatted)
        assert "Traceback" not in formatted

    for text_value in (caplog.text, captured.out, captured.err):
        _assert_no_secrets(text_value)

    # pytest's capture handlers and every other handler remain intact.
    assert _handler_state() == state_before


# ---------------------------------------------------------------------------
# A database error inside a request
# ---------------------------------------------------------------------------


def test_database_error_in_request_is_answered_and_logged_safely(
    failing_driver,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, connection_class = failing_driver
    state_before = _handler_state()

    def _failing_get_db():
        db = Session(engine)

        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _failing_get_db

    try:
        with TestClient(app) as client:
            connection_class.fail_execute = True
            connection_class.fail_rollback = True

            response = client.post(
                "/api/auth/login",
                data={
                    "username": "agent@example.test",
                    "password": "Some-Password-123",
                },
            )

        connection_class.fail_execute = False
        connection_class.fail_rollback = False
    finally:
        app.dependency_overrides.pop(get_db, None)

    captured = capsys.readouterr()

    assert response.status_code == 500
    assert response.json() == {"detail": DATABASE_ERROR_DETAIL}

    # The endpoint's query failed (primary). Closing the request's session
    # then failed to roll back; SQLAlchemy raised THAT error with the
    # original as context, and the handler still reports the original
    # first and the rollback failure separately.
    [record] = [r for r in caplog.records if r.name == "app.main"]

    assert record.getMessage() == (
        "Database error while handling a request (OperationalError: a disk "
        "I/O error occurred [SQLITE_IOERR_READ]). The rollback also failed "
        "(OperationalError: the database is locked [SQLITE_BUSY])."
    )
    assert record.exc_info is None

    for text_value in (response.text, caplog.text, captured.out, captured.err):
        _assert_no_secrets(text_value)
        assert "SELECT" not in text_value
        assert "agent@example.test" not in text_value  # a bound parameter
        assert "Traceback" not in text_value

    assert _handler_state() == state_before


def test_handler_reports_original_failure_when_rollback_also_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SQLAlchemy can raise its failed rollback in place of the original."""

    primary = OperationalError(
        "UPDATE tickets SET title=?",
        (FAKE_HASH,),
        _driver_error(f"{RAW_DRIVER_TEXT}: {FAKE_HASH}", "SQLITE_IOERR_WRITE"),
    )
    rollback = OperationalError(
        None,
        None,
        _driver_error(f"{RAW_DRIVER_TEXT} on rollback: {FAKE_HASH}", "SQLITE_BUSY"),
    )

    try:
        try:
            raise primary
        except OperationalError:
            raise rollback
    except OperationalError as raised:
        assert raised is rollback and raised.__context__ is primary
        response = asyncio.run(database_error_handler(None, raised))

    assert response.status_code == 500

    [record] = [r for r in caplog.records if r.name == "app.main"]

    assert record.getMessage() == (
        "Database error while handling a request (OperationalError: a disk "
        "I/O error occurred [SQLITE_IOERR_WRITE]). The rollback also failed "
        "(OperationalError: the database is locked [SQLITE_BUSY])."
    )
    _assert_no_secrets(caplog.text)
