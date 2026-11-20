"""Create the FIRST ResolveAI administrator from the local command line.

Usage::

    python -m app.scripts.create_admin

This is a one-time bootstrap. It refuses to run once any administrator
account exists (active or inactive); further administrators are created
by an existing administrator through the admin-only API. It is
deliberately not exposed over HTTP, so creating the first administrator
requires local access to the server and its database.

The script never runs migrations. The database must already be at the
Alembic head revision (see docs/database-migrations.md).

Concurrency (SQLite only): administrators are counted once before the
prompts and again after the new row is inserted, inside the same write
transaction, and the insert is committed only if exactly one
administrator exists. Two bootstraps running at the same time therefore
create at most one administrator *because SQLite allows only one writer
at a time*: the second INSERT waits until the first transaction commits,
and its re-count then sees both rows. This guarantee is specific to the
current SQLite deployment. It is NOT guaranteed on PostgreSQL or other
databases, where concurrent transactions can each see only their own
uncommitted row; moving to such a database requires an explicit lock or
constraint for this bootstrap.

If the database is locked for longer than SQLite's busy timeout, or any
other database error occurs, the transaction is rolled back and a short
diagnostic is printed. SQL parameters, passwords and hashes are never
printed.

Exit codes: 0 when the administrator was created, 1 otherwise.
"""

import sys
from getpass import getpass

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.roles import UserRole
from app.db.database import engine
from app.models import User
from app.scripts.provisioning import (
    database_failure_report,
    database_is_ready,
    describe_database_error,
    rollback_after_failure,
    rollback_failure_note,
    sanitised_database_logging,
)
from app.services.user_service import (
    MIN_PASSWORD_LENGTH,
    InvalidUserDataError,
    UserAlreadyExistsError,
    create_user,
    validate_email,
    validate_name,
    validate_password,
)


ADMIN_EXISTS_MESSAGE = (
    "An administrator account already exists, so the first-administrator "
    "bootstrap is disabled. Additional administrators must be created by "
    "an existing administrator through the admin-only API."
)


def count_administrators(db: Session) -> int:
    """Count administrator accounts, including inactive ones."""

    return db.scalar(
        select(func.count())
        .select_from(User)
        .where(func.lower(User.role) == UserRole.ADMIN.value)
    )


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


@sanitised_database_logging
def create_admin() -> int:
    """Interactively create the first administrator. Returns an exit code."""

    print("\n=== ResolveAI First Administrator Setup ===\n")

    # Both checks run before any input is requested, in particular before
    # the password, and neither writes anything.
    if not database_is_ready(engine, "create the first administrator"):
        return 1

    try:
        with Session(engine) as db:
            if count_administrators(db) > 0:
                return _fail(ADMIN_EXISTS_MESSAGE)
    except SQLAlchemyError as error:
        return _fail(
            "Could not check for existing administrators because of a "
            f"database error ({describe_database_error(error)})."
        )

    try:
        name = validate_name(input("Enter administrator name: "))
        email = validate_email(input("Enter administrator email: "))
    except InvalidUserDataError:
        return _fail("Invalid name or email.")

    password = getpass(
        f"Enter password (minimum {MIN_PASSWORD_LENGTH} characters): "
    )
    confirm_password = getpass("Confirm password: ")

    try:
        validate_password(password)
    except InvalidUserDataError as error:
        return _fail(str(error))

    if password != confirm_password:
        return _fail("Passwords do not match.")

    # expire_on_commit=False keeps the flushed values readable after the
    # commit, so nothing can fail (or touch the database) after it.
    with Session(engine, expire_on_commit=False) as db:
        try:
            admin = create_user(
                db,
                name=name,
                email=email,
                password=password,
                role=UserRole.ADMIN,
                commit=False,
            )

            # Re-check inside the same write transaction: if another
            # administrator was created after the first check, this
            # insert must not be committed.
            if count_administrators(db) != 1:
                return _fail(
                    ADMIN_EXISTS_MESSAGE
                    + rollback_failure_note(rollback_after_failure(db))
                )

            db.commit()

        # Every rollback below goes through rollback_after_failure(), which
        # never raises a database error: a failing rollback inside these
        # handlers would otherwise print a traceback chaining the original
        # exception, whose text can contain SQL, parameters or driver text.
        # The original failure stays primary; a failed rollback (recorded
        # by the service or SQLAlchemy, or this one) is reported once.

        except UserAlreadyExistsError as error:
            return _fail(
                "An account with this email already exists."
                + rollback_failure_note(rollback_after_failure(db, error))
            )

        except InvalidUserDataError as error:
            return _fail(
                str(error)
                + rollback_failure_note(rollback_after_failure(db, error))
            )

        except SQLAlchemyError as error:
            # Includes a lock held past SQLite's busy timeout by a
            # concurrent bootstrap. str(error) may contain the password
            # hash as a bound parameter, so it is never printed.
            detail, rollback_failure = database_failure_report(db, error)

            if rollback_failure is None:
                outcome = (
                    "The transaction was rolled back and no administrator "
                    "was created."
                )
            else:
                outcome = (
                    f"The rollback also failed ({rollback_failure}); nothing "
                    "was committed, so no administrator was created."
                )

            return _fail(
                "Could not create the administrator because of a database "
                f"error ({detail}). {outcome}"
            )

        except Exception:
            rollback_after_failure(db)
            raise

        print("\nAdministrator created successfully!")
        print(f"ID: {admin.id}")
        print(f"Name: {admin.name}")
        print(f"Email: {admin.email}")
        print(f"Role: {admin.role}")

    return 0


if __name__ == "__main__":
    raise SystemExit(create_admin())
