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
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.ticket import Ticket
    from app.models.user import User


# Longest message body accepted, in characters. Shared by the request
# schema and the service; the CHECK constraint below must use the same
# value (and so must migration 0004).
MAX_MESSAGE_LENGTH = 10_000

# Roles a message can be sent with (the stored ``users.role`` values).
MESSAGE_SENDER_ROLES: tuple[str, ...] = ("admin", "agent", "customer")


class TicketMessage(Base):
    """One message in a ticket's conversation.

    Messages are human conversation between the ticket's customer and
    support staff. They are append-only: there is no editing or deletion
    (other than with their ticket). System activity is NOT stored here;
    it stays in ``ticket_events``.

    ``sender_role`` is a snapshot of the sender's role when the message
    was sent, so a later role change does not rewrite history. The
    sender's display name is read through ``sender`` when a response is
    built.

    A ticket's messages are deleted with it (``ON DELETE CASCADE``);
    users are never deleted through messages.

    The CHECK constraints and index below must match migration 0004
    exactly.
    """

    __tablename__ = "ticket_messages"

    __table_args__ = (
        CheckConstraint(
            "sender_role IN ('admin', 'agent', 'customer')",
            name="ck_ticket_messages_sender_role",
        ),
        # Not empty and not only whitespace (space, tab, LF, CR).
        CheckConstraint(
            "trim(content, char(9, 10, 13, 32)) <> ''",
            name="ck_ticket_messages_content_not_blank",
        ),
        CheckConstraint(
            f"length(content) <= {MAX_MESSAGE_LENGTH}",
            name="ck_ticket_messages_content_length",
        ),
        # A ticket's conversation, in insertion order (cursor pagination
        # by message ID).
        Index(
            "ix_ticket_messages_ticket_id_id",
            "ticket_id",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    ticket_id: Mapped[int] = mapped_column(
        ForeignKey(
            "tickets.id",
            name="fk_ticket_messages_ticket_id_tickets",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    sender_user_id: Mapped[int] = mapped_column(
        ForeignKey(
            "users.id",
            name="fk_ticket_messages_sender_user_id_users",
        ),
        nullable=False,
    )

    sender_role: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    ticket: Mapped[Ticket] = relationship(
        back_populates="messages",
    )

    # No reverse collection is kept on User.
    sender: Mapped[User] = relationship(
        foreign_keys=[sender_user_id],
    )
