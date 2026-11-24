
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.ticket_event import TicketEvent
    from app.models.user import User


class Ticket(Base):
    __tablename__ = "tickets"

    __table_args__ = (
        CheckConstraint(
            "priority IN ('Low', 'Medium', 'High')",
            name="ck_tickets_priority",
        ),
        CheckConstraint(
            "status IN ('Open', 'In Progress', 'Resolved')",
            name="ck_tickets_status",
        ),
    )

    # Primary key
    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    # Customer issue details
    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    requester_name: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    # Machine-learning prediction fields
    predicted_category: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    category: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    prediction_confidence: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    # Ticket management fields
    priority: Mapped[str] = mapped_column(
        String(20),
        default="Medium",
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="Open",
        nullable=False,
    )

    # Foreign key connecting tickets to users
    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Agent the ticket is assigned to (NULL while unassigned). Assignment
    # rules are enforced by the service layer, not by this column.
    assigned_to_user_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "users.id",
            name="fk_tickets_assigned_to_user_id_users",
        ),
        nullable=True,
        index=True,
    )

    # When the current assignment was made (UTC); NULL while unassigned.
    assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Optimistic concurrency counter (see __mapper_args__). Every ORM
    # UPDATE of a ticket checks the version it loaded and increments it,
    # so two sessions cannot silently overwrite each other: the stale one
    # raises sqlalchemy.orm.exc.StaleDataError. Rows that existed before
    # migration 0003 start at 1.
    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
        nullable=False,
    )

    __mapper_args__ = {"version_id_col": version}

    # Many tickets belong to one user. Two foreign keys now reference
    # users, so each relationship names the column it uses.
    creator: Mapped[User] = relationship(
        back_populates="tickets",
        foreign_keys=[created_by_user_id],
    )

    assignee: Mapped[User | None] = relationship(
        back_populates="assigned_tickets",
        foreign_keys=[assigned_to_user_id],
    )

    # Activity history, oldest first. The database deletes a ticket's
    # events with it (ON DELETE CASCADE); passive_deletes leaves unloaded
    # events to the database, and the delete cascade removes loaded ones.
    events: Mapped[list[TicketEvent]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="(TicketEvent.created_at, TicketEvent.id)",
    )
