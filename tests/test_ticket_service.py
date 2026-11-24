
import sqlite3

import pytest

from sqlalchemy import event, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import Ticket, User
from app.services import ticket_service
from app.services.ticket_service import (
    count_tickets,
    create_ticket,
    delete_ticket,
    get_ticket_by_id,
    get_tickets,
    update_ticket,
)


@pytest.fixture
def test_user(db_session: Session) -> User:
    """Create a user for ticket service tests."""

    user = User(
        name="Test Agent",
        email="agent@example.test",
        password_hash="test-hash",
    )

    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    return user


def test_create_ticket(
    db_session: Session,
    test_user: User,
) -> None:

    ticket = create_ticket(
        db_session,
        creator=test_user,
        title="Payment issue",
        description="Customer was charged twice.",
    )

    assert ticket.id is not None
    assert ticket.title == "Payment issue"
    assert ticket.status == "Open"
    assert ticket.priority == "Medium"
    assert ticket.creator.id == test_user.id


def test_get_ticket_by_id(
    db_session: Session,
    test_user: User,
) -> None:

    ticket = create_ticket(
        db_session,
        creator=test_user,
        title="Login problem",
        description="Unable to log in.",
    )

    result = get_ticket_by_id(db_session, ticket.id)

    assert result is not None
    assert result.title == "Login problem"

    assert get_ticket_by_id(db_session, 9999) is None


def test_get_tickets_with_filters(
    db_session: Session,
    test_user: User,
) -> None:

    create_ticket(
        db_session,
        creator=test_user,
        title="Payment failed",
        description="Payment was unsuccessful.",
        category="Billing",
        priority="High",
    )

    create_ticket(
        db_session,
        creator=test_user,
        title="Delivery delay",
        description="Order has not arrived.",
        category="Orders",
        priority="Medium",
    )

    create_ticket(
        db_session,
        creator=test_user,
        title="Second payment issue",
        description="Customer reports duplicate payment.",
        category="Billing",
        priority="High",
    )

    results = get_tickets(
        db_session,
        category="Billing",
        priority="High",
        search="payment",
    )

    assert len(results) == 2

    for ticket in results:
        assert ticket.category == "Billing"
        assert ticket.priority == "High"


def test_update_ticket(
    db_session: Session,
    test_user: User,
) -> None:

    ticket = create_ticket(
        db_session,
        creator=test_user,
        title="Payment problem",
        description="Charged twice.",
        predicted_category="Billing",
        prediction_confidence=0.94,
        category="Billing",
    )

    updated_ticket = update_ticket(
        db_session,
        ticket,
        {
            "category": "Orders",
            "priority": "High",
            "status": "In Progress",
        },
    )

    assert updated_ticket.category == "Orders"
    assert updated_ticket.priority == "High"
    assert updated_ticket.status == "In Progress"

    # The original ML prediction must remain unchanged.
    assert updated_ticket.predicted_category == "Billing"


def test_update_rejects_protected_fields(
    db_session: Session,
    test_user: User,
) -> None:

    ticket = create_ticket(
        db_session,
        creator=test_user,
        title="Test ticket",
        description="Testing protected fields.",
        predicted_category="Billing",
    )

    with pytest.raises(ValueError):
        update_ticket(
            db_session,
            ticket,
            {"predicted_category": "Orders"},
        )

    assert ticket.predicted_category == "Billing"


def test_delete_ticket(
    db_session: Session,
    test_user: User,
) -> None:

    ticket = create_ticket(
        db_session,
        creator=test_user,
        title="Delete test",
        description="This ticket will be deleted.",
    )

    ticket_id = ticket.id

    delete_ticket(db_session, ticket)

    assert get_ticket_by_id(db_session, ticket_id) is None


@pytest.fixture
def other_user(db_session: Session) -> User:
    """Create a second user to verify creator scoping."""

    user = User(
        name="Other Customer",
        email="other@example.test",
        password_hash="test-hash",
        role="customer",
    )

    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    return user


def test_get_tickets_filters_by_creator(
    db_session: Session,
    test_user: User,
    other_user: User,
) -> None:

    own = create_ticket(
        db_session,
        creator=test_user,
        title="Payment failed",
        description="Payment was unsuccessful.",
        category="Billing",
    )

    create_ticket(
        db_session,
        creator=other_user,
        title="Payment failed too",
        description="Another unsuccessful payment.",
        category="Billing",
    )

    results = get_tickets(
        db_session,
        category="Billing",
        search="payment",
        created_by_user_id=test_user.id,
    )

    assert [ticket.id for ticket in results] == [own.id]

    # Omitting the creator filter preserves the unscoped behaviour.
    assert len(get_tickets(db_session, category="Billing")) == 2


def test_get_ticket_by_id_scoped_to_creator(
    db_session: Session,
    test_user: User,
    other_user: User,
) -> None:

    ticket = create_ticket(
        db_session,
        creator=test_user,
        title="Login problem",
        description="Unable to log in.",
    )

    scoped = get_ticket_by_id(
        db_session,
        ticket.id,
        created_by_user_id=test_user.id,
    )

    assert scoped is not None
    assert scoped.id == ticket.id

    assert get_ticket_by_id(
        db_session,
        ticket.id,
        created_by_user_id=other_user.id,
    ) is None

    assert get_ticket_by_id(
        db_session,
        9999,
        created_by_user_id=test_user.id,
    ) is None


# ---------------------------------------------------------------------------
# Phase 7: creation history, assignment filters and counts
# ---------------------------------------------------------------------------


def _events(db_session: Session) -> list[tuple]:
    return [
        tuple(row)
        for row in db_session.execute(
            text(
                "SELECT ticket_id, event_type, actor_user_id, from_assignee_id, "
                "to_assignee_id, from_status, to_status, reason FROM ticket_events "
                "ORDER BY id"
            )
        )
    ]


def test_create_ticket_writes_exactly_one_created_event(
    db_session: Session,
    test_user: User,
) -> None:
    ticket = create_ticket(
        db_session,
        creator=test_user,
        title="Payment issue",
        description="Customer was charged twice.",
    )

    assert _events(db_session) == [
        (ticket.id, "created", test_user.id, None, None, None, None, None),
    ]

    [event] = ticket.events

    assert event.actor is test_user
    assert event.created_at is not None
    assert ticket.version == 1


def test_event_failure_rolls_back_ticket_creation(
    db_session: Session,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = RuntimeError("event insertion failed")

    def _fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(ticket_service, "record_ticket_event", _fail)

    with pytest.raises(RuntimeError) as error:
        create_ticket(
            db_session,
            creator=test_user,
            title="Never saved",
            description="The event insert fails.",
        )

    assert error.value is failure
    assert db_session.scalar(select(func.count()).select_from(Ticket)) == 0
    assert _events(db_session) == []


def test_commit_failure_rolls_back_ticket_and_event(
    db_session: Session,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_commit = db_session.commit
    commit_error = OperationalError("COMMIT", None, sqlite3.OperationalError("io"))

    def flush_then_fail() -> None:
        db_session.flush()
        assert len(_events(db_session)) == 1
        raise commit_error

    monkeypatch.setattr(db_session, "commit", flush_then_fail)

    with pytest.raises(OperationalError) as error:
        create_ticket(
            db_session,
            creator=test_user,
            title="Never saved",
            description="The commit fails.",
        )

    assert error.value is commit_error

    monkeypatch.setattr(db_session, "commit", real_commit)

    assert db_session.scalar(select(func.count()).select_from(Ticket)) == 0
    assert _events(db_session) == []


@pytest.fixture
def assignment_tickets(
    db_session: Session,
    test_user: User,
    other_user: User,
) -> dict[str, Ticket]:
    """Tickets assigned to the agent, to nobody, and one per status."""

    tickets = {
        name: create_ticket(
            db_session,
            creator=other_user,
            title=f"{name} ticket",
            description="Assignment filter test.",
            status=status,
        )
        for name, status in [
            ("mine_open", "Open"),
            ("mine_resolved", "Resolved"),
            ("unassigned_open", "Open"),
            ("unassigned_progress", "In Progress"),
        ]
    }

    for name in ("mine_open", "mine_resolved"):
        tickets[name].assigned_to_user_id = test_user.id

    db_session.commit()

    return tickets


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({}, {"mine_open", "mine_resolved", "unassigned_open", "unassigned_progress"}),
        ({"assigned_to_user_id": "AGENT"}, {"mine_open", "mine_resolved"}),
        ({"unassigned": True}, {"unassigned_open", "unassigned_progress"}),
        ({"assigned_to_user_id": "AGENT", "status": "Open"}, {"mine_open"}),
        ({"unassigned": True, "status": "Resolved"}, set()),
        ({"assigned_to_user_id": 999}, set()),
    ],
)
def test_assignment_filters_and_counts_agree(
    db_session: Session,
    test_user: User,
    assignment_tickets: dict[str, Ticket],
    filters: dict,
    expected: set[str],
) -> None:
    if filters.get("assigned_to_user_id") == "AGENT":
        filters = {**filters, "assigned_to_user_id": test_user.id}

    found = get_tickets(db_session, **filters)
    names = {
        name for name, ticket in assignment_tickets.items() if ticket in found
    }

    assert names == expected
    assert count_tickets(db_session, **filters) == len(expected)
    # The count ignores pagination.
    assert count_tickets(db_session, **filters) == len(
        get_tickets(db_session, limit=100, **filters)
    )
    assert len(get_tickets(db_session, limit=1, **filters)) == min(1, len(expected))


def test_assignment_filters_cannot_be_combined(db_session: Session) -> None:
    with pytest.raises(ValueError):
        get_tickets(db_session, assigned_to_user_id=1, unassigned=True)

    with pytest.raises(ValueError):
        count_tickets(db_session, assigned_to_user_id=1, unassigned=True)


def test_load_assignee_avoids_one_query_per_ticket(
    db_session: Session,
    test_user: User,
    other_user: User,
) -> None:
    for index in range(6):
        ticket = create_ticket(
            db_session,
            creator=other_user,
            title=f"Ticket {index}",
            description="Eager loading test.",
        )
        ticket.assigned_to_user_id = test_user.id

    db_session.commit()
    db_session.expunge_all()

    statements: list[str] = []
    engine = db_session.get_bind()

    def _record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)

    try:
        tickets = get_tickets(db_session, load_assignee=True)
        names = {ticket.assignee.name for ticket in tickets}
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert names == {"Test Agent"}
    # One query for the tickets, one for all their assignees.
    assert len(statements) == 2
