"""Ticket conversation service: permissions, ordering, pagination, rollback.

Uses conftest's in-memory SQLite session (foreign keys enforced). The
concurrency tests, which need independent connections, are in
test_ticket_message_concurrency.py. resolveai.db is never used.
"""

from collections.abc import Callable
from datetime import timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.locking import TransactionAlreadyStartedError
from app.models import Ticket, TicketEvent, TicketMessage, User
from app.models.ticket_message import MAX_MESSAGE_LENGTH
from app.services import ticket_message_service
from app.services.ticket_message_service import (
    InvalidTicketMessageError,
    TicketResolvedError,
    can_read_conversation,
    create_ticket_message,
    list_ticket_messages,
)
from app.services.ticket_workflow import (
    TicketNotFoundError,
    TicketPermissionError,
    change_ticket_status,
)


@pytest.fixture
def world(db_session: Session, create_user: Callable[..., User]) -> SimpleNamespace:
    """Admin, two agents, two customers and three of the customer's tickets.

    ``assigned`` is In Progress and assigned to ``agent``; ``free`` is Open
    and unassigned; ``resolved`` is Resolved and assigned to ``agent``.
    ``other`` belongs to the second customer.
    """

    admin = create_user(email="admin@example.test", role="admin", name="Ada Admin")
    agent = create_user(email="agent@example.test", role="agent", name="Alex Agent")
    agent_2 = create_user(email="agent2@example.test", role="agent", name="Bo Agent")
    customer = create_user(email="c1@example.test", role="customer", name="Cara Customer")
    customer_2 = create_user(email="c2@example.test", role="customer", name="Dan Customer")

    tickets = {
        "assigned": Ticket(
            title="Assigned",
            description="Assigned ticket.",
            creator=customer,
            status="In Progress",
            assigned_to_user_id=agent.id,
        ),
        "free": Ticket(title="Free", description="Unassigned ticket.", creator=customer),
        "resolved": Ticket(
            title="Resolved",
            description="Resolved ticket.",
            creator=customer,
            status="Resolved",
            assigned_to_user_id=agent.id,
        ),
        "other": Ticket(title="Other", description="Someone else's.", creator=customer_2),
    }
    db_session.add_all(tickets.values())
    db_session.commit()

    return SimpleNamespace(
        db=db_session,
        admin=admin,
        agent=agent,
        agent_2=agent_2,
        customer=customer,
        customer_2=customer_2,
        **tickets,
    )


def _send(w: SimpleNamespace, sender: User, ticket: Ticket, content: str = "Hello"):
    return create_ticket_message(
        w.db,
        actor_id=sender.id,
        ticket_id=ticket.id,
        content=content,
    )


def _count(db: Session, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


# ---------------------------------------------------------------------------
# Creating messages: who may write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sender", "ticket", "role"),
    [
        ("admin", "assigned", "admin"),
        ("admin", "free", "admin"),
        ("admin", "other", "admin"),
        ("agent", "assigned", "agent"),
        ("customer", "assigned", "customer"),
        ("customer", "free", "customer"),
    ],
)
def test_permitted_senders_create_a_message(
    world: SimpleNamespace,
    sender: str,
    ticket: str,
    role: str,
) -> None:
    user = getattr(world, sender)
    target = getattr(world, ticket)

    view = _send(world, user, target, "  I have checked the issue.  ")

    assert view.ticket_id == target.id
    assert view.sender_user_id == user.id
    assert view.sender_name == user.name
    assert view.sender_role == role
    assert view.content == "I have checked the issue."
    assert view.created_at.tzinfo is timezone.utc

    stored = world.db.get(TicketMessage, view.id)

    assert (stored.ticket_id, stored.sender_user_id, stored.sender_role) == (
        target.id,
        user.id,
        role,
    )
    assert stored.content == "I have checked the issue."


@pytest.mark.parametrize(
    ("sender", "ticket", "error"),
    [
        # Agents write only on tickets assigned to them.
        ("agent", "free", TicketPermissionError),
        ("agent_2", "assigned", TicketPermissionError),
        ("agent", "other", TicketPermissionError),
        # Customers: another customer's ticket looks missing.
        ("customer", "other", TicketNotFoundError),
        ("customer_2", "assigned", TicketNotFoundError),
        # Resolved tickets are read-only for everyone.
        ("admin", "resolved", TicketResolvedError),
        ("agent", "resolved", TicketResolvedError),
        ("agent_2", "resolved", TicketResolvedError),
        ("customer", "resolved", TicketResolvedError),
    ],
)
def test_forbidden_senders_are_rejected_and_nothing_is_written(
    world: SimpleNamespace,
    sender: str,
    ticket: str,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        _send(world, getattr(world, sender), getattr(world, ticket))

    assert _count(world.db, TicketMessage) == 0
    # The session is usable again (the transaction was rolled back).
    assert world.db.get(Ticket, world.assigned.id) is not None


def test_customer_cannot_learn_that_another_customers_resolved_ticket_exists(
    world: SimpleNamespace,
) -> None:
    world.other.status = "Resolved"
    world.db.commit()

    # 404, not the resolved-ticket 409.
    with pytest.raises(TicketNotFoundError):
        _send(world, world.customer, world.other)


def test_missing_ticket_is_not_found(world: SimpleNamespace) -> None:
    with pytest.raises(TicketNotFoundError):
        create_ticket_message(world.db, actor_id=world.admin.id, ticket_id=9999, content="Hi")


def test_missing_actor_is_rejected(world: SimpleNamespace) -> None:
    with pytest.raises(TicketPermissionError):
        create_ticket_message(
            world.db, actor_id=9999, ticket_id=world.assigned.id, content="Hi"
        )


@pytest.mark.parametrize("sender", ["admin", "agent", "customer"])
def test_inactive_actor_is_rejected(world: SimpleNamespace, sender: str) -> None:
    user = getattr(world, sender)
    user.is_active = False
    world.db.commit()

    with pytest.raises(TicketPermissionError):
        _send(world, user, world.assigned)

    assert _count(world.db, TicketMessage) == 0


def test_unknown_role_is_rejected(world: SimpleNamespace) -> None:
    world.customer.role = "Customer"  # not an exact stored role
    world.db.commit()

    with pytest.raises(TicketPermissionError):
        _send(world, world.customer, world.assigned)


def test_agent_who_lost_the_assignment_can_no_longer_write(
    world: SimpleNamespace,
) -> None:
    _send(world, world.agent, world.assigned)

    world.assigned.assigned_to_user_id = world.agent_2.id
    world.db.commit()

    with pytest.raises(TicketPermissionError):
        _send(world, world.agent, world.assigned)

    # The new assignee can.
    _send(world, world.agent_2, world.assigned)

    assert _count(world.db, TicketMessage) == 2


def test_reopened_ticket_accepts_messages_again(world: SimpleNamespace) -> None:
    with pytest.raises(TicketResolvedError):
        _send(world, world.customer, world.resolved)

    change_ticket_status(
        world.db,
        actor_id=world.agent.id,
        ticket_id=world.resolved.id,
        new_status="In Progress",
        expected_version=world.resolved.version,
    )

    view = _send(world, world.customer, world.resolved, "Thanks for reopening.")

    assert view.ticket_id == world.resolved.id


def test_sender_role_is_a_snapshot(world: SimpleNamespace) -> None:
    view = _send(world, world.agent, world.assigned)

    world.agent.role = "admin"
    world.db.commit()

    page = list_ticket_messages(world.db, viewer=world.admin, ticket_id=world.assigned.id)

    assert [message.sender_role for message in page.messages] == ["agent"]
    assert world.db.get(TicketMessage, view.id).sender_role == "agent"


# ---------------------------------------------------------------------------
# Content validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    ["", "   ", "\n\t \r\n", "x" * (MAX_MESSAGE_LENGTH + 1), None, 42, b"bytes", ["a"]],
    ids=["empty", "spaces", "whitespace", "too-long", "none", "int", "bytes", "list"],
)
def test_invalid_content_is_rejected_before_any_database_work(
    world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    content: object,
) -> None:
    def must_not_lock(*_args, **_kwargs):
        raise AssertionError("the database must not be touched")

    monkeypatch.setattr(ticket_message_service, "_lock_for_message", must_not_lock)

    with pytest.raises(InvalidTicketMessageError) as error:
        create_ticket_message(
            world.db, actor_id=world.admin.id, ticket_id=world.free.id, content=content
        )

    # The error never repeats the content.
    assert "x" * 20 not in str(error.value)


def test_content_is_stripped_and_maximum_length_accepted(world: SimpleNamespace) -> None:
    body = "line one\n\nline two"

    assert _send(world, world.admin, world.free, f"\n  {body}  \n").content == body

    longest = "y" * MAX_MESSAGE_LENGTH

    assert _send(world, world.admin, world.free, f"  {longest}  ").content == longest


def test_html_like_content_is_stored_verbatim(world: SimpleNamespace) -> None:
    content = '<img src=x onerror="alert(1)"> & <b>bold</b>'

    view = _send(world, world.customer, world.free, content)

    assert world.db.get(TicketMessage, view.id).content == content


# ---------------------------------------------------------------------------
# Side effects
# ---------------------------------------------------------------------------


def test_messages_never_change_the_ticket_or_its_history(world: SimpleNamespace) -> None:
    ticket = world.assigned
    before = (ticket.version, ticket.updated_at, ticket.status, ticket.assigned_to_user_id)
    events_before = _count(world.db, TicketEvent)

    _send(world, world.customer, ticket)
    _send(world, world.agent, ticket)
    _send(world, world.admin, ticket)

    world.db.refresh(ticket)

    assert (
        ticket.version,
        ticket.updated_at,
        ticket.status,
        ticket.assigned_to_user_id,
    ) == before
    assert _count(world.db, TicketEvent) == events_before


def test_refuses_to_run_inside_an_open_write_transaction(world: SimpleNamespace) -> None:
    world.free.title = "Pending change"
    world.db.flush()

    with pytest.raises(TransactionAlreadyStartedError):
        _send(world, world.admin, world.free)

    # Rolled back: neither the message nor the pending change was kept.
    assert _count(world.db, TicketMessage) == 0
    assert world.db.get(Ticket, world.free.id).title == "Free"


# ---------------------------------------------------------------------------
# Failure and rollback
# ---------------------------------------------------------------------------


def _operational_error() -> OperationalError:
    return OperationalError("INSERT INTO ticket_messages ...", {}, Exception("disk I/O error"))


def test_commit_failure_rolls_back_and_keeps_nothing(
    world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_commit = world.db.commit

    def failing_commit():
        raise _operational_error()

    monkeypatch.setattr(world.db, "commit", failing_commit)

    with pytest.raises(OperationalError):
        _send(world, world.admin, world.free, "Will not be kept")

    monkeypatch.setattr(world.db, "commit", original_commit)

    assert _count(world.db, TicketMessage) == 0

    # The lock was released: the next message succeeds.
    _send(world, world.admin, world.free, "Kept")

    assert world.db.scalars(select(TicketMessage.content)).all() == ["Kept"]


def test_failure_after_the_insert_leaves_no_partial_row(
    world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_view(*_args, **_kwargs):
        raise RuntimeError("failure after the INSERT was flushed")

    monkeypatch.setattr(ticket_message_service, "_view", failing_view)

    with pytest.raises(RuntimeError):
        _send(world, world.customer, world.free)

    assert _count(world.db, TicketMessage) == 0


def test_rollback_failure_is_recorded_without_replacing_the_error(
    world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db.errors import rollback_failure_of

    def failing_view(*_args, **_kwargs):
        raise RuntimeError("primary failure")

    rollback_error = _operational_error()
    original_rollback = world.db.rollback

    def failing_rollback():
        original_rollback()
        raise rollback_error

    monkeypatch.setattr(ticket_message_service, "_view", failing_view)
    monkeypatch.setattr(world.db, "rollback", failing_rollback)

    with pytest.raises(RuntimeError, match="primary failure") as error:
        _send(world, world.customer, world.free)

    assert rollback_failure_of(error.value) is rollback_error


# ---------------------------------------------------------------------------
# Reading: access, ordering and pagination
# ---------------------------------------------------------------------------


def _fill(w: SimpleNamespace, ticket: Ticket, count: int) -> list[int]:
    senders = [w.customer, w.admin]

    if ticket.assigned_to_user_id == w.agent.id:
        senders.append(w.agent)

    return [
        _send(w, senders[number % len(senders)], ticket, f"message {number}").id
        for number in range(count)
    ]


@pytest.mark.parametrize("viewer", ["admin", "agent", "agent_2", "customer"])
def test_permitted_viewers_read_the_conversation(
    world: SimpleNamespace,
    viewer: str,
) -> None:
    ids = _fill(world, world.assigned, 3)

    page = list_ticket_messages(
        world.db, viewer=getattr(world, viewer), ticket_id=world.assigned.id
    )

    assert [message.id for message in page.messages] == ids[::-1]
    assert page.total == 3
    assert [message.sender_name for message in page.messages] == [
        "Alex Agent",
        "Ada Admin",
        "Cara Customer",
    ]


def test_resolved_conversations_stay_readable(world: SimpleNamespace) -> None:
    world.resolved.status = "In Progress"
    world.db.commit()
    _send(world, world.customer, world.resolved)
    world.resolved.status = "Resolved"
    world.db.commit()

    for viewer in (world.admin, world.agent, world.agent_2, world.customer):
        page = list_ticket_messages(world.db, viewer=viewer, ticket_id=world.resolved.id)

        assert page.total == 1


@pytest.mark.parametrize(
    ("viewer", "ticket"),
    [("customer", "other"), ("customer_2", "assigned")],
)
def test_customer_cannot_read_another_customers_conversation(
    world: SimpleNamespace,
    viewer: str,
    ticket: str,
) -> None:
    _send(world, world.admin, getattr(world, ticket), "Private")

    with pytest.raises(TicketNotFoundError):
        list_ticket_messages(
            world.db, viewer=getattr(world, viewer), ticket_id=getattr(world, ticket).id
        )


def test_missing_ticket_has_no_conversation(world: SimpleNamespace) -> None:
    with pytest.raises(TicketNotFoundError):
        list_ticket_messages(world.db, viewer=world.admin, ticket_id=9999)


def test_conversations_are_kept_per_ticket(world: SimpleNamespace) -> None:
    first = _fill(world, world.assigned, 2)
    second = _fill(world, world.free, 2)

    assigned = list_ticket_messages(world.db, viewer=world.admin, ticket_id=world.assigned.id)
    free = list_ticket_messages(world.db, viewer=world.admin, ticket_id=world.free.id)

    assert [m.id for m in assigned.messages] == first[::-1]
    assert [m.id for m in free.messages] == second[::-1]
    assert (assigned.total, free.total) == (2, 2)


def test_cursor_pagination_walks_back_without_gaps_or_duplicates(
    world: SimpleNamespace,
) -> None:
    ids = _fill(world, world.free, 7)
    seen: list[int] = []
    before_id = None

    while True:
        page = list_ticket_messages(
            world.db,
            viewer=world.customer,
            ticket_id=world.free.id,
            limit=3,
            before_id=before_id,
        )

        assert page.total == 7

        if not page.messages:
            break

        seen.extend(message.id for message in page.messages)
        before_id = page.messages[-1].id

    assert seen == ids[::-1]
    assert len(set(seen)) == 7


def test_cursor_is_stable_while_new_messages_arrive(world: SimpleNamespace) -> None:
    ids = _fill(world, world.free, 4)

    newest = list_ticket_messages(world.db, viewer=world.admin, ticket_id=world.free.id, limit=2)

    assert [m.id for m in newest.messages] == [ids[3], ids[2]]

    _send(world, world.customer, world.free, "arrived meanwhile")

    older = list_ticket_messages(
        world.db,
        viewer=world.admin,
        ticket_id=world.free.id,
        limit=2,
        before_id=newest.messages[-1].id,
    )

    assert [m.id for m in older.messages] == [ids[1], ids[0]]
    assert older.total == 5


def test_default_page_is_the_newest_fifty(world: SimpleNamespace) -> None:
    ids = _fill(world, world.free, 55)

    page = list_ticket_messages(world.db, viewer=world.admin, ticket_id=world.free.id)

    assert [m.id for m in page.messages] == ids[::-1][:50]
    assert page.total == 55


def test_listed_timestamps_are_utc(world: SimpleNamespace) -> None:
    created = _send(world, world.admin, world.free)
    world.db.expire_all()

    [listed] = list_ticket_messages(world.db, viewer=world.admin, ticket_id=world.free.id).messages

    assert listed.created_at.tzinfo is timezone.utc
    assert listed.created_at == created.created_at


def test_inactive_viewer_reads_nothing(world: SimpleNamespace) -> None:
    world.admin.is_active = False
    world.db.commit()

    assert can_read_conversation(world.admin, world.free) is False

    with pytest.raises(TicketNotFoundError):
        list_ticket_messages(world.db, viewer=world.admin, ticket_id=world.free.id)


@pytest.mark.parametrize(
    ("limit", "before_id", "error"),
    [
        (0, None, ValueError),
        (101, None, ValueError),
        (None, None, ValueError),
        (10, 0, ValueError),
        (10, -5, ValueError),
        (True, None, TypeError),
        ("10", None, TypeError),
        (10, "5", TypeError),
        (10, 2.5, TypeError),
    ],
)
def test_invalid_page_arguments_are_rejected(
    world: SimpleNamespace,
    limit: object,
    before_id: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        list_ticket_messages(
            world.db,
            viewer=world.admin,
            ticket_id=world.free.id,
            limit=limit,
            before_id=before_id,
        )
