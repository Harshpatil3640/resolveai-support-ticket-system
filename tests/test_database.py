
import pytest

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Ticket, User


def test_create_user_and_ticket(db_session: Session) -> None:
    """Test creating a user and an associated ticket."""

    user = User(
        name="Harsh",
        email="harsh@example.test",
        password_hash="test-hash",
    )

    ticket = Ticket(
        title="Payment charged twice",
        description="I was charged twice for my subscription.",
        requester_name="Customer One",
        predicted_category="Billing & Payments",
        category="Billing & Payments",
        prediction_confidence=0.94,
        creator=user,
    )

    db_session.add(user)
    db_session.add(ticket)

    db_session.commit()

    assert user.id is not None
    assert ticket.id is not None
    assert ticket.creator.email == "harsh@example.test"
    assert user.tickets[0].title == "Payment charged twice"


def test_default_values(db_session: Session) -> None:
    """Test the default values of user and ticket records."""

    user = User(
        name="Rahul",
        email="rahul@example.test",
        password_hash="test-hash",
    )

    ticket = Ticket(
        title="Unable to log in",
        description="I cannot access my account.",
        creator=user,
    )

    db_session.add_all([user, ticket])
    db_session.flush()

    assert user.role == "agent"
    assert user.is_active is True
    assert ticket.priority == "Medium"
    assert ticket.status == "Open"


def test_ticket_rejects_nonexistent_user(
    db_session: Session,
) -> None:
    """Test that a ticket cannot reference a missing user."""

    ticket = Ticket(
        title="Invalid ticket",
        description="Testing foreign-key validation.",
        created_by_user_id=9999,
    )

    db_session.add(ticket)

    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("priority", "Urgent"),
        ("status", "Pending"),
    ],
)
def test_ticket_rejects_invalid_values(
    db_session: Session,
    field_name: str,
    invalid_value: str,
) -> None:
    """Test database constraints for ticket priority and status."""

    user = User(
        name="Test User",
        email="test@example.test",
        password_hash="test-hash",
    )

    db_session.add(user)
    db_session.flush()

    ticket = Ticket(
        title="Test ticket",
        description="Testing database constraints.",
        creator=user,
    )

    setattr(ticket, field_name, invalid_value)

    db_session.add(ticket)

    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()
