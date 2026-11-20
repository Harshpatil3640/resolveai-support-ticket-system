"""Reset a support agent's password from the local command line.

Usage::

    python -m app.scripts.reset_agent_password

Only accounts with the agent role can be reset with this script. The new
password is saved and every existing session for the account is signed
out (its token version is incremented) in a single transaction: either
both changes are committed or neither is.

The script never runs migrations. The database must already be at the
Alembic head revision (see docs/database-migrations.md).
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
    rollback_after_failure,
    rollback_failure_note,
    sanitised_database_logging,
)
from app.services.auth_service import UserNotFoundError
from app.services.user_service import (
    MIN_PASSWORD_LENGTH,
    InvalidUserDataError,
    normalize_email,
    reset_password,
    validate_password,
)


@sanitised_database_logging
def reset_agent_password() -> None:
    print("\n=== ResolveAI Agent Password Reset ===\n")

    # Verify the schema before asking for any input.
    if not database_is_ready(engine, "reset an agent password"):
        raise SystemExit(1)

    email = normalize_email(input("Enter the agent's registered email: "))

    if not email:
        print("Email cannot be empty.")
        return

    with Session(engine) as db:
        # Case-insensitive, so accounts stored before email normalisation
        # (with capital letters) are found too.
        try:
            matches = db.scalars(
                select(User).where(func.lower(User.email) == email)
            ).all()
        except SQLAlchemyError as error:
            # Nothing was written. Only a sanitised description is shown:
            # never the SQL, its parameters or the driver's message. The
            # rollback is guarded: a failing rollback must not raise here.
            detail, rollback_failure = database_failure_report(db, error)
            note = rollback_failure_note(rollback_failure)
            print(
                "Could not look up the account because of a database error "
                f"({detail}).{note}",
                file=sys.stderr,
            )
            raise SystemExit(1) from None

        if not matches:
            print("No account found with this email.")
            return

        if len(matches) > 1:
            print(
                "More than one account matches this email when case is "
                "ignored. Resolve the duplicate accounts before resetting."
            )
            return

        agent = matches[0]

        if agent.role != UserRole.AGENT.value:
            print("This account is not registered with the agent role.")
            return

        print(f"\nAccount found: {agent.name} ({agent.email})")

        new_password = getpass(
            f"Enter new password (minimum {MIN_PASSWORD_LENGTH} characters): "
        )
        confirm_password = getpass("Confirm new password: ")

        try:
            validate_password(new_password)
        except InvalidUserDataError as error:
            print(error)
            return

        if new_password != confirm_password:
            print("Passwords do not match.")
            return

        try:
            reset_password(db, agent, new_password)

        except (SQLAlchemyError, UserNotFoundError) as error:
            # reset_password() has rolled back (or recorded why it could
            # not): nothing was committed, so neither the password nor the
            # token version changed. str(error) may contain the new
            # password hash as a bound parameter, so it is never printed.
            # The original failure stays primary; a failed rollback is
            # reported once, as a separate diagnostic.
            if isinstance(error, SQLAlchemyError):
                detail, rollback_failure = database_failure_report(db, error)
            else:
                detail = "the account no longer exists"
                rollback_failure = rollback_after_failure(db, error)

            print(
                f"Password reset failed ({detail})."
                f"{rollback_failure_note(rollback_failure)} The password and "
                "existing sessions were left unchanged.",
                file=sys.stderr,
            )
            raise SystemExit(1) from None

        print("\nPassword successfully reset!")
        print("All existing sessions for this account have been signed out.")


if __name__ == "__main__":
    reset_agent_password()
