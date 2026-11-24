"""Ticket workflow: assignment, claiming, status changes and history.

Business rules (Phase 7):

* Only active users whose role is exactly ``agent`` can be assigned a
  ticket (``ASSIGNABLE_ROLES``). Administrators manage tickets; they are
  never assignees.
* Active administrators can assign, reassign and unassign any ticket.
  An active agent can claim an unassigned ticket for themselves.
* Status changes follow ``STATUS_TRANSITIONS``. An administrator can
  change any ticket; an agent only the tickets assigned to them;
  customers never. A ticket can only move into "In Progress" (or from
  Open to Resolved) while it has an assignee, and unassigning an
  "In Progress" ticket moves it back to "Open", so an "In Progress"
  ticket always has an assignee after a workflow change.
* Repeating the current assignee or status is a no-op: nothing is
  written, no history is recorded and the version does not change.
* Every real change bumps the ticket's optimistic ``version`` and records
  one ``ticket_events`` row in the same transaction. Events hold only
  IDs, fixed event types, statuses, reason codes and timestamps.

Transactions. ``assign_ticket``, ``claim_ticket`` and
``change_ticket_status`` own the session's transaction. Each one starts it
with the shared serialised-change lock (``app.db.locking``: SQLite's
``BEGIN IMMEDIATE``), re-reads the acting user and the ticket inside it
(``SELECT ... FOR UPDATE`` where supported), checks ``expected_version``,
writes the change and its event, and commits; on ANY failure it rolls
back and re-raises the original error (a failed rollback is attached as a
secondary failure, ``app.db.errors.rollback_failure_of``). A stale ORM
write (``StaleDataError``) becomes ``TicketVersionConflictError``.

``record_ticket_event`` and ``unassign_open_tickets_for_user`` never
commit or roll back: they work inside the caller's transaction.

Lock order. On databases with row locks, every change locks rows in one
global order: the active administrator rows (in id order, as the user
administration service does), then the other user row involved, then the
ticket. User deactivation will lock its user before that user's tickets,
so the two kinds of change cannot deadlock.

Errors carry fixed messages only: never submitted values, names, emails,
SQL or driver text.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TypeVar

from sqlalchemy import Select, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.roles import UserRole
from app.db import locking as _locking
from app.db.errors import (
    record_rollback_failure,
    roll_back_keeping_primary,
    rollback_failure_of,
)
from app.models import Ticket, TicketEvent, User
from app.services.user_locking import lock_active_administrator_ids


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATUS_OPEN = "Open"
STATUS_IN_PROGRESS = "In Progress"
STATUS_RESOLVED = "Resolved"

TICKET_STATUSES: tuple[str, ...] = (
    STATUS_OPEN,
    STATUS_IN_PROGRESS,
    STATUS_RESOLVED,
)

# Roles that can be assigned tickets. Exact stored values only.
ASSIGNABLE_ROLES: frozenset[UserRole] = frozenset({UserRole.AGENT})

EVENT_CREATED = "created"
EVENT_ASSIGNED = "assigned"
EVENT_REASSIGNED = "reassigned"
EVENT_UNASSIGNED = "unassigned"
EVENT_STATUS_CHANGED = "status_changed"

EVENT_TYPES: tuple[str, ...] = (
    EVENT_CREATED,
    EVENT_ASSIGNED,
    EVENT_REASSIGNED,
    EVENT_UNASSIGNED,
    EVENT_STATUS_CHANGED,
)

REASON_ASSIGNEE_DEACTIVATED = "assignee_deactivated"
REASON_ASSIGNEE_ROLE_CHANGED = "assignee_role_changed"

UNASSIGNMENT_REASONS: tuple[str, ...] = (
    REASON_ASSIGNEE_DEACTIVATED,
    REASON_ASSIGNEE_ROLE_CHANGED,
)


@dataclass(frozen=True)
class TransitionRule:
    """A permitted status change.

    ``requires_assignee``: the ticket must have an assignee. Permission
    (administrator, or the assigned agent) is the same for every rule.
    """

    requires_assignee: bool


# Every permitted (from, to) change. Anything else (including Resolved to
# Open) is rejected; from == to is a no-op and is not listed.
STATUS_TRANSITIONS: dict[tuple[str, str], TransitionRule] = {
    (STATUS_OPEN, STATUS_IN_PROGRESS): TransitionRule(requires_assignee=True),
    (STATUS_OPEN, STATUS_RESOLVED): TransitionRule(requires_assignee=True),
    (STATUS_IN_PROGRESS, STATUS_OPEN): TransitionRule(requires_assignee=False),
    (STATUS_IN_PROGRESS, STATUS_RESOLVED): TransitionRule(requires_assignee=False),
    (STATUS_RESOLVED, STATUS_IN_PROGRESS): TransitionRule(requires_assignee=True),
}


# ---------------------------------------------------------------------------
# Errors (fixed messages only)
# ---------------------------------------------------------------------------


class TicketWorkflowError(Exception):
    """Base class for ticket workflow errors."""


class TicketNotFoundError(TicketWorkflowError, LookupError):
    """The ticket does not exist."""

    def __init__(self) -> None:
        super().__init__("Ticket not found.")


class TicketVersionConflictError(TicketWorkflowError):
    """The ticket changed since the caller loaded it."""

    def __init__(self) -> None:
        super().__init__(
            "The ticket was changed by someone else. Reload it and try again."
        )


class InvalidStatusTransitionError(TicketWorkflowError, ValueError):
    """The requested status change is not permitted."""


class AssigneeNotAssignableError(TicketWorkflowError, ValueError):
    """The selected user does not exist, is inactive or is not an agent."""

    def __init__(self) -> None:
        super().__init__("Assignee must be an active agent.")


class TicketAlreadyAssignedError(TicketWorkflowError):
    """A claim was attempted on a ticket that already has an assignee."""

    def __init__(self) -> None:
        super().__init__("Ticket is already assigned.")


class TicketNotClaimableError(TicketWorkflowError):
    """A claim was attempted on an unassigned ticket that is Resolved.

    Agents may claim only unresolved tickets; an administrator can still
    assign a Resolved ticket with ``assign_ticket``.
    """

    def __init__(self) -> None:
        super().__init__("Resolved tickets cannot be claimed.")


class TicketPermissionError(TicketWorkflowError, PermissionError):
    """The acting user may not perform this ticket action.

    Also raised when the actor lost the required access (deactivated or
    role changed) after the request was authorised.
    """

    def __init__(self) -> None:
        super().__init__("You do not have permission to perform this action.")


class InvalidTicketEventError(TicketWorkflowError, ValueError):
    """An event value is outside the approved set (a programming error)."""


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------


def _role_of(value: object) -> UserRole | None:
    """The ``UserRole`` for an exact stored role value, else None."""

    if isinstance(value, UserRole):
        return value

    try:
        return UserRole(value)
    except ValueError:
        return None


def validate_status_transition(
    from_status: str,
    to_status: str,
    *,
    actor_role: UserRole | str,
    actor_is_assignee: bool,
    has_assignee: bool,
) -> bool:
    """Decide a status change; return True if it changes the status.

    Checks, in order: the statuses are known values; the actor may manage
    the ticket (an administrator, or an agent who is its assignee); a
    repeated status is a no-op (returns False); the change is listed in
    ``STATUS_TRANSITIONS``; and the ticket has an assignee where the rule
    requires one. Whether the actor is active is the caller's check.

    Raises:
        InvalidStatusTransitionError: unknown status, change not permitted,
            or the ticket must be assigned first.
        TicketPermissionError: the actor may not change this ticket.
    """

    if from_status not in TICKET_STATUSES or to_status not in TICKET_STATUSES:
        raise InvalidStatusTransitionError("Unknown ticket status.")

    role = _role_of(actor_role)
    permitted = role is UserRole.ADMIN or (
        role is UserRole.AGENT and actor_is_assignee
    )

    if not permitted:
        raise TicketPermissionError()

    if from_status == to_status:
        return False

    rule = STATUS_TRANSITIONS.get((from_status, to_status))

    if rule is None:
        # Both values are known statuses, so naming them is safe.
        raise InvalidStatusTransitionError(
            f"Status cannot change from {from_status} to {to_status}."
        )

    if rule.requires_assignee and not has_assignee:
        raise InvalidStatusTransitionError(
            "The ticket must be assigned before this status change."
        )

    return True


def can_manage_ticket(actor: User | None, ticket: Ticket) -> bool:
    """Whether ``actor`` may change ``ticket`` (management, not reading).

    Active administrators manage every ticket; active agents only tickets
    assigned to them; customers, inactive users and unknown roles none.
    Read access is governed separately by the ticket API.
    """

    if actor is None or not actor.is_active:
        return False

    role = _role_of(actor.role)

    if role is UserRole.ADMIN:
        return True

    return (
        role is UserRole.AGENT
        and ticket.assigned_to_user_id is not None
        and ticket.assigned_to_user_id == actor.id
    )


def _is_assignable(user: User | None) -> bool:
    return (
        user is not None
        and bool(user.is_active)
        and _role_of(user.role) in ASSIGNABLE_ROLES
    )


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record_ticket_event(
    db: Session,
    *,
    ticket_id: int,
    actor_id: int,
    event_type: str,
    from_assignee_id: int | None = None,
    to_assignee_id: int | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    reason: str | None = None,
) -> TicketEvent:
    """Add one history event to the session; never commits or flushes.

    The caller owns the transaction: the event is written with the
    caller's flush or commit, and disappears with the caller's rollback.
    Only approved values are accepted.

    Raises:
        InvalidTicketEventError: an event type, status or reason outside
            the approved set. Nothing was added.
    """

    if event_type not in EVENT_TYPES:
        raise InvalidTicketEventError("Unknown ticket event type.")

    for status in (from_status, to_status):
        if status is not None and status not in TICKET_STATUSES:
            raise InvalidTicketEventError("Unknown ticket status.")

    if reason is not None and reason not in UNASSIGNMENT_REASONS:
        raise InvalidTicketEventError("Unknown ticket event reason.")

    event = TicketEvent(
        ticket_id=ticket_id,
        actor_user_id=actor_id,
        event_type=event_type,
        from_assignee_id=from_assignee_id,
        to_assignee_id=to_assignee_id,
        from_status=from_status,
        to_status=to_status,
        reason=reason,
        created_at=_now(),
    )
    db.add(event)

    return event


def list_ticket_events(db: Session, ticket_id: int) -> list[TicketEvent]:
    """The history of one ticket, oldest first (``created_at``, then ``id``).

    Only that ticket's events are returned; a missing ticket has none.
    Whether the caller may read the ticket is checked by the caller.
    """

    return list(
        db.scalars(
            select(TicketEvent)
            .where(TicketEvent.ticket_id == ticket_id)
            .order_by(TicketEvent.created_at, TicketEvent.id)
        )
    )


# ---------------------------------------------------------------------------
# Locked reads
# ---------------------------------------------------------------------------


def _user_for_update(user_id: int) -> Select:
    return (
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _ticket_for_update(ticket_id: int) -> Select:
    return (
        select(Ticket)
        .where(Ticket.id == ticket_id)
        .with_for_update()
        # Overwrite any stale copy already in the session.
        .execution_options(populate_existing=True)
    )


@dataclass
class _LockedState:
    """What a change read inside its serialised transaction."""

    active_admin_ids: list[int]
    users: dict[int, User | None]
    ticket: Ticket | None


def _begin_serialised_change(db: Session) -> None:
    """Start the write-locked transaction (shared ``app.db.locking`` helper).

    Called through this module-level name so tests can run a change
    without the lock.
    """

    _locking.begin_serialised_change(db)


def _lock_for_ticket_change(
    db: Session,
    *,
    ticket_id: int,
    user_ids: tuple[int, ...] = (),
    lock_admins: bool = False,
) -> _LockedState:
    """Take the lock, then read everything a change depends on.

    Order: administrator rows (if requested), the given users in the given
    order, then the ticket (see "Lock order" in the module docstring).
    """

    _begin_serialised_change(db)

    # Shared with user administration (app.services.user_locking), so
    # both take administrator row locks with one query, in one order.
    admin_ids = lock_active_administrator_ids(db) if lock_admins else []
    users = {
        user_id: db.scalars(_user_for_update(user_id)).one_or_none()
        for user_id in user_ids
    }
    ticket = db.scalars(_ticket_for_update(ticket_id)).one_or_none()

    return _LockedState(admin_ids, users, ticket)


# ---------------------------------------------------------------------------
# Transaction handling
# ---------------------------------------------------------------------------

_T = TypeVar("_T")


def _check_expected_version(expected_version: object) -> None:
    if isinstance(expected_version, bool) or not isinstance(expected_version, int):
        raise TypeError("expected_version must be an integer.")


def _run_change(db: Session, change: Callable[[], _T]) -> _T:
    """Run ``change`` and commit; on ANY failure roll back and re-raise.

    The original failure stays primary. A stale versioned write becomes
    ``TicketVersionConflictError`` (carrying any rollback failure).
    """

    try:
        result = change()
        db.commit()

    except Exception as caught:
        primary = roll_back_keeping_primary(db, caught)

        if isinstance(primary, StaleDataError):
            conflict = TicketVersionConflictError()
            record_rollback_failure(conflict, rollback_failure_of(primary))
            raise conflict from None

        if primary is caught:
            raise

        raise primary from None

    return result


def _require_version(ticket: Ticket, expected_version: int) -> None:
    if ticket.version != expected_version:
        raise TicketVersionConflictError()


def _set_assignee(ticket: Ticket, assignee_id: int | None) -> None:
    ticket.assigned_to_user_id = assignee_id
    ticket.assigned_at = _now() if assignee_id is not None else None


# ---------------------------------------------------------------------------
# Change results
#
# The ``*_with_result`` functions return what changed, captured inside the
# transaction before its commit, so callers (the API's audit log) never
# need to read the database again after the commit. The plain functions
# return only the ticket.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssignmentResult:
    """Outcome of an assignment, reassignment or unassignment.

    ``changed`` is False for a no-op (``event_type`` is then None).
    """

    ticket: Ticket
    changed: bool
    event_type: str | None
    previous_assignee_id: int | None
    new_assignee_id: int | None


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of a claim (a claim always changes the ticket)."""

    ticket: Ticket
    assignee_id: int


@dataclass(frozen=True)
class StatusChangeResult:
    """Outcome of a status change, or of an edit that may change status.

    ``changed`` is True only when the status itself changed.
    """

    ticket: Ticket
    changed: bool
    from_status: str
    to_status: str


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


def assign_ticket(
    db: Session,
    *,
    actor_id: int,
    ticket_id: int,
    assignee_id: int | None,
    expected_version: int,
) -> Ticket:
    """Assign, reassign or unassign a ticket; see ``assign_ticket_with_result``."""

    return assign_ticket_with_result(
        db,
        actor_id=actor_id,
        ticket_id=ticket_id,
        assignee_id=assignee_id,
        expected_version=expected_version,
    ).ticket


def assign_ticket_with_result(
    db: Session,
    *,
    actor_id: int,
    ticket_id: int,
    assignee_id: int | None,
    expected_version: int,
) -> AssignmentResult:
    """Assign, reassign or (``assignee_id=None``) unassign a ticket.

    Checks, in order, inside the serialised transaction: the actor is
    still an active administrator; the ticket exists; ``expected_version``
    matches; a selected assignee exists, is active and is an agent; and
    the assignment actually changes (otherwise a no-op).

    A change records ``assigned`` (from nobody), ``reassigned`` (from
    another agent) or ``unassigned``. Unassigning an "In Progress" ticket
    also moves it back to "Open", recorded on the same event.

    Returns an ``AssignmentResult``; its ticket is expired by the commit
    and reloads on access.

    Raises:
        TicketPermissionError, TicketNotFoundError,
        TicketVersionConflictError, AssigneeNotAssignableError,
        TypeError (``expected_version`` is not an integer),
        sqlalchemy.exc.SQLAlchemyError (including a lock time-out).
    """

    _check_expected_version(expected_version)

    def change() -> AssignmentResult:
        state = _lock_for_ticket_change(
            db,
            ticket_id=ticket_id,
            user_ids=() if assignee_id is None else (assignee_id,),
            lock_admins=True,
        )

        if actor_id not in state.active_admin_ids:
            raise TicketPermissionError()

        ticket = state.ticket

        if ticket is None:
            raise TicketNotFoundError()

        _require_version(ticket, expected_version)

        if assignee_id is not None and not _is_assignable(state.users[assignee_id]):
            raise AssigneeNotAssignableError()

        previous_assignee = ticket.assigned_to_user_id

        if previous_assignee == assignee_id:
            return AssignmentResult(
                ticket=ticket,
                changed=False,
                event_type=None,
                previous_assignee_id=previous_assignee,
                new_assignee_id=assignee_id,
            )

        from_status = ticket.status
        to_status = from_status

        if assignee_id is None and from_status == STATUS_IN_PROGRESS:
            to_status = STATUS_OPEN
            ticket.status = to_status

        _set_assignee(ticket, assignee_id)

        if assignee_id is None:
            event_type = EVENT_UNASSIGNED
        elif previous_assignee is None:
            event_type = EVENT_ASSIGNED
        else:
            event_type = EVENT_REASSIGNED

        status_changed = to_status != from_status
        record_ticket_event(
            db,
            ticket_id=ticket.id,
            actor_id=actor_id,
            event_type=event_type,
            from_assignee_id=previous_assignee,
            to_assignee_id=assignee_id,
            from_status=from_status if status_changed else None,
            to_status=to_status if status_changed else None,
        )

        return AssignmentResult(
            ticket=ticket,
            changed=True,
            event_type=event_type,
            previous_assignee_id=previous_assignee,
            new_assignee_id=assignee_id,
        )

    return _run_change(db, change)


def claim_ticket(
    db: Session,
    *,
    actor_id: int,
    ticket_id: int,
    expected_version: int,
) -> Ticket:
    """Claim an unassigned ticket; see ``claim_ticket_with_result``."""

    return claim_ticket_with_result(
        db,
        actor_id=actor_id,
        ticket_id=ticket_id,
        expected_version=expected_version,
    ).ticket


def claim_ticket_with_result(
    db: Session,
    *,
    actor_id: int,
    ticket_id: int,
    expected_version: int,
) -> ClaimResult:
    """Assign an unassigned ticket to the acting agent.

    Checks, in order, inside the serialised transaction: the actor is an
    active agent; the ticket exists; it has no assignee (otherwise
    ``TicketAlreadyAssignedError``, also if it is assigned to the actor);
    it is not Resolved (otherwise ``TicketNotClaimableError``; Open and
    In Progress tickets, including legacy unassigned In Progress ones, can
    be claimed); and ``expected_version`` matches. Records an ``assigned``
    event. The status is not changed.

    Two agents claiming the same ticket at once cannot both succeed: the
    second waits for the lock and then finds the ticket assigned.

    Raises:
        TicketPermissionError, TicketNotFoundError,
        TicketAlreadyAssignedError, TicketNotClaimableError,
        TicketVersionConflictError, TypeError,
        sqlalchemy.exc.SQLAlchemyError.
    """

    _check_expected_version(expected_version)

    def change() -> ClaimResult:
        state = _lock_for_ticket_change(
            db,
            ticket_id=ticket_id,
            user_ids=(actor_id,),
        )

        if not _is_assignable(state.users[actor_id]):
            raise TicketPermissionError()

        ticket = state.ticket

        if ticket is None:
            raise TicketNotFoundError()

        if ticket.assigned_to_user_id is not None:
            raise TicketAlreadyAssignedError()

        if ticket.status == STATUS_RESOLVED:
            raise TicketNotClaimableError()

        _require_version(ticket, expected_version)

        _set_assignee(ticket, actor_id)
        record_ticket_event(
            db,
            ticket_id=ticket.id,
            actor_id=actor_id,
            event_type=EVENT_ASSIGNED,
            to_assignee_id=actor_id,
        )

        return ClaimResult(ticket=ticket, assignee_id=actor_id)

    return _run_change(db, change)


# ---------------------------------------------------------------------------
# Status changes
# ---------------------------------------------------------------------------


def change_ticket_status(
    db: Session,
    *,
    actor_id: int,
    ticket_id: int,
    new_status: str,
    expected_version: int,
) -> Ticket:
    """Change a ticket's status; see ``change_ticket_status_with_result``."""

    return change_ticket_status_with_result(
        db,
        actor_id=actor_id,
        ticket_id=ticket_id,
        new_status=new_status,
        expected_version=expected_version,
    ).ticket


def change_ticket_status_with_result(
    db: Session,
    *,
    actor_id: int,
    ticket_id: int,
    new_status: str,
    expected_version: int,
) -> StatusChangeResult:
    """Change a ticket's status according to ``STATUS_TRANSITIONS``.

    Checks, in order: ``new_status`` is a known status (before any
    database work); then, inside the serialised transaction, the actor
    exists and is active; the ticket exists; the actor may manage it
    (``can_manage_ticket``); ``expected_version`` matches; and the change
    is permitted (``validate_status_transition``). A repeated status is a
    no-op. A real change records a ``status_changed`` event.

    Raises:
        InvalidStatusTransitionError, TicketPermissionError,
        TicketNotFoundError, TicketVersionConflictError, TypeError,
        sqlalchemy.exc.SQLAlchemyError.
    """

    _check_expected_version(expected_version)

    if new_status not in TICKET_STATUSES:
        raise InvalidStatusTransitionError("Unknown ticket status.")

    def change() -> StatusChangeResult:
        state = _lock_for_ticket_change(
            db,
            ticket_id=ticket_id,
            user_ids=(actor_id,),
        )
        actor = state.users[actor_id]

        if actor is None or not actor.is_active:
            raise TicketPermissionError()

        ticket = state.ticket

        if ticket is None:
            raise TicketNotFoundError()

        if not can_manage_ticket(actor, ticket):
            raise TicketPermissionError()

        _require_version(ticket, expected_version)

        from_status = ticket.status
        changed = validate_status_transition(
            from_status,
            new_status,
            actor_role=actor.role,
            actor_is_assignee=ticket.assigned_to_user_id == actor.id,
            has_assignee=ticket.assigned_to_user_id is not None,
        )

        if changed:
            ticket.status = new_status
            record_ticket_event(
                db,
                ticket_id=ticket.id,
                actor_id=actor_id,
                event_type=EVENT_STATUS_CHANGED,
                from_status=from_status,
                to_status=new_status,
            )

        return StatusChangeResult(
            ticket=ticket,
            changed=changed,
            from_status=from_status,
            to_status=new_status if changed else from_status,
        )

    return _run_change(db, change)


# ---------------------------------------------------------------------------
# General ticket editing
# ---------------------------------------------------------------------------

# Fields a manager may change with ``edit_ticket``. Assignment, version,
# creator and AI prediction fields are deliberately absent.
EDITABLE_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "description",
        "requester_name",
        "category",
        "priority",
        "status",
    }
)


def edit_ticket(
    db: Session,
    *,
    actor_id: int,
    ticket_id: int,
    updates: Mapping[str, object],
    expected_version: int | None = None,
) -> Ticket:
    """Edit a ticket's fields; see ``edit_ticket_with_result``."""

    return edit_ticket_with_result(
        db,
        actor_id=actor_id,
        ticket_id=ticket_id,
        updates=updates,
        expected_version=expected_version,
    ).ticket


def edit_ticket_with_result(
    db: Session,
    *,
    actor_id: int,
    ticket_id: int,
    updates: Mapping[str, object],
    expected_version: int | None = None,
) -> StatusChangeResult:
    """Change a ticket's editable fields on behalf of ``actor_id``.

    Checks, in order: every field is in ``EDITABLE_FIELDS`` and a new
    status is a known status (before any database work); then, inside the
    serialised transaction, the actor exists and is active; the ticket
    exists; the actor may manage it (``can_manage_ticket``: an active
    administrator, or the agent it is assigned to); ``expected_version``
    matches, when given; and a status change is permitted
    (``validate_status_transition``).

    The other fields are applied as given (the API's request schema
    validates them). A real status change records a ``status_changed``
    event in the same transaction. Changing nothing writes nothing.

    Raises:
        ValueError: a field outside ``EDITABLE_FIELDS``.
        InvalidStatusTransitionError, TicketPermissionError,
        TicketNotFoundError, TicketVersionConflictError, TypeError,
        sqlalchemy.exc.SQLAlchemyError.
    """

    if expected_version is not None:
        _check_expected_version(expected_version)

    invalid_fields = set(updates) - EDITABLE_FIELDS

    if invalid_fields:
        raise ValueError("Some fields cannot be updated.")

    new_status = updates.get("status")

    if "status" in updates and new_status not in TICKET_STATUSES:
        raise InvalidStatusTransitionError("Unknown ticket status.")

    def change() -> StatusChangeResult:
        state = _lock_for_ticket_change(
            db,
            ticket_id=ticket_id,
            user_ids=(actor_id,),
        )
        actor = state.users[actor_id]

        if actor is None or not actor.is_active:
            raise TicketPermissionError()

        ticket = state.ticket

        if ticket is None:
            raise TicketNotFoundError()

        if not can_manage_ticket(actor, ticket):
            raise TicketPermissionError()

        if expected_version is not None:
            _require_version(ticket, expected_version)

        from_status = ticket.status
        status_changed = "status" in updates and validate_status_transition(
            from_status,
            new_status,
            actor_role=actor.role,
            actor_is_assignee=ticket.assigned_to_user_id == actor.id,
            has_assignee=ticket.assigned_to_user_id is not None,
        )

        for field, value in updates.items():
            if field != "status":
                setattr(ticket, field, value)

        if status_changed:
            ticket.status = new_status
            record_ticket_event(
                db,
                ticket_id=ticket.id,
                actor_id=actor_id,
                event_type=EVENT_STATUS_CHANGED,
                from_status=from_status,
                to_status=new_status,
            )

        return StatusChangeResult(
            ticket=ticket,
            changed=status_changed,
            from_status=from_status,
            to_status=new_status if status_changed else from_status,
        )

    return _run_change(db, change)


# ---------------------------------------------------------------------------
# Unassignment for user administration
# ---------------------------------------------------------------------------


def unassign_open_tickets_for_user(
    db: Session,
    *,
    actor_id: int,
    user_id: int,
    reason: str,
) -> list[int]:
    """Unassign the user's unresolved tickets inside the caller's transaction.

    For each ticket assigned to ``user_id``, in id order: an "Open" ticket
    is unassigned and stays Open; an "In Progress" ticket is unassigned and
    returns to Open; a "Resolved" ticket keeps its assignee and is not
    touched. Every changed ticket gets one ``unassigned`` event with
    ``reason`` (and the status change, where there is one), and its
    version is incremented.

    Never begins, commits or rolls back: the caller (user administration,
    inside its own serialised transaction) owns the transaction and must
    roll back on any failure. The changes are flushed, so they are visible
    to the caller's later queries.

    Returns the IDs of the changed tickets, ascending.

    Raises:
        InvalidTicketEventError: ``reason`` is not an approved reason.
            Nothing was changed.
        sqlalchemy.exc.SQLAlchemyError: a database failure; the caller
            must roll back.
    """

    if reason not in UNASSIGNMENT_REASONS:
        raise InvalidTicketEventError("Unknown ticket event reason.")

    tickets = db.scalars(
        select(Ticket)
        .where(
            Ticket.assigned_to_user_id == user_id,
            Ticket.status.in_((STATUS_OPEN, STATUS_IN_PROGRESS)),
        )
        .order_by(Ticket.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()

    changed_ids: list[int] = []

    for ticket in tickets:
        from_status = ticket.status
        to_status = STATUS_OPEN
        _set_assignee(ticket, None)
        ticket.status = to_status

        status_changed = to_status != from_status
        record_ticket_event(
            db,
            ticket_id=ticket.id,
            actor_id=actor_id,
            event_type=EVENT_UNASSIGNED,
            from_assignee_id=user_id,
            from_status=from_status if status_changed else None,
            to_status=to_status if status_changed else None,
            reason=reason,
        )
        changed_ids.append(ticket.id)

    if changed_ids:
        db.flush()

    return changed_ids
