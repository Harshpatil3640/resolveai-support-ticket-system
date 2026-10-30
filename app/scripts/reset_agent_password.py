
from getpass import getpass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.database import engine
from app.models import User


def reset_agent_password() -> None:
    print("\n=== ResolveAI Agent Password Reset ===\n")

    email = input("Enter the agent's registered email: ").strip().lower()

    if not email:
        print("Email cannot be empty.")
        return

    with Session(engine) as db:
        agent = db.scalar(
            select(User).where(User.email == email)
        )

        if agent is None:
            print("No account found with this email.")
            return

        if agent.role != "agent":
            print("This account is not registered with the agent role.")
            return

        print(f"\nAccount found: {agent.name} ({agent.email})")

        new_password = getpass("Enter new password (minimum 12 characters): ")
        confirm_password = getpass("Confirm new password: ")

        if len(new_password) < 12:
            print("Password must contain at least 12 characters.")
            return

        if new_password != confirm_password:
            print("Passwords do not match.")
            return

        agent.password_hash = hash_password(new_password)

        try:
            db.commit()
            print("\nPassword successfully reset!")
        except Exception:
            db.rollback()
            print("Password reset failed. Please check the error and try again.")


if __name__ == "__main__":
    reset_agent_password()
