"""Tests for the shared user-creation service.

Uses the in-memory SQLite session from conftest.py; resolveai.db is never
touched.
"""

import sqlite3

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import (
    IntegrityError,
    OperationalError,
    PendingRollbackError,
)
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.roles import UserRole
from app.core.security import create_access_token, verify_password
from app.db import database as app_database
from app.db.base import Base
from app.db.errors import describe_database_error, rollback_failure_of
from app.models import Ticket, User
from app.schemas import UserRead
from app.services import user_service
from app.services.auth_service import (
    UserNotFoundError,
    get_active_user_from_token,
)
from app.services.user_service import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    InvalidUserDataError,
    UserAlreadyExistsError,
    create_user,
    reset_password,
    validate_password,
)


PASSWORD = "StrongPassword123!"


def _create(db: Session, **overrides) -> User:
    fields = {
        "name": "Test User",
        "email": "user@example.test",
        "password": PASSWORD,
        "role": UserRole.AGENT,
    }
    fields.update(overrides)

    return create_user(db, **fields)


def _user_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User))


def _stored_rows(db: Session) -> list[tuple]:
    return db.execute(
        text("SELECT id, name, email, password_hash, role FROM users ORDER BY id")
    ).all()


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(UserRole))
def test_creates_user_for_every_role(db_session: Session, role: UserRole) -> None:
    user = _create(db_session, email=f"{role.value}@example.test", role=role)

    assert user.id is not None
    assert user.role == role.value


def test_accepts_role_as_its_stored_string_value(db_session: Session) -> None:
    user = _create(db_session, role="customer")

    assert user.role == UserRole.CUSTOMER.value


def test_role_is_required(db_session: Session) -> None:
    with pytest.raises(TypeError):
        create_user(  # type: ignore[call-arg]
            db_session,
            name="No Role",
            email="norole@example.test",
            password=PASSWORD,
        )

    assert _user_count(db_session) == 0


@pytest.mark.parametrize(
    "role",
    ["superuser", "ADMIN", "Admin", "", None, 1, True],
)
def test_invalid_role_is_rejected(db_session: Session, role: object) -> None:
    with pytest.raises(InvalidUserDataError, match="Invalid role"):
        _create(db_session, role=role)

    assert _user_count(db_session) == 0


# ---------------------------------------------------------------------------
# Field validation and normalisation
# ---------------------------------------------------------------------------


def test_email_is_trimmed_and_lowercased(db_session: Session) -> None:
    user = _create(db_session, email="  Mixed.Case@Example.TEST \t")

    assert user.email == "mixed.case@example.test"


def test_name_is_trimmed(db_session: Session) -> None:
    user = _create(db_session, name="   Ada Lovelace  ")

    assert user.name == "Ada Lovelace"


@pytest.mark.parametrize(
    "email",
    [
        "",
        "   ",
        "no-at-sign.example.test",
        "@example.test",
        "user@",
        "two@at@example.test",
        "spa ce@example.test",
        "a" * 250 + "@example.test",
        None,
    ],
)
def test_invalid_email_is_rejected(db_session: Session, email: object) -> None:
    with pytest.raises(InvalidUserDataError, match="valid email"):
        _create(db_session, email=email)

    assert _user_count(db_session) == 0


@pytest.mark.parametrize("name", ["", "    ", None, "x" * 101])
def test_invalid_name_is_rejected(db_session: Session, name: object) -> None:
    with pytest.raises(InvalidUserDataError, match="Name"):
        _create(db_session, name=name)

    assert _user_count(db_session) == 0


@pytest.mark.parametrize(
    "password",
    ["", "short", "x" * (MIN_PASSWORD_LENGTH - 1), None],
)
def test_short_password_is_rejected(db_session: Session, password: object) -> None:
    with pytest.raises(InvalidUserDataError, match="at least 12 characters"):
        _create(db_session, password=password)

    assert _user_count(db_session) == 0


def test_password_of_exactly_minimum_length_is_accepted(
    db_session: Session,
) -> None:
    user = _create(db_session, password="x" * MIN_PASSWORD_LENGTH)

    assert verify_password("x" * MIN_PASSWORD_LENGTH, user.password_hash)


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


def test_duplicate_email_is_rejected(db_session: Session) -> None:
    _create(db_session, email="dup@example.test")

    with pytest.raises(UserAlreadyExistsError):
        _create(db_session, email="dup@example.test", role=UserRole.ADMIN)

    assert _user_count(db_session) == 1


def test_duplicate_email_differing_only_in_case_is_rejected(
    db_session: Session,
) -> None:
    _create(db_session, email="dup@example.test")

    with pytest.raises(UserAlreadyExistsError):
        _create(db_session, email="  DUP@Example.Test ")

    assert _user_count(db_session) == 1


def test_legacy_mixed_case_email_counts_as_duplicate(db_session: Session) -> None:
    """A row stored with capitals (before normalisation) still blocks."""

    db_session.execute(
        text(
            "INSERT INTO users (name, email, password_hash, role, is_active, "
            "token_version, created_at) VALUES ('Legacy', 'Legacy@Example.TEST', "
            "'hash', 'agent', 1, 0, '2026-11-01 00:00:00')"
        )
    )
    db_session.commit()

    with pytest.raises(UserAlreadyExistsError):
        _create(db_session, email="legacy@example.test")

    assert _user_count(db_session) == 1


def test_unique_index_violation_is_mapped_and_rolled_back(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent insert that slips past the pre-check is still caught."""

    _create(db_session, email="race@example.test")

    # Simulate the race: the pre-check sees no account.
    monkeypatch.setattr(user_service, "email_is_registered", lambda *_: False)

    with pytest.raises(UserAlreadyExistsError):
        _create(db_session, email="race@example.test")

    assert not db_session.in_transaction()
    assert _user_count(db_session) == 1


# ---------------------------------------------------------------------------
# Password storage and the created account
# ---------------------------------------------------------------------------


def test_password_is_hashed_and_verifies(db_session: Session) -> None:
    user = _create(db_session)

    assert user.password_hash != PASSWORD
    assert verify_password(PASSWORD, user.password_hash)
    assert not verify_password(PASSWORD + "x", user.password_hash)


def test_plaintext_password_is_never_stored_or_returned(
    db_session: Session,
) -> None:
    user = _create(db_session)

    for row in _stored_rows(db_session):
        assert all(PASSWORD not in str(value) for value in row)

    assert PASSWORD not in repr(user)
    assert all(
        PASSWORD not in str(value) for value in vars(user).values()
    )


def test_public_response_model_has_no_password_hash(
    db_session: Session,
) -> None:
    user = _create(db_session)

    public = UserRead.model_validate(user).model_dump()

    assert "password_hash" not in public
    assert "password" not in public
    assert user.password_hash not in str(public)


def test_new_account_is_active_with_token_version_zero(
    db_session: Session,
) -> None:
    user = _create(db_session)

    db_session.expire_all()
    stored = db_session.get(User, user.id)

    assert stored.is_active is True
    assert stored.token_version == 0


# ---------------------------------------------------------------------------
# Transaction semantics
# ---------------------------------------------------------------------------


def test_commit_true_commits(db_session: Session) -> None:
    user = _create(db_session)
    user_id = user.id

    # A rollback afterwards cannot undo a committed insert.
    db_session.rollback()
    db_session.expire_all()

    assert db_session.get(User, user_id) is not None


def test_commit_false_can_be_committed_or_rolled_back(
    db_session: Session,
) -> None:
    kept = _create(db_session, email="kept@example.test", commit=False)

    assert kept.id is not None
    assert db_session.in_transaction()

    db_session.commit()

    _create(db_session, email="discarded@example.test", commit=False)
    db_session.rollback()

    emails = db_session.scalars(select(User.email)).all()

    assert emails == ["kept@example.test"]


def test_database_failure_with_commit_true_rolls_back(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollbacks: list[None] = []
    real_rollback = db_session.rollback

    def _rollback() -> None:
        rollbacks.append(None)
        real_rollback()

    def _failing_commit() -> None:
        raise OperationalError("INSERT INTO users", {}, Exception("disk I/O error"))

    monkeypatch.setattr(db_session, "rollback", _rollback)
    monkeypatch.setattr(db_session, "commit", _failing_commit)

    with pytest.raises(OperationalError):
        _create(db_session)

    assert len(rollbacks) == 1
    assert _user_count(db_session) == 0


def test_validation_failure_writes_nothing(db_session: Session) -> None:
    with pytest.raises(InvalidUserDataError):
        _create(db_session, password="short")

    # Validation runs before anything is added to the session.
    assert not db_session.new
    assert _user_count(db_session) == 0


# ---------------------------------------------------------------------------
# Password length limits
# ---------------------------------------------------------------------------


def test_password_length_limits_are_12_and_128() -> None:
    assert MIN_PASSWORD_LENGTH == 12
    assert MAX_PASSWORD_LENGTH == 128


@pytest.mark.parametrize(
    ("length", "expected_error"),
    [
        (11, "at least 12 characters"),
        (12, None),
        (128, None),
        (129, "at most 128 characters"),
    ],
)
def test_password_length_boundaries(
    db_session: Session,
    length: int,
    expected_error: str | None,
) -> None:
    password = "p" * length

    if expected_error is None:
        validate_password(password)
        user = _create(db_session, password=password)

        assert verify_password(password, user.password_hash)
    else:
        with pytest.raises(InvalidUserDataError, match=expected_error):
            validate_password(password)

        with pytest.raises(InvalidUserDataError, match=expected_error):
            _create(db_session, password=password)

        assert _user_count(db_session) == 0


# ---------------------------------------------------------------------------
# commit=False: a failed flush
# ---------------------------------------------------------------------------


def test_commit_false_failed_flush_leaves_rollback_to_caller(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The service neither commits nor rolls back; the caller's work is lost."""

    _create(db_session, email="taken@example.test")

    # Caller-owned work, flushed earlier in the same transaction.
    caller_user = User(
        name="Caller Work",
        email="caller.work@example.test",
        password_hash="caller-hash",
    )
    db_session.add(caller_user)
    db_session.flush()

    calls: list[str] = []
    real_commit, real_rollback = db_session.commit, db_session.rollback

    def _commit() -> None:
        calls.append("commit")
        real_commit()

    def _rollback() -> None:
        calls.append("rollback")
        real_rollback()

    monkeypatch.setattr(db_session, "commit", _commit)
    monkeypatch.setattr(db_session, "rollback", _rollback)

    # Make the INSERT itself fail: the pre-check misses the duplicate.
    monkeypatch.setattr(user_service, "email_is_registered", lambda *_: False)

    with pytest.raises(UserAlreadyExistsError):
        _create(db_session, email="taken@example.test", commit=False)

    # The service did not commit or roll back the caller's transaction.
    assert calls == []

    # SQLAlchemy has marked the transaction for rollback.
    assert not db_session.get_transaction().is_active

    with pytest.raises(PendingRollbackError):
        db_session.scalar(select(func.count()).select_from(User))

    # The caller must roll back, and its earlier flushed work is gone.
    db_session.rollback()

    emails = db_session.scalars(select(User.email)).all()

    assert emails == ["taken@example.test"]


# ---------------------------------------------------------------------------
# reset_password()
# ---------------------------------------------------------------------------


NEW_PASSWORD = "Brand-New-Passphrase-77"


def _stored_credentials(db: Session, user_id: int) -> tuple[str, int]:
    """(password_hash, token_version) read directly from the database."""

    return tuple(
        db.execute(
            text(
                "SELECT password_hash, token_version FROM users WHERE id = :id"
            ),
            {"id": user_id},
        ).one()
    )


def test_reset_password_changes_password_and_token_version(
    db_session: Session,
) -> None:
    user = _create(db_session)
    user_id = user.id

    assert reset_password(db_session, user, NEW_PASSWORD) == 1

    db_session.rollback()  # proves both changes were committed
    password_hash, token_version = _stored_credentials(db_session, user_id)

    assert token_version == 1
    assert verify_password(NEW_PASSWORD, password_hash)
    assert not verify_password(PASSWORD, password_hash)


@pytest.mark.parametrize(
    "bad_password",
    ["short", "p" * (MAX_PASSWORD_LENGTH + 1), None],
)
def test_reset_password_validation_failure_changes_nothing(
    db_session: Session,
    bad_password: object,
) -> None:
    user = _create(db_session)
    before = _stored_credentials(db_session, user.id)

    with pytest.raises(InvalidUserDataError):
        reset_password(db_session, user, bad_password)

    assert _stored_credentials(db_session, user.id) == before


def test_reset_password_token_failure_keeps_old_password(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The new password is never committed if invalidation fails."""

    user = _create(db_session)
    user_id = user.id
    before = _stored_credentials(db_session, user_id)

    def _failing_invalidate(*_args, **_kwargs):
        raise OperationalError("UPDATE users", {}, Exception("database is locked"))

    monkeypatch.setattr(
        user_service,
        "invalidate_user_sessions",
        _failing_invalidate,
    )

    with pytest.raises(OperationalError):
        reset_password(db_session, user, NEW_PASSWORD)

    assert _stored_credentials(db_session, user_id) == before


def test_reset_password_write_failure_never_increments_token_version(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the password UPDATE fails, token invalidation is never attempted."""

    user = _create(db_session)
    user_id = user.id
    before = _stored_credentials(db_session, user_id)

    db_session.execute(
        text(
            "CREATE TRIGGER block_password_update "
            "BEFORE UPDATE OF password_hash ON users "
            "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"
        )
    )
    db_session.commit()

    invalidations: list[None] = []
    real_invalidate = user_service.invalidate_user_sessions

    def _spy(*args, **kwargs):
        invalidations.append(None)
        return real_invalidate(*args, **kwargs)

    monkeypatch.setattr(user_service, "invalidate_user_sessions", _spy)

    with pytest.raises(IntegrityError):
        reset_password(db_session, user, NEW_PASSWORD)

    assert invalidations == []
    assert _stored_credentials(db_session, user_id) == before


def test_reset_password_commit_false_leaves_transaction_to_caller(
    db_session: Session,
) -> None:
    user = _create(db_session)
    user_id = user.id
    before = _stored_credentials(db_session, user_id)

    assert reset_password(db_session, user, NEW_PASSWORD, commit=False) == 1
    assert db_session.in_transaction()

    db_session.rollback()

    assert _stored_credentials(db_session, user_id) == before

    reset_password(db_session, user, NEW_PASSWORD, commit=False)
    db_session.commit()

    password_hash, token_version = _stored_credentials(db_session, user_id)

    assert token_version == 1
    assert verify_password(NEW_PASSWORD, password_hash)


def test_reset_password_for_deleted_user_raises_and_rolls_back(
    db_session: Session,
) -> None:
    user = _create(db_session)

    db_session.execute(
        text("DELETE FROM users WHERE id = :id"),
        {"id": user.id},
    )

    with pytest.raises(UserNotFoundError):
        reset_password(db_session, user, NEW_PASSWORD)

    # The rollback restored the row the transaction had deleted.
    assert _user_count(db_session) == 1


# ---------------------------------------------------------------------------
# SQL parameters never appear in error messages
# ---------------------------------------------------------------------------


def test_application_engine_hides_sql_parameters() -> None:
    assert app_database.engine.hide_parameters is True


def test_hidden_parameters_keep_hash_out_of_error_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With hide_parameters=True, a failed INSERT does not reveal the hash."""

    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        hide_parameters=True,
    )
    Base.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER block_insert BEFORE INSERT ON users "
                "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
            )
        )

    hashes: list[str] = []
    real_hash = user_service.hash_password

    def _recording_hash(password: str) -> str:
        hashes.append(real_hash(password))
        return hashes[-1]

    monkeypatch.setattr(user_service, "hash_password", _recording_hash)

    try:
        with Session(engine) as db:
            with pytest.raises(IntegrityError) as error_info:
                _create(db)
    finally:
        engine.dispose()

    message = str(error_info.value)

    assert hashes, "the failure must happen after the password was hashed"
    assert hashes[0] not in message
    assert "$argon2" not in message
    assert PASSWORD not in message
    assert "parameters hidden" in message


# ---------------------------------------------------------------------------
# Duplicate-email classification
# ---------------------------------------------------------------------------


def _integrity_error(db: Session, sql: str) -> IntegrityError:
    """Run raw SQL that must violate a constraint; return the real error."""

    with pytest.raises(IntegrityError) as error_info:
        db.execute(text(sql))

    db.rollback()

    return error_info.value


def _insert_sql(email: str, user_id: str = "NULL") -> str:
    return (
        "INSERT INTO users (id, name, email, password_hash, role, is_active, "
        "token_version, created_at) VALUES "
        f"({user_id}, 'N', {email}, 'h', 'agent', 1, 0, '2026-11-19')"
    )


def test_real_unique_violation_on_email_is_a_duplicate(
    db_session: Session,
) -> None:
    _create(db_session, email="taken@example.test")

    error = _integrity_error(db_session, _insert_sql("'taken@example.test'"))

    assert error.orig.sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE"
    assert user_service._is_duplicate_email_error(error) is True


def test_not_null_violation_on_email_is_not_a_duplicate(
    db_session: Session,
) -> None:
    error = _integrity_error(db_session, _insert_sql("NULL"))

    assert error.orig.sqlite_errorname == "SQLITE_CONSTRAINT_NOTNULL"
    assert "users.email" in str(error.orig)  # the old substring check matched
    assert user_service._is_duplicate_email_error(error) is False


def test_primary_key_violation_is_not_a_duplicate(db_session: Session) -> None:
    user = _create(db_session)

    error = _integrity_error(
        db_session,
        _insert_sql("'other@example.test'", user_id=str(user.id)),
    )

    assert error.orig.sqlite_errorname == "SQLITE_CONSTRAINT_PRIMARYKEY"
    assert user_service._is_duplicate_email_error(error) is False


def test_message_without_structured_code_is_not_a_duplicate() -> None:
    """The exact message alone is not trusted without the UNIQUE code."""

    error = IntegrityError(
        "INSERT INTO users",
        None,
        sqlite3.IntegrityError("UNIQUE constraint failed: users.email"),
    )

    assert user_service._is_duplicate_email_error(error) is False


def test_not_null_failure_during_create_keeps_its_meaning_and_rolls_back(
    db_session: Session,
) -> None:
    """A caller's pending row with no email fails the shared flush."""

    db_session.add(
        User(name="Pending", email=None, password_hash="h", role="agent")
    )

    with pytest.raises(IntegrityError) as error_info:
        _create(db_session, email="new@example.test")

    assert not isinstance(error_info.value, UserAlreadyExistsError)
    assert error_info.value.orig.sqlite_errorname == "SQLITE_CONSTRAINT_NOTNULL"

    # commit=True: the whole transaction, including the new user, is gone.
    assert not db_session.new
    assert _user_count(db_session) == 0


def test_unrelated_integrity_error_during_create_is_propagated(
    db_session: Session,
) -> None:
    """A foreign-key failure from a caller's pending ticket is not a duplicate."""

    db_session.add(
        Ticket(
            title="Orphan",
            description="References a user that does not exist.",
            created_by_user_id=9999,
        )
    )

    with pytest.raises(IntegrityError) as error_info:
        _create(db_session, email="new@example.test")

    assert not isinstance(error_info.value, UserAlreadyExistsError)
    assert (
        error_info.value.orig.sqlite_errorname == "SQLITE_CONSTRAINT_FOREIGNKEY"
    )
    assert _user_count(db_session) == 0


def test_genuine_duplicate_during_create_still_maps_and_rolls_back(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create(db_session, email="race@example.test")
    monkeypatch.setattr(user_service, "email_is_registered", lambda *_: False)

    with pytest.raises(UserAlreadyExistsError) as error_info:
        _create(db_session, email="race@example.test")

    cause = error_info.value.__cause__

    assert isinstance(cause, IntegrityError)
    assert cause.orig.sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE"
    assert _user_count(db_session) == 1


# ---------------------------------------------------------------------------
# reset_password(): commit failure
# ---------------------------------------------------------------------------


def test_reset_password_commit_failure_persists_nothing(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _create(db_session)
    user_id = user.id
    before = _stored_credentials(db_session, user_id)
    token = create_access_token(subject=str(user_id), token_version=0)

    # Record the state inside the transaction once BOTH updates are
    # prepared: the password flushed and the token version incremented.
    prepared: list[tuple[str, int]] = []
    real_invalidate = user_service.invalidate_user_sessions

    def _spy_invalidate(db, target, *, commit):
        version = real_invalidate(db, target, commit=commit)
        prepared.append(_stored_credentials(db, user_id))
        return version

    rollbacks: list[None] = []
    real_rollback = db_session.rollback

    def _rollback() -> None:
        rollbacks.append(None)
        real_rollback()

    def _failing_commit() -> None:
        driver_error = sqlite3.OperationalError("database is locked")
        driver_error.sqlite_errorname = "SQLITE_BUSY"
        raise OperationalError("COMMIT", None, driver_error)

    monkeypatch.setattr(user_service, "invalidate_user_sessions", _spy_invalidate)
    monkeypatch.setattr(db_session, "rollback", _rollback)
    monkeypatch.setattr(db_session, "commit", _failing_commit)

    with pytest.raises(OperationalError):
        reset_password(db_session, user, NEW_PASSWORD)

    # Both changes had been prepared in the transaction...
    assert len(prepared) == 1
    prepared_hash, prepared_version = prepared[0]
    assert prepared_hash != before[0]
    assert verify_password(NEW_PASSWORD, prepared_hash)
    assert prepared_version == 1

    # ...and the failed commit was rolled back, persisting neither.
    assert len(rollbacks) == 1
    assert _stored_credentials(db_session, user_id) == before

    stored_hash, stored_version = _stored_credentials(db_session, user_id)
    assert verify_password(PASSWORD, stored_hash)
    assert not verify_password(NEW_PASSWORD, stored_hash)
    assert stored_version == 0

    # A token issued before the attempted reset is still valid.
    assert get_active_user_from_token(db_session, token) is not None


def test_trigger_error_is_not_a_duplicate_even_with_the_same_message(
    db_session: Session,
) -> None:
    """A trigger can raise any message; only the structured code counts."""

    db_session.execute(
        text(
            "CREATE TRIGGER imitate_duplicate BEFORE INSERT ON users "
            "BEGIN SELECT RAISE(ABORT, "
            "'UNIQUE constraint failed: users.email'); END"
        )
    )
    db_session.commit()

    error = _integrity_error(db_session, _insert_sql("'new@example.test'"))

    assert error.orig.sqlite_errorname == "SQLITE_CONSTRAINT_TRIGGER"
    assert str(error.orig) == "UNIQUE constraint failed: users.email"
    assert user_service._is_duplicate_email_error(error) is False

    # Through create_user() the original IntegrityError is propagated.
    with pytest.raises(IntegrityError) as error_info:
        _create(db_session, email="another@example.test")

    assert not isinstance(error_info.value, UserAlreadyExistsError)
    assert _user_count(db_session) == 0


# ---------------------------------------------------------------------------
# Operation AND rollback fail: the original failure stays primary (C)
# ---------------------------------------------------------------------------


FAKE_HASH = "$argon2id$v=19$m=65536,t=3,p=4$ZmFrZS1zYWx0$ZmFrZS1oYXNoLXZhbHVl"
RAW_DRIVER_TEXT = "driver detail leaked"


def _secret_error(statement: str, code: str) -> OperationalError:
    """SQL parameters and the driver message both carry the fake hash."""

    driver_error = sqlite3.OperationalError(f"{RAW_DRIVER_TEXT}: {FAKE_HASH}")
    driver_error.sqlite_errorname = code

    return OperationalError(statement, (FAKE_HASH,), driver_error)


def _assert_safe_description(description: str) -> None:
    assert FAKE_HASH not in description
    assert "$argon2" not in description
    assert RAW_DRIVER_TEXT not in description
    assert "INSERT" not in description
    assert "ROLLBACK" not in description


def _fail_rollback(db: Session, monkeypatch: pytest.MonkeyPatch) -> OperationalError:
    rollback_error = _secret_error("ROLLBACK", "SQLITE_IOERR")

    def _failing_rollback() -> None:
        raise rollback_error

    monkeypatch.setattr(db, "rollback", _failing_rollback)

    return rollback_error


def test_create_user_keeps_commit_failure_primary_when_rollback_fails(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit_error = _secret_error("COMMIT", "SQLITE_BUSY")

    def _failing_commit() -> None:
        raise commit_error

    monkeypatch.setattr(db_session, "commit", _failing_commit)
    rollback_error = _fail_rollback(db_session, monkeypatch)

    with pytest.raises(OperationalError) as error_info:
        _create(db_session)

    assert error_info.value is commit_error
    assert rollback_failure_of(error_info.value) is rollback_error

    primary = describe_database_error(error_info.value)
    secondary = describe_database_error(rollback_failure_of(error_info.value))

    assert primary == "OperationalError: the database is locked [SQLITE_BUSY]"
    assert secondary == "OperationalError: a disk I/O error occurred [SQLITE_IOERR]"
    _assert_safe_description(primary)
    _assert_safe_description(secondary)


def test_duplicate_found_by_lookup_stays_duplicate_when_rollback_fails(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create(db_session, email="taken@example.test")
    rollback_error = _fail_rollback(db_session, monkeypatch)

    with pytest.raises(UserAlreadyExistsError) as error_info:
        _create(db_session, email="taken@example.test")

    assert rollback_failure_of(error_info.value) is rollback_error


def test_duplicate_from_unique_index_stays_duplicate_when_rollback_fails(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create(db_session, email="race@example.test")
    monkeypatch.setattr(user_service, "email_is_registered", lambda *_: False)
    rollback_error = _fail_rollback(db_session, monkeypatch)

    with pytest.raises(UserAlreadyExistsError) as error_info:
        _create(db_session, email="race@example.test")

    assert isinstance(error_info.value.__cause__, IntegrityError)
    assert rollback_failure_of(error_info.value) is rollback_error


def test_reset_password_keeps_operation_failure_primary_when_rollback_fails(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _create(db_session)
    invalidate_error = _secret_error("UPDATE users", "SQLITE_IOERR_WRITE")

    def _failing_invalidate(*_args, **_kwargs):
        raise invalidate_error

    monkeypatch.setattr(user_service, "invalidate_user_sessions", _failing_invalidate)
    rollback_error = _fail_rollback(db_session, monkeypatch)

    with pytest.raises(OperationalError) as error_info:
        reset_password(db_session, user, NEW_PASSWORD)

    assert error_info.value is invalidate_error
    assert rollback_failure_of(error_info.value) is rollback_error

    _assert_safe_description(describe_database_error(error_info.value))
    _assert_safe_description(
        describe_database_error(rollback_failure_of(error_info.value))
    )


def test_reset_password_missing_user_stays_not_found_when_rollback_fails(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _create(db_session)
    db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
    rollback_error = _fail_rollback(db_session, monkeypatch)

    with pytest.raises(UserNotFoundError) as error_info:
        reset_password(db_session, user, NEW_PASSWORD)

    assert rollback_failure_of(error_info.value) is rollback_error


# ---------------------------------------------------------------------------
# Real driver: SQLAlchemy's own internal rollback fails (C)
# ---------------------------------------------------------------------------


@pytest.fixture
def failing_rollback_engine(tmp_path):
    """A file database whose real sqlite3 rollback fails on demand."""

    class FailingRollbackConnection(sqlite3.Connection):
        failing = False

        def rollback(self) -> None:
            if type(self).failing:
                error = sqlite3.OperationalError(
                    f"{RAW_DRIVER_TEXT} on rollback: {FAKE_HASH}"
                )
                error.sqlite_errorname = "SQLITE_IOERR"
                raise error

            super().rollback()

    engine = create_engine(
        f"sqlite:///{tmp_path / 'driver.db'}",
        connect_args={"factory": FailingRollbackConnection},
    )
    Base.metadata.create_all(engine)

    yield engine, FailingRollbackConnection

    FailingRollbackConnection.failing = False
    engine.dispose()


def test_genuine_duplicate_is_not_converted_into_a_rollback_error(
    failing_rollback_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLAlchemy raises its failed rollback; the duplicate must survive."""

    engine, connection_class = failing_rollback_engine

    with Session(engine) as db:
        _create(db, email="race@example.test")

    monkeypatch.setattr(user_service, "email_is_registered", lambda *_: False)

    with Session(engine) as db:
        connection_class.failing = True

        with pytest.raises(UserAlreadyExistsError) as error_info:
            _create(db, email="race@example.test")

        connection_class.failing = False

    cause = error_info.value.__cause__
    rollback_failure = rollback_failure_of(error_info.value)

    assert isinstance(cause, IntegrityError)
    assert cause.orig.sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE"
    assert rollback_failure is not None
    assert describe_database_error(rollback_failure) == (
        "OperationalError: a disk I/O error occurred [SQLITE_IOERR]"
    )

    with Session(engine) as db:
        assert db.scalars(select(User.email)).all() == ["race@example.test"]


def test_reset_password_write_failure_stays_primary_with_driver_rollback_failure(
    failing_rollback_engine,
) -> None:
    engine, connection_class = failing_rollback_engine

    with Session(engine) as db:
        user_id = _create(db).id
        db.execute(
            text(
                "CREATE TRIGGER block_password_update "
                "BEFORE UPDATE OF password_hash ON users "
                "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"
            )
        )
        db.commit()

    with Session(engine) as db:
        user = db.get(User, user_id)
        connection_class.failing = True

        with pytest.raises(IntegrityError) as error_info:
            reset_password(db, user, NEW_PASSWORD)

        connection_class.failing = False

    error = error_info.value

    assert error.orig.sqlite_errorname == "SQLITE_CONSTRAINT_TRIGGER"
    assert describe_database_error(rollback_failure_of(error)) == (
        "OperationalError: a disk I/O error occurred [SQLITE_IOERR]"
    )

    with Session(engine) as db:
        stored = db.get(User, user_id)

        assert verify_password(PASSWORD, stored.password_hash)
        assert stored.token_version == 0


# ---------------------------------------------------------------------------
# Phase 7.5: user administration unassigns tickets atomically
# ---------------------------------------------------------------------------


def _staff_user(db: Session, label: str, role: str) -> User:
    user = User(
        name=f"{label} name",
        email=f"{label}@example.test",
        password_hash="unused-hash",
        role=role,
    )
    db.add(user)
    db.commit()

    return user


@pytest.fixture
def staff(db_session: Session) -> dict[str, User]:
    return {
        label: _staff_user(db_session, label, role)
        for label, role in [
            ("admin", "admin"),
            ("agent", "agent"),
            ("other", "agent"),
            ("customer", "customer"),
        ]
    }


def _add_ticket(
    db: Session,
    creator: User,
    *,
    status: str,
    assignee: User | None,
) -> int:
    ticket = Ticket(
        title=f"{status} ticket",
        description="User administration integration test.",
        created_by_user_id=creator.id,
        status=status,
        assigned_to_user_id=None if assignee is None else assignee.id,
    )
    db.add(ticket)
    db.commit()

    return ticket.id


@pytest.fixture
def agent_tickets(db_session: Session, staff: dict[str, User]) -> dict[str, int]:
    """Ticket IDs, created in this order (ascending IDs)."""

    agent, other, creator = staff["agent"], staff["other"], staff["customer"]

    return {
        "in_progress": _add_ticket(db_session, creator, status="In Progress", assignee=agent),
        "resolved": _add_ticket(db_session, creator, status="Resolved", assignee=agent),
        "open": _add_ticket(db_session, creator, status="Open", assignee=agent),
        "others": _add_ticket(db_session, creator, status="Open", assignee=other),
        "unassigned": _add_ticket(db_session, creator, status="Open", assignee=None),
    }


def _ticket_states(db: Session) -> dict[int, tuple]:
    """{ticket id: (assignee, status, version)} straight from SQL."""

    return {
        row[0]: tuple(row[1:])
        for row in db.execute(
            text(
                "SELECT id, assigned_to_user_id, status, version FROM tickets"
            )
        )
    }


def _ticket_event_rows(db: Session) -> list[tuple]:
    return [
        tuple(row)
        for row in db.execute(
            text(
                "SELECT ticket_id, event_type, actor_user_id, from_assignee_id, "
                "to_assignee_id, from_status, to_status, reason "
                "FROM ticket_events ORDER BY id"
            )
        )
    ]


def _user_row(db: Session, user: User) -> tuple:
    return tuple(
        db.execute(
            text("SELECT role, is_active, token_version FROM users WHERE id = :id"),
            {"id": user.id},
        ).one()
    )


def _deactivate(db: Session, staff: dict[str, User], who: str = "agent"):
    return user_service.set_user_active_status(
        db, actor_id=staff["admin"].id, user_id=staff[who].id, is_active=False
    )


def test_deactivating_an_agent_unassigns_their_unresolved_tickets(
    db_session: Session,
    staff: dict[str, User],
    agent_tickets: dict[str, int],
) -> None:
    before = _ticket_states(db_session)
    admin, agent = staff["admin"].id, staff["agent"].id

    change = _deactivate(db_session, staff)

    assert change.changed is True
    # Ascending ticket IDs: processed deterministically.
    assert change.unassigned_ticket_ids == (
        agent_tickets["in_progress"],
        agent_tickets["open"],
    )

    after = _ticket_states(db_session)

    # In Progress returns to Open; Open stays Open; each bumped once.
    assert after[agent_tickets["in_progress"]] == (None, "Open", 2)
    assert after[agent_tickets["open"]] == (None, "Open", 2)

    # Resolved, other agents' and unassigned tickets are untouched.
    for name in ("resolved", "others", "unassigned"):
        assert after[agent_tickets[name]] == before[agent_tickets[name]]

    assert _ticket_event_rows(db_session) == [
        (agent_tickets["in_progress"], "unassigned", admin, agent, None,
         "In Progress", "Open", "assignee_deactivated"),
        (agent_tickets["open"], "unassigned", admin, agent, None,
         None, None, "assignee_deactivated"),
    ]
    # The Phase 6 status change itself is unchanged.
    assert _user_row(db_session, staff["agent"]) == ("agent", 0, 1)


def test_reactivation_does_not_restore_assignments(
    db_session: Session,
    staff: dict[str, User],
    agent_tickets: dict[str, int],
) -> None:
    _deactivate(db_session, staff)
    after_deactivation = _ticket_states(db_session)
    events = _ticket_event_rows(db_session)

    change = user_service.set_user_active_status(
        db_session,
        actor_id=staff["admin"].id,
        user_id=staff["agent"].id,
        is_active=True,
    )

    assert change.changed is True
    assert change.unassigned_ticket_ids == ()
    assert _ticket_states(db_session) == after_deactivation
    assert _ticket_event_rows(db_session) == events
    assert _user_row(db_session, staff["agent"]) == ("agent", 1, 2)


def test_repeated_deactivation_changes_no_ticket(
    db_session: Session,
    staff: dict[str, User],
    agent_tickets: dict[str, int],
) -> None:
    _deactivate(db_session, staff)
    states = _ticket_states(db_session)
    events = _ticket_event_rows(db_session)

    repeated = _deactivate(db_session, staff)

    assert (repeated.changed, repeated.unassigned_ticket_ids) == (False, ())
    assert _ticket_states(db_session) == states
    assert _ticket_event_rows(db_session) == events
    assert _user_row(db_session, staff["agent"]) == ("agent", 0, 1)


def test_deactivating_any_account_leaves_no_unresolved_ticket_with_it(
    db_session: Session,
    staff: dict[str, User],
) -> None:
    """Legacy data: a non-agent holding a ticket (e.g. demoted before 7.5)."""

    second_admin = _staff_user(db_session, "admin2", "admin")
    ticket_id = _add_ticket(
        db_session, staff["customer"], status="Open", assignee=second_admin
    )

    change = _deactivate(db_session, {**staff, "admin2": second_admin}, "admin2")

    assert change.unassigned_ticket_ids == (ticket_id,)
    assert _ticket_states(db_session)[ticket_id] == (None, "Open", 2)


@pytest.mark.parametrize("new_role", ["customer", "admin"])
def test_agent_losing_the_agent_role_unassigns_their_tickets(
    db_session: Session,
    staff: dict[str, User],
    agent_tickets: dict[str, int],
    new_role: str,
) -> None:
    before = _ticket_states(db_session)
    admin, agent = staff["admin"].id, staff["agent"].id

    change = user_service.change_user_role(
        db_session, actor_id=admin, user_id=agent, role=new_role
    )

    assert (change.previous_role, change.new_role) == ("agent", new_role)
    assert change.unassigned_ticket_ids == (
        agent_tickets["in_progress"],
        agent_tickets["open"],
    )

    after = _ticket_states(db_session)

    assert after[agent_tickets["in_progress"]] == (None, "Open", 2)
    assert after[agent_tickets["open"]] == (None, "Open", 2)

    for name in ("resolved", "others", "unassigned"):
        assert after[agent_tickets[name]] == before[agent_tickets[name]]

    assert [row[-1] for row in _ticket_event_rows(db_session)] == [
        "assignee_role_changed",
        "assignee_role_changed",
    ]
    # Role changes still do not increment token_version.
    assert _user_row(db_session, staff["agent"]) == (new_role, 1, 0)


@pytest.mark.parametrize("old_role", ["customer", "admin"])
def test_becoming_an_agent_touches_no_ticket(
    db_session: Session,
    staff: dict[str, User],
    agent_tickets: dict[str, int],
    old_role: str,
) -> None:
    user = _staff_user(db_session, f"becoming-{old_role}", old_role)
    # Legacy data: the user already holds an unresolved ticket.
    held = _add_ticket(db_session, staff["customer"], status="In Progress", assignee=user)
    before = _ticket_states(db_session)

    change = user_service.change_user_role(
        db_session, actor_id=staff["admin"].id, user_id=user.id, role="agent"
    )

    assert (change.changed, change.unassigned_ticket_ids) == (True, ())
    assert _ticket_states(db_session) == before
    assert before[held] == (user.id, "In Progress", 1)
    assert _ticket_event_rows(db_session) == []


@pytest.mark.parametrize(
    ("who", "role"),
    [("agent", "agent"), ("customer", "admin")],
    ids=["repeated-agent-role", "customer-to-admin"],
)
def test_role_changes_that_keep_assignability_touch_no_ticket(
    db_session: Session,
    staff: dict[str, User],
    agent_tickets: dict[str, int],
    who: str,
    role: str,
) -> None:
    before = _ticket_states(db_session)

    change = user_service.change_user_role(
        db_session, actor_id=staff["admin"].id, user_id=staff[who].id, role=role
    )

    assert change.unassigned_ticket_ids == ()
    assert _ticket_states(db_session) == before
    assert _ticket_event_rows(db_session) == []


USER_CHANGES = {
    "deactivate": lambda db, staff: _deactivate(db, staff),
    "demote": lambda db, staff: user_service.change_user_role(
        db, actor_id=staff["admin"].id, user_id=staff["agent"].id, role="customer"
    ),
}


@pytest.mark.parametrize("change", list(USER_CHANGES))
def test_event_failure_rolls_back_the_user_change_and_every_ticket(
    db_session: Session,
    staff: dict[str, User],
    agent_tickets: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    from app.services import ticket_workflow

    tickets_before = _ticket_states(db_session)
    user_before = _user_row(db_session, staff["agent"])
    failure = RuntimeError("event insertion failed")
    calls = []
    real_record = ticket_workflow.record_ticket_event

    def fail_on_second(*args, **kwargs):
        # The first ticket's change and event are already in the session.
        calls.append(kwargs["ticket_id"])

        if len(calls) == 2:
            raise failure

        return real_record(*args, **kwargs)

    monkeypatch.setattr(ticket_workflow, "record_ticket_event", fail_on_second)

    with pytest.raises(RuntimeError) as error:
        USER_CHANGES[change](db_session, staff)

    assert error.value is failure
    assert len(calls) == 2
    assert _ticket_states(db_session) == tickets_before
    assert _ticket_event_rows(db_session) == []
    assert _user_row(db_session, staff["agent"]) == user_before


@pytest.mark.parametrize("change", list(USER_CHANGES))
def test_commit_failure_rolls_back_user_tickets_and_history(
    db_session: Session,
    staff: dict[str, User],
    agent_tickets: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    tickets_before = _ticket_states(db_session)
    user_before = _user_row(db_session, staff["agent"])
    commit_error = OperationalError("COMMIT", None, sqlite3.OperationalError("io"))

    def flush_then_fail() -> None:
        db_session.flush()
        # Everything reached the database inside the transaction...
        assert len(_ticket_event_rows(db_session)) == 2
        raise commit_error

    monkeypatch.setattr(db_session, "commit", flush_then_fail)

    with pytest.raises(OperationalError) as error:
        USER_CHANGES[change](db_session, staff)

    assert error.value is commit_error

    monkeypatch.undo()

    # ...and none of it survived the rollback.
    assert _ticket_states(db_session) == tickets_before
    assert _ticket_event_rows(db_session) == []
    assert _user_row(db_session, staff["agent"]) == user_before


@pytest.mark.parametrize("change", list(USER_CHANGES))
def test_rollback_failure_keeps_the_original_error(
    db_session: Session,
    staff: dict[str, User],
    agent_tickets: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    commit_error = OperationalError("COMMIT", None, sqlite3.OperationalError("io"))
    monkeypatch.setattr(db_session, "commit", _raise_with(commit_error))
    monkeypatch.setattr(
        db_session, "rollback", _raise_with(RuntimeError("rollback failed"))
    )

    with pytest.raises(OperationalError) as error:
        USER_CHANGES[change](db_session, staff)

    assert error.value is commit_error
    assert isinstance(rollback_failure_of(error.value), RuntimeError)

    monkeypatch.undo()
    db_session.rollback()

    assert _ticket_event_rows(db_session) == []


def _raise_with(error: BaseException):
    def _raise(*_args, **_kwargs):
        raise error

    return _raise


def test_many_tickets_are_unassigned_in_id_order_with_one_version_bump(
    db_session: Session,
    staff: dict[str, User],
) -> None:
    statuses = ["Open", "In Progress", "Resolved", "In Progress", "Open", "Open"]
    ids = [
        _add_ticket(db_session, staff["customer"], status=status, assignee=staff["agent"])
        for status in statuses
    ]

    change = _deactivate(db_session, staff)

    unresolved = [i for i, s in zip(ids, statuses) if s != "Resolved"]

    assert list(change.unassigned_ticket_ids) == unresolved
    assert [row[0] for row in _ticket_event_rows(db_session)] == unresolved

    states = _ticket_states(db_session)

    for ticket_id, status in zip(ids, statuses):
        if status == "Resolved":
            assert states[ticket_id] == (staff["agent"].id, "Resolved", 1)
        else:
            assert states[ticket_id] == (None, "Open", 2)


def test_shared_admin_lock_query_is_used_by_both_services() -> None:
    from app.services import ticket_workflow, user_locking

    assert (
        user_service._active_administrators_for_update
        is user_locking.active_administrator_ids_for_update
    )
    assert user_service.lock_active_administrator_ids is (
        user_locking.lock_active_administrator_ids
    )
    assert ticket_workflow.lock_active_administrator_ids is (
        user_locking.lock_active_administrator_ids
    )
    assert not hasattr(ticket_workflow, "_active_administrator_ids_for_update")
