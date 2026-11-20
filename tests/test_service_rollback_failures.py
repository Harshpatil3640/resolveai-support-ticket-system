"""Rollback failures in the API services never replace the original (FIX 3).

The original operation fails with a SQLAlchemy error whose driver message
carries one fake secret; the rollback then raises a NON-database exception
carrying another. The original must stay the exception raised, and the
rollback failure must only ever be described with fixed text. Uses the
in-memory SQLite session from conftest.py.
"""

import logging
import sqlite3
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.db.errors import (
    UNEXPECTED_ROLLBACK_FAILURE,
    describe_database_error,
    describe_rollback_failure,
    rollback_failure_of,
)
from app.models import User
from app.services.auth_service import (
    UserNotFoundError,
    invalidate_user_sessions,
    revoke_access_token,
)
from app.services.ticket_service import create_ticket


OPERATION_SECRET = "$argon2id$v=19$m=65536,t=3,p=4$c2VjcmV0QQ$b3BlcmF0aW9uLUE"
ROLLBACK_SECRET = "$argon2id$v=19$m=65536,t=3,p=4$c2VjcmV0Qg$cm9sbGJhY2stQg"
OPERATION_DRIVER_TEXT = "operation driver detail"
ROLLBACK_TEXT = "rollback bug"

FORBIDDEN = (
    OPERATION_SECRET,
    ROLLBACK_SECRET,
    "$argon2",
    OPERATION_DRIVER_TEXT,
    ROLLBACK_TEXT,
    "INSERT INTO",
    "SELECT",
    "RuntimeError",
    "Traceback",
)


def _operation_error(statement: str) -> OperationalError:
    """The original failure: SQL, parameters and driver message carry a secret."""

    driver_error = sqlite3.OperationalError(
        f"{OPERATION_DRIVER_TEXT}: {OPERATION_SECRET}"
    )
    driver_error.sqlite_errorname = "SQLITE_IOERR_WRITE"

    return OperationalError(statement, (OPERATION_SECRET,), driver_error)


def _raiser(error: BaseException) -> Callable[..., None]:
    def _raise(*_args, **_kwargs) -> None:
        raise error

    return _raise


def _rollback_bug() -> RuntimeError:
    return RuntimeError(f"{ROLLBACK_TEXT}: {ROLLBACK_SECRET} SELECT * FROM vault")


def _break_session(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    commit_error: BaseException | None,
) -> None:
    if commit_error is not None:
        monkeypatch.setattr(db, "commit", _raiser(commit_error))

    monkeypatch.setattr(db, "rollback", _raiser(_rollback_bug()))


def _assert_safe(text_value: str) -> None:
    for fragment in FORBIDDEN:
        assert fragment not in text_value, f"exposed {fragment!r}"


def _assert_primary_with_safe_secondary(
    raised: BaseException,
    original: BaseException,
) -> None:
    assert raised is original

    secondary = rollback_failure_of(raised)

    assert isinstance(secondary, RuntimeError)
    assert describe_rollback_failure(secondary) == UNEXPECTED_ROLLBACK_FAILURE

    _assert_safe(describe_rollback_failure(secondary))

    if isinstance(raised, OperationalError):
        _assert_safe(describe_database_error(raised))


# ---------------------------------------------------------------------------
# ticket_service
# ---------------------------------------------------------------------------


def test_ticket_commit_failure_stays_primary_when_rollback_raises(
    db_session: Session,
    agent_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit_error = _operation_error("INSERT INTO tickets (title) VALUES (?)")
    _break_session(db_session, monkeypatch, commit_error)

    with pytest.raises(OperationalError) as error_info:
        create_ticket(
            db_session,
            creator=agent_user,
            title="Printer jam",
            description="The printer is jammed.",
        )

    _assert_primary_with_safe_secondary(error_info.value, commit_error)


# ---------------------------------------------------------------------------
# auth_service
# ---------------------------------------------------------------------------


def test_invalidate_sessions_commit_failure_stays_primary(
    db_session: Session,
    agent_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit_error = _operation_error("UPDATE users SET token_version=?")
    _break_session(db_session, monkeypatch, commit_error)

    with pytest.raises(OperationalError) as error_info:
        invalidate_user_sessions(db_session, agent_user)

    _assert_primary_with_safe_secondary(error_info.value, commit_error)


def test_invalidate_sessions_missing_user_stays_not_found(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_user = User(
        id=999_999,
        name="Ghost",
        email="ghost@example.test",
        password_hash="unused",
    )
    _break_session(db_session, monkeypatch, commit_error=None)

    with pytest.raises(UserNotFoundError) as error_info:
        invalidate_user_sessions(db_session, missing_user)

    assert isinstance(rollback_failure_of(error_info.value), RuntimeError)
    assert describe_rollback_failure(
        rollback_failure_of(error_info.value)
    ) == UNEXPECTED_ROLLBACK_FAILURE


def test_revoke_commit_failure_stays_primary(
    db_session: Session,
    agent_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = create_access_token(subject=str(agent_user.id), token_version=0)
    commit_error = _operation_error("INSERT INTO revoked_tokens (jti) VALUES (?)")
    _break_session(db_session, monkeypatch, commit_error)

    with pytest.raises(OperationalError) as error_info:
        revoke_access_token(db_session, token)

    _assert_primary_with_safe_secondary(error_info.value, commit_error)


def _integrity_error() -> IntegrityError:
    driver_error = sqlite3.IntegrityError(
        f"{OPERATION_DRIVER_TEXT}: {OPERATION_SECRET}"
    )
    driver_error.sqlite_errorname = "SQLITE_CONSTRAINT_PRIMARYKEY"

    return IntegrityError(
        "INSERT INTO revoked_tokens (jti) VALUES (?)",
        (OPERATION_SECRET,),
        driver_error,
    )


def test_revoke_concurrent_integrity_error_still_returns_a_result(
    db_session: Session,
    agent_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing behaviour: after a successful rollback, re-check and return."""

    token = create_access_token(subject=str(agent_user.id), token_version=0)
    monkeypatch.setattr(db_session, "commit", _raiser(_integrity_error()))

    # The simulated concurrent row does not exist, so the re-check is False.
    assert revoke_access_token(db_session, token) is False


def test_revoke_integrity_error_stays_primary_when_rollback_raises(
    db_session: Session,
    agent_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = create_access_token(subject=str(agent_user.id), token_version=0)
    integrity_error = _integrity_error()
    _break_session(db_session, monkeypatch, integrity_error)

    with pytest.raises(IntegrityError) as error_info:
        revoke_access_token(db_session, token)

    _assert_primary_with_safe_secondary(error_info.value, integrity_error)


# ---------------------------------------------------------------------------
# What actually reaches the API's output
# ---------------------------------------------------------------------------


EXPECTED_API_LOG = (
    "Database error while handling a request (OperationalError: a disk I/O "
    "error occurred [SQLITE_IOERR_WRITE]). The rollback also failed (an "
    "unexpected non-database error occurred; details hidden)."
)


def _assert_api_output_safe(
    response,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured = capsys.readouterr()

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}

    [record] = [r for r in caplog.records if r.name == "app.main"]

    assert record.getMessage() == EXPECTED_API_LOG
    assert record.exc_info is None

    for text_value in (response.text, caplog.text, captured.out, captured.err):
        _assert_safe(text_value)


def test_ticket_endpoint_output_is_safe_when_commit_and_rollback_fail(
    client: TestClient,
    db_session: Session,
    agent_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "app.api.routes.tickets.predict_category",
        lambda description: ("PAYMENT", 0.9),
    )
    _break_session(
        db_session,
        monkeypatch,
        _operation_error("INSERT INTO tickets (title) VALUES (?)"),
    )

    with caplog.at_level(logging.INFO):
        response = client.post(
            "/api/tickets",
            json={"title": "Card declined", "description": "It failed."},
            headers=agent_headers,
        )

    _assert_api_output_safe(response, caplog, capsys)


def test_logout_endpoint_output_is_safe_when_commit_and_rollback_fail(
    client: TestClient,
    db_session: Session,
    agent_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _break_session(
        db_session,
        monkeypatch,
        _operation_error("INSERT INTO revoked_tokens (jti) VALUES (?)"),
    )

    response = client.post("/api/auth/logout", headers=agent_headers)

    _assert_api_output_safe(response, caplog, capsys)
