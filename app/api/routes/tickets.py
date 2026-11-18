
import logging
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Query,
    status,
)
from sqlalchemy.orm import Session

from app.api.dependencies import (
    require_admin,
    require_agent_or_admin,
    require_any_role,
)
from app.core.roles import UserRole
from app.db.database import get_db
from app.ml.predictor import predict_category
from app.models import User
from app.schemas.ticket import (
    Priority,
    Status,
    TicketCreate,
    TicketRead,
    TicketUpdate,
)
from app.services.ticket_service import (
    create_ticket,
    delete_ticket,
    get_ticket_by_id,
    get_tickets,
    update_ticket,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Tickets"])

# Roles that may access every ticket in the shared support queue.
STAFF_ROLES: frozenset[UserRole] = frozenset(
    {UserRole.ADMIN, UserRole.AGENT}
)


def _ticket_owner_scope(user: User) -> int | None:
    """Return the creator ID a user's ticket access is restricted to.

    Staff (admin/agent) see all tickets, so no restriction (``None``).
    Everyone else is restricted to tickets they created. This fails
    closed: any role that is not explicitly staff is scoped to its own
    tickets.
    """

    if user.role in STAFF_ROLES:
        return None

    return user.id



@router.post(
    "",
    response_model=TicketRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a support ticket",
)
def create_ticket_endpoint(
    ticket_data: TicketCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_any_role)],
):
    """Create a support ticket with an optional AI category prediction."""

    # Default values in case AI prediction is unavailable.
    predicted_category = None
    prediction_confidence = None

    # Step 1: Attempt AI category prediction.
    try:
        predicted_category, prediction_confidence = predict_category(
            ticket_data.description
        )

        logger.info(
            "AI predicted ticket category: %s (probability: %.4f)",
            predicted_category,
            prediction_confidence,
        )

    except Exception:
        # AI failure must never prevent ticket creation.
        logger.exception(
            "AI category prediction failed. "
            "Creating ticket without an AI prediction."
        )

    # Step 2: Save the ticket regardless of prediction outcome.
    ticket_fields = ticket_data.model_dump()

    ticket = create_ticket(
        db=db,
        creator=current_user,
        **ticket_fields,
        predicted_category=predicted_category,
        prediction_confidence=prediction_confidence,
    )

    return ticket


@router.get(
    "",
    response_model=list[TicketRead],
    summary="List support tickets",
)
def list_tickets_endpoint(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_any_role)],
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ticket_status: Status | None = Query(
        default=None,
        alias="status",
    ),
    priority: Priority | None = None,
    category: Annotated[
        str | None,
        Query(min_length=1, max_length=100),
    ] = None,
    search: Annotated[
        str | None,
        Query(min_length=1, max_length=200),
    ] = None,
) -> list[TicketRead]:
    """Retrieve support tickets with filtering and pagination."""

    tickets = get_tickets(
        db=db,
        skip=skip,
        limit=limit,
        status=ticket_status,
        priority=priority,
        category=category,
        search=search,
        created_by_user_id=_ticket_owner_scope(current_user),
    )

    return tickets


@router.get(
    "/{ticket_id}",
    response_model=TicketRead,
    summary="Retrieve a support ticket",
)
def retrieve_ticket_endpoint(
    ticket_id: Annotated[int, Path(ge=1)],
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_any_role)],
) -> TicketRead:
    """Retrieve an individual support ticket by its ID.

    Customers only see their own tickets; a ticket owned by someone
    else returns the same 404 as a nonexistent ticket.
    """

    ticket = get_ticket_by_id(
        db=db,
        ticket_id=ticket_id,
        created_by_user_id=_ticket_owner_scope(current_user),
    )

    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ticket not found.",
        )

    return ticket



@router.patch(
    "/{ticket_id}",
    response_model=TicketRead,
    summary="Update a support ticket",
)
def update_ticket_endpoint(
    ticket_id: Annotated[int, Path(ge=1)],
    ticket_data: TicketUpdate,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[User, Depends(require_agent_or_admin)],
) -> TicketRead:
    """Update permitted fields of an existing support ticket."""

    ticket = get_ticket_by_id(
        db=db,
        ticket_id=ticket_id,
    )

    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ticket not found.",
        )

    updates = ticket_data.model_dump(exclude_unset=True)

    updated_ticket = update_ticket(
        db=db,
        ticket=ticket,
        updates=updates,
    )

    return updated_ticket



@router.delete(
    "/{ticket_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a support ticket",
)
def delete_ticket_endpoint(
    ticket_id: Annotated[int, Path(ge=1)],
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[User, Depends(require_admin)],
) -> None:
    """Delete an existing support ticket."""

    ticket = get_ticket_by_id(
        db=db,
        ticket_id=ticket_id,
    )

    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ticket not found.",
        )

    delete_ticket(
        db=db,
        ticket=ticket,
    )

    return None
