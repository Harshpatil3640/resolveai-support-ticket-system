"""User account creation shared by the provisioning scripts and future APIs.

Every account is created through ``create_user()`` so that validation,
email normalisation, password hashing and duplicate handling are the same
everywhere. The returned ``User`` is an ORM object; API responses must use
a response model such as ``app.schemas.UserRead``, which has no password
hash field.
"""

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.roles import UserRole
from app.core.security import hash_password
from app.db.errors import (
    record_rollback_failure,
    rollback_failure_of,
    rollback_without_raising,
    separate_rollback_failure,
)
from app.models import User
from app.services.auth_service import UserNotFoundError, invalidate_user_sessions


MIN_PASSWORD_LENGTH = 12

# Upper bound on password length, so a single request cannot submit an
# arbitrarily large value for hashing. Long passphrases remain possible.
# HTTP request schemas that accept passwords MUST also declare
# max_length=MAX_PASSWORD_LENGTH (128), so oversized bodies are rejected
# before they reach this service.
MAX_PASSWORD_LENGTH = 128

# Column sizes from app.models.user (SQLite does not enforce them).
MAX_NAME_LENGTH = 100
MAX_EMAIL_LENGTH = 255

# A genuine duplicate email is a SQLite UNIQUE violation (structured
# extended result code) whose message names exactly the users.email
# column. NOT NULL, foreign-key, trigger and other integrity errors do
# not match and keep their original meaning.
_UNIQUE_VIOLATION_CODE = "SQLITE_CONSTRAINT_UNIQUE"
_DUPLICATE_EMAIL_MESSAGE = "UNIQUE constraint failed: users.email"


class UserServiceError(Exception):
    """Base class for user-service errors."""


class InvalidUserDataError(UserServiceError, ValueError):
    """A name, email, password or role failed validation."""


class UserAlreadyExistsError(UserServiceError):
    """An account with the same (case-insensitive) email already exists."""


# ---------------------------------------------------------------------------
# Validation helpers (also used by the CLI scripts to fail early)
# ---------------------------------------------------------------------------


def normalize_email(email: str) -> str:
    """Trim surrounding whitespace and lowercase an email address."""

    return email.strip().lower()


def validate_name(name: object) -> str:
    """Return the trimmed name, or raise ``InvalidUserDataError``."""

    if not isinstance(name, str) or not name.strip():
        raise InvalidUserDataError("Name is required.")

    trimmed = name.strip()

    if len(trimmed) > MAX_NAME_LENGTH:
        raise InvalidUserDataError(
            f"Name must be at most {MAX_NAME_LENGTH} characters."
        )

    return trimmed


def validate_email(email: object) -> str:
    """Return the normalised email, or raise ``InvalidUserDataError``.

    Requires exactly one "@" with text on both sides and no whitespace.
    """

    if not isinstance(email, str):
        raise InvalidUserDataError("A valid email address is required.")

    normalized = normalize_email(email)
    local_part, at_sign, domain = normalized.partition("@")

    if (
        not at_sign
        or not local_part
        or not domain
        or "@" in domain
        or any(character.isspace() for character in normalized)
        or len(normalized) > MAX_EMAIL_LENGTH
    ):
        raise InvalidUserDataError("A valid email address is required.")

    return normalized


def validate_password(password: object) -> None:
    """Raise ``InvalidUserDataError`` unless the password length is allowed.

    A password must be a string of MIN_PASSWORD_LENGTH (12) to
    MAX_PASSWORD_LENGTH (128) characters inclusive.
    """

    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise InvalidUserDataError(
            f"Password must contain at least {MIN_PASSWORD_LENGTH} characters."
        )

    if len(password) > MAX_PASSWORD_LENGTH:
        raise InvalidUserDataError(
            f"Password must contain at most {MAX_PASSWORD_LENGTH} characters."
        )


def validate_role(role: object) -> UserRole:
    """Return the ``UserRole`` for ``role``, or raise ``InvalidUserDataError``.

    Accepts a ``UserRole`` member or its exact stored value ("admin",
    "agent", "customer").
    """

    if isinstance(role, UserRole):
        return role

    if isinstance(role, str):
        try:
            return UserRole(role)
        except ValueError:
            pass

    allowed = ", ".join(member.value for member in UserRole)

    raise InvalidUserDataError(f"Invalid role. Expected one of: {allowed}.")


def email_is_registered(db: Session, email: str) -> bool:
    """Whether any account uses ``email``, compared case-insensitively."""

    return (
        db.scalar(
            select(User.id).where(
                func.lower(User.email) == normalize_email(email)
            )
        )
        is not None
    )


def _is_duplicate_email_error(error: IntegrityError) -> bool:
    """Whether ``error`` is SQLite's unique-index violation on users.email.

    The driver message is only compared here, never printed.
    """

    driver_error = error.orig

    return (
        getattr(driver_error, "sqlite_errorname", None) == _UNIQUE_VIOLATION_CODE
        and str(driver_error) == _DUPLICATE_EMAIL_MESSAGE
    )


# ---------------------------------------------------------------------------
# Account creation
# ---------------------------------------------------------------------------


def create_user(
    db: Session,
    *,
    name: str,
    email: str,
    password: str,
    role: UserRole | str,
    commit: bool = True,
) -> User:
    """Create an active user account and return it.

    All arguments after ``db`` are keyword-only and required; in
    particular there is no default role.

    The name is trimmed, the email is trimmed and lowercased, the password
    is hashed with ``hash_password()`` (the plaintext is never stored), and
    the account starts active with ``token_version`` 0.

    Transaction semantics (as for ``invalidate_user_sessions``):

    * ``commit=True`` (default): the function owns the session's current
      transaction. It commits on success and rolls back on ANY failure
      before re-raising. Pending work the caller added to the session is
      committed or rolled back with it.
    * ``commit=False``: the INSERT is flushed (so the new ``id`` is
      assigned and constraint violations surface). This function never
      calls ``commit()`` or ``rollback()`` in this mode; the caller owns
      the transaction.

      **If that flush fails**, SQLAlchemy itself rolls back the database
      transaction and marks the session inactive. The caller MUST then
      call ``rollback()`` before using the session again, and ALL work in
      that transaction is lost, including anything the caller flushed
      earlier. Earlier writes in the same transaction do not survive a
      failed flush. (A duplicate found by the pre-INSERT lookup raises
      before any flush and leaves the caller's transaction untouched.)

      ``flush()`` writes every pending object in the session, not only
      the new user, so a caller's own invalid pending object can also
      make this flush fail.

    Raises:
        InvalidUserDataError: a field failed validation. Nothing was
            written.
        UserAlreadyExistsError: an account with the same email exists,
            compared case-insensitively. Detected before the INSERT, and
            also from the database's unique index if a concurrent request
            created it in the meantime.
        sqlalchemy.exc.SQLAlchemyError: any other database failure.

    The exception raised is always the ORIGINAL failure (a genuine
    duplicate stays ``UserAlreadyExistsError``). If rolling back also
    failed, that rollback error is attached as a secondary failure,
    available through ``app.db.errors.rollback_failure_of()``.
    """

    trimmed_name = validate_name(name)
    normalized_email = validate_email(email)
    validate_password(password)
    user_role = validate_role(role)

    try:
        # Case-insensitive, so legacy rows stored with capitals also count.
        if email_is_registered(db, normalized_email):
            raise UserAlreadyExistsError(
                "An account with this email already exists."
            )

        user = User(
            name=trimmed_name,
            email=normalized_email,
            password_hash=hash_password(password),
            role=user_role.value,
            is_active=True,
            token_version=0,
        )

        db.add(user)
        db.flush()

        if commit:
            db.commit()

    except Exception as caught:
        # Keep the ORIGINAL failure primary. SQLAlchemy may have raised its
        # own failed rollback in its place (see separate_rollback_failure),
        # and our rollback below may fail too; either is only recorded as
        # the secondary failure, never raised instead of the original.
        error, rollback_failure = separate_rollback_failure(caught)
        own_rollback_failure = rollback_without_raising(db) if commit else None
        rollback_failure = rollback_failure or own_rollback_failure

        if isinstance(error, IntegrityError) and _is_duplicate_email_error(error):
            duplicate = UserAlreadyExistsError(
                "An account with this email already exists."
            )
            record_rollback_failure(duplicate, rollback_failure)
            raise duplicate from error

        record_rollback_failure(error, rollback_failure)

        if error is caught:
            raise

        raise error from None

    if commit:
        # Reload the committed row (commit() expires loaded attributes).
        db.refresh(user)

    return user


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


def reset_password(
    db: Session,
    user: User,
    new_password: str,
    *,
    commit: bool = True,
) -> int:
    """Set a new password and sign the user out everywhere, atomically.

    The new password is validated, hashed and written, then
    ``invalidate_user_sessions(db, user, commit=False)`` increments
    ``token_version`` so every token issued before the reset is rejected.
    Both changes belong to ONE transaction:

    1. ``password_hash`` is assigned and flushed first. If that UPDATE
       fails, ``token_version`` is never incremented.
    2. ``token_version`` is incremented with an atomic SQL UPDATE.
    3. Only then is the transaction committed (``commit=True``).

    Returns the new token version. Role or active-status restrictions are
    the caller's responsibility.

    Transaction semantics match ``create_user``:

    * ``commit=True`` (default): commits both changes together; on ANY
      failure rolls back so that neither the password nor the token
      version changes, then re-raises.
    * ``commit=False``: never commits or rolls back; the caller owns the
      transaction and must roll back after a failure. A failed flush
      loses all work in the transaction, as described for ``create_user``.

    Raises:
        InvalidUserDataError: the password failed validation. Nothing was
            changed.
        UserNotFoundError: the user row no longer exists.
        sqlalchemy.exc.SQLAlchemyError: any database failure.

    As for ``create_user``, the exception raised is always the original
    failure; a failed rollback is attached as a secondary failure
    (``app.db.errors.rollback_failure_of()``).
    """

    validate_password(new_password)
    new_hash = hash_password(new_password)

    # Read before any flush: after a failed flush the instance cannot load.
    user_id = user.id

    try:
        user.password_hash = new_hash

        try:
            db.flush()
        except Exception as flush_error:
            error, rollback_failure = separate_rollback_failure(flush_error)

            if isinstance(error, StaleDataError):
                # The UPDATE matched no row: the user was deleted.
                missing = UserNotFoundError(
                    f"Cannot reset password: user {user_id} does not exist."
                )
                record_rollback_failure(missing, rollback_failure)
                raise missing from error

            raise

        new_version = invalidate_user_sessions(db, user, commit=False)

        if commit:
            db.commit()

    except Exception as caught:
        # Keep the ORIGINAL failure primary; a failed rollback (SQLAlchemy's
        # internal one, or ours below) is only recorded as secondary.
        error, rollback_failure = separate_rollback_failure(caught)
        own_rollback_failure = rollback_without_raising(db) if commit else None
        record_rollback_failure(
            error,
            rollback_failure_of(error) or rollback_failure or own_rollback_failure,
        )

        if error is caught:
            raise

        raise error from None

    return new_version
