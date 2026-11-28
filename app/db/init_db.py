
from app.db.database import DATABASE_FILE
from app.db.migrations import upgrade_to_head


def create_tables() -> None:
    """Bring the application database up to the latest migration.

    The schema is managed by Alembic; ``Base.metadata.create_all`` is no
    longer used because it would create tables without recording a
    migration revision. A pre-Alembic database is refused by the baseline
    migration and must be verified and stamped first
    (see docs/database-migrations.md).
    """

    upgrade_to_head()


if __name__ == "__main__":
    create_tables()

    print("Database initialized successfully.")
    if DATABASE_FILE is not None:
        print(f"Database location: {DATABASE_FILE}")
    else:
        # The URL is not printed: it may contain a password.
        print("Database location: the configured DATABASE_URL.")
