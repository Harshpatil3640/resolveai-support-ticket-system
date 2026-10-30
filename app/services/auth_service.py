
from datetime import datetime, timezone

import jwt
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import (
    decode_access_token,
    verify_password,
)
from app.models import RevokedToken, User


def authenticate_user(
    db: Session,
    email: str,
    password: str,
) -> User | None:
    """Verify user credentials and return an active user."""

    normalized_email = email.strip().lower()

    user = db.scalar(
        select(User).where(User.email == normalized_email)
    )

    if user is None:
        return None

    if not user.is_active:
        return None

    if not verify_password(password, user.password_hash):
        return None

    return user


def get_active_user_from_token(
    db: Session,
    token: str,
) -> User | None:
    """Validate a token and retrieve its active user."""

    try:
        payload = decode_access_token(token)

    except jwt.InvalidTokenError:
        return None

    token_id = payload.get("jti")

    if not isinstance(token_id, str):
        return None

    # Reject tokens that have been revoked.
    revoked = db.get(RevokedToken, token_id)

    if revoked is not None:
        return None

    # The token subject must contain a valid user ID.
    try:
        user_id = int(payload["sub"])
    except (TypeError, ValueError):
        return None

    if user_id <= 0:
        return None

    user = db.get(User, user_id)

    if user is None or not user.is_active:
        return None

    return user


def revoke_access_token(
    db: Session,
    token: str,
) -> bool:
    """Revoke a valid access token.

    Returns True when the token is revoked or was already revoked.
    Returns False when the token is invalid or expired.
    """

    try:
        payload = decode_access_token(token)

    except jwt.InvalidTokenError:
        return False

    token_id = payload.get("jti")

    if not isinstance(token_id, str):
        return False

    # Make logout idempotent.
    if db.get(RevokedToken, token_id) is not None:
        return True

    expires_at = datetime.fromtimestamp(
        payload["exp"],
        tz=timezone.utc,
    )

    revoked_token = RevokedToken(
        jti=token_id,
        expires_at=expires_at,
    )

    db.add(revoked_token)

    try:
        db.commit()

    except IntegrityError:
        db.rollback()

        # Another request may have revoked this token first.
        return db.get(RevokedToken, token_id) is not None

    return True
