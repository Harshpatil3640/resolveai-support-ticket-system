"""Add the ticket_messages conversation table.

* ``ticket_messages``: human conversation on a ticket (customer, agents,
  administrators). Append-only; system activity stays in
  ``ticket_events``.
* ``ticket_id`` references ``tickets.id`` with ``ON DELETE CASCADE``
  (``fk_ticket_messages_ticket_id_tickets``), so deleting a ticket still
  works and removes its conversation.
* ``sender_user_id`` references ``users.id``
  (``fk_ticket_messages_sender_user_id_users``); users are never deleted
  through messages.
* Named CHECK constraints: the sender role is one of the three stored
  role values, and the content is neither blank nor longer than 10,000
  characters.
* Index ``ix_ticket_messages_ticket_id_id`` on ``(ticket_id, id)`` serves
  a ticket's newest-first cursor pagination.

No existing table is altered or rebuilt, so every user, ticket and
ticket event row is left exactly as it is. The new table references
existing tables only, so the migration runs the same with or without
foreign-key enforcement. Downgrade drops the index and the table (and
with them every message); nothing else changes.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


MESSAGES_INDEX = "ix_ticket_messages_ticket_id_id"

# Must match app.models.ticket_message.MAX_MESSAGE_LENGTH. Written out
# here because a migration must not change when application code does.
MAX_MESSAGE_LENGTH = 10_000


def upgrade() -> None:
    op.create_table(
        "ticket_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("sender_user_id", sa.Integer(), nullable=False),
        sa.Column("sender_role", sa.String(length=30), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "sender_role IN ('admin', 'agent', 'customer')",
            name="ck_ticket_messages_sender_role",
        ),
        sa.CheckConstraint(
            "trim(content, char(9, 10, 13, 32)) <> ''",
            name="ck_ticket_messages_content_not_blank",
        ),
        sa.CheckConstraint(
            f"length(content) <= {MAX_MESSAGE_LENGTH}",
            name="ck_ticket_messages_content_length",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
            name="fk_ticket_messages_ticket_id_tickets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sender_user_id"],
            ["users.id"],
            name="fk_ticket_messages_sender_user_id_users",
        ),
    )
    op.create_index(
        MESSAGES_INDEX,
        "ticket_messages",
        ["ticket_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(MESSAGES_INDEX, table_name="ticket_messages")
    op.drop_table("ticket_messages")
