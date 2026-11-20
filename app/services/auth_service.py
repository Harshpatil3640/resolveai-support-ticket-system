
from datetime import datetime, timezone

import jwt
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.core.security import (
    TOKEN_VERSION_CLAIM,
    decode_access_token,
    verify_password,
)
from app.db.errors import roll_back_keeping_primary, rollback_failure_of
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

    # Tokens issued before the user's sessions were invalidated carry an
    # older version and are rejected. The role is deliberately NOT read
    # from the token; it is always loaded from the database.
    if payload[TOKEN_VERSION_CLAIM] != user.token_version:
        return None

    return user


class UserNotFoundError(LookupError):
    """Raised when an operation targets a user row that does not exist."""


def invalidate_user_sessions(
    db: Session,
    user: User,
    *,
    commit: bool = True,
) -> int:
    """Invalidate every access token previously issued to ``user``.

    Increments ``token_version`` with a single atomic SQL UPDATE, so
    concurrent invalidations cannot be lost, and returns the resulting
    version. ``user.token_version`` is updated to match.

    Intended for password resets and account deactivation. Role changes
    do not need it: roles are re-read from the database on each request.

    Transaction semantics:

    * ``commit=True`` (default): the function owns the session's current
      transaction. On success it commits it; on ANY failure (SQL error,
      missing user, commit error) it rolls it back and re-raises. Pending
      work the caller added to the session is committed or rolled back
      with it.
    * ``commit=False``: the caller owns the transaction. The increment is
      executed but never committed or rolled back here, and exceptions
      propagate unchanged, so the caller decides whether to commit or roll
      back. Other pending changes in the session (including unflushed
      changes to ``user`` itself) are left untouched.

    Raises:
        UserNotFoundError: no row exists for ``user.id``. Nothing was
            changed by this function.
        sqlalchemy.exc.SQLAlchemyError: the UPDATE or the commit failed.
    """

    try:
        result = db.execute(
            update(User)
            .where(User.id == user.id)
            .values(token_version=User.token_version + 1)
            # The attribute is reloaded explicitly below.
            .execution_options(synchronize_session=False)
        )

        if result.rowcount == 0:
            raise UserNotFoundError(
                f"Cannot invalidate sessions: user {user.id} does not exist."
            )

        # Read back the incremented value inside the same transaction.
        # Only token_version is refreshed, so any other unflushed changes
        # the caller made to this user are preserved.
        db.refresh(user, attribute_names=["token_version"])
        new_version = user.token_version

        if commit:
            db.commit()

    except Exception as caught:
        # The original failure stays primary (a missing user remains
        # UserNotFoundError); a failed rollback is recorded as secondary.
        primary = roll_back_keeping_primary(db, caught, roll_back=commit)

        if primary is caught:
            raise

        raise primary from None

    if commit:
        # commit() expires loaded attributes. Record the committed version
        # on the instance so it is available without another query (and
        # even after the session is closed).
        set_committed_value(user, "token_version", new_version)

    return new_version


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

    except SQLAlchemyError as caught:
        # Guarded rollback: the original failure stays primary and a
        # failed rollback is recorded as secondary, never raised instead.
        primary = roll_back_keeping_primary(db, caught)

        if (
            isinstance(primary, IntegrityError)
            and rollback_failure_of(primary) is None
        ):
            # Another request may have revoked this token first.
            return db.get(RevokedToken, token_id) is not None

        if primary is caught:
            raise

        raise primary from None

    return True
