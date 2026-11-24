"""Ticket workflow service (app.services.ticket_workflow).

Most tests use conftest's in-memory SQLite session (foreign keys
enforced). Concurrency and "does not commit" tests use a temporary SQLite
FILE with one connection per session/thread, following the Phase 6 admin
concurrency tests. resolveai.db is never used.
"""

import itertools
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.roles import UserRole
from app.db.base import Base
from app.db.errors import (
    UNEXPECTED_ROLLBACK_FAILURE,
    describe_rollback_failure,
    rollback_failure_of,
)
from app.db.locking import TransactionAlreadyStartedError
from app.models import Ticket, TicketEvent, User
from app.services import ticket_workflow
from app.services.ticket_workflow import (
    ASSIGNABLE_ROLES,
    STATUS_TRANSITIONS,
    TICKET_STATUSES,
    AssigneeNotAssignableError,
    InvalidStatusTransitionError,
    InvalidTicketEventError,
    TicketAlreadyAssignedError,
    TicketNotClaimableError,
    TicketNotFoundError,
    TicketPermissionError,
    TicketVersionConflictError,
    TicketWorkflowError,
    assign_ticket,
    can_manage_ticket,
    change_ticket_status,
    claim_ticket,
    list_ticket_events,
    record_ticket_event,
    unassign_open_tickets_for_user,
    validate_status_transition,
)
from app.services.user_service import set_user_active_status


OPEN, IN_PROGRESS, RESOLVED = "Open", "In Progress", "Resolved"

EVENT_FIELDS = (
    "event_type",
    "actor_user_id",
    "from_assignee_id",
    "to_assignee_id",
    "from_status",
    "to_status",
    "reason",
)

SECRET_TITLE = "Title-secret-7f3a"
SECRET_DESCRIPTION = "Description-secret-9c1e with card 4111 1111 1111 1111"


# ---------------------------------------------------------------------------
# Fixtures and helpers (in-memory session)
# ---------------------------------------------------------------------------


@pytest.fixture
def make_user(db_session: Session) -> Callable[..., User]:
    """Persist a user without hashing a password (these tests never log in)."""

    def _make(*, email: str, role: str, name: str = "Test User") -> User:
        user = User(name=name, email=email, password_hash="unused-hash", role=role)
        db_session.add(user)
        db_session.commit()

        return user

    return _make


@pytest.fixture
def people(make_user: Callable[..., User]) -> SimpleNamespace:
    """One user per role and state; names and emails are never logged."""

    return SimpleNamespace(
        admin=make_user(email="admin@example.test", role="admin", name="Ada Admin"),
        agent=make_user(email="agent@example.test", role="agent", name="Amy Agent"),
        other_agent=make_user(
            email="other.agent@example.test", role="agent", name="Otto Other"
        ),
        customer=make_user(
            email="customer@example.test", role="customer", name="Cory Customer"
        ),
    )


@pytest.fixture
def make_ticket(db_session: Session, people: SimpleNamespace) -> Callable[..., Ticket]:
    def _make(*, status: str = OPEN, assignee: User | None = None) -> Ticket:
        ticket = Ticket(
            title=SECRET_TITLE,
            description=SECRET_DESCRIPTION,
            creator=people.customer,
            status=status,
            assigned_to_user_id=None if assignee is None else assignee.id,
            assigned_at=None if assignee is None else datetime.now(timezone.utc),
        )
        db_session.add(ticket)
        db_session.commit()

        return ticket

    return _make


def _ticket_row(db: Session, ticket_id: int) -> tuple:
    """(assigned_to_user_id, status, version, assigned_at is set), from SQL."""

    row = db.execute(
        text(
            "SELECT assigned_to_user_id, status, version, assigned_at "
            "FROM tickets WHERE id = :id"
        ),
        {"id": ticket_id},
    ).one()

    return (row[0], row[1], row[2], row[3] is not None)


def _event_rows(db: Session, ticket_id: int | None = None) -> list[tuple]:
    """Stored event fields, oldest first, straight from SQL."""

    where = "" if ticket_id is None else "WHERE ticket_id = :id"
    rows = db.execute(
        text(
            f"SELECT {', '.join(EVENT_FIELDS)} FROM ticket_events {where} "
            "ORDER BY created_at, id"
        ),
        {"id": ticket_id},
    )

    return [tuple(row) for row in rows]


def _set_user(db: Session, user: User, **values) -> None:
    for key, value in values.items():
        setattr(user, key, value)

    db.commit()


def _commit_before_lock(
    monkeypatch: pytest.MonkeyPatch,
    statement: str,
    parameters: dict,
) -> None:
    """Simulate another request committing between authorisation and lock."""

    original = ticket_workflow._begin_serialised_change

    def concurrent_commit_then_lock(db: Session) -> None:
        db.execute(text(statement), parameters)
        db.commit()
        original(db)

    monkeypatch.setattr(
        ticket_workflow, "_begin_serialised_change", concurrent_commit_then_lock
    )


def _operation_error() -> OperationalError:
    driver_error = sqlite3.OperationalError("disk I/O error: secret-driver-text")
    driver_error.sqlite_errorname = "SQLITE_IOERR_WRITE"

    return OperationalError("UPDATE tickets SET secret=?", ("secret",), driver_error)


def _raiser(error: BaseException) -> Callable[..., None]:
    def _raise(*_args, **_kwargs) -> None:
        raise error

    return _raise


# ---------------------------------------------------------------------------
# Constants and pure rules
# ---------------------------------------------------------------------------


def test_only_agents_are_assignable() -> None:
    assert ASSIGNABLE_ROLES == frozenset({UserRole.AGENT})
    assert TICKET_STATUSES == (OPEN, IN_PROGRESS, RESOLVED)


# Independent statement of the approved table: (from, to) -> requires an
# assignee. Every other pair with from != to is rejected.
APPROVED_TRANSITIONS = {
    (OPEN, IN_PROGRESS): True,
    (OPEN, RESOLVED): True,
    (IN_PROGRESS, OPEN): False,
    (IN_PROGRESS, RESOLVED): False,
    (RESOLVED, IN_PROGRESS): True,
}


def test_transition_table_matches_the_approved_rules() -> None:
    assert {
        pair: rule.requires_assignee for pair, rule in STATUS_TRANSITIONS.items()
    } == APPROVED_TRANSITIONS
    assert (RESOLVED, OPEN) not in STATUS_TRANSITIONS


def _expected_outcome(
    from_status: str,
    to_status: str,
    role: str,
    is_assignee: bool,
    has_assignee: bool,
) -> str:
    if not (role == "admin" or (role == "agent" and is_assignee)):
        return "forbidden"

    if from_status == to_status:
        return "no-op"

    if (from_status, to_status) not in APPROVED_TRANSITIONS:
        return "invalid"

    if APPROVED_TRANSITIONS[(from_status, to_status)] and not has_assignee:
        return "invalid"

    return "changed"


# (actor role, actor is the assignee, ticket has an assignee). An actor
# can only be the assignee of an assigned ticket.
ACTOR_CASES = [
    ("admin", False, True),
    ("admin", False, False),
    ("agent", True, True),
    ("agent", False, True),
    ("agent", False, False),
    ("customer", False, True),
    ("customer", False, False),
    ("Admin", False, True),  # legacy spelling: not an administrator
    ("superuser", False, True),
]


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    list(itertools.product(TICKET_STATUSES, repeat=2)),
    ids=lambda value: value.replace(" ", ""),
)
@pytest.mark.parametrize(
    ("role", "is_assignee", "has_assignee"),
    ACTOR_CASES,
    ids=[f"{r}-assignee{int(a)}-assigned{int(h)}" for r, a, h in ACTOR_CASES],
)
def test_validate_status_transition_full_matrix(
    from_status: str,
    to_status: str,
    role: str,
    is_assignee: bool,
    has_assignee: bool,
) -> None:
    expected = _expected_outcome(from_status, to_status, role, is_assignee, has_assignee)

    def decide() -> bool:
        return validate_status_transition(
            from_status,
            to_status,
            actor_role=role,
            actor_is_assignee=is_assignee,
            has_assignee=has_assignee,
        )

    if expected == "forbidden":
        with pytest.raises(TicketPermissionError):
            decide()
    elif expected == "invalid":
        with pytest.raises(InvalidStatusTransitionError):
            decide()
    else:
        assert decide() is (expected == "changed")


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [("Open", "Closed"), ("Closed", "Open"), ("open", "Resolved")],
)
def test_validate_status_transition_rejects_unknown_statuses(
    from_status: str,
    to_status: str,
) -> None:
    with pytest.raises(InvalidStatusTransitionError) as error:
        validate_status_transition(
            from_status,
            to_status,
            actor_role=UserRole.ADMIN,
            actor_is_assignee=False,
            has_assignee=True,
        )

    assert str(error.value) == "Unknown ticket status."


def test_rejected_transition_message_is_fixed() -> None:
    with pytest.raises(InvalidStatusTransitionError) as error:
        validate_status_transition(
            RESOLVED,
            OPEN,
            actor_role="admin",
            actor_is_assignee=False,
            has_assignee=True,
        )

    assert str(error.value) == "Status cannot change from Resolved to Open."


def test_can_manage_ticket(people: SimpleNamespace, make_ticket) -> None:
    mine = make_ticket(assignee=people.agent)
    theirs = make_ticket(assignee=people.other_agent)
    unassigned = make_ticket()

    for ticket in (mine, theirs, unassigned):
        assert can_manage_ticket(people.admin, ticket) is True
        assert can_manage_ticket(people.customer, ticket) is False
        assert can_manage_ticket(None, ticket) is False

    assert can_manage_ticket(people.agent, mine) is True
    assert can_manage_ticket(people.agent, theirs) is False
    assert can_manage_ticket(people.agent, unassigned) is False

    people.agent.is_active = False
    people.admin.is_active = False

    assert can_manage_ticket(people.agent, mine) is False
    assert can_manage_ticket(people.admin, mine) is False

    people.admin.is_active = True
    people.admin.role = "Admin"

    assert can_manage_ticket(people.admin, mine) is False


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


def test_admin_assigns_an_unassigned_ticket(
    db_session: Session, people: SimpleNamespace, make_ticket
) -> None:
    ticket = make_ticket()
    before = datetime.now(timezone.utc)

    result = assign_ticket(
        db_session,
        actor_id=people.admin.id,
        ticket_id=ticket.id,
        assignee_id=people.agent.id,
        expected_version=1,
    )

    assert result is ticket
    assert _ticket_row(db_session, ticket.id) == (people.agent.id, OPEN, 2, True)
    assert ticket.assignee is people.agent
    assert ticket.assigned_at.replace(tzinfo=timezone.utc) >= before - timedelta(seconds=5)
    assert _event_rows(db_session, ticket.id) == [
        ("assigned", people.admin.id, None, people.agent.id, None, None, None),
    ]


def test_admin_reassigns_a_ticket(
    db_session: Session, people: SimpleNamespace, make_ticket
) -> None:
    ticket = make_ticket(status=IN_PROGRESS, assignee=people.agent)

    assign_ticket(
        db_session,
        actor_id=people.admin.id,
        ticket_id=ticket.id,
        assignee_id=people.other_agent.id,
        expected_version=1,
    )

    assert _ticket_row(db_session, ticket.id) == (
        people.other_agent.id,
        IN_PROGRESS,
        2,
        True,
    )
    assert _event_rows(db_session, ticket.id) == [
        (
            "reassigned",
            people.admin.id,
            people.agent.id,
            people.other_agent.id,
            None,
            None,
            None,
        ),
    ]


@pytest.mark.parametrize(
    ("status", "expected_status", "status_fields"),
    [
        (OPEN, OPEN, (None, None)),
        (IN_PROGRESS, OPEN, (IN_PROGRESS, OPEN)),
        (RESOLVED, RESOLVED, (None, None)),
    ],
    ids=["open", "in-progress-returns-to-open", "resolved"],
)
def test_admin_unassigns_a_ticket(
    db_session: Session,
    people: SimpleNamespace,
    make_ticket,
    status: str,
    expected_status: str,
    status_fields: tuple,
) -> None:
    ticket = make_ticket(status=status, assignee=people.agent)

    assign_ticket(
        db_session,
        actor_id=people.admin.id,
        ticket_id=ticket.id,
        assignee_id=None,
        expected_version=1,
    )

    assert _ticket_row(db_session, ticket.id) == (None, expected_status, 2, False)
    assert _event_rows(db_session, ticket.id) == [
        ("unassigned", people.admin.id, people.agent.id, None, *status_fields, None),
    ]


@pytest.mark.parametrize("assigned", [True, False], ids=["same-agent", "still-unassigned"])
def test_repeated_assignment_is_a_no_op(
    db_session: Session, people: SimpleNamespace, make_ticket, assigned: bool
) -> None:
    ticket = make_ticket(assignee=people.agent if assigned else None)
    before = _ticket_row(db_session, ticket.id)
    assigned_at = ticket.assigned_at

    assign_ticket(
        db_session,
        actor_id=people.admin.id,
        ticket_id=ticket.id,
        assignee_id=people.agent.id if assigned else None,
        expected_version=1,
    )

    assert _ticket_row(db_session, ticket.id) == before
    assert ticket.assigned_at == assigned_at
    assert _event_rows(db_session) == []


@pytest.mark.parametrize(
    "assignee",
    ["inactive agent", "customer", "admin", "inactive admin", "legacy Agent role", "missing"],
)
def test_assignment_to_a_non_assignable_user_is_rejected(
    db_session: Session,
    people: SimpleNamespace,
    make_user: Callable[..., User],
    make_ticket,
    assignee: str,
) -> None:
    ticket = make_ticket()
    target_id = {
        "inactive agent": lambda: _inactive(db_session, make_user, "agent"),
        "customer": lambda: people.customer.id,
        "admin": lambda: make_user(email="second.admin@example.test", role="admin").id,
        "inactive admin": lambda: _inactive(db_session, make_user, "admin"),
        "legacy Agent role": lambda: make_user(email="legacy@example.test", role="Agent").id,
        "missing": lambda: 999,
    }[assignee]()

    with pytest.raises(AssigneeNotAssignableError) as error:
        assign_ticket(
            db_session,
            actor_id=people.admin.id,
            ticket_id=ticket.id,
            assignee_id=target_id,
            expected_version=1,
        )

    assert str(error.value) == "Assignee must be an active agent."
    assert _ticket_row(db_session, ticket.id) == (None, OPEN, 1, False)
    assert _event_rows(db_session) == []


def _inactive(db: Session, make_user: Callable[..., User], role: str) -> int:
    user = make_user(email=f"inactive.{role}@example.test", role=role)
    _set_user(db, user, is_active=False)

    return user.id


def test_reassigning_to_the_current_but_now_inactive_agent_is_rejected(
    db_session: Session, people: SimpleNamespace, make_ticket
) -> None:
    ticket = make_ticket(assignee=people.agent)
    _set_user(db_session, people.agent, is_active=False)

    with pytest.raises(AssigneeNotAssignableError):
        assign_ticket(
            db_session,
            actor_id=people.admin.id,
            ticket_id=ticket.id,
            assignee_id=people.agent.id,
            expected_version=1,
        )


@pytest.mark.parametrize("actor", ["agent", "customer", "inactive admin", "missing"])
def test_only_an_active_admin_can_assign(
    db_session: Session,
    people: SimpleNamespace,
    make_user: Callable[..., User],
    make_ticket,
    actor: str,
) -> None:
    ticket = make_ticket()
    actor_id = {
        "agent": lambda: people.agent.id,
        "customer": lambda: people.customer.id,
        "inactive admin": lambda: _inactive(db_session, make_user, "admin"),
        "missing": lambda: 999,
    }[actor]()

    with pytest.raises(TicketPermissionError) as error:
        assign_ticket(
            db_session,
            actor_id=actor_id,
            ticket_id=ticket.id,
            assignee_id=people.agent.id,
            expected_version=1,
        )

    assert str(error.value) == "You do not have permission to perform this action."
    assert _ticket_row(db_session, ticket.id) == (None, OPEN, 1, False)
    assert _event_rows(db_session) == []


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE users SET is_active = 0 WHERE id = :actor",
        "UPDATE users SET role = 'agent' WHERE id = :actor",
    ],
    ids=["actor deactivated", "actor demoted"],
)
def test_admin_who_lost_access_before_the_lock_cannot_assign(
    db_session: Session,
    people: SimpleNamespace,
    make_ticket,
    monkeypatch: pytest.MonkeyPatch,
    statement: str,
) -> None:
    ticket = make_ticket()
    # The request was authorised with this (now stale) admin object.
    assert people.admin.role == "admin" and people.admin.is_active
    _commit_before_lock(monkeypatch, statement, {"actor": people.admin.id})

    with pytest.raises(TicketPermissionError):
        assign_ticket(
            db_session,
            actor_id=people.admin.id,
            ticket_id=ticket.id,
            assignee_id=people.agent.id,
            expected_version=1,
        )

    assert _ticket_row(db_session, ticket.id) == (None, OPEN, 1, False)
    assert _event_rows(db_session) == []


def test_assignee_deactivated_before_the_lock_is_rejected(
    db_session: Session,
    people: SimpleNamespace,
    make_ticket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = make_ticket()
    _commit_before_lock(
        monkeypatch,
        "UPDATE users SET is_active = 0 WHERE id = :agent",
        {"agent": people.agent.id},
    )

    with pytest.raises(AssigneeNotAssignableError):
        assign_ticket(
            db_session,
            actor_id=people.admin.id,
            ticket_id=ticket.id,
            assignee_id=people.agent.id,
            expected_version=1,
        )

    assert _event_rows(db_session) == []


@pytest.mark.parametrize("operation", ["assign", "claim", "status"])
def test_missing_ticket_is_reported(
    db_session: Session, people: SimpleNamespace, operation: str
) -> None:
    calls = {
        "assign": lambda: assign_ticket(
            db_session,
            actor_id=people.admin.id,
            ticket_id=999,
            assignee_id=people.agent.id,
            expected_version=1,
        ),
        "claim": lambda: claim_ticket(
            db_session, actor_id=people.agent.id, ticket_id=999, expected_version=1
        ),
        "status": lambda: change_ticket_status(
            db_session,
            actor_id=people.admin.id,
            ticket_id=999,
            new_status=IN_PROGRESS,
            expected_version=1,
        ),
    }

    with pytest.raises(TicketNotFoundError) as error:
        calls[operation]()

    assert str(error.value) == "Ticket not found."


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [OPEN, IN_PROGRESS],
    ids=["open", "legacy-unassigned-in-progress"],
)
def test_active_agent_claims_an_unassigned_ticket(
    db_session: Session, people: SimpleNamespace, make_ticket, status: str
) -> None:
    ticket = make_ticket(status=status)

    claim_ticket(
        db_session, actor_id=people.agent.id, ticket_id=ticket.id, expected_version=1
    )

    # Claiming assigns the ticket without changing its status.
    assert _ticket_row(db_session, ticket.id) == (people.agent.id, status, 2, True)
    assert _event_rows(db_session, ticket.id) == [
        ("assigned", people.agent.id, None, people.agent.id, None, None, None),
    ]


def test_an_unassigned_resolved_ticket_cannot_be_claimed(
    db_session: Session, people: SimpleNamespace, make_ticket
) -> None:
    ticket = make_ticket(status=RESOLVED)

    with pytest.raises(TicketNotClaimableError) as error:
        claim_ticket(
            db_session, actor_id=people.agent.id, ticket_id=ticket.id, expected_version=1
        )

    assert str(error.value) == "Resolved tickets cannot be claimed."
    # Unchanged: still unassigned, still Resolved, still version 1.
    assert _ticket_row(db_session, ticket.id) == (None, RESOLVED, 1, False)
    assert _event_rows(db_session) == []


def test_an_assigned_resolved_ticket_still_reports_already_assigned(
    db_session: Session, people: SimpleNamespace, make_ticket
) -> None:
    ticket = make_ticket(status=RESOLVED, assignee=people.other_agent)

    with pytest.raises(TicketAlreadyAssignedError):
        claim_ticket(
            db_session, actor_id=people.agent.id, ticket_id=ticket.id, expected_version=1
        )

    assert _ticket_row(db_session, ticket.id) == (people.other_agent.id, RESOLVED, 1, True)
    assert _event_rows(db_session) == []


def test_resolved_claim_is_refused_before_the_version_check(
    db_session: Session, people: SimpleNamespace, make_ticket
) -> None:
    """A stale version does not hide the reason the claim is refused."""

    ticket = make_ticket(status=RESOLVED)

    with pytest.raises(TicketNotClaimableError):
        claim_ticket(
            db_session, actor_id=people.agent.id, ticket_id=ticket.id, expected_version=7
        )

    assert _ticket_row(db_session, ticket.id) == (None, RESOLVED, 1, False)


def test_admin_can_still_assign_a_resolved_ticket(
    db_session: Session, people: SimpleNamespace, make_ticket
) -> None:
    ticket = make_ticket(status=RESOLVED)

    assign_ticket(
        db_session,
        actor_id=people.admin.id,
        ticket_id=ticket.id,
        assignee_id=people.agent.id,
        expected_version=1,
    )

    assert _ticket_row(db_session, ticket.id) == (people.agent.id, RESOLVED, 2, True)
    assert _event_rows(db_session, ticket.id) == [
        ("assigned", people.admin.id, None, people.agent.id, None, None, None),
    ]


@pytest.mark.parametrize("actor", ["customer", "admin", "inactive agent", "missing"])
def test_only_an_active_agent_can_claim(
    db_session: Session,
    people: SimpleNamespace,
    make_user: Callable[..., User],
    make_ticket,
    actor: str,
) -> None:
    ticket = make_ticket()
    actor_id = {
        "customer": lambda: people.customer.id,
        "admin": lambda: people.admin.id,
        "inactive agent": lambda: _inactive(db_session, make_user, "agent"),
        "missing": lambda: 999,
    }[actor]()

    with pytest.raises(TicketPermissionError):
        claim_ticket(
            db_session, actor_id=actor_id, ticket_id=ticket.id, expected_version=1
        )

    assert _ticket_row(db_session, ticket.id) == (None, OPEN, 1, False)
    assert _event_rows(db_session) == []


@pytest.mark.parametrize("holder", ["other agent", "the claimer"])
def test_an_assigned_ticket_cannot_be_claimed(
    db_session: Session, people: SimpleNamespace, make_ticket, holder: str
) -> None:
    owner = people.other_agent if holder == "other agent" else people.agent
    ticket = make_ticket(assignee=owner)

    with pytest.raises(TicketAlreadyAssignedError) as error:
        claim_ticket(
            db_session, actor_id=people.agent.id, ticket_id=ticket.id, expected_version=1
        )

    assert str(error.value) == "Ticket is already assigned."
    assert _ticket_row(db_session, ticket.id) == (owner.id, OPEN, 1, True)
    assert _event_rows(db_session) == []


# ---------------------------------------------------------------------------
# Status changes (service)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    list(itertools.product(TICKET_STATUSES, repeat=2)),
    ids=lambda value: value.replace(" ", ""),
)
@pytest.mark.parametrize(
    "actor_case",
    ["admin-assigned", "admin-unassigned", "assignee", "other-agent", "customer"],
)
def test_change_ticket_status_follows_the_table(
    db_session: Session,
    people: SimpleNamespace,
    make_ticket,
    from_status: str,
    to_status: str,
    actor_case: str,
) -> None:
    assigned = actor_case != "admin-unassigned"
    ticket = make_ticket(status=from_status, assignee=people.agent if assigned else None)
    actor = {
        "admin-assigned": people.admin,
        "admin-unassigned": people.admin,
        "assignee": people.agent,
        "other-agent": people.other_agent,
        "customer": people.customer,
    }[actor_case]
    expected = _expected_outcome(
        from_status,
        to_status,
        actor.role,
        actor.id == ticket.assigned_to_user_id,
        assigned,
    )
    before = _ticket_row(db_session, ticket.id)

    def change() -> Ticket:
        return change_ticket_status(
            db_session,
            actor_id=actor.id,
            ticket_id=ticket.id,
            new_status=to_status,
            expected_version=1,
        )

    if expected in ("forbidden", "invalid"):
        error_type = (
            TicketPermissionError if expected == "forbidden" else InvalidStatusTransitionError
        )

        with pytest.raises(error_type):
            change()

        assert _ticket_row(db_session, ticket.id) == before
        assert _event_rows(db_session) == []
    elif expected == "no-op":
        change()

        assert _ticket_row(db_session, ticket.id) == before
        assert _event_rows(db_session) == []
    else:
        change()

        assert _ticket_row(db_session, ticket.id) == (
            before[0],
            to_status,
            2,
            before[3],
        )
        assert _event_rows(db_session, ticket.id) == [
            ("status_changed", actor.id, None, None, from_status, to_status, None),
        ]

    # After any successful change, In Progress implies an assignee.
    assigned_to, status, _version, _ = _ticket_row(db_session, ticket.id)
    if status == IN_PROGRESS and expected == "changed":
        assert assigned_to is not None


def test_in_progress_without_an_assignee_is_rejected(
    db_session: Session, people: SimpleNamespace, make_ticket
) -> None:
    ticket = make_ticket()

    with pytest.raises(InvalidStatusTransitionError) as error:
        change_ticket_status(
            db_session,
            actor_id=people.admin.id,
            ticket_id=ticket.id,
            new_status=IN_PROGRESS,
            expected_version=1,
        )

    assert str(error.value) == "The ticket must be assigned before this status change."
    assert _ticket_row(db_session, ticket.id) == (None, OPEN, 1, False)


def test_legacy_unassigned_in_progress_ticket_can_be_moved_by_an_admin(
    db_session: Session, people: SimpleNamespace, make_ticket
) -> None:
    """Pre-0003 data: In Progress with no assignee, kept by the migration."""

    ticket = make_ticket(status=IN_PROGRESS)

    change_ticket_status(
        db_session,
        actor_id=people.admin.id,
        ticket_id=ticket.id,
        new_status=OPEN,
        expected_version=1,
    )

    assert _ticket_row(db_session, ticket.id) == (None, OPEN, 2, False)


def test_status_change_refuses_an_unknown_status_before_any_database_work(
    db_session: Session,
    people: SimpleNamespace,
    make_ticket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = make_ticket(assignee=people.agent)
    monkeypatch.setattr(
        ticket_workflow,
        "_lock_for_ticket_change",
        _raiser(AssertionError("must not lock")),
    )

    with pytest.raises(InvalidStatusTransitionError):
        change_ticket_status(
            db_session,
            actor_id=people.admin.id,
            ticket_id=ticket.id,
            new_status="Closed",
            expected_version=1,
        )


@pytest.mark.parametrize("who", ["inactive admin", "inactive assignee"])
def test_inactive_users_cannot_change_status(
    db_session: Session,
    people: SimpleNamespace,
    make_ticket,
    who: str,
) -> None:
    ticket = make_ticket(assignee=people.agent)
    actor = people.admin if who == "inactive admin" else people.agent
    _set_user(db_session, actor, is_active=False)

    with pytest.raises(TicketPermissionError):
        change_ticket_status(
            db_session,
            actor_id=actor.id,
            ticket_id=ticket.id,
            new_status=IN_PROGRESS,
            expected_version=1,
        )

    assert _event_rows(db_session) == []


# ---------------------------------------------------------------------------
# Versioning and transactions
# ---------------------------------------------------------------------------


@pytest.fixture
def operations(db_session: Session, people: SimpleNamespace, make_ticket) -> dict:
    """One call per service, each on a ticket it can change, at version 1."""

    unassigned = make_ticket()
    for_claim = make_ticket()
    for_status = make_ticket(assignee=people.agent)

    def assign(version: int = 1) -> Ticket:
        return assign_ticket(
            db_session,
            actor_id=people.admin.id,
            ticket_id=unassigned.id,
            assignee_id=people.agent.id,
            expected_version=version,
        )

    def claim(version: int = 1) -> Ticket:
        return claim_ticket(
            db_session,
            actor_id=people.agent.id,
            ticket_id=for_claim.id,
            expected_version=version,
        )

    def status(version: int = 1) -> Ticket:
        return change_ticket_status(
            db_session,
            actor_id=people.agent.id,
            ticket_id=for_status.id,
            new_status=IN_PROGRESS,
            expected_version=version,
        )

    return {
        "assign": (assign, unassigned.id),
        "claim": (claim, for_claim.id),
        "status": (status, for_status.id),
    }


OPERATIONS = ["assign", "claim", "status"]


@pytest.mark.parametrize("name", OPERATIONS)
def test_correct_expected_version_succeeds_and_bumps_it(
    db_session: Session, operations: dict, name: str
) -> None:
    call, ticket_id = operations[name]

    call(1)

    assert _ticket_row(db_session, ticket_id)[2] == 2
    assert len(_event_rows(db_session, ticket_id)) == 1


@pytest.mark.parametrize("name", OPERATIONS)
@pytest.mark.parametrize("version", [0, 2, 99])
def test_stale_expected_version_is_a_conflict(
    db_session: Session, operations: dict, name: str, version: int
) -> None:
    call, ticket_id = operations[name]
    before = _ticket_row(db_session, ticket_id)

    with pytest.raises(TicketVersionConflictError) as error:
        call(version)

    assert str(error.value) == (
        "The ticket was changed by someone else. Reload it and try again."
    )
    assert _ticket_row(db_session, ticket_id) == before
    assert _event_rows(db_session) == []


@pytest.mark.parametrize("name", OPERATIONS)
@pytest.mark.parametrize("version", ["1", 1.0, True, None])
def test_expected_version_must_be_an_integer(
    operations: dict, name: str, version: object
) -> None:
    call, _ticket_id = operations[name]

    with pytest.raises(TypeError):
        call(version)


@pytest.mark.parametrize("name", OPERATIONS)
def test_stale_orm_write_becomes_a_version_conflict(
    db_session: Session,
    operations: dict,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """The row's version changes after the locked read (cannot happen while
    the lock is held, so it is simulated in the same transaction)."""

    call, ticket_id = operations[name]
    before = _ticket_row(db_session, ticket_id)
    original = ticket_workflow._lock_for_ticket_change

    def lock_then_bump(db: Session, **kwargs):
        state = original(db, **kwargs)
        db.execute(
            text("UPDATE tickets SET version = version + 1 WHERE id = :id"),
            {"id": ticket_id},
        )
        return state

    monkeypatch.setattr(ticket_workflow, "_lock_for_ticket_change", lock_then_bump)

    with pytest.raises(TicketVersionConflictError) as error:
        call(1)

    assert error.value.__cause__ is None
    # Rolled back: the simulated bump too, and no event.
    assert _ticket_row(db_session, ticket_id) == before
    assert _event_rows(db_session) == []


@pytest.mark.parametrize("name", OPERATIONS)
def test_failure_while_recording_the_event_rolls_back_the_change(
    db_session: Session,
    operations: dict,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    call, ticket_id = operations[name]
    before = _ticket_row(db_session, ticket_id)
    failure = RuntimeError("event insertion failed")
    monkeypatch.setattr(ticket_workflow, "record_ticket_event", _raiser(failure))

    with pytest.raises(RuntimeError) as error:
        call(1)

    assert error.value is failure
    assert _ticket_row(db_session, ticket_id) == before
    assert _event_rows(db_session) == []


@pytest.mark.parametrize("name", OPERATIONS)
def test_commit_failure_rolls_back_ticket_and_event(
    db_session: Session,
    operations: dict,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    call, ticket_id = operations[name]
    before = _ticket_row(db_session, ticket_id)
    commit_error = _operation_error()

    def flush_then_fail() -> None:
        # Both the ticket UPDATE and the event INSERT reach the database.
        db_session.flush()
        assert len(_event_rows(db_session, ticket_id)) == 1
        raise commit_error

    monkeypatch.setattr(db_session, "commit", flush_then_fail)

    with pytest.raises(OperationalError) as error:
        call(1)

    assert error.value is commit_error
    assert _ticket_row(db_session, ticket_id) == before
    assert _event_rows(db_session) == []


@pytest.mark.parametrize("name", OPERATIONS)
def test_rollback_failure_does_not_replace_the_original_error(
    db_session: Session,
    operations: dict,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    call, _ticket_id = operations[name]
    commit_error = _operation_error()
    monkeypatch.setattr(db_session, "commit", _raiser(commit_error))
    monkeypatch.setattr(
        db_session, "rollback", _raiser(RuntimeError("rollback failed: secret"))
    )

    with pytest.raises(OperationalError) as error:
        call(1)

    assert error.value is commit_error

    secondary = rollback_failure_of(error.value)

    assert isinstance(secondary, RuntimeError)
    assert describe_rollback_failure(secondary) == UNEXPECTED_ROLLBACK_FAILURE

    monkeypatch.undo()
    db_session.rollback()


def test_version_conflict_keeps_a_rollback_failure_as_secondary(
    db_session: Session,
    operations: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call, ticket_id = operations["assign"]
    original = ticket_workflow._lock_for_ticket_change

    def lock_then_bump(db: Session, **kwargs):
        state = original(db, **kwargs)
        db.execute(
            text("UPDATE tickets SET version = version + 1 WHERE id = :id"),
            {"id": ticket_id},
        )
        return state

    monkeypatch.setattr(ticket_workflow, "_lock_for_ticket_change", lock_then_bump)
    monkeypatch.setattr(
        db_session, "rollback", _raiser(RuntimeError("rollback failed: secret"))
    )

    with pytest.raises(TicketVersionConflictError) as error:
        call(1)

    assert isinstance(rollback_failure_of(error.value), RuntimeError)

    monkeypatch.undo()
    db_session.rollback()


def test_change_refuses_to_join_a_transaction_that_already_wrote(
    db_session: Session, people: SimpleNamespace, make_ticket
) -> None:
    ticket = make_ticket()
    db_session.execute(
        text("UPDATE users SET name = 'Uncommitted' WHERE id = :id"),
        {"id": people.customer.id},
    )

    with pytest.raises(TransactionAlreadyStartedError):
        assign_ticket(
            db_session,
            actor_id=people.admin.id,
            ticket_id=ticket.id,
            assignee_id=people.agent.id,
            expected_version=1,
        )

    # The service owns the transaction: the earlier write was rolled back.
    assert db_session.execute(
        text("SELECT name FROM users WHERE id = :id"), {"id": people.customer.id}
    ).scalar_one() == "Cory Customer"
    assert _ticket_row(db_session, ticket.id) == (None, OPEN, 1, False)


def test_workflow_errors_share_a_base_class() -> None:
    for error_type in (
        TicketNotFoundError,
        TicketVersionConflictError,
        InvalidStatusTransitionError,
        AssigneeNotAssignableError,
        TicketAlreadyAssignedError,
        TicketNotClaimableError,
        TicketPermissionError,
        InvalidTicketEventError,
    ):
        assert issubclass(error_type, TicketWorkflowError)

    assert issubclass(TicketPermissionError, PermissionError)
    assert issubclass(TicketNotFoundError, LookupError)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def test_full_workflow_history_is_consistent_and_contains_only_approved_data(
    db_session: Session, people: SimpleNamespace, make_ticket
) -> None:
    ticket = make_ticket()
    a, admin, b = people.agent.id, people.admin.id, people.other_agent.id
    steps = [
        lambda v: assign_ticket(db_session, actor_id=admin, ticket_id=ticket.id, assignee_id=a, expected_version=v),
        lambda v: change_ticket_status(db_session, actor_id=a, ticket_id=ticket.id, new_status=IN_PROGRESS, expected_version=v),
        lambda v: assign_ticket(db_session, actor_id=admin, ticket_id=ticket.id, assignee_id=b, expected_version=v),
        lambda v: assign_ticket(db_session, actor_id=admin, ticket_id=ticket.id, assignee_id=None, expected_version=v),
        lambda v: claim_ticket(db_session, actor_id=a, ticket_id=ticket.id, expected_version=v),
        lambda v: change_ticket_status(db_session, actor_id=a, ticket_id=ticket.id, new_status=RESOLVED, expected_version=v),
        lambda v: change_ticket_status(db_session, actor_id=admin, ticket_id=ticket.id, new_status=IN_PROGRESS, expected_version=v),
    ]

    for step in steps:
        step(_ticket_row(db_session, ticket.id)[2])

    assert _event_rows(db_session, ticket.id) == [
        ("assigned", admin, None, a, None, None, None),
        ("status_changed", a, None, None, OPEN, IN_PROGRESS, None),
        ("reassigned", admin, a, b, None, None, None),
        ("unassigned", admin, b, None, IN_PROGRESS, OPEN, None),
        ("assigned", a, None, a, None, None, None),
        ("status_changed", a, None, None, OPEN, RESOLVED, None),
        ("status_changed", admin, None, None, RESOLVED, IN_PROGRESS, None),
    ]

    # Consistent with the committed ticket: one event per version bump,
    # the last assignment event names the assignee, the last status event
    # the status.
    assigned_to, status, version, _ = _ticket_row(db_session, ticket.id)
    events = list_ticket_events(db_session, ticket.id)

    assert (assigned_to, status, version) == (a, IN_PROGRESS, 8)
    assert len(events) == version - 1
    assert [e for e in events if e.event_type != "status_changed"][-1].to_assignee_id == assigned_to
    assert [e.to_status for e in events if e.to_status][-1] == status

    # Only approved columns exist, and no value is a name, email or text.
    assert {column.name for column in TicketEvent.__table__.columns} == {
        "id",
        "ticket_id",
        *EVENT_FIELDS,
        "created_at",
    }
    forbidden = {
        SECRET_TITLE,
        SECRET_DESCRIPTION,
        *(u.name for u in vars(people).values()),
        *(u.email for u in vars(people).values()),
    }
    stored = db_session.execute(text("SELECT * FROM ticket_events")).all()

    for row in stored:
        for value in row:
            assert isinstance(value, (int, str, type(None)))
            assert value not in forbidden
            assert not any(secret in str(value) for secret in forbidden)


def test_history_order_is_deterministic_and_per_ticket(
    db_session: Session,
    people: SimpleNamespace,
    make_ticket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = make_ticket(), make_ticket()
    t0 = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
    times = iter([t0 + timedelta(minutes=5), t0, t0, t0 + timedelta(minutes=1)])
    monkeypatch.setattr(ticket_workflow, "_now", lambda: next(times))

    ids = []
    for ticket_id, event_type in [
        (first.id, "status_changed"),  # latest
        (first.id, "created"),  # tie at t0, lower id
        (first.id, "assigned"),  # tie at t0, higher id
        (second.id, "created"),
    ]:
        event = record_ticket_event(
            db_session,
            ticket_id=ticket_id,
            actor_id=people.admin.id,
            event_type=event_type,
            from_status=OPEN if event_type == "status_changed" else None,
            to_status=IN_PROGRESS if event_type == "status_changed" else None,
        )
        db_session.flush()
        ids.append(event.id)

    db_session.commit()

    assert [e.id for e in list_ticket_events(db_session, first.id)] == [
        ids[1],
        ids[2],
        ids[0],
    ]
    assert [e.id for e in list_ticket_events(db_session, second.id)] == [ids[3]]
    assert list_ticket_events(db_session, 999) == []


def test_event_relationships_resolve(
    db_session: Session, people: SimpleNamespace, make_ticket
) -> None:
    ticket = make_ticket(assignee=people.agent)

    assign_ticket(
        db_session,
        actor_id=people.admin.id,
        ticket_id=ticket.id,
        assignee_id=people.other_agent.id,
        expected_version=1,
    )

    [event] = list_ticket_events(db_session, ticket.id)

    assert event.ticket is ticket
    assert event.actor is people.admin
    assert event.from_assignee is people.agent
    assert event.to_assignee is people.other_agent
    assert ticket.events == [event]


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_type": "deleted"},
        {"from_status": "Closed"},
        {"to_status": "open"},
        {"reason": "fired"},
    ],
    ids=lambda o: "-".join(f"{k}={v}" for k, v in o.items()),
)
def test_record_ticket_event_accepts_only_approved_values(
    db_session: Session, people: SimpleNamespace, make_ticket, overrides: dict
) -> None:
    ticket = make_ticket()
    fields = {"event_type": "created", **overrides}

    with pytest.raises(InvalidTicketEventError) as error:
        record_ticket_event(
            db_session, ticket_id=ticket.id, actor_id=people.admin.id, **fields
        )

    # Fixed message; the rejected value is not echoed.
    assert all(str(value) not in str(error.value) for value in overrides.values())
    assert not db_session.new


def test_record_ticket_event_never_commits(
    db_session: Session,
    people: SimpleNamespace,
    make_ticket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = make_ticket()
    monkeypatch.setattr(db_session, "commit", _raiser(AssertionError("committed")))
    monkeypatch.setattr(db_session, "flush", _raiser(AssertionError("flushed")))

    event = record_ticket_event(
        db_session, ticket_id=ticket.id, actor_id=people.admin.id, event_type="created"
    )

    assert event in db_session.new

    monkeypatch.undo()
    db_session.rollback()

    assert _event_rows(db_session) == []


# ---------------------------------------------------------------------------
# File-backed database (separate connections)
# ---------------------------------------------------------------------------

LOCK_TIMEOUT_SECONDS = 30
WAIT_SECONDS = 15
BLOCKED_CHECK_SECONDS = 0.5


@pytest.fixture
def file_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'workflow.db'}",
        connect_args={"check_same_thread": False, "timeout": LOCK_TIMEOUT_SECONDS},
    )

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)

    yield engine

    engine.dispose()


def _seed_users(engine, *specs: tuple[str, str, bool]) -> list[int]:
    with Session(engine) as db:
        users = [
            User(
                name=f"{label} name",
                email=f"{label}@example.test",
                password_hash="unused",
                role=role,
                is_active=active,
            )
            for label, role, active in specs
        ]
        db.add_all(users)
        db.commit()

        return [user.id for user in users]


def _seed_ticket(engine, creator_id: int, *, status: str = OPEN, assignee_id=None) -> int:
    with Session(engine) as db:
        ticket = Ticket(
            title=SECRET_TITLE,
            description=SECRET_DESCRIPTION,
            created_by_user_id=creator_id,
            status=status,
            assigned_to_user_id=assignee_id,
        )
        db.add(ticket)
        db.commit()

        return ticket.id


def _file_row(engine, ticket_id: int) -> tuple:
    with Session(engine) as db:
        return _ticket_row(db, ticket_id)


def _file_events(engine, ticket_id: int | None = None) -> list[tuple]:
    with Session(engine) as db:
        return _event_rows(db, ticket_id)


def _claim(engine, agent_id: int, ticket_id: int) -> int:
    with Session(engine) as db:
        return claim_ticket(
            db, actor_id=agent_id, ticket_id=ticket_id, expected_version=1
        ).assigned_to_user_id


def _start(operation: Callable[[], object]) -> tuple[threading.Thread, dict]:
    outcome: dict = {}

    def run() -> None:
        try:
            outcome["result"] = operation()
        except Exception as error:  # recorded and asserted by the test
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    return thread, outcome


class _HoldFirstChange:
    """Hold the first ticket change right after its locked reads.

    ``race(first, second)`` starts ``first``, waits until it has read the
    actor and ticket inside its transaction, starts ``second``, records
    whether ``second`` is still waiting, then releases ``first``. Both
    threads are always joined before it returns.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.holding = threading.Event()
        self.release = threading.Event()
        self._first = True
        self._guard = threading.Lock()
        original = ticket_workflow._lock_for_ticket_change

        def lock_then_hold(db: Session, **kwargs):
            state = original(db, **kwargs)

            with self._guard:
                hold, self._first = self._first, False

            if hold:
                self.holding.set()
                self.release.wait(WAIT_SECONDS)

            return state

        monkeypatch.setattr(ticket_workflow, "_lock_for_ticket_change", lock_then_hold)

    def race(self, first, second, *, wait_for_second: bool = False):
        threads: list[threading.Thread] = []
        second_was_waiting = False

        try:
            first_thread, first_outcome = _start(first)
            threads.append(first_thread)

            if not self.holding.wait(WAIT_SECONDS):
                raise AssertionError("the first change never reached its lock")

            second_thread, second_outcome = _start(second)
            threads.append(second_thread)
            second_thread.join(WAIT_SECONDS if wait_for_second else BLOCKED_CHECK_SECONDS)
            second_was_waiting = second_thread.is_alive()
        finally:
            self.release.set()

            for thread in threads:
                thread.join(WAIT_SECONDS)

        assert not any(thread.is_alive() for thread in threads)

        return first_outcome, second_outcome, second_was_waiting


def test_two_agents_racing_to_claim_exactly_one_succeeds(
    file_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    customer, agent_a, agent_b = _seed_users(
        file_engine,
        ("customer", "customer", True),
        ("agent-a", "agent", True),
        ("agent-b", "agent", True),
    )
    ticket_id = _seed_ticket(file_engine, customer)
    hold = _HoldFirstChange(monkeypatch)

    first, second, second_was_waiting = hold.race(
        lambda: _claim(file_engine, agent_a, ticket_id),
        lambda: _claim(file_engine, agent_b, ticket_id),
    )

    assert second_was_waiting, "the second claim did not wait for the lock"
    assert first == {"result": agent_a}
    # The second claim read the ticket only after the first committed.
    assert isinstance(second.get("error"), TicketAlreadyAssignedError)
    assert _file_row(file_engine, ticket_id) == (agent_a, OPEN, 2, True)
    assert _file_events(file_engine, ticket_id) == [
        ("assigned", agent_a, None, agent_a, None, None, None),
    ]


def test_without_the_lock_the_version_check_still_stops_a_double_claim(
    file_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: with the lock disabled, both claims read "unassigned" and
    both try to write; the optimistic version check rejects the second."""

    customer, agent_a, agent_b = _seed_users(
        file_engine,
        ("customer", "customer", True),
        ("agent-a", "agent", True),
        ("agent-b", "agent", True),
    )
    ticket_id = _seed_ticket(file_engine, customer)
    monkeypatch.setattr(ticket_workflow, "_begin_serialised_change", lambda db: None)
    hold = _HoldFirstChange(monkeypatch)

    first, second, second_was_waiting = hold.race(
        lambda: _claim(file_engine, agent_a, ticket_id),
        lambda: _claim(file_engine, agent_b, ticket_id),
        wait_for_second=True,
    )

    assert not second_was_waiting
    # The unlocked second claim saw an unassigned ticket and committed.
    assert second == {"result": agent_b}
    # The held first claim's versioned UPDATE then matched no row.
    assert isinstance(first.get("error"), TicketVersionConflictError)
    assert _file_row(file_engine, ticket_id) == (agent_b, OPEN, 2, True)
    assert len(_file_events(file_engine, ticket_id)) == 1


def test_many_concurrent_claims_assign_the_ticket_once(file_engine) -> None:
    count = 8
    customer, *agents = _seed_users(
        file_engine,
        ("customer", "customer", True),
        *[(f"agent-{index}", "agent", True) for index in range(count)],
    )
    ticket_id = _seed_ticket(file_engine, customer)
    barrier = threading.Barrier(count)

    def claim(agent_id: int):
        barrier.wait()
        return _claim(file_engine, agent_id, ticket_id)

    started = [_start(lambda agent_id=agent_id: claim(agent_id)) for agent_id in agents]

    for thread, _outcome in started:
        thread.join(WAIT_SECONDS * 2)

    assert not any(thread.is_alive() for thread, _ in started)

    outcomes = [outcome for _, outcome in started]
    winners = [outcome["result"] for outcome in outcomes if "result" in outcome]
    losers = [outcome["error"] for outcome in outcomes if "error" in outcome]

    assert len(winners) == 1
    assert len(losers) == count - 1
    assert all(isinstance(error, TicketAlreadyAssignedError) for error in losers)
    assert _file_row(file_engine, ticket_id)[:3] == (winners[0], OPEN, 2)
    assert len(_file_events(file_engine, ticket_id)) == 1


def test_assignment_and_assignee_deactivation_are_serialised(
    file_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Phase 6 deactivation waits for an assignment holding the lock."""

    admin_a, admin_b, agent, customer = _seed_users(
        file_engine,
        ("admin-a", "admin", True),
        ("admin-b", "admin", True),
        ("agent", "agent", True),
        ("customer", "customer", True),
    )
    ticket_id = _seed_ticket(file_engine, customer)
    hold = _HoldFirstChange(monkeypatch)

    def assign():
        with Session(file_engine) as db:
            return assign_ticket(
                db,
                actor_id=admin_a,
                ticket_id=ticket_id,
                assignee_id=agent,
                expected_version=1,
            ).assigned_to_user_id

    def deactivate():
        with Session(file_engine) as db:
            return set_user_active_status(
                db, actor_id=admin_b, user_id=agent, is_active=False
            ).changed

    first, second, second_was_waiting = hold.race(assign, deactivate)

    assert second_was_waiting, "the deactivation did not wait for the lock"
    assert first == {"result": agent}
    assert second == {"result": True}
    # Phase 7.5: the deactivation, which ran after the assignment
    # committed, unassigned the ticket again.
    assert _file_row(file_engine, ticket_id)[:3] == (None, OPEN, 3)
    assert [row[0] for row in _file_events(file_engine, ticket_id)] == [
        "assigned",
        "unassigned",
    ]


@pytest.mark.parametrize(
    "outcome",
    ["assigned", "no-op", "forbidden", "not found", "conflict", "invalid"],
)
def test_lock_is_released_after_every_outcome(file_engine, outcome: str) -> None:
    admin, agent, customer = _seed_users(
        file_engine,
        ("admin", "admin", True),
        ("agent", "agent", True),
        ("customer", "customer", True),
    )
    ticket_id = _seed_ticket(file_engine, customer)
    calls = {
        "assigned": dict(actor_id=admin, ticket_id=ticket_id, assignee_id=agent, expected_version=1),
        "no-op": dict(actor_id=admin, ticket_id=ticket_id, assignee_id=None, expected_version=1),
        "forbidden": dict(actor_id=agent, ticket_id=ticket_id, assignee_id=agent, expected_version=1),
        "not found": dict(actor_id=admin, ticket_id=999, assignee_id=agent, expected_version=1),
        "conflict": dict(actor_id=admin, ticket_id=ticket_id, assignee_id=agent, expected_version=5),
        "invalid": dict(actor_id=admin, ticket_id=ticket_id, assignee_id=customer, expected_version=1),
    }

    with Session(file_engine) as db:
        try:
            assign_ticket(db, **calls[outcome])
        except TicketWorkflowError:
            pass

        # The session is still open: its transaction must already be over.
        path = file_engine.url.database
        other = sqlite3.connect(path, timeout=0.1)

        try:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
        finally:
            other.close()


# ---------------------------------------------------------------------------
# Unassignment helper (for user administration, Phase 7.5)
# ---------------------------------------------------------------------------


@pytest.fixture
def unassignment_setup(file_engine) -> SimpleNamespace:
    admin, agent, other, customer = _seed_users(
        file_engine,
        ("admin", "admin", True),
        ("agent", "agent", True),
        ("other", "agent", True),
        ("customer", "customer", True),
    )

    return SimpleNamespace(
        admin=admin,
        agent=agent,
        other=other,
        # Seeded out of status order to show the id ordering.
        in_progress=_seed_ticket(file_engine, customer, status=IN_PROGRESS, assignee_id=agent),
        resolved=_seed_ticket(file_engine, customer, status=RESOLVED, assignee_id=agent),
        open=_seed_ticket(file_engine, customer, status=OPEN, assignee_id=agent),
        others=_seed_ticket(file_engine, customer, status=OPEN, assignee_id=other),
        unassigned=_seed_ticket(file_engine, customer, status=IN_PROGRESS),
    )


def _all_rows(engine, setup: SimpleNamespace) -> dict:
    return {
        name: _file_row(engine, getattr(setup, name))
        for name in ("in_progress", "resolved", "open", "others", "unassigned")
    }


@pytest.mark.parametrize("reason", ["assignee_deactivated", "assignee_role_changed"])
def test_unassign_open_tickets_for_user(
    file_engine, unassignment_setup: SimpleNamespace, reason: str
) -> None:
    s = unassignment_setup
    before = _all_rows(file_engine, s)

    with Session(file_engine) as db:
        changed = unassign_open_tickets_for_user(
            db, actor_id=s.admin, user_id=s.agent, reason=reason
        )

        # Not committed: another connection still sees the old state.
        assert _all_rows(file_engine, s) == before
        assert _file_events(file_engine) == []

        db.commit()

    assert changed == sorted([s.in_progress, s.open])

    after = _all_rows(file_engine, s)

    assert after["in_progress"] == (None, OPEN, 2, False)
    assert after["open"] == (None, OPEN, 2, False)
    # Resolved, other agents' and unassigned tickets are untouched.
    for name in ("resolved", "others", "unassigned"):
        assert after[name] == before[name]

    # One event per changed ticket, in ticket id order.
    assert _file_events(file_engine) == [
        ("unassigned", s.admin, s.agent, None, IN_PROGRESS, OPEN, reason),
        ("unassigned", s.admin, s.agent, None, None, None, reason),
    ]


def test_unassign_helper_changes_are_undone_by_the_callers_rollback(
    file_engine, unassignment_setup: SimpleNamespace
) -> None:
    s = unassignment_setup
    before = _all_rows(file_engine, s)

    with Session(file_engine) as db:
        assert unassign_open_tickets_for_user(
            db, actor_id=s.admin, user_id=s.agent, reason="assignee_deactivated"
        )
        db.rollback()

    assert _all_rows(file_engine, s) == before
    assert _file_events(file_engine) == []


def test_unassign_helper_never_commits_or_rolls_back(
    db_session: Session,
    people: SimpleNamespace,
    make_ticket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_ticket(assignee=people.agent)
    monkeypatch.setattr(db_session, "commit", _raiser(AssertionError("committed")))
    monkeypatch.setattr(db_session, "rollback", _raiser(AssertionError("rolled back")))

    assert unassign_open_tickets_for_user(
        db_session,
        actor_id=people.admin.id,
        user_id=people.agent.id,
        reason="assignee_role_changed",
    )

    monkeypatch.undo()
    db_session.rollback()


def test_unassign_helper_rejects_an_unknown_reason_and_changes_nothing(
    file_engine, unassignment_setup: SimpleNamespace
) -> None:
    s = unassignment_setup
    before = _all_rows(file_engine, s)

    with Session(file_engine) as db:
        with pytest.raises(InvalidTicketEventError):
            unassign_open_tickets_for_user(
                db, actor_id=s.admin, user_id=s.agent, reason="fired"
            )

        assert not db.dirty and not db.new

    assert _all_rows(file_engine, s) == before


def test_unassign_helper_with_no_open_tickets_does_nothing(
    file_engine, unassignment_setup: SimpleNamespace
) -> None:
    s = unassignment_setup

    with Session(file_engine) as db:
        assert (
            unassign_open_tickets_for_user(
                db, actor_id=s.admin, user_id=s.admin, reason="assignee_deactivated"
            )
            == []
        )
        assert not db.dirty and not db.new
