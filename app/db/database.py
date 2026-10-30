from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker


# Locate the root directory of the project
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Define the SQLite database file location
DATABASE_FILE = PROJECT_ROOT / "resolveai.db"

# Create the database connection URL
DATABASE_URL = URL.create(
    drivername="sqlite",
    database=str(DATABASE_FILE),
)

# Create the SQLAlchemy engine
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

@event.listens_for(engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

# Create a session factory
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


# Provide a database session to application functions
def get_db() -> Generator[Session, None, None]:

    db = SessionLocal()

    try:
        yield db

    finally:
        db.close()