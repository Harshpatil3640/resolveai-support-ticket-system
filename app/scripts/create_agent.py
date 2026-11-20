
import sys
from getpass import getpass

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.roles import UserRole
from app.db.database import engine
from app.scripts.provisioning import (
    database_failure_report,
    database_is_ready,
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


def require_migrated_database() -> None:
    """Stop unless the database this script writes to is at Alembic head.

    The check is read-only. This script never runs migrations: migrating
    is an explicit operator step (see docs/database-migrations.md).
    """

    if not database_is_ready(engine, "create an agent"):
        raise SystemExit(1)


@sanitised_database_logging
def create_agent() -> None:
    """Create an initial support agent interactively."""

    print("\n=== ResolveAI Support Agent Creation ===\n")

    # Verify the schema before asking for any input, in particular the
    # password, so nothing is collected for a database that cannot be used.
    require_migrated_database()

    try:
        name = validate_name(input("Enter agent name: "))
        email = validate_email(input("Enter agent email: "))
    except InvalidUserDataError:
        print("Invalid name or email.")
        return

    password = getpass(
        f"Enter password (minimum {MIN_PASSWORD_LENGTH} characters): "
    )
    confirm_password = getpass("Confirm password: ")

    try:
        validate_password(password)
    except InvalidUserDataError as error:
        print(error)
        return

    if password != confirm_password:
        print("Passwords do not match.")
        return

    with Session(engine) as db:
        try:
            agent = create_user(
                db,
                name=name,
                email=email,
                password=password,
                role=UserRole.AGENT,
            )

        except UserAlreadyExistsError as error:
            # create_user() rolled back; report its rollback failure, if any.
            print(
                "An account with this email already exists."
                + rollback_failure_note(rollback_after_failure(db, error))
            )
            return

        except InvalidUserDataError as error:
            print(error)
            return

        except SQLAlchemyError as error:
            # create_user() has already rolled back any uncommitted work.
            # str(error) may contain the password hash as a bound
            # parameter, so only a sanitised description is printed. The
            # original failure stays primary; a failed rollback is reported
            # once, as a separate diagnostic.
            detail, rollback_failure = database_failure_report(db, error)
            note = rollback_failure_note(rollback_failure)
            print(
                "Could not create the agent because of a database error "
                f"({detail}).{note} Check whether the account exists before "
                "retrying.",
                file=sys.stderr,
            )
            raise SystemExit(1) from None

        print("\nAgent created successfully!")
        print(f"Agent ID: {agent.id}")
        print(f"Name: {agent.name}")
        print(f"Email: {agent.email}")
        print(f"Role: {agent.role}")
        print(f"Active: {agent.is_active}")


if __name__ == "__main__":
    create_agent()
