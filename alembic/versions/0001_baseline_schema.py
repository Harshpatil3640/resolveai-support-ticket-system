"""Baseline schema.

Describes the database exactly as it existed before Alembic was
introduced (previously created with ``Base.metadata.create_all``).

* New, empty databases: ``alembic upgrade head`` runs this migration.
* Existing databases: do NOT run this migration. Verify the schema with
  ``python -m app.db.check_baseline`` and stamp it at this revision.
  See docs/database-migrations.md.

When run against a live database, the migration refuses to start if the
database already contains ANY table other than SQLite's internal tables
and Alembic's version table, so it can only initialise an empty database.

Offline SQL generation (``alembic upgrade ... --sql``) has no database
connection and therefore CANNOT perform this check. Never apply offline
baseline SQL to an existing database.

Revision ID: 0001
Revises:
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op


revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _refuse_non_empty_database() -> None:
    """Fail loudly unless the target database has no application tables.

    Only two kinds of table are tolerated:

    * SQLite's internal tables (``sqlite_*``, e.g. ``sqlite_sequence`` or
      ``sqlite_stat1``). SQLAlchemy's SQLite inspector never reports them,
      and SQLite reserves the prefix, so no user table can be hidden this way.
    * Alembic's own version table, which Alembic creates (inside the same
      transaction) before running the first migration.

    Raising here aborts the migration transaction, so the database is left
    exactly as it was, including the version table.

    Offline mode has no connection to inspect, so the check is skipped
    there; see the module docstring.
    """

    if context.is_offline_mode():
        return

    version_table = op.get_context().version_table

    existing = sorted(
        name
        for name in sa.inspect(op.get_bind()).get_table_names()
        if name != version_table
    )

    if existing:
        raise RuntimeError(
            "The baseline migration only initialises an empty database, but "
            f"this database already contains tables: {', '.join(existing)}. "
            "Nothing was changed. If this is an existing ResolveAI database, "
            "back it up, verify it with 'python -m app.db.check_baseline' and "
            "stamp the baseline with 'python -m app.db.check_baseline --stamp' "
            "before running 'alembic upgrade head'. Otherwise, point Alembic "
            "at a new, empty database file "
            "(see docs/database-migrations.md)."
        )


def upgrade() -> None:
    _refuse_non_empty_database()

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("requester_name", sa.String(length=100), nullable=True),
        sa.Column("predicted_category", sa.String(length=100), nullable=True),
        sa.Column("category", sa.String(length=100), nullable=True),
        sa.Column("prediction_confidence", sa.Float(), nullable=True),
        sa.Column("priority", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "priority IN ('Low', 'Medium', 'High')",
            name="ck_tickets_priority",
        ),
        sa.CheckConstraint(
            "status IN ('Open', 'In Progress', 'Resolved')",
            name="ck_tickets_status",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tickets_created_by_user_id",
        "tickets",
        ["created_by_user_id"],
        unique=False,
    )

    op.create_table(
        "revoked_tokens",
        sa.Column("jti", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("jti"),
    )
    op.create_index(
        "ix_revoked_tokens_expires_at",
        "revoked_tokens",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_revoked_tokens_expires_at", table_name="revoked_tokens")
    op.drop_table("revoked_tokens")

    op.drop_index("ix_tickets_created_by_user_id", table_name="tickets")
    op.drop_table("tickets")

    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
