"""Safe, fixed-text descriptions of database driver errors.

Driver messages, SQL and bound parameters can contain passwords, password
hashes or other user data, so they are never shown. A description is
chosen only from SQLite's structured result code (``sqlite_errorname``)
and an allow-list of fixed phrases.

This module imports nothing from the application, so the database layer,
the API and the scripts can all use it without circular imports.
"""

import re
from typing import TYPE_CHECKING

from sqlalchemy.exc import DBAPIError, SQLAlchemyError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


# Fixed, operator-facing descriptions keyed by SQLite primary result code.
# Extended codes (e.g. SQLITE_BUSY_SNAPSHOT, SQLITE_CONSTRAINT_UNIQUE) are
# looked up by their primary code.
_SQLITE_DESCRIPTIONS = {
    "SQLITE_BUSY": "the database is locked",
    "SQLITE_LOCKED": "the database is locked",
    "SQLITE_CONSTRAINT": "a database constraint was violated",
    "SQLITE_READONLY": "the database is read-only",
    "SQLITE_FULL": "the disk or database is full",
    "SQLITE_IOERR": "a disk I/O error occurred",
    "SQLITE_CORRUPT": "the database file is corrupt",
    "SQLITE_NOTADB": "the file is not a SQLite database",
    "SQLITE_CANTOPEN": "the database file could not be opened",
    "SQLITE_PERM": "permission to access the database was denied",
    "SQLITE_NOMEM": "the database ran out of memory",
    "SQLITE_INTERRUPT": "the database operation was interrupted",
    "SQLITE_SCHEMA": "the database schema changed during the operation",
    "SQLITE_TOOBIG": "a value was too large for the database",
    "SQLITE_MISMATCH": "a value had the wrong data type",
}

UNKNOWN_DATABASE_ERROR = "the database operation failed"

# SQLite result-code names are upper-case identifiers such as SQLITE_BUSY
# or SQLITE_IOERR_SHORT_READ. Always applied with fullmatch(), so nothing
# (not even a trailing newline) may follow the name.
_SQLITE_CODE_NAME = re.compile(r"SQLITE_[A-Z]+(?:_[A-Z]+)*")


def describe_driver_error(driver_error: object) -> str:
    """Describe a DBAPI (driver) exception using fixed text only.

    Returns "<fixed description> [<SQLITE_CODE>]" for a recognised SQLite
    result code, otherwise "the database operation failed". The driver's
    message and any other attribute values are never used.

    The code is echoed only if it is exactly a built-in ``str`` (a ``str``
    subclass could change what formatting produces) that fully matches the
    result-code syntax and whose primary code is allow-listed.
    """

    code = getattr(driver_error, "sqlite_errorname", None)

    if type(code) is str and _SQLITE_CODE_NAME.fullmatch(code):
        primary = "_".join(code.split("_")[:2])
        description = _SQLITE_DESCRIPTIONS.get(primary)

        if description is not None:
            return f"{description} [{code}]"

    return UNKNOWN_DATABASE_ERROR


def describe_exception(error: BaseException) -> str:
    """Fixed-text description of any exception, without its class name.

    SQLAlchemy driver errors and raw driver errors are described by their
    SQLite result code; anything else gets the generic description.
    """

    if isinstance(error, DBAPIError):
        return describe_driver_error(error.orig)

    if isinstance(error, SQLAlchemyError):
        return UNKNOWN_DATABASE_ERROR

    return describe_driver_error(error)


def describe_database_error(error: BaseException) -> str:
    """A safe one-line diagnostic for a database error.

    Built only from fixed text: the exception class name plus an
    allow-listed description chosen by SQLite's structured result code,
    for example "OperationalError: the database is locked [SQLITE_BUSY]".
    It NEVER includes ``str(error)``, ``str(error.orig)``, any driver
    message, SQL, bound parameters or other exception attribute values,
    because any of those may contain passwords, hashes or user data.
    """

    return f"{type(error).__name__}: {describe_exception(error)}"


# ---------------------------------------------------------------------------
# Rollback failures: keep the original failure primary
# ---------------------------------------------------------------------------


_ROLLBACK_FAILURE_ATTRIBUTE = "rollback_failure"


def separate_rollback_failure(
    error: BaseException,
) -> tuple[BaseException, SQLAlchemyError | None]:
    """Split ``error`` into (primary failure, rollback failure or None).

    When a flush fails, SQLAlchemy rolls the transaction back itself. If
    that internal rollback also fails, SQLAlchemy raises the ROLLBACK's
    error, and the original failure is only its ``__context__``. Such an
    error is a ``DBAPIError`` with no SQL statement (a rollback runs none)
    whose context is another SQLAlchemy error. For it, the original is
    returned as primary and the raised error as the rollback failure; any
    other error is its own primary failure.
    """

    context = error.__context__

    if (
        isinstance(error, DBAPIError)
        and error.statement is None
        and isinstance(context, SQLAlchemyError)
    ):
        return context, error

    return error, None


UNEXPECTED_ROLLBACK_FAILURE = (
    "an unexpected non-database error occurred; details hidden"
)


def rollback_failure_of(error: BaseException) -> Exception | None:
    """The rollback failure recorded on ``error``, if any."""

    failure = getattr(error, _ROLLBACK_FAILURE_ATTRIBUTE, None)

    return failure if isinstance(failure, Exception) else None


def record_rollback_failure(
    error: BaseException,
    rollback_failure: Exception | None,
) -> None:
    """Attach a secondary rollback failure to the primary ``error``.

    The first recorded failure is kept; ``None`` records nothing. An
    exception type that cannot take attributes simply goes unannotated.
    """

    if rollback_failure is None or rollback_failure_of(error) is not None:
        return

    try:
        setattr(error, _ROLLBACK_FAILURE_ATTRIBUTE, rollback_failure)
    except AttributeError:
        pass


def describe_rollback_failure(rollback_failure: BaseException) -> str:
    """Safe description of a failed rollback.

    A database error gets the allow-listed diagnostic. Any other exception
    gets a fixed message: its class and message are never shown, since
    they are not known to be safe.
    """

    if isinstance(rollback_failure, SQLAlchemyError):
        return describe_database_error(rollback_failure)

    return UNEXPECTED_ROLLBACK_FAILURE


def rollback_without_raising(db: "Session") -> Exception | None:
    """Roll back; return the rollback's exception instead of raising it.

    Raising it inside an ``except`` block would replace the failure being
    handled and chain that failure (with its SQL and driver message) into
    any printed traceback. Any ``Exception`` from ``rollback()`` (a
    database error or not) is therefore captured and returned, to be
    reported as a secondary failure with ``describe_rollback_failure``.
    ``BaseException``s such as ``KeyboardInterrupt`` are not intercepted.
    """

    try:
        db.rollback()
    except Exception as rollback_error:
        return rollback_error

    return None


def roll_back_keeping_primary(
    db: "Session",
    caught: BaseException,
    *,
    roll_back: bool = True,
) -> BaseException:
    """Roll back after ``caught`` and return the failure to re-raise.

    The returned (primary) failure is the ORIGINAL operation's failure,
    even when SQLAlchemy raised its own failed rollback in its place. A
    rollback failure (one already recorded, SQLAlchemy's internal one, or
    the one from rolling back here when ``roll_back`` is true) is recorded
    on it as the secondary failure. Callers re-raise the result::

        except Exception as caught:
            primary = roll_back_keeping_primary(db, caught)
            if primary is caught:
                raise
            raise primary from None
    """

    primary, internal_failure = separate_rollback_failure(caught)
    own_failure = rollback_without_raising(db) if roll_back else None

    record_rollback_failure(
        primary,
        rollback_failure_of(primary) or internal_failure or own_failure,
    )

    return primary
