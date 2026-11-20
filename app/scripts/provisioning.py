"""Helpers shared by the local account-provisioning scripts."""

import functools
import sys
from collections.abc import Callable
from pathlib import Path
from typing import ParamSpec, TypeVar

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.errors import (
    describe_database_error,
    describe_rollback_failure,
    record_rollback_failure,
    rollback_failure_of,
    rollback_without_raising,
    separate_rollback_failure,
)
from app.db.migrations import DatabaseNotAtHeadError, ensure_database_at_head
from app.db.safe_logging import sanitised_database_logs


__all__ = [
    "database_failure_report",
    "database_is_ready",
    "describe_database_error",
    "rollback_after_failure",
    "rollback_failure_note",
    "sanitised_database_logging",
]

_P = ParamSpec("_P")
_R = TypeVar("_R")


def rollback_after_failure(
    db: Session,
    failure: BaseException | None = None,
) -> str | None:
    """Roll back after ``failure`` without ever raising a database error.

    Returns ``None`` if no rollback failed, otherwise a safe description of
    the rollback failure. A rollback failure already recorded on
    ``failure`` (by the user service, or SQLAlchemy's own rollback) takes
    precedence over this rollback's, so it is reported once. Nothing was
    committed in either case; the caller reports the outcome.
    """

    own_failure = rollback_without_raising(db)
    recorded = rollback_failure_of(failure) if failure is not None else None
    rollback_failure = recorded or own_failure

    if rollback_failure is None:
        return None

    return describe_rollback_failure(rollback_failure)


def database_failure_report(
    db: Session,
    caught: BaseException,
) -> tuple[str, str | None]:
    """Roll back after a database failure and describe it safely.

    Returns (primary diagnostic, rollback diagnostic or None). The primary
    is the original operation's failure, even if SQLAlchemy raised its own
    failed rollback in its place.
    """

    primary, internal_rollback_failure = separate_rollback_failure(caught)
    record_rollback_failure(primary, internal_rollback_failure)

    return (
        describe_database_error(primary),
        rollback_after_failure(db, primary),
    )


def rollback_failure_note(rollback_failure: str | None) -> str:
    """Fixed text to append to a message when the rollback also failed."""

    if rollback_failure is None:
        return ""

    return f" The rollback also failed ({rollback_failure})."


def sanitised_database_logging(function: Callable[_P, _R]) -> Callable[_P, _R]:
    """Run ``function`` with SQLAlchemy's pool log records sanitised.

    Uses the same logger-level filter as the API (``app.db.safe_logging``),
    attached for the duration of the call and removed afterwards. Records
    still go to whatever handlers exist (in a plain CLI, Python's
    last-resort handler prints the sanitised one-line message).
    """

    @functools.wraps(function)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with sanitised_database_logs():
            return function(*args, **kwargs)

    return wrapper


def database_is_ready(engine: Engine, action: str) -> bool:
    """Check, read-only, that ``engine``'s database is usable at Alembic head.

    On failure, prints "Cannot <action>." and the reason to stderr and
    returns False. Never runs or stamps a migration: migrating is an
    explicit operator step (see docs/database-migrations.md).
    """

    try:
        ensure_database_at_head(Path(engine.url.database))
    except DatabaseNotAtHeadError as error:
        print(f"Cannot {action}. {error}", file=sys.stderr)
        return False

    return True
