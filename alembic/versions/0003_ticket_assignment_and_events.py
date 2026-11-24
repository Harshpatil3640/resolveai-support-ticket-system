"""Add ticket assignment columns and the ticket_events history table.

* ``tickets.assigned_to_user_id``: nullable, foreign key to ``users.id``
  (``fk_tickets_assigned_to_user_id_users``), indexed
  (``ix_tickets_assigned_to_user_id``).
* ``tickets.assigned_at``: nullable timestamp.
* ``tickets.version``: optimistic-concurrency counter, NOT NULL, server
  default 1, so every existing ticket starts at version 1.
* ``ticket_events``: persistent ticket activity history. Its ticket
  foreign key uses ``ON DELETE CASCADE`` so deleting a ticket still works.

No existing row is changed: tickets keep their values (an "In Progress"
ticket without an assignee stays exactly as it is), the new columns are
NULL or 1, and no history is generated for existing tickets.

SQLite strategy. SQLite cannot add a foreign key with ALTER TABLE, and
Alembic refuses to try, so ``tickets`` is rebuilt with batch mode (create
a new table, copy every row, drop the old one, rename). The rebuild uses
an explicit definition of the table (``copy_from``) instead of reflecting
it, so every column, both CHECK constraints, the existing foreign key and
index are recreated exactly. Before rebuilding, the live table's columns
are checked against that definition; if they differ, the migration stops
without changing anything, so a column the definition does not know about
can never be lost. (Offline ``--sql`` generation has no connection, so it
cannot perform this check.)

Order matters when foreign keys are enforced: ``tickets`` is rebuilt
before ``ticket_events`` (which references it) exists, and on downgrade
``ticket_events`` is dropped before ``tickets`` is rebuilt.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ASSIGNEE_FK = "fk_tickets_assigned_to_user_id_users"
ASSIGNEE_INDEX = "ix_tickets_assigned_to_user_id"
EVENTS_INDEX = "ix_ticket_events_ticket_id_created_at_id"

STATUSES = "('Open', 'In Progress', 'Resolved')"

# Columns of tickets at revision 0002, in table order.
_COLUMNS_0002 = (
    "id",
    "title",
    "description",
    "requester_name",
    "predicted_category",
    "category",
    "prediction_confidence",
    "priority",
    "status",
    "created_by_user_id",
    "created_at",
    "updated_at",
)

_NEW_COLUMNS = ("assigned_to_user_id", "assigned_at", "version")


def _tickets_table(*, with_assignment: bool) -> sa.Table:
    """The tickets table at revision 0002 or 0003, defined explicitly.

    Mirrors 0001 (tickets has not changed since) plus, for 0003, the
    assignment columns, foreign key and index. Used as batch mode's
    ``copy_from`` so nothing depends on reflecting CHECK constraints.
    """

    columns: list[sa.SchemaItem] = [
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
    ]

    if with_assignment:
        columns += [
            sa.Column("assigned_to_user_id", sa.Integer(), nullable=True),
            sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "version",
                sa.Integer(),
                server_default=sa.text("1"),
                nullable=False,
            ),
        ]

    constraints: list[sa.SchemaItem] = [
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "priority IN ('Low', 'Medium', 'High')",
            name="ck_tickets_priority",
        ),
        sa.CheckConstraint(
            f"status IN {STATUSES}",
            name="ck_tickets_status",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.Index("ix_tickets_created_by_user_id", "created_by_user_id"),
    ]

    if with_assignment:
        constraints += [
            sa.ForeignKeyConstraint(
                ["assigned_to_user_id"],
                ["users.id"],
                name=ASSIGNEE_FK,
            ),
            sa.Index(ASSIGNEE_INDEX, "assigned_to_user_id"),
        ]

    return sa.Table("tickets", sa.MetaData(), *columns, *constraints)


def _require_ticket_columns(expected: tuple[str, ...]) -> None:
    """Stop, changing nothing, unless tickets has exactly these columns.

    The rebuild copies only the columns of the explicit definition, so an
    unexpected column would otherwise be dropped with its data. Column
    names come from the database, so only counts are reported.
    """

    # Offline (--sql) generation has no connection to inspect. Read from
    # the migration context, which is also set when these operations are
    # run directly through alembic.operations.Operations.
    if op.get_context().as_sql:
        return

    found = [
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("tickets")
    ]

    if sorted(found) != sorted(expected):
        missing = len(set(expected) - set(found))
        unexpected = len(set(found) - set(expected))
        raise RuntimeError(
            "Migration 0003 stopped: the tickets table does not have the "
            f"expected columns ({missing} missing, {unexpected} unexpected; "
            "names not shown). Nothing was changed. Verify the database "
            "schema (see docs/database-migrations.md) before upgrading."
        )


def upgrade() -> None:
    _require_ticket_columns(_COLUMNS_0002)

    with op.batch_alter_table(
        "tickets",
        copy_from=_tickets_table(with_assignment=False),
        recreate="always",
    ) as batch_op:
        batch_op.add_column(
            sa.Column("assigned_to_user_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True)
        )
        # Existing rows receive 1 from the server default during the copy.
        batch_op.add_column(
            sa.Column(
                "version",
                sa.Integer(),
                server_default=sa.text("1"),
                nullable=False,
            )
        )
        batch_op.create_foreign_key(
            ASSIGNEE_FK,
            "users",
            ["assigned_to_user_id"],
            ["id"],
        )
        batch_op.create_index(ASSIGNEE_INDEX, ["assigned_to_user_id"])

    op.create_table(
        "ticket_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("from_assignee_id", sa.Integer(), nullable=True),
        sa.Column("to_assignee_id", sa.Integer(), nullable=True),
        sa.Column("from_status", sa.String(length=20), nullable=True),
        sa.Column("to_status", sa.String(length=20), nullable=True),
        sa.Column("reason", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "event_type IN ('created', 'assigned', 'reassigned', "
            "'unassigned', 'status_changed')",
            name="ck_ticket_events_event_type",
        ),
        sa.CheckConstraint(
            f"from_status IS NULL OR from_status IN {STATUSES}",
            name="ck_ticket_events_from_status",
        ),
        sa.CheckConstraint(
            f"to_status IS NULL OR to_status IN {STATUSES}",
            name="ck_ticket_events_to_status",
        ),
        sa.CheckConstraint(
            "reason IS NULL OR "
            "reason IN ('assignee_deactivated', 'assignee_role_changed')",
            name="ck_ticket_events_reason",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
            name="fk_ticket_events_ticket_id_tickets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_ticket_events_actor_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["from_assignee_id"],
            ["users.id"],
            name="fk_ticket_events_from_assignee_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["to_assignee_id"],
            ["users.id"],
            name="fk_ticket_events_to_assignee_id_users",
        ),
    )
    op.create_index(
        EVENTS_INDEX,
        "ticket_events",
        ["ticket_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    _require_ticket_columns(_COLUMNS_0002 + _NEW_COLUMNS)

    # History goes first: nothing may reference tickets while it is
    # rebuilt.
    op.drop_index(EVENTS_INDEX, table_name="ticket_events")
    op.drop_table("ticket_events")

    with op.batch_alter_table(
        "tickets",
        copy_from=_tickets_table(with_assignment=True),
        recreate="always",
    ) as batch_op:
        batch_op.drop_index(ASSIGNEE_INDEX)
        batch_op.drop_constraint(ASSIGNEE_FK, type_="foreignkey")
        batch_op.drop_column("version")
        batch_op.drop_column("assigned_at")
        batch_op.drop_column("assigned_to_user_id")
