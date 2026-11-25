"""TicketMessage model behaviour (revision 0004 schema).

Uses conftest's in-memory SQLite session, with foreign keys enforced and
the schema created from ``Base.metadata``. resolveai.db is never used.
"""

from collections.abc import Callable
from datetime import datetime, timezone

import pytest
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import Ticket, TicketEvent, TicketMessage, User
from app.models.ticket_message import MAX_MESSAGE_LENGTH, MESSAGE_SENDER_ROLES


T0 = datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def customer(create_user: Callable[..., User]) -> User:
    return create_user(email="owner@example.test", role="customer", name="Owner")


@pytest.fixture
def ticket(db_session: Session, customer: User) -> Ticket:
    ticket = Ticket(
        title="Payment failed",
        description="Card was declined.",
        creator=customer,
    )
    db_session.add(ticket)
    db_session.commit()

    return ticket


def _message(ticket: Ticket, sender: User, **fields) -> TicketMessage:
    return TicketMessage(
        ticket=ticket,
        sender=sender,
        sender_role=fields.pop("sender_role", "customer"),
        content=fields.pop("content", "Any update on this?"),
        **fields,
    )


def _message_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(TicketMessage))


# ---------------------------------------------------------------------------
# Registration and table definition
# ---------------------------------------------------------------------------


def test_model_is_registered_on_the_shared_metadata() -> None:
    assert Base.metadata.tables["ticket_messages"] is TicketMessage.__table__


def test_columns_and_nullability() -> None:
    columns = TicketMessage.__table__.c

    assert [column.name for column in columns] == [
        "id",
        "ticket_id",
        "sender_user_id",
        "sender_role",
        "content",
        "created_at",
    ]

    for name in ("ticket_id", "sender_user_id", "sender_role", "content", "created_at"):
        assert columns[name].nullable is False

    assert columns.sender_role.type.length == 30
    assert columns.created_at.type.timezone is True


def test_constraint_and_index_names_match_the_migration() -> None:
    table = TicketMessage.__table__

    assert {
        c.name for c in table.constraints if isinstance(c, CheckConstraint)
    } == {
        "ck_ticket_messages_sender_role",
        "ck_ticket_messages_content_not_blank",
        "ck_ticket_messages_content_length",
    }
    assert {
        c.name for c in table.constraints if isinstance(c, ForeignKeyConstraint)
    } == {
        "fk_ticket_messages_ticket_id_tickets",
        "fk_ticket_messages_sender_user_id_users",
    }

    [ticket_fk] = table.c.ticket_id.foreign_keys
    [sender_fk] = table.c.sender_user_id.foreign_keys

    assert ticket_fk.ondelete == "CASCADE"
    assert sender_fk.ondelete is None

    # The ordering index: a ticket's messages by ID.
    assert [
        (index.name, [column.name for column in index.columns])
        for index in table.indexes
    ] == [("ix_ticket_messages_ticket_id_id", ["ticket_id", "id"])]


def test_relationships_follow_the_ticket_event_conventions() -> None:
    messages = inspect(Ticket).relationships["messages"]

    assert messages.mapper.class_ is TicketMessage
    assert messages.passive_deletes is True
    assert messages.cascade.delete_orphan is True
    assert messages.back_populates == "ticket"

    # No reverse collection on User.
    assert not any(
        relationship.mapper.class_ is TicketMessage
        for relationship in inspect(User).relationships
    )


def test_shared_limits() -> None:
    assert MAX_MESSAGE_LENGTH == 10_000
    assert MESSAGE_SENDER_ROLES == ("admin", "agent", "customer")


# ---------------------------------------------------------------------------
# Valid rows
# ---------------------------------------------------------------------------


def test_valid_message_is_stored(
    db_session: Session,
    ticket: Ticket,
    customer: User,
) -> None:
    message = _message(ticket, customer, created_at=T0)
    db_session.add(message)
    db_session.commit()

    stored = db_session.get(TicketMessage, message.id)

    assert stored.ticket_id == ticket.id
    assert stored.sender_user_id == customer.id
    assert stored.sender_role == "customer"
    assert stored.content == "Any update on this?"
    assert stored.ticket is ticket
    assert stored.sender is customer


def test_created_at_defaults_to_utc_now(
    db_session: Session,
    ticket: Ticket,
    customer: User,
) -> None:
    before = datetime.now(timezone.utc)
    message = _message(ticket, customer)
    db_session.add(message)
    db_session.flush()

    assert message.created_at.tzinfo is not None
    assert before <= message.created_at <= datetime.now(timezone.utc)


@pytest.mark.parametrize("role", MESSAGE_SENDER_ROLES)
def test_every_sender_role_is_accepted(
    db_session: Session,
    ticket: Ticket,
    customer: User,
    role: str,
) -> None:
    db_session.add(_message(ticket, customer, sender_role=role))
    db_session.commit()

    assert _message_count(db_session) == 1


def test_maximum_length_content_is_accepted(
    db_session: Session,
    ticket: Ticket,
    customer: User,
) -> None:
    db_session.add(_message(ticket, customer, content="é" * MAX_MESSAGE_LENGTH))
    db_session.commit()

    assert _message_count(db_session) == 1


def test_messages_load_in_id_order(
    db_session: Session,
    ticket: Ticket,
    customer: User,
) -> None:
    for number in range(3):
        db_session.add(_message(ticket, customer, content=f"message {number}"))
        db_session.commit()

    db_session.expire(ticket)

    assert [message.content for message in ticket.messages] == [
        "message 0",
        "message 1",
        "message 2",
    ]


def test_adding_a_message_does_not_change_the_ticket(
    db_session: Session,
    ticket: Ticket,
    customer: User,
) -> None:
    version, updated_at = ticket.version, ticket.updated_at

    db_session.add(_message(ticket, customer))
    db_session.commit()
    db_session.refresh(ticket)

    assert ticket.version == version
    assert ticket.updated_at == updated_at
    assert db_session.scalar(select(func.count()).select_from(TicketEvent)) == 0


# ---------------------------------------------------------------------------
# Rejected rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"sender_role": "superuser"},
        {"sender_role": "Customer"},
        {"content": ""},
        {"content": "    "},
        {"content": "\t\r\n "},
        {"content": "x" * (MAX_MESSAGE_LENGTH + 1)},
    ],
    ids=[
        "unknown-role",
        "role-wrong-case",
        "empty",
        "spaces",
        "mixed-whitespace",
        "too-long",
    ],
)
def test_check_constraints_reject_invalid_values(
    db_session: Session,
    ticket: Ticket,
    customer: User,
    fields: dict,
) -> None:
    db_session.add(_message(ticket, customer, **fields))

    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()

    assert _message_count(db_session) == 0


@pytest.mark.parametrize(
    "missing",
    ["ticket_id", "sender_user_id", "sender_role", "content"],
)
def test_required_fields(
    db_session: Session,
    ticket: Ticket,
    customer: User,
    missing: str,
) -> None:
    values = {
        "ticket_id": ticket.id,
        "sender_user_id": customer.id,
        "sender_role": "customer",
        "content": "Hello",
    }
    del values[missing]
    db_session.add(TicketMessage(**values))

    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()

    assert _message_count(db_session) == 0


@pytest.mark.parametrize("column", ["ticket_id", "sender_user_id"])
def test_foreign_keys_reject_missing_rows(
    db_session: Session,
    ticket: Ticket,
    customer: User,
    column: str,
) -> None:
    values = {
        "ticket_id": ticket.id,
        "sender_user_id": customer.id,
        "sender_role": "customer",
        "content": "Hello",
        column: 999,
    }
    db_session.add(TicketMessage(**values))

    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()

    assert _message_count(db_session) == 0


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


def test_deleting_a_ticket_through_the_orm_deletes_its_messages(
    db_session: Session,
    ticket: Ticket,
    customer: User,
    create_user: Callable[..., User],
) -> None:
    other = Ticket(title="Other", description="Other ticket.", creator=customer)
    db_session.add_all(
        [
            other,
            _message(ticket, customer),
            _message(ticket, customer),
        ]
    )
    db_session.add(_message(other, customer))
    db_session.commit()

    # Unloaded messages are deleted by the database (passive_deletes).
    db_session.expire_all()
    db_session.delete(db_session.get(Ticket, ticket.id))
    db_session.commit()

    assert db_session.scalars(select(TicketMessage.ticket_id)).all() == [other.id]
    # The sender is kept.
    assert db_session.get(User, customer.id) is not None


def test_deleting_a_ticket_with_loaded_messages(
    db_session: Session,
    ticket: Ticket,
    customer: User,
) -> None:
    db_session.add(_message(ticket, customer))
    db_session.commit()

    assert len(ticket.messages) == 1

    db_session.delete(ticket)
    db_session.commit()

    assert _message_count(db_session) == 0


def test_database_cascade_deletes_messages_with_raw_sql(
    db_session: Session,
    ticket: Ticket,
    customer: User,
) -> None:
    db_session.add(_message(ticket, customer))
    db_session.commit()

    db_session.execute(text("DELETE FROM tickets WHERE id = :id"), {"id": ticket.id})
    db_session.commit()

    assert _message_count(db_session) == 0


def test_a_sender_with_messages_cannot_be_deleted(
    db_session: Session,
    ticket: Ticket,
    create_user: Callable[..., User],
) -> None:
    agent = create_user(email="writer@example.test", role="agent", name="Writer")
    db_session.add(_message(ticket, agent, sender_role="agent"))
    db_session.commit()

    with pytest.raises(IntegrityError):
        db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": agent.id})

    db_session.rollback()

    assert _message_count(db_session) == 1
