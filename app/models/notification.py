from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.ticket import Ticket
    from app.models.ticket_message import TicketMessage
    from app.models.user import User


# A new message on a ticket the recipient takes part in. The only type in
# Phase 10; the CHECK constraint below (and migration 0005) must list
# exactly NOTIFICATION_TYPES.
NOTIFICATION_TYPE_TICKET_MESSAGE = "ticket_message"

NOTIFICATION_TYPES: tuple[str, ...] = (NOTIFICATION_TYPE_TICKET_MESSAGE,)


class Notification(Base):
    """One notification for one recipient.

    Rows hold only IDs, a fixed type code and timestamps: never message
    content, names or emails. The message, ticket and actor are read
    through their relationships when a response is built, so the
    conversation stays the only copy of what was written.

    ``read_at`` is NULL while the notification is unread. Once set it is
    never cleared (the service layer only ever writes it where it is
    still NULL).

    A notification is deleted with its ticket or its message
    (``ON DELETE CASCADE``); users are never deleted through
    notifications. Nobody is notified of their own action
    (``ck_notifications_not_self``), and a message notifies each
    recipient at most once
    (``uq_notifications_message_id_recipient_user_id``).

    Existing Phase 9 messages have no notifications: none are backfilled.

    The CHECK constraints, unique constraint and indexes below must match
    migration 0005 exactly.
    """

    __tablename__ = "notifications"

    __table_args__ = (
        CheckConstraint(
            "notification_type IN ('ticket_message')",
            name="ck_notifications_notification_type",
        ),
        CheckConstraint(
            "notification_type <> 'ticket_message' OR message_id IS NOT NULL",
            name="ck_notifications_message_required",
        ),
        CheckConstraint(
            "recipient_user_id <> actor_user_id",
            name="ck_notifications_not_self",
        ),
        # One notification per message per recipient. message_id leads,
        # so the index also serves the message-delete cascade.
        UniqueConstraint(
            "message_id",
            "recipient_user_id",
            name="uq_notifications_message_id_recipient_user_id",
        ),
        # A recipient's notifications, newest first (cursor by ID).
        Index(
            "ix_notifications_recipient_user_id_id",
            "recipient_user_id",
            "id",
        ),
        # A recipient's unread count (read_at IS NULL).
        Index(
            "ix_notifications_recipient_user_id_read_at",
            "recipient_user_id",
            "read_at",
        ),
        # The ticket-delete cascade.
        Index(
            "ix_notifications_ticket_id",
            "ticket_id",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    recipient_user_id: Mapped[int] = mapped_column(
        ForeignKey(
            "users.id",
            name="fk_notifications_recipient_user_id_users",
        ),
        nullable=False,
    )

    # The user whose action caused the notification (the message sender).
    actor_user_id: Mapped[int] = mapped_column(
        ForeignKey(
            "users.id",
            name="fk_notifications_actor_user_id_users",
        ),
        nullable=False,
    )

    ticket_id: Mapped[int] = mapped_column(
        ForeignKey(
            "tickets.id",
            name="fk_notifications_ticket_id_tickets",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    # Required for ``ticket_message`` notifications
    # (ck_notifications_message_required); nullable for future types.
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "ticket_messages.id",
            name="fk_notifications_message_id_ticket_messages",
            ondelete="CASCADE",
        ),
        nullable=True,
    )

    notification_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Two foreign keys reference users, so each relationship names its
    # column. No reverse collections are kept on User, Ticket or
    # TicketMessage: the database deletes notifications with their ticket
    # or message.
    recipient: Mapped[User] = relationship(
        foreign_keys=[recipient_user_id],
    )

    actor: Mapped[User] = relationship(
        foreign_keys=[actor_user_id],
    )

    ticket: Mapped[Ticket] = relationship(
        foreign_keys=[ticket_id],
    )

    message: Mapped[TicketMessage | None] = relationship(
        foreign_keys=[message_id],
    )
