
import pytest

from sqlalchemy.orm import Session

from app.models import Ticket, User
from app.services.ticket_service import (
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
