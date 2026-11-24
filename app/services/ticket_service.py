
from collections.abc import Mapping
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.db.errors import roll_back_keeping_primary
from app.models import Ticket, User
from app.services.ticket_workflow import EVENT_CREATED, record_ticket_event


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
    """Commit a transaction and roll back if it fails.

    The commit's failure is always the exception raised; if the rollback
    also fails, that is recorded on it as a secondary failure
    (``app.db.errors.rollback_failure_of``) rather than replacing it.
    """
    try:
        db.commit()
    except Exception as caught:
        primary = roll_back_keeping_primary(db, caught)

        if primary is caught:
            raise

        raise primary from None


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
    """Create and persist a support ticket with its ``created`` event.

    The ticket and one ``created`` history event (actor: the creator) are
    committed in one transaction. On ANY failure (the INSERT, the event or
    the commit) everything is rolled back and the original failure is
    re-raised; a failed rollback is attached as a secondary failure
    (``app.db.errors.rollback_failure_of``).
    """

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

    try:
        db.add(ticket)
        # Assigns the ticket ID the event refers to.
        db.flush()
        record_ticket_event(
            db,
            ticket_id=ticket.id,
            actor_id=creator.id,
            event_type=EVENT_CREATED,
        )
        db.commit()

    except Exception as caught:
        primary = roll_back_keeping_primary(db, caught)

        if primary is caught:
            raise

        raise primary from None

    db.refresh(ticket)

    return ticket


def get_ticket_by_id(
    db: Session,
    ticket_id: int,
    *,
    created_by_user_id: int | None = None,
) -> Ticket | None:
    """Retrieve a ticket using its primary key.

    When ``created_by_user_id`` is supplied, the ticket is only returned
    if it was created by that user; otherwise ``None`` is returned, so
    a non-owned ticket is indistinguishable from a missing one.
    """

    if created_by_user_id is None:
        return db.get(Ticket, ticket_id)

    statement = select(Ticket).where(
        Ticket.id == ticket_id,
        Ticket.created_by_user_id == created_by_user_id,
    )

    return db.scalars(statement).first()


def _ticket_conditions(
    *,
    status: str | None = None,
    category: str | None = None,
    priority: str | None = None,
    search: str | None = None,
    created_by_user_id: int | None = None,
    assigned_to_user_id: int | None = None,
    unassigned: bool = False,
) -> list[ColumnElement[bool]]:
    """WHERE conditions shared by ``get_tickets`` and ``count_tickets``.

    One definition keeps the count and the page consistent.
    """

    if assigned_to_user_id is not None and unassigned:
        raise ValueError(
            "assigned_to_user_id and unassigned cannot be combined."
        )

    conditions: list[ColumnElement[bool]] = []

    if created_by_user_id is not None:
        conditions.append(Ticket.created_by_user_id == created_by_user_id)

    if assigned_to_user_id is not None:
        conditions.append(Ticket.assigned_to_user_id == assigned_to_user_id)

    if unassigned:
        conditions.append(Ticket.assigned_to_user_id.is_(None))

    if status is not None:
        conditions.append(Ticket.status == status)

    if category is not None:
        conditions.append(Ticket.category == category)

    if priority is not None:
        conditions.append(Ticket.priority == priority)

    if search and search.strip():
        search_term = f"%{search.strip()}%"

        conditions.append(
            or_(
                Ticket.title.ilike(search_term),
                Ticket.description.ilike(search_term),
            )
        )

    return conditions


def get_tickets(
    db: Session,
    *,
    skip: int = 0,
    limit: int = 100,
    status: str | None = None,
    category: str | None = None,
    priority: str | None = None,
    search: str | None = None,
    created_by_user_id: int | None = None,
    assigned_to_user_id: int | None = None,
    unassigned: bool = False,
    load_assignee: bool = False,
) -> list[Ticket]:
    """Retrieve tickets with optional filtering and pagination.

    ``created_by_user_id`` restricts results to tickets created by that
    user (used to scope customer access). ``assigned_to_user_id`` keeps
    tickets assigned to that user; ``unassigned`` keeps tickets with no
    assignee (the two cannot be combined). Tickets are ordered newest
    first (``created_at``, then ``id``, descending). ``load_assignee``
    loads every assignee with one extra query instead of one per ticket.
    """

    statement = (
        select(Ticket)
        .where(
            *_ticket_conditions(
                status=status,
                category=category,
                priority=priority,
                search=search,
                created_by_user_id=created_by_user_id,
                assigned_to_user_id=assigned_to_user_id,
                unassigned=unassigned,
            )
        )
        .order_by(Ticket.created_at.desc(), Ticket.id.desc())
        .offset(skip)
        .limit(limit)
    )

    if load_assignee:
        statement = statement.options(selectinload(Ticket.assignee))

    return list(db.scalars(statement).all())


def count_tickets(
    db: Session,
    *,
    status: str | None = None,
    category: str | None = None,
    priority: str | None = None,
    search: str | None = None,
    created_by_user_id: int | None = None,
    assigned_to_user_id: int | None = None,
    unassigned: bool = False,
) -> int:
    """Number of tickets matching the same filters as ``get_tickets``.

    Counts before pagination (``skip``/``limit`` do not apply).
    """

    return db.scalar(
        select(func.count())
        .select_from(Ticket)
        .where(
            *_ticket_conditions(
                status=status,
                category=category,
                priority=priority,
                search=search,
                created_by_user_id=created_by_user_id,
                assigned_to_user_id=assigned_to_user_id,
                unassigned=unassigned,
            )
        )
    )


def update_ticket(
    db: Session,
    ticket: Ticket,
    updates: Mapping[str, Any],
) -> Ticket:
    """Update permitted ticket fields (no permission or workflow checks).

    A low-level helper for trusted callers. The HTTP API edits tickets
    through ``app.services.ticket_workflow.edit_ticket``, which enforces
    management permission, status transitions and the version check.
    """

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
