
import pytest

from sqlalchemy.orm import Session

from app.core.security import (
    create_access_token,
    hash_password,
)
from app.models import User
from app.services.auth_service import (
    authenticate_user,
    get_active_user_from_token,
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
        subject=str(auth_user.id)
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
        subject=str(auth_user.id)
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
        subject=str(auth_user.id)
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
