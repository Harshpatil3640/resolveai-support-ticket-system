"""User account management shared by the provisioning scripts and the API.

Every account is created through ``create_user()`` so that validation,
email normalisation, password hashing and duplicate handling are the same
everywhere. The returned ``User`` is an ORM object; API responses must use
a response model such as ``app.schemas.UserRead``, which has no password
hash field.

The administration functions at the end of this module (listing users,
creating an account as an administrator and changing a user's role or
active status) are used by the admin-only API. Role and status changes
enforce the "at least one active administrator" rule, and every change
re-checks the acting administrator, inside a serialised transaction; see
``change_user_role`` and ``create_user_as_admin``.
"""

from dataclasses import dataclass

from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.roles import UserRole
from app.core.security import hash_password
from app.db.errors import (
    record_rollback_failure,
    roll_back_keeping_primary,
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


# ---------------------------------------------------------------------------
# Administration: listing users, changing roles and active status
# ---------------------------------------------------------------------------


DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MAX_SEARCH_LENGTH = 200

# Escape character for LIKE patterns, so "%" and "_" typed into a search
# match literally instead of acting as wildcards.
_LIKE_ESCAPE = "\\"


class SelfModificationError(UserServiceError):
    """An administrator tried to change their own role or active status."""


class LastActiveAdministratorError(UserServiceError):
    """The change would leave no active administrator."""


class AdministratorRequiredError(UserServiceError, PermissionError):
    """The acting account is no longer an active administrator.

    Raised when the actor's administrator access was removed (by a
    concurrent request) after the request was authorised but before its
    change was made.
    """


class TransactionAlreadyStartedError(UserServiceError, RuntimeError):
    """The session already has a database write transaction in progress.

    A role or status change must start its own serialised transaction (it
    takes SQLite's write lock before reading), so it refuses to run inside
    a transaction that has already written. This is a programming error,
    never a client error.
    """


@dataclass(frozen=True)
class RoleChange:
    """Outcome of ``change_user_role``.

    ``changed`` is False when the user already had the requested role;
    nothing was written in that case.
    """

    user: User
    previous_role: str
    new_role: str
    changed: bool


@dataclass(frozen=True)
class StatusChange:
    """Outcome of ``set_user_active_status``.

    ``changed`` is False when the account already had the requested
    status; nothing was written (and ``token_version`` was not
    incremented) in that case.
    """

    user: User
    was_active: bool
    is_active: bool
    changed: bool


def get_user(db: Session, user_id: int) -> User | None:
    """Return the user with ``user_id``, or None if there is none."""

    return db.get(User, user_id)


def _like_pattern(term: str) -> str:
    """A "contains" LIKE pattern matching ``term`` literally."""

    escaped = (
        term.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )

    return f"%{escaped}%"


def list_users(
    db: Session,
    *,
    skip: int = 0,
    limit: int = DEFAULT_PAGE_SIZE,
    search: str | None = None,
    role: UserRole | str | None = None,
    is_active: bool | None = None,
) -> tuple[list[User], int]:
    """Return one page of users and the total number matching the filters.

    ``search`` matches the name or email case-insensitively (a blank
    search is ignored; "%" and "_" match literally). ``role`` and
    ``is_active`` filter by exact stored values. ``total`` counts every
    matching user, before ``skip`` and ``limit`` are applied. Users are
    ordered newest first (``created_at``, then ``id``, descending), so
    pages are stable even when several users share a creation time.

    Raises:
        InvalidUserDataError: ``skip``, ``limit``, ``search`` or ``role``
            is out of range.
    """

    if skip < 0:
        raise InvalidUserDataError("skip must not be negative.")

    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise InvalidUserDataError(
            f"limit must be between 1 and {MAX_PAGE_SIZE}."
        )

    conditions = []

    if search is not None and search.strip():
        term = search.strip()

        if len(term) > MAX_SEARCH_LENGTH:
            raise InvalidUserDataError(
                f"search must be at most {MAX_SEARCH_LENGTH} characters."
            )

        pattern = _like_pattern(term)
        conditions.append(
            or_(
                User.name.ilike(pattern, escape=_LIKE_ESCAPE),
                User.email.ilike(pattern, escape=_LIKE_ESCAPE),
            )
        )

    if role is not None:
        conditions.append(User.role == validate_role(role).value)

    if is_active is not None:
        conditions.append(User.is_active.is_(bool(is_active)))

    total = db.scalar(
        select(func.count()).select_from(User).where(*conditions)
    )

    users = db.scalars(
        select(User)
        .where(*conditions)
        .order_by(User.created_at.desc(), User.id.desc())
        .offset(skip)
        .limit(limit)
    ).all()

    return list(users), total


# -- Serialised role and status changes --------------------------------------
#
# Security rule: at least one ACTIVE administrator must always remain. An
# administrator is a user whose stored role is exactly "admin" (the value
# require_admin accepts) and who is active.
#
# A count-then-update is not enough: two administrators deactivating each
# other at the same time could both see two active administrators and both
# succeed. So the check and the write happen in ONE transaction that no
# other administrative change can interleave with:
#
# * SQLite ignores SELECT ... FOR UPDATE, and the pysqlite driver runs
#   SELECTs outside any transaction until the first write. The change
#   therefore starts with BEGIN IMMEDIATE, which takes SQLite's single
#   write lock BEFORE anything is read. A concurrent change waits (up to
#   the connection's busy timeout) until this transaction commits or rolls
#   back, and only then reads; if the lock is not released in time it
#   fails with "database is locked" and changes nothing.
# * Databases with row locks get SELECT ... FOR UPDATE on every active
#   administrator row (in id order, so two changes cannot deadlock) and on
#   the target row. A concurrent change blocks until this one commits and
#   then re-reads the locked rows.


def _active_administrators_for_update() -> Select:
    """IDs of the active administrators, locked where supported."""

    return (
        select(User.id)
        .where(
            User.role == UserRole.ADMIN.value,
            User.is_active.is_(True),
        )
        .order_by(User.id)
        .with_for_update()
    )


def _user_for_update(user_id: int) -> Select:
    """The user row, locked where supported, reloaded from the database."""

    return (
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        # Overwrite any stale copy already in the session's identity map
        # (e.g. loaded while the request was being authorised).
        .execution_options(populate_existing=True)
    )


def _begin_serialised_change(db: Session) -> None:
    """Start the transaction for a role or status change, write-locked.

    On SQLite this takes the database write lock (BEGIN IMMEDIATE) before
    anything is read. Other databases rely on the FOR UPDATE row locks
    taken by the reads that follow.

    Raises:
        TransactionAlreadyStartedError: the session's SQLite connection is
            already inside a transaction (something was written earlier),
            so BEGIN IMMEDIATE cannot be issued.
        sqlalchemy.exc.OperationalError: the lock was not released by a
            concurrent change within the busy timeout.
    """

    connection = db.connection()

    if connection.dialect.name != "sqlite":
        return

    if connection.connection.driver_connection.in_transaction:
        raise TransactionAlreadyStartedError(
            "A role or status change must start a new transaction."
        )

    connection.exec_driver_sql("BEGIN IMMEDIATE")


def _lock_for_change(db: Session, user_id: int) -> tuple[User | None, list[int]]:
    """Lock and read the state an administrative change depends on.

    Returns the user ``user_id`` (None if it does not exist; the target of
    a role or status change, or the actor of an account creation) and the
    IDs of the active administrators, both read inside the serialised
    transaction.
    """

    _begin_serialised_change(db)

    # Administrator rows first, then the target, always in this order.
    active_admin_ids = list(db.scalars(_active_administrators_for_update()))
    target = db.scalars(_user_for_update(user_id)).one_or_none()

    return target, active_admin_ids


def _reject_self_change(actor_id: int, user_id: int, what: str) -> None:
    if actor_id == user_id:
        raise SelfModificationError(
            f"Administrators cannot change their own {what}."
        )


def _ensure_not_last_active_administrator(
    active_admin_ids: list[int],
    user_id: int,
) -> None:
    """Raise if ``user_id`` is the only active administrator left."""

    if active_admin_ids == [user_id]:
        raise LastActiveAdministratorError(
            "At least one active administrator must remain."
        )


def _ensure_actor_is_active_administrator(
    active_admin_ids: list[int],
    actor_id: int,
) -> None:
    """Raise unless the actor is still an active administrator.

    The request was authorised before the lock was taken; a concurrent
    change may have demoted or deactivated the actor since then.
    """

    if actor_id not in active_admin_ids:
        raise AdministratorRequiredError(
            "The acting account is no longer an active administrator."
        )


def change_user_role(
    db: Session,
    *,
    actor_id: int,
    user_id: int,
    role: UserRole | str,
) -> RoleChange:
    """Change a user's role on behalf of the administrator ``actor_id``.

    Checks, in order (all except the first inside the serialised
    transaction described above):

    1. An administrator cannot change their own role.
    2. The user must exist.
    3. Demoting the last active administrator is refused.
    4. The actor must still be an active administrator.

    Setting the role the user already has changes nothing and returns
    ``changed=False``. ``token_version`` is NOT incremented: the role is
    re-read from the database on every request, so the change takes
    effect immediately for tokens the user already holds.

    The function owns the session's transaction: it commits on success
    (also when nothing changed, to release the lock) and rolls back on ANY
    failure before re-raising the original exception; a failed rollback
    is attached as a secondary failure
    (``app.db.errors.rollback_failure_of()``). The returned user is
    expired by the commit and reloads its attributes on access.

    Raises:
        InvalidUserDataError: ``role`` is not a valid role.
        SelfModificationError: ``user_id`` is the actor.
        UserNotFoundError: the user does not exist.
        LastActiveAdministratorError: the user is the last active
            administrator and would lose the administrator role.
        AdministratorRequiredError: the actor is no longer an active
            administrator.
        TransactionAlreadyStartedError: the session has already written
            in its current transaction.
        sqlalchemy.exc.SQLAlchemyError: any database failure, including a
            lock held by a concurrent change past the busy timeout.
    """

    new_role = validate_role(role)
    _reject_self_change(actor_id, user_id, "role")

    try:
        target, active_admin_ids = _lock_for_change(db, user_id)

        if target is None:
            raise UserNotFoundError(f"User {user_id} does not exist.")

        previous_role = target.role
        changed = previous_role != new_role.value

        if changed and new_role is not UserRole.ADMIN:
            _ensure_not_last_active_administrator(active_admin_ids, user_id)

        _ensure_actor_is_active_administrator(active_admin_ids, actor_id)

        if changed:
            target.role = new_role.value
            db.flush()

        db.commit()

    except Exception as caught:
        primary = roll_back_keeping_primary(db, caught)

        if primary is caught:
            raise

        raise primary from None

    return RoleChange(
        user=target,
        previous_role=previous_role,
        new_role=new_role.value,
        changed=changed,
    )


def set_user_active_status(
    db: Session,
    *,
    actor_id: int,
    user_id: int,
    is_active: bool,
) -> StatusChange:
    """Activate or deactivate a user on behalf of the administrator ``actor_id``.

    Checks, in order (all except the first two inside the serialised
    transaction described above):

    1. ``is_active`` must be a bool.
    2. An administrator cannot change their own status.
    3. The user must exist.
    4. Deactivating the last active administrator is refused.
    5. The actor must still be an active administrator.

    A real change writes ``is_active`` and then increments
    ``token_version`` (``invalidate_user_sessions(commit=False)``, an
    atomic SQL UPDATE), and commits both together, so every token issued
    before the change is rejected afterwards. This applies to reactivation
    too: tokens issued before a deactivation never become valid again.
    Requesting the status the account already has changes nothing, does
    not increment ``token_version``, and returns ``changed=False``.

    Transaction ownership, failure handling and the returned user's state
    are as for ``change_user_role``.

    Raises:
        InvalidUserDataError: ``is_active`` is not a bool.
        SelfModificationError: ``user_id`` is the actor.
        UserNotFoundError: the user does not exist.
        LastActiveAdministratorError: the user is the last active
            administrator and would be deactivated.
        AdministratorRequiredError: the actor is no longer an active
            administrator.
        TransactionAlreadyStartedError: the session has already written
            in its current transaction.
        sqlalchemy.exc.SQLAlchemyError: any database failure, including a
            lock held by a concurrent change past the busy timeout.
    """

    if not isinstance(is_active, bool):
        raise InvalidUserDataError("is_active must be true or false.")

    _reject_self_change(actor_id, user_id, "status")

    try:
        target, active_admin_ids = _lock_for_change(db, user_id)

        if target is None:
            raise UserNotFoundError(f"User {user_id} does not exist.")

        was_active = bool(target.is_active)
        changed = was_active != is_active

        if changed and not is_active:
            _ensure_not_last_active_administrator(active_admin_ids, user_id)

        _ensure_actor_is_active_administrator(active_admin_ids, actor_id)

        if changed:
            # Status first: if this UPDATE fails, token_version is never
            # incremented. Both are committed together below.
            target.is_active = is_active
            db.flush()
            invalidate_user_sessions(db, target, commit=False)

        db.commit()

    except Exception as caught:
        primary = roll_back_keeping_primary(db, caught)

        if primary is caught:
            raise

        raise primary from None

    return StatusChange(
        user=target,
        was_active=was_active,
        is_active=is_active,
        changed=changed,
    )


@dataclass(frozen=True)
class UserCreation:
    """Outcome of ``create_user_as_admin``.

    ``user_id``, ``role`` and ``is_active`` were read before the commit, so
    they are available (e.g. for the audit log) without touching the
    database again. ``user`` is expired by the commit and reloads its
    attributes on access.
    """

    user: User
    user_id: int
    role: str
    is_active: bool


def create_user_as_admin(
    db: Session,
    *,
    actor_id: int,
    name: str,
    email: str,
    password: str,
    role: UserRole | str,
) -> UserCreation:
    """Create an account on behalf of the administrator ``actor_id``.

    Runs in the same serialised transaction as role and status changes
    (SQLite write lock first; elsewhere the active administrator rows,
    then the actor's row, locked FOR UPDATE), so the actor cannot lose
    their administrator access between the check below and the commit:

    1. The actor's row is reloaded; the actor must still exist, be active
       and have exactly the role "admin".
    2. ``create_user(..., commit=False)`` validates, normalises, hashes and
       inserts the account (duplicates are detected there).
    3. The transaction is committed.

    The function owns the session's transaction: on ANY failure it rolls
    back before re-raising the original exception; a failed rollback is
    attached as a secondary failure (``app.db.errors.rollback_failure_of``).
    Nothing is written unless everything succeeds.

    Raises:
        AdministratorRequiredError: the actor is missing, inactive or no
            longer an administrator. Nothing was written.
        InvalidUserDataError: a field failed validation.
        UserAlreadyExistsError: the email is already registered.
        TransactionAlreadyStartedError: the session has already written
            in its current transaction.
        sqlalchemy.exc.SQLAlchemyError: any database failure, including a
            lock held by a concurrent change past the busy timeout.
    """

    try:
        actor, _active_admin_ids = _lock_for_change(db, actor_id)

        if (
            actor is None
            or not actor.is_active
            or actor.role != UserRole.ADMIN.value
        ):
            raise AdministratorRequiredError(
                "The acting account is no longer an active administrator."
            )

        user = create_user(
            db,
            name=name,
            email=email,
            password=password,
            role=role,
            commit=False,
        )
        created = UserCreation(
            user=user,
            user_id=user.id,
            role=user.role,
            is_active=bool(user.is_active),
        )

        db.commit()

    except Exception as caught:
        primary = roll_back_keeping_primary(db, caught)

        if primary is caught:
            raise

        raise primary from None

    return created
