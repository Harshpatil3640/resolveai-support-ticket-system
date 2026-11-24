
import logging
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Query,
    Response,
    status,
)
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError
from starlette.requests import Request

from app.api.dependencies import (
    FORBIDDEN_DETAIL,
    require_admin,
    require_agent,
    require_agent_or_admin,
    require_any_role,
)
from app.core.audit_logging import (
    log_ticket_assignment,
    log_ticket_claim,
    log_ticket_deletion,
    log_ticket_status_change,
)
from app.core.roles import UserRole
from app.db.database import get_db
from app.db.errors import describe_rollback_failure, rollback_failure_of
from app.ml.predictor import predict_category
from app.models import Ticket, User
from app.schemas.ticket import (
    MAX_DATABASE_INTEGER,
    AssigneeSummary,
    CustomerTicketRead,
    Priority,
    StaffTicketRead,
    Status,
    TicketAssignmentUpdate,
    TicketClaim,
    TicketCreate,
    TicketEventRead,
    TicketRead,
    TicketStatusUpdate,
    TicketUpdate,
)
from app.services.ticket_service import (
    count_tickets,
    create_ticket,
    delete_ticket,
    get_ticket_by_id,
    get_tickets,
)
from app.services.ticket_workflow import (
    AssigneeNotAssignableError,
    InvalidStatusTransitionError,
    InvalidTicketEventError,
    StatusChangeResult,
    TicketAlreadyAssignedError,
    TicketNotClaimableError,
    TicketNotFoundError,
    TicketPermissionError,
    TicketVersionConflictError,
    TicketWorkflowError,
    assign_ticket_with_result,
    change_ticket_status_with_result,
    claim_ticket_with_result,
    edit_ticket_with_result,
    list_ticket_events,
)

logger = logging.getLogger(__name__)

TICKET_NOT_FOUND_DETAIL = "Ticket not found."
VERSION_CONFLICT_DETAIL = (
    "The ticket was changed by someone else. Reload and try again."
)
INTERNAL_ERROR_DETAIL = "Internal server error."
ASSIGNED_TO_STAFF_ONLY_DETAIL = "The assigned_to filter is only available to staff."
ASSIGNED_TO_INVALID_DETAIL = "assigned_to must be 'me', 'none' or a user ID."

# "me", "none" or a positive integer of at most 19 digits.
ASSIGNED_TO_PATTERN = r"^(me|none|[1-9][0-9]{0,18})$"


# ---------------------------------------------------------------------------
# Error mapping (one place for every ticket endpoint)
# ---------------------------------------------------------------------------


def _ticket_http_error(error: Exception) -> HTTPException:
    """Translate a workflow error or stale ORM write into a safe response.

    Every detail is fixed text (the workflow errors' messages are fixed,
    and never contain submitted values). A failed rollback recorded on the
    error is logged with the allow-listed diagnostic only.
    """

    rollback_failure = rollback_failure_of(error)

    if rollback_failure is not None:
        logger.error(
            "Rolling back a rejected ticket request failed (%s).",
            describe_rollback_failure(rollback_failure),
        )

    if isinstance(error, TicketNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, TICKET_NOT_FOUND_DETAIL)

    if isinstance(error, TicketPermissionError):
        return HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN_DETAIL)

    if isinstance(error, (TicketVersionConflictError, StaleDataError)):
        return HTTPException(status.HTTP_409_CONFLICT, VERSION_CONFLICT_DETAIL)

    if isinstance(
        error,
        (
            TicketAlreadyAssignedError,
            TicketNotClaimableError,
            InvalidStatusTransitionError,
        ),
    ):
        return HTTPException(status.HTTP_409_CONFLICT, str(error))

    if isinstance(error, AssigneeNotAssignableError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error))

    if isinstance(error, InvalidTicketEventError):
        logger.error("A ticket history event was rejected (programming error).")
    else:
        logger.error("An unexpected ticket workflow error occurred.")

    return HTTPException(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        INTERNAL_ERROR_DETAIL,
    )


class TicketRoute(APIRoute):
    """Route that maps ticket workflow errors to HTTP responses.

    Applies to every endpoint of this router only, so other routers'
    error handling is unchanged. A stale versioned write or delete
    (``StaleDataError``, which would otherwise reach the generic database
    error handler as a 500) becomes 409. Every other database error still
    goes to the application's safe database error handler.
    """

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handler_with_ticket_errors(request: Request) -> Response:
            try:
                return await handler(request)
            except (TicketWorkflowError, StaleDataError) as error:
                raise _ticket_http_error(error) from None

        return handler_with_ticket_errors


router = APIRouter(tags=["Tickets"], route_class=TicketRoute)

# Roles that may access every ticket in the shared support queue.
STAFF_ROLES: frozenset[UserRole] = frozenset(
    {UserRole.ADMIN, UserRole.AGENT}
)

TicketId = Annotated[int, Path(ge=1, le=MAX_DATABASE_INTEGER)]

# Administrators and agents receive StaffTicketRead; customers receive
# CustomerTicketRead, which has no assignee identity. StaffTicketRead's
# fields are all required, so a customer response can never validate as
# a staff one.
TicketResponse = StaffTicketRead | CustomerTicketRead
TicketListResponse = list[StaffTicketRead] | list[CustomerTicketRead]


def _is_staff(user: User) -> bool:
    return user.role in STAFF_ROLES


def _ticket_owner_scope(user: User) -> int | None:
    """Return the creator ID a user's ticket access is restricted to.

    Staff (admin/agent) see all tickets, so no restriction (``None``).
    Everyone else is restricted to tickets they created. This fails
    closed: any role that is not explicitly staff is scoped to its own
    tickets.
    """

    if _is_staff(user):
        return None

    return user.id


def _staff_read(ticket: Ticket) -> StaffTicketRead:
    assignee = ticket.assignee

    return StaffTicketRead(
        **TicketRead.model_validate(ticket).model_dump(),
        is_assigned=ticket.assigned_to_user_id is not None,
        assigned_to_user_id=ticket.assigned_to_user_id,
        assignee=None if assignee is None else AssigneeSummary.model_validate(assignee),
        assigned_at=ticket.assigned_at,
        version=ticket.version,
    )


def _customer_read(ticket: Ticket) -> CustomerTicketRead:
    return CustomerTicketRead(
        **TicketRead.model_validate(ticket).model_dump(),
        is_assigned=ticket.assigned_to_user_id is not None,
    )


def _ticket_response(
    ticket: Ticket,
    user: User,
) -> StaffTicketRead | CustomerTicketRead:
    """The response model for ``user``'s role (fails closed to customer)."""

    return _staff_read(ticket) if _is_staff(user) else _customer_read(ticket)


def _log_status_change(
    result: StatusChangeResult,
    *,
    actor_id: int,
    ticket_id: int,
) -> None:
    """Audit a committed status change; a no-op is not logged.

    Uses the before/after statuses captured inside the transaction.
    """

    if result.changed:
        log_ticket_status_change(
            actor_id=actor_id,
            ticket_id=ticket_id,
            status_before=result.from_status,
            status_after=result.to_status,
        )


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=TicketResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a support ticket",
)
def create_ticket_endpoint(
    ticket_data: TicketCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_any_role)],
):
    """Create a support ticket with an optional AI category prediction.

    The ticket and its ``created`` history event are saved together.
    """

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

    return _ticket_response(ticket, current_user)


@router.get(
    "",
    response_model=TicketListResponse,
    summary="List support tickets",
)
def list_tickets_endpoint(
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_any_role)],
    skip: Annotated[int, Query(ge=0, le=MAX_DATABASE_INTEGER)] = 0,
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
    assigned_to: Annotated[
        str | None,
        Query(
            pattern=ASSIGNED_TO_PATTERN,
            description=(
                "Staff only: 'me' (assigned to you), 'none' (unassigned) "
                "or a user ID."
            ),
        ),
    ] = None,
) -> list[StaffTicketRead] | list[CustomerTicketRead]:
    """Retrieve support tickets with filtering and pagination.

    The response is a JSON list. ``X-Total-Count`` holds the number of
    tickets matching every filter, before ``skip`` and ``limit``.
    """

    filters: dict[str, Any] = {
        "status": ticket_status,
        "priority": priority,
        "category": category,
        "search": search,
        "created_by_user_id": _ticket_owner_scope(current_user),
    }

    if assigned_to is not None:
        if not _is_staff(current_user):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ASSIGNED_TO_STAFF_ONLY_DETAIL,
            )

        if assigned_to == "me":
            filters["assigned_to_user_id"] = current_user.id
        elif assigned_to == "none":
            filters["unassigned"] = True
        else:
            user_id = int(assigned_to)

            if user_id > MAX_DATABASE_INTEGER:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    ASSIGNED_TO_INVALID_DETAIL,
                )

            filters["assigned_to_user_id"] = user_id

    total = count_tickets(db, **filters)
    tickets = get_tickets(
        db,
        skip=skip,
        limit=limit,
        load_assignee=_is_staff(current_user),
        **filters,
    )

    response.headers["X-Total-Count"] = str(total)

    return [_ticket_response(ticket, current_user) for ticket in tickets]


@router.get(
    "/{ticket_id}",
    response_model=TicketResponse,
    summary="Retrieve a support ticket",
)
def retrieve_ticket_endpoint(
    ticket_id: TicketId,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_any_role)],
) -> StaffTicketRead | CustomerTicketRead:
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
            detail=TICKET_NOT_FOUND_DETAIL,
        )

    return _ticket_response(ticket, current_user)


@router.patch(
    "/{ticket_id}",
    response_model=StaffTicketRead,
    summary="Update a support ticket",
)
def update_ticket_endpoint(
    ticket_id: TicketId,
    ticket_data: TicketUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_agent_or_admin)],
) -> StaffTicketRead:
    """Update permitted fields of a ticket the caller manages.

    Administrators can edit any ticket; agents only tickets assigned to
    them. A status change follows the workflow transition rules and is
    recorded in the ticket history. ``expected_version`` (optional)
    rejects the edit with 409 if the ticket changed since it was read.
    Assignment cannot be changed here.
    """

    updates = ticket_data.model_dump(
        exclude_unset=True,
        exclude={"expected_version"},
    )
    actor_id = current_user.id

    result = edit_ticket_with_result(
        db,
        actor_id=actor_id,
        ticket_id=ticket_id,
        updates=updates,
        expected_version=ticket_data.expected_version,
    )

    # Committed. A status change made here is audited like /status.
    _log_status_change(result, actor_id=actor_id, ticket_id=ticket_id)

    return _staff_read(result.ticket)


@router.delete(
    "/{ticket_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a support ticket",
)
def delete_ticket_endpoint(
    ticket_id: TicketId,
    db: Annotated[Session, Depends(get_db)],
    current_admin: Annotated[User, Depends(require_admin)],
) -> None:
    """Delete an existing support ticket and, with it, its history.

    If the ticket changes while it is being deleted, nothing is deleted
    and the response is 409. The ``ticket.delete`` audit record is the
    only record of the deletion that remains, since the ticket's history
    is deleted with it.
    """

    actor_id = current_admin.id

    ticket = get_ticket_by_id(
        db=db,
        ticket_id=ticket_id,
    )

    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=TICKET_NOT_FOUND_DETAIL,
        )

    delete_ticket(
        db=db,
        ticket=ticket,
    )

    # Committed (delete_ticket raises otherwise).
    log_ticket_deletion(actor_id=actor_id, ticket_id=ticket_id)

    return None


@router.get(
    "/{ticket_id}/history",
    response_model=list[TicketEventRead],
    summary="List a ticket's activity history",
)
def ticket_history_endpoint(
    ticket_id: TicketId,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[User, Depends(require_agent_or_admin)],
) -> list[TicketEventRead]:
    """Return the ticket's history events, oldest first.

    Staff only. Events are ordered by time, then by ID; each carries IDs,
    fixed event, status and reason values and its timestamp. Tickets
    created before history was introduced have no ``created`` event.
    """

    if get_ticket_by_id(db=db, ticket_id=ticket_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=TICKET_NOT_FOUND_DETAIL,
        )

    return [
        TicketEventRead.model_validate(event)
        for event in list_ticket_events(db, ticket_id)
    ]


# ---------------------------------------------------------------------------
# Workflow: assignment, claiming and status
# ---------------------------------------------------------------------------


@router.put(
    "/{ticket_id}/assignment",
    response_model=StaffTicketRead,
    summary="Assign, reassign or unassign a ticket",
)
def assign_ticket_endpoint(
    ticket_id: TicketId,
    payload: TicketAssignmentUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_admin: Annotated[User, Depends(require_admin)],
) -> StaffTicketRead:
    """Assign the ticket to an active agent, or unassign it with null.

    Repeating the current assignment changes nothing.
    """

    actor_id = current_admin.id

    result = assign_ticket_with_result(
        db,
        actor_id=actor_id,
        ticket_id=ticket_id,
        assignee_id=payload.assigned_to_user_id,
        expected_version=payload.expected_version,
    )

    # Committed. Values were captured inside the transaction; a no-op is
    # not an action and is not logged.
    if result.changed:
        log_ticket_assignment(
            actor_id=actor_id,
            ticket_id=ticket_id,
            event_type=result.event_type,
            previous_assignee_id=result.previous_assignee_id,
            new_assignee_id=result.new_assignee_id,
        )

    return _staff_read(result.ticket)


@router.post(
    "/{ticket_id}/claim",
    response_model=StaffTicketRead,
    summary="Claim an unassigned ticket",
)
def claim_ticket_endpoint(
    ticket_id: TicketId,
    payload: TicketClaim,
    db: Annotated[Session, Depends(get_db)],
    current_agent: Annotated[User, Depends(require_agent)],
) -> StaffTicketRead:
    """Assign an unassigned Open or In Progress ticket to yourself."""

    actor_id = current_agent.id

    result = claim_ticket_with_result(
        db,
        actor_id=actor_id,
        ticket_id=ticket_id,
        expected_version=payload.expected_version,
    )

    # Committed (a claim always changes the ticket, or raises).
    log_ticket_claim(
        actor_id=actor_id,
        ticket_id=ticket_id,
        assignee_id=result.assignee_id,
    )

    return _staff_read(result.ticket)


@router.patch(
    "/{ticket_id}/status",
    response_model=StaffTicketRead,
    summary="Change a ticket's status",
)
def change_ticket_status_endpoint(
    ticket_id: TicketId,
    payload: TicketStatusUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_agent_or_admin)],
) -> StaffTicketRead:
    """Change the status of a ticket you manage, following the workflow.

    Repeating the current status changes nothing.
    """

    actor_id = current_user.id

    result = change_ticket_status_with_result(
        db,
        actor_id=actor_id,
        ticket_id=ticket_id,
        new_status=payload.status,
        expected_version=payload.expected_version,
    )

    # Committed.
    _log_status_change(result, actor_id=actor_id, ticket_id=ticket_id)

    return _staff_read(result.ticket)
