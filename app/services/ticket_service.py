
from collections.abc import Mapping
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Ticket, User


# Fields that a support agent is allowed to modify.
ALLOWED_UPDATE_FIELDS = {
    "title",
    "description",
    "requester_name",
    "category",
    "priority",
    "status",
}


def _commit_changes(db: Session) -> None:
    """Commit a transaction and roll back if it fails."""
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise


def create_ticket(
    db: Session,
    *,
    creator: User,
    title: str,
    description: str,
    requester_name: str | None = None,
    predicted_category: str | None = None,
    prediction_confidence: float | None = None,
    category: str | None = None,
    priority: str = "Medium",
    status: str = "Open",
) -> Ticket:
    """Create and persist a support ticket."""

    ticket = Ticket(
        creator=creator,
        title=title,
        description=description,
        requester_name=requester_name,
        predicted_category=predicted_category,
        prediction_confidence=prediction_confidence,
        category=category,
        priority=priority,
        status=status,
    )

    db.add(ticket)
    _commit_changes(db)
    db.refresh(ticket)

    return ticket


def get_ticket_by_id(
    db: Session,
    ticket_id: int,
) -> Ticket | None:
    """Retrieve a ticket using its primary key."""

    return db.get(Ticket, ticket_id)


def get_tickets(
    db: Session,
    *,
    skip: int = 0,
    limit: int = 100,
    status: str | None = None,
    category: str | None = None,
    priority: str | None = None,
    search: str | None = None,
) -> list[Ticket]:
    """Retrieve tickets with optional filtering and pagination."""

    statement = select(Ticket)

    if status is not None:
        statement = statement.where(Ticket.status == status)

    if category is not None:
        statement = statement.where(Ticket.category == category)

    if priority is not None:
        statement = statement.where(Ticket.priority == priority)

    if search and search.strip():
        search_term = f"%{search.strip()}%"

        statement = statement.where(
            or_(
                Ticket.title.ilike(search_term),
                Ticket.description.ilike(search_term),
            )
        )

    statement = (
        statement
        .order_by(Ticket.created_at.desc(), Ticket.id.desc())
        .offset(skip)
        .limit(limit)
    )

    return list(db.scalars(statement).all())


def update_ticket(
    db: Session,
    ticket: Ticket,
    updates: Mapping[str, Any],
) -> Ticket:
    """Update permitted ticket fields."""

    invalid_fields = set(updates) - ALLOWED_UPDATE_FIELDS

    if invalid_fields:
        raise ValueError(
            f"Fields cannot be updated: {sorted(invalid_fields)}"
        )

    for field, value in updates.items():
        setattr(ticket, field, value)

    _commit_changes(db)
    db.refresh(ticket)

    return ticket


def delete_ticket(
    db: Session,
    ticket: Ticket,
) -> None:
    """Delete a ticket from the database."""

    db.delete(ticket)
    _commit_changes(db)
