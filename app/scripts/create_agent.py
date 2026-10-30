
from getpass import getpass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.base import Base
from app.db.database import engine
from app.models import User


def create_agent() -> None:
    """Create an initial support agent interactively."""

    print("\n=== ResolveAI Support Agent Creation ===\n")

    name = input("Enter agent name: ").strip()
    email = input("Enter agent email: ").strip().lower()

    if not name or not email or "@" not in email:
        print("Invalid name or email.")
        return

    password = getpass("Enter password (minimum 12 characters): ")
    confirm_password = getpass("Confirm password: ")

    if len(password) < 12:
        print("Password must contain at least 12 characters.")
        return

    if password != confirm_password:
        print("Passwords do not match.")
        return

    # Ensure all registered models have their tables.
    Base.metadata.create_all(bind=engine)

    with Session(engine) as db:

        existing_user = db.scalar(
            select(User).where(User.email == email)
        )

        if existing_user:
            print("An account with this email already exists.")
            return

        agent = User(
            name=name,
            email=email,
            password_hash=hash_password(password),
            role="agent",
            is_active=True,
        )

        db.add(agent)

        try:
            db.commit()
            db.refresh(agent)

        except IntegrityError:
            db.rollback()
            print("Could not create agent. Check for duplicate email.")
            return

        print("\nAgent created successfully!")
        print(f"Agent ID: {agent.id}")
        print(f"Name: {agent.name}")
        print(f"Email: {agent.email}")
        print(f"Role: {agent.role}")
        print(f"Active: {agent.is_active}")


if __name__ == "__main__":
    create_agent()
