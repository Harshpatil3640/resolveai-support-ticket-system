
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import RevokedToken


def test_create_and_retrieve_revoked_token(
    db_session: Session,
) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)

    revoked_token = RevokedToken(
        jti="test-token-12345",
        expires_at=expires_at,
    )

    db_session.add(revoked_token)
    db_session.commit()

    result = db_session.get(
        RevokedToken,
        "test-token-12345",
    )

    assert result is not None
    assert result.jti == "test-token-12345"
    assert result.expires_at is not None
    assert result.revoked_at is not None


def test_revoked_token_jti_is_unique(
    db_session: Session,
) -> None:
    from sqlalchemy.exc import IntegrityError
    import pytest

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)

    first_token = RevokedToken(
        jti="duplicate-token",
        expires_at=expires_at,
    )

    db_session.add(first_token)
    db_session.commit()

    # Detach the first object from the ORM session.
    # Its database record remains present.
    db_session.expunge(first_token)

    second_token = RevokedToken(
        jti="duplicate-token",
        expires_at=expires_at,
    )

    db_session.add(second_token)

    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()
