
import pytest

from pydantic import ValidationError

from app.schemas import TicketCreate, TicketUpdate


def test_ticket_create_defaults() -> None:
    ticket = TicketCreate(
        title="Payment issue",
        description="Customer was charged twice.",
    )

    assert ticket.priority == "Medium"
    assert ticket.requester_name is None


def test_ticket_create_strips_whitespace() -> None:
    ticket = TicketCreate(
        title="  Payment issue  ",
        description="  Customer was charged twice.  ",
    )

    assert ticket.title == "Payment issue"
    assert ticket.description == "Customer was charged twice."


def test_ticket_create_rejects_invalid_priority() -> None:
    with pytest.raises(ValidationError):
        TicketCreate(
            title="Payment issue",
            description="Customer was charged twice.",
            priority="Urgent",
        )


def test_ticket_create_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        TicketCreate(
            title="Payment issue",
            description="Customer was charged twice.",
            predicted_category="Billing",
        )


def test_ticket_update_accepts_partial_data() -> None:
    update = TicketUpdate(
        priority="High",
        status="In Progress",
    )

    result = update.model_dump(exclude_unset=True)

    assert result == {
        "priority": "High",
        "status": "In Progress",
    }


def test_ticket_update_rejects_protected_fields() -> None:
    with pytest.raises(ValidationError):
        TicketUpdate(
            predicted_category="Orders",
        )


def test_ticket_update_rejects_empty_update() -> None:
    with pytest.raises(ValidationError):
        TicketUpdate()


def test_ticket_update_rejects_null_title() -> None:
    with pytest.raises(ValidationError):
        TicketUpdate(title=None)
