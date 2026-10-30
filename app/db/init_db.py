
from app import models
from app.db.base import Base
from app.db.database import DATABASE_FILE, engine


def create_tables() -> None:
    """Create database tables if they do not already exist."""
    Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    create_tables()

    print("Database initialized successfully.")
    print(f"Database location: {DATABASE_FILE}")
    print(f"Created tables: {list(Base.metadata.tables.keys())}")
