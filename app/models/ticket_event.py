
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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.ticket import Ticket
    from app.models.user import User


class TicketEvent(Base):
    """One entry in a ticket's persistent activity history.

    Rows hold only IDs, statuses and fixed codes, never names, emails or
    free text. They are written by the service layer in the same
    transaction as the change they describe. A ticket's events are deleted
    with it (``ON DELETE CASCADE``); users are never deleted.

    The CHECK constraints below must match migration 0003 exactly.
    """

    __tablename__ = "ticket_events"

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('created', 'assigned', 'reassigned', "
            "'unassigned', 'status_changed')",
            name="ck_ticket_events_event_type",
        ),
        CheckConstraint(
            "from_status IS NULL OR "
            "from_status IN ('Open', 'In Progress', 'Resolved')",
            name="ck_ticket_events_from_status",
        ),
        CheckConstraint(
            "to_status IS NULL OR "
            "to_status IN ('Open', 'In Progress', 'Resolved')",
            name="ck_ticket_events_to_status",
        ),
        CheckConstraint(
            "reason IS NULL OR "
            "reason IN ('assignee_deactivated', 'assignee_role_changed')",
            name="ck_ticket_events_reason",
        ),
        # A ticket's timeline, in order.
        Index(
            "ix_ticket_events_ticket_id_created_at_id",
            "ticket_id",
            "created_at",
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
            name="fk_ticket_events_ticket_id_tickets",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    event_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    # The user who made the change (for automatic changes, the
    # administrator whose action caused them).
    actor_user_id: Mapped[int] = mapped_column(
        ForeignKey(
            "users.id",
            name="fk_ticket_events_actor_user_id_users",
        ),
        nullable=False,
    )

    from_assignee_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "users.id",
            name="fk_ticket_events_from_assignee_id_users",
        ),
        nullable=True,
    )

    to_assignee_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "users.id",
            name="fk_ticket_events_to_assignee_id_users",
        ),
        nullable=True,
    )

    from_status: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )

    to_status: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )

    reason: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    ticket: Mapped[Ticket] = relationship(
        back_populates="events",
    )

    # Three foreign keys reference users, so each relationship names its
    # column. No reverse collections are kept on User.
    actor: Mapped[User] = relationship(
        foreign_keys=[actor_user_id],
    )

    from_assignee: Mapped[User | None] = relationship(
        foreign_keys=[from_assignee_id],
    )

    to_assignee: Mapped[User | None] = relationship(
        foreign_keys=[to_assignee_id],
    )
