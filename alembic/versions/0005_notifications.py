"""Add the notifications table.

* ``notifications``: one row per recipient per event. In Phase 10 the
  only type is ``ticket_message`` (a new conversation message). Rows hold
  IDs, the fixed type code and timestamps only, never message content.
* ``recipient_user_id`` and ``actor_user_id`` reference ``users.id``
  (``fk_notifications_recipient_user_id_users``,
  ``fk_notifications_actor_user_id_users``); users are never deleted
  through notifications.
* ``ticket_id`` references ``tickets.id`` and ``message_id`` references
  ``ticket_messages.id``, both with ``ON DELETE CASCADE``
  (``fk_notifications_ticket_id_tickets``,
  ``fk_notifications_message_id_ticket_messages``), so deleting a ticket
  or a message still works and removes its notifications.
  ``message_id`` is nullable for future notification types.
* Named CHECK constraints: the type is a known type; a ``ticket_message``
  notification has a message; nobody is notified of their own action.
* Unique constraint ``uq_notifications_message_id_recipient_user_id``:
  a message notifies each recipient at most once. ``message_id`` leads,
  so it also serves the message-delete cascade.
* Indexes: ``ix_notifications_recipient_user_id_id`` (a recipient's
  newest-first cursor pagination), ``ix_notifications_recipient_user_id_read_at``
  (unread counts) and ``ix_notifications_ticket_id`` (the ticket-delete
  cascade).

No existing table is altered or rebuilt, so every user, ticket, ticket
event and ticket message row is left exactly as it is. Existing messages
are NOT backfilled: they generate no notifications. The new table
references existing tables only, so the migration runs the same with or
without foreign-key enforcement. Downgrade drops the indexes and the
table (and with them every notification); nothing else changes.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RECIPIENT_INDEX = "ix_notifications_recipient_user_id_id"
UNREAD_INDEX = "ix_notifications_recipient_user_id_read_at"
TICKET_INDEX = "ix_notifications_ticket_id"


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("recipient_user_id", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("notification_type", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "notification_type IN ('ticket_message')",
            name="ck_notifications_notification_type",
        ),
        sa.CheckConstraint(
            "notification_type <> 'ticket_message' OR message_id IS NOT NULL",
            name="ck_notifications_message_required",
        ),
        sa.CheckConstraint(
            "recipient_user_id <> actor_user_id",
            name="ck_notifications_not_self",
        ),
        sa.UniqueConstraint(
            "message_id",
            "recipient_user_id",
            name="uq_notifications_message_id_recipient_user_id",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_user_id"],
            ["users.id"],
            name="fk_notifications_recipient_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_notifications_actor_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
            name="fk_notifications_ticket_id_tickets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["ticket_messages.id"],
            name="fk_notifications_message_id_ticket_messages",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        RECIPIENT_INDEX,
        "notifications",
        ["recipient_user_id", "id"],
        unique=False,
    )
    op.create_index(
        UNREAD_INDEX,
        "notifications",
        ["recipient_user_id", "read_at"],
        unique=False,
    )
    op.create_index(
        TICKET_INDEX,
        "notifications",
        ["ticket_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(TICKET_INDEX, table_name="notifications")
    op.drop_index(UNREAD_INDEX, table_name="notifications")
    op.drop_index(RECIPIENT_INDEX, table_name="notifications")
    op.drop_table("notifications")
