"""Tests for the safe database-error descriptions used by the scripts.

No database is used: the errors are constructed directly.
"""

import logging
import sqlite3

import pytest
from sqlalchemy.exc import (
    IntegrityError,
    InvalidRequestError,
    OperationalError,
)

from app.db.errors import UNKNOWN_DATABASE_ERROR, describe_driver_error
from app.db.safe_logging import POOL_LOGGER_NAMES
from app.scripts.provisioning import (
    describe_database_error,
    rollback_after_failure,
    rollback_failure_note,
    sanitised_database_logging,
)


FAKE_HASH = "$argon2id$v=19$m=65536,t=3,p=4$ZmFrZS1zYWx0$ZmFrZS1oYXNoLXZhbHVl"
RAW_DRIVER_TEXT = "driver detail leaked"
RAW_MESSAGE = f"{RAW_DRIVER_TEXT}: {FAKE_HASH}"
SQL = "UPDATE users SET password_hash=? WHERE users.id = ?"


def _driver_error(
    error_class: type[sqlite3.Error],
    code: object,
) -> sqlite3.Error:
    """A driver error whose MESSAGE contains a fake password hash."""

    error = error_class(RAW_MESSAGE)

    if code is not None:
        error.sqlite_errorname = code

    return error


def _assert_safe(description: str) -> None:
    assert FAKE_HASH not in description
    assert "$argon2" not in description
    assert RAW_DRIVER_TEXT not in description
    assert "password_hash" not in description
    assert "UPDATE" not in description


@pytest.mark.parametrize(
    ("error_type", "driver_class", "code", "expected"),
    [
        (
            OperationalError,
            sqlite3.OperationalError,
            "SQLITE_BUSY",
            "OperationalError: the database is locked [SQLITE_BUSY]",
        ),
        (
            OperationalError,
            sqlite3.OperationalError,
            "SQLITE_LOCKED",
            "OperationalError: the database is locked [SQLITE_LOCKED]",
        ),
        (
            OperationalError,
            sqlite3.OperationalError,
            "SQLITE_BUSY_SNAPSHOT",
            "OperationalError: the database is locked [SQLITE_BUSY_SNAPSHOT]",
        ),
        (
            IntegrityError,
            sqlite3.IntegrityError,
            "SQLITE_CONSTRAINT_UNIQUE",
            "IntegrityError: a database constraint was violated "
            "[SQLITE_CONSTRAINT_UNIQUE]",
        ),
        (
            OperationalError,
            sqlite3.OperationalError,
            "SQLITE_IOERR_SHORT_READ",
            "OperationalError: a disk I/O error occurred "
            "[SQLITE_IOERR_SHORT_READ]",
        ),
        (
            OperationalError,
            sqlite3.OperationalError,
            "SQLITE_FULL",
            "OperationalError: the disk or database is full [SQLITE_FULL]",
        ),
    ],
)
def test_known_codes_map_to_fixed_descriptions(
    error_type: type[OperationalError],
    driver_class: type[sqlite3.Error],
    code: str,
    expected: str,
) -> None:
    error = error_type(SQL, (FAKE_HASH, 1), _driver_error(driver_class, code))

    description = describe_database_error(error)

    assert description == expected
    _assert_safe(description)


@pytest.mark.parametrize(
    "code",
    [
        None,                          # hand-built error with no code
        "SQLITE_SOMETHING_NEW",        # well-formed but not allow-listed
        f"SQLITE_BUSY {FAKE_HASH}",    # malformed: must not be echoed
        "sqlite_busy",                 # wrong case
        5,                             # not a string
    ],
    ids=["missing", "unrecognised", "malformed", "lowercase", "not-a-string"],
)
def test_unknown_or_malformed_codes_use_the_generic_description(
    code: object,
) -> None:
    error = OperationalError(
        SQL,
        (FAKE_HASH,),
        _driver_error(sqlite3.OperationalError, code),
    )

    description = describe_database_error(error)

    assert description == f"OperationalError: {UNKNOWN_DATABASE_ERROR}"
    _assert_safe(description)


def test_non_driver_sqlalchemy_error_reveals_only_its_class() -> None:
    error = InvalidRequestError(f"something about {RAW_MESSAGE}")

    description = describe_database_error(error)

    assert description == f"InvalidRequestError: {UNKNOWN_DATABASE_ERROR}"
    _assert_safe(description)


def test_describe_driver_error_never_uses_the_message() -> None:
    error = _driver_error(sqlite3.DatabaseError, "SQLITE_NOTADB")

    description = describe_driver_error(error)

    assert description == "the file is not a SQLite database [SQLITE_NOTADB]"
    _assert_safe(description)


def test_real_sqlite_error_is_described_by_its_code() -> None:
    connection = sqlite3.connect(":memory:")

    try:
        connection.execute("CREATE TABLE t (value TEXT NOT NULL)")

        with pytest.raises(sqlite3.IntegrityError) as error_info:
            connection.execute("INSERT INTO t VALUES (NULL)")
    finally:
        connection.close()

    description = describe_driver_error(error_info.value)

    assert description == (
        "a database constraint was violated [SQLITE_CONSTRAINT_NOTNULL]"
    )
    assert "t.value" not in description


# ---------------------------------------------------------------------------
# Strict code validation: exact str type, full match (A6)
# ---------------------------------------------------------------------------


class _LeakyCode(str):
    """A str subclass whose formatting or printing would expose a secret."""

    def __format__(self, format_spec: str) -> str:
        return f"SQLITE_BUSY {FAKE_HASH}"

    def __str__(self) -> str:
        return f"SQLITE_BUSY {FAKE_HASH}"


@pytest.mark.parametrize(
    "value",
    ["SQLITE_BUSY", "SQLITE_CONSTRAINT_UNIQUE"],
)
def test_str_subclass_code_is_rejected(value: str) -> None:
    code = _LeakyCode(value)
    assert isinstance(code, str)  # the old isinstance() check accepted it

    error = OperationalError(
        SQL,
        None,
        _driver_error(sqlite3.OperationalError, code),
    )

    description = describe_database_error(error)

    assert description == f"OperationalError: {UNKNOWN_DATABASE_ERROR}"
    assert describe_driver_error(error.orig) == UNKNOWN_DATABASE_ERROR
    _assert_safe(description)


@pytest.mark.parametrize(
    "code",
    [
        "SQLITE_BUSY\n",
        "SQLITE_CONSTRAINT_UNIQUE\n",  # previously echoed, newline included
        "SQLITE_BUSY\r\n",
        "\nSQLITE_BUSY",
        "SQLITE_BUSY ",
    ],
    ids=["busy-newline", "extended-newline", "crlf", "leading-newline", "space"],
)
def test_code_with_extra_characters_is_rejected(code: str) -> None:
    error = OperationalError(
        SQL,
        None,
        _driver_error(sqlite3.OperationalError, code),
    )

    description = describe_database_error(error)

    assert description == f"OperationalError: {UNKNOWN_DATABASE_ERROR}"
    assert "\n" not in description
    assert "\r" not in description
    _assert_safe(description)


# ---------------------------------------------------------------------------
# Guarded rollback (A8)
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal stand-in exposing rollback(), which may raise."""

    def __init__(self, rollback_error: BaseException | None = None) -> None:
        self.rollback_error = rollback_error
        self.rollbacks = 0

    def rollback(self) -> None:
        self.rollbacks += 1

        if self.rollback_error is not None:
            raise self.rollback_error


def _secret_database_error(code: str = "SQLITE_BUSY") -> OperationalError:
    """SQL, parameters AND driver message all carry the fake hash."""

    return OperationalError(
        SQL,
        (FAKE_HASH, 1),
        _driver_error(sqlite3.OperationalError, code),
    )


def test_rollback_after_failure_returns_none_on_success() -> None:
    session = _FakeSession()

    assert rollback_after_failure(session) is None
    assert session.rollbacks == 1
    assert rollback_failure_note(None) == ""


def test_failed_rollback_is_described_safely_and_not_raised() -> None:
    session = _FakeSession(_secret_database_error("SQLITE_IOERR"))

    try:
        raise _secret_database_error()
    except OperationalError:
        failure = rollback_after_failure(session)

    assert failure == "OperationalError: a disk I/O error occurred [SQLITE_IOERR]"

    note = rollback_failure_note(failure)

    assert note == f" The rollback also failed ({failure})."
    _assert_safe(note)


def test_non_database_rollback_error_is_reported_with_fixed_text() -> None:
    """A non-database rollback exception is captured, never raised or shown."""

    secret = f"rollback bug {FAKE_HASH} SELECT * FROM secrets"
    session = _FakeSession(RuntimeError(secret))

    try:
        raise _secret_database_error()
    except OperationalError:
        failure = rollback_after_failure(session)

    assert failure == (
        "an unexpected non-database error occurred; details hidden"
    )

    note = rollback_failure_note(failure)

    assert "RuntimeError" not in note
    assert "rollback bug" not in note
    assert "SELECT" not in note
    _assert_safe(note)


# ---------------------------------------------------------------------------
# SQLAlchemy's own log records while a script runs (A8)
# ---------------------------------------------------------------------------


def _logging_state() -> list[tuple]:
    """Handlers, levels, propagation and filters of the relevant loggers."""

    names = ("", "sqlalchemy", "sqlalchemy.pool", *POOL_LOGGER_NAMES)

    return [
        (
            name,
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
            list(logging.getLogger(name).filters),
        )
        for name in names
    ]


def test_sqlalchemy_log_records_are_sanitised_during_a_script(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    pool_logger = logging.getLogger("sqlalchemy.pool.impl.QueuePool")
    state_before = _logging_state()

    driver_error = _driver_error(sqlite3.OperationalError, "SQLITE_IOERR")

    @sanitised_database_logging
    def _script() -> None:
        try:
            raise driver_error
        except sqlite3.OperationalError:
            # What SQLAlchemy's pool logs when a rollback on return fails.
            pool_logger.error("Exception during reset or similar", exc_info=True)

    _script()

    captured = capsys.readouterr()

    # The record reached the existing handlers (pytest's), sanitised.
    [record] = [r for r in caplog.records if r.name == pool_logger.name]

    assert record.getMessage() == (
        "Database connection problem reported by SQLAlchemy "
        "(a disk I/O error occurred [SQLITE_IOERR]); details hidden."
    )
    assert record.exc_info is None

    # Any handler's formatting of it is safe, including a traceback-capable
    # default Formatter (as used by Python's last-resort handler).
    formatted = logging.Formatter().format(record)

    for text in (formatted, caplog.text, captured.out, captured.err):
        _assert_safe(text)
        assert "Traceback" not in text
        assert "Exception during reset" not in text

    # The filter is removed again: no logging state persists.
    assert _logging_state() == state_before


def test_sanitised_logging_filter_is_removed_when_the_script_fails() -> None:
    state_before = _logging_state()

    @sanitised_database_logging
    def _script() -> None:
        raise SystemExit(1)

    with pytest.raises(SystemExit):
        _script()

    assert _logging_state() == state_before
