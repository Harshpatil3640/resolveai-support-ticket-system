
import pytest

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.security import (
    create_access_token,
    hash_password,
)
from app.models import User
from app.services.auth_service import (
    UserNotFoundError,
    authenticate_user,
    get_active_user_from_token,
    invalidate_user_sessions,
    revoke_access_token,
)


@pytest.fixture
def auth_user(db_session: Session) -> User:
    """Create a test user with a hashed password."""

    user = User(
        name="Test Agent",
        email="agent@example.test",
        password_hash=hash_password("StrongPassword123!"),
    )

    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    return user


def test_authenticate_user_success(
    db_session: Session,
    auth_user: User,
) -> None:

    result = authenticate_user(
        db_session,
        "agent@example.test",
        "StrongPassword123!",
    )

    assert result is not None
    assert result.id == auth_user.id


def test_authenticate_user_wrong_password(
    db_session: Session,
    auth_user: User,
) -> None:

    result = authenticate_user(
        db_session,
        "agent@example.test",
        "WrongPassword123!",
    )

    assert result is None


def test_authenticate_user_unknown_email(
    db_session: Session,
) -> None:

    result = authenticate_user(
        db_session,
        "unknown@example.test",
        "SomePassword123!",
    )

    assert result is None


def test_inactive_user_cannot_authenticate(
    db_session: Session,
    auth_user: User,
) -> None:

    auth_user.is_active = False
    db_session.commit()

    result = authenticate_user(
        db_session,
        "agent@example.test",
        "StrongPassword123!",
    )

    assert result is None


def test_get_active_user_from_valid_token(
    db_session: Session,
    auth_user: User,
) -> None:

    token = create_access_token(
        subject=str(auth_user.id),
        token_version=auth_user.token_version,
    )

    result = get_active_user_from_token(
        db_session,
        token,
    )

    assert result is not None
    assert result.id == auth_user.id


def test_invalid_token_returns_no_user(
    db_session: Session,
) -> None:

    result = get_active_user_from_token(
        db_session,
        "invalid.token.value",
    )

    assert result is None


def test_revoked_token_cannot_authenticate(
    db_session: Session,
    auth_user: User,
) -> None:

    token = create_access_token(
        subject=str(auth_user.id),
        token_version=auth_user.token_version,
    )

    assert revoke_access_token(db_session, token) is True

    result = get_active_user_from_token(
        db_session,
        token,
    )

    assert result is None


def test_revoke_token_is_idempotent(
    db_session: Session,
    auth_user: User,
) -> None:

    token = create_access_token(
        subject=str(auth_user.id),
        token_version=auth_user.token_version,
    )

    first_result = revoke_access_token(
        db_session,
        token,
    )

    second_result = revoke_access_token(
        db_session,
        token,
    )

    assert first_result is True
    assert second_result is True


# ---------------------------------------------------------------------------
# Token version validation and session invalidation
# ---------------------------------------------------------------------------


def _token_for(user: User, version: int | None = None) -> str:
    return create_access_token(
        subject=str(user.id),
        token_version=user.token_version if version is None else version,
    )


def test_new_user_starts_at_token_version_zero(auth_user: User) -> None:
    assert auth_user.token_version == 0


def test_token_with_matching_version_is_accepted(
    db_session: Session,
    auth_user: User,
) -> None:
    result = get_active_user_from_token(db_session, _token_for(auth_user))

    assert result is not None
    assert result.id == auth_user.id


def test_token_with_mismatched_version_is_rejected(
    db_session: Session,
    auth_user: User,
) -> None:
    token = _token_for(auth_user, version=auth_user.token_version + 1)

    assert get_active_user_from_token(db_session, token) is None


def test_token_without_version_claim_is_rejected(
    db_session: Session,
    auth_user: User,
    forge_access_token,
) -> None:
    legacy_token = forge_access_token(
        subject=str(auth_user.id),
        omit=("token_version",),
    )

    assert get_active_user_from_token(db_session, legacy_token) is None


def test_invalidate_user_sessions_rejects_existing_tokens(
    db_session: Session,
    auth_user: User,
) -> None:
    old_token = _token_for(auth_user)

    new_version = invalidate_user_sessions(db_session, auth_user)

    assert new_version == 1
    assert auth_user.token_version == 1
    assert get_active_user_from_token(db_session, old_token) is None


def test_fresh_token_after_invalidation_is_accepted(
    db_session: Session,
    auth_user: User,
) -> None:
    invalidate_user_sessions(db_session, auth_user)

    fresh_token = _token_for(auth_user)
    result = get_active_user_from_token(db_session, fresh_token)

    assert result is not None
    assert result.id == auth_user.id


def test_invalidate_user_sessions_increments_each_time(
    db_session: Session,
    auth_user: User,
) -> None:
    assert invalidate_user_sessions(db_session, auth_user) == 1
    assert invalidate_user_sessions(db_session, auth_user) == 2

    db_session.expire_all()

    assert db_session.get(User, auth_user.id).token_version == 2


def test_invalidate_user_sessions_only_affects_target_user(
    db_session: Session,
    auth_user: User,
) -> None:
    other_user = User(
        name="Other Agent",
        email="other.agent@example.test",
        password_hash=hash_password("StrongPassword123!"),
    )
    db_session.add(other_user)
    db_session.commit()

    other_token = _token_for(other_user)

    invalidate_user_sessions(db_session, auth_user)

    db_session.refresh(other_user)

    assert other_user.token_version == 0
    assert get_active_user_from_token(db_session, other_token) is not None


def test_invalidate_user_sessions_without_commit_can_roll_back(
    db_session: Session,
    auth_user: User,
) -> None:
    assert invalidate_user_sessions(db_session, auth_user, commit=False) == 1

    db_session.rollback()
    db_session.refresh(auth_user)

    assert auth_user.token_version == 0


def test_revoked_token_with_valid_version_is_still_rejected(
    db_session: Session,
    auth_user: User,
) -> None:
    token = _token_for(auth_user)

    assert revoke_access_token(db_session, token) is True
    assert get_active_user_from_token(db_session, token) is None


def test_inactive_user_with_valid_version_token_is_rejected(
    db_session: Session,
    auth_user: User,
) -> None:
    token = _token_for(auth_user)

    auth_user.is_active = False
    db_session.commit()

    assert get_active_user_from_token(db_session, token) is None


def test_role_change_does_not_invalidate_token(
    db_session: Session,
    auth_user: User,
) -> None:
    token = _token_for(auth_user)

    auth_user.role = "admin"
    db_session.commit()

    result = get_active_user_from_token(db_session, token)

    assert result is not None
    assert result.role == "admin"
    assert result.token_version == 0


# ---------------------------------------------------------------------------
# invalidate_user_sessions: missing users and transaction semantics
# ---------------------------------------------------------------------------


def _stored_version(db: Session, user_id: int) -> int | None:
    """Read token_version straight from the database, bypassing the ORM."""

    return db.execute(
        text("SELECT token_version FROM users WHERE id = :id"),
        {"id": user_id},
    ).scalar_one_or_none()


def _count_rollbacks(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> list[None]:
    calls: list[None] = []
    real_rollback = db.rollback

    def _rollback() -> None:
        calls.append(None)
        real_rollback()

    monkeypatch.setattr(db, "rollback", _rollback)

    return calls


def _simulated_failure() -> OperationalError:
    return OperationalError("UPDATE users", {}, Exception("disk I/O error"))


def test_invalidate_user_sessions_commit_true_commits(
    db_session: Session,
    auth_user: User,
) -> None:
    assert invalidate_user_sessions(db_session, auth_user) == 1

    assert not db_session.in_transaction()

    # A rollback afterwards cannot undo a committed change.
    db_session.rollback()

    assert _stored_version(db_session, auth_user.id) == 1


def test_invalidated_version_is_available_after_session_closes(
    db_session: Session,
    auth_user: User,
) -> None:
    invalidate_user_sessions(db_session, auth_user)

    db_session.expunge(auth_user)

    # No query is needed: the committed version is set on the instance.
    assert auth_user.token_version == 1


def test_invalidate_user_sessions_commit_false_can_be_committed(
    db_session: Session,
    auth_user: User,
) -> None:
    assert invalidate_user_sessions(db_session, auth_user, commit=False) == 1
    assert auth_user.token_version == 1

    # Still the caller's open transaction.
    assert db_session.in_transaction()

    db_session.commit()
    db_session.rollback()

    assert _stored_version(db_session, auth_user.id) == 1


def test_invalidate_user_sessions_commit_false_keeps_caller_changes(
    db_session: Session,
    auth_user: User,
) -> None:
    """Unflushed caller changes, on the user and elsewhere, survive."""

    auth_user.name = "Renamed Agent"
    other_user = User(
        name="Other Agent",
        email="other.agent@example.test",
        password_hash=hash_password("StrongPassword123!"),
    )
    db_session.add(other_user)

    invalidate_user_sessions(db_session, auth_user, commit=False)

    assert auth_user.name == "Renamed Agent"

    db_session.commit()
    db_session.expire_all()

    stored = db_session.get(User, auth_user.id)

    assert stored.name == "Renamed Agent"
    assert stored.token_version == 1
    assert db_session.get(User, other_user.id) is not None


def test_invalidate_user_sessions_raises_for_missing_user(
    db_session: Session,
    auth_user: User,
) -> None:
    missing_user = User(
        id=999_999,
        name="Ghost",
        email="ghost@example.test",
        password_hash="unused",
    )

    with pytest.raises(UserNotFoundError, match="999999"):
        invalidate_user_sessions(db_session, missing_user)

    assert issubclass(UserNotFoundError, LookupError)
    assert _stored_version(db_session, auth_user.id) == 0


def test_missing_user_with_commit_true_rolls_back(
    db_session: Session,
    auth_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With commit=True the function owns the transaction and ends it."""

    db_session.execute(
        text("DELETE FROM users WHERE id = :id"),
        {"id": auth_user.id},
    )
    rollbacks = _count_rollbacks(db_session, monkeypatch)

    with pytest.raises(UserNotFoundError):
        invalidate_user_sessions(db_session, auth_user)

    assert len(rollbacks) == 1
    # The rollback restored the row the transaction had deleted.
    assert _stored_version(db_session, auth_user.id) == 0


def test_missing_user_with_commit_false_leaves_transaction_to_caller(
    db_session: Session,
    auth_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_session.execute(
        text("DELETE FROM users WHERE id = :id"),
        {"id": auth_user.id},
    )
    rollbacks = _count_rollbacks(db_session, monkeypatch)

    with pytest.raises(UserNotFoundError):
        invalidate_user_sessions(db_session, auth_user, commit=False)

    assert rollbacks == []
    assert db_session.in_transaction()
    # The caller's own (uncommitted) work is still in place.
    assert _stored_version(db_session, auth_user.id) is None

    db_session.rollback()

    assert _stored_version(db_session, auth_user.id) == 0


def test_execution_failure_with_commit_true_rolls_back(
    db_session: Session,
    auth_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_user.name = "Pending Change"
    rollbacks = _count_rollbacks(db_session, monkeypatch)

    def _failing_execute(*_args, **_kwargs):
        raise _simulated_failure()

    monkeypatch.setattr(db_session, "execute", _failing_execute)

    with pytest.raises(OperationalError):
        invalidate_user_sessions(db_session, auth_user)

    assert len(rollbacks) == 1

    # Restore the real execute() so the expired instance can reload.
    monkeypatch.undo()

    # Session.rollback() discards the pending change, as documented.
    assert auth_user.name == "Test Agent"
    assert auth_user.token_version == 0


def test_execution_failure_with_commit_false_does_not_roll_back(
    db_session: Session,
    auth_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_user.name = "Pending Change"
    rollbacks = _count_rollbacks(db_session, monkeypatch)

    def _failing_execute(*_args, **_kwargs):
        raise _simulated_failure()

    monkeypatch.setattr(db_session, "execute", _failing_execute)

    with pytest.raises(OperationalError):
        invalidate_user_sessions(db_session, auth_user, commit=False)

    assert rollbacks == []
    assert auth_user.name == "Pending Change"
    assert auth_user in db_session.dirty


def test_commit_failure_rolls_back_the_increment(
    db_session: Session,
    auth_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollbacks = _count_rollbacks(db_session, monkeypatch)

    def _failing_commit() -> None:
        raise _simulated_failure()

    monkeypatch.setattr(db_session, "commit", _failing_commit)

    with pytest.raises(OperationalError):
        invalidate_user_sessions(db_session, auth_user)

    assert len(rollbacks) == 1
    assert _stored_version(db_session, auth_user.id) == 0

    db_session.refresh(auth_user)

    assert auth_user.token_version == 0
