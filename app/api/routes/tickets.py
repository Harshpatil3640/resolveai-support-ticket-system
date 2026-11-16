
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

from app.api.dependencies import get_current_user
from app.db.database import get_db
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

router = APIRouter(tags=["Tickets"])


@router.post(
    "",
    response_model=TicketRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a support ticket",
)
def create_ticket_endpoint(
    ticket_data: TicketCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Create a new support ticket for the authenticated agent."""

    ticket = create_ticket(
        db=db,
        creator=current_user,
        **ticket_data.model_dump(),
    )

    return ticket


@router.get(
    "",
    response_model=list[TicketRead],
    summary="List support tickets",
)
def list_tickets_endpoint(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[User, Depends(get_current_user)],
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
    _: Annotated[User, Depends(get_current_user)],
) -> TicketRead:
    """Retrieve an individual support ticket by its ID."""

    ticket = get_ticket_by_id(
        db=db,
        ticket_id=ticket_id,
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
    _: Annotated[User, Depends(get_current_user)],
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
    _: Annotated[User, Depends(get_current_user)],
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
