"""Ticket conversation service: permissions, ordering, pagination, rollback.

Uses conftest's in-memory SQLite session (foreign keys enforced). The
concurrency tests, which need independent connections, are in
test_ticket_message_concurrency.py. resolveai.db is never used.
"""

from collections.abc import Callable
from datetime import timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.db.locking import TransactionAlreadyStartedError
from app.models import Notification, Ticket, TicketEvent, TicketMessage, User
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


# ---------------------------------------------------------------------------
# Notifications (Phase 10): written in the message's transaction
# ---------------------------------------------------------------------------


def _notified(db: Session, message_id: int | None = None) -> list[int]:
    """Recipient IDs of the stored notifications (of one message), sorted."""

    statement = select(Notification.recipient_user_id).order_by(
        Notification.recipient_user_id
    )

    if message_id is not None:
        statement = statement.where(Notification.message_id == message_id)

    return list(db.scalars(statement))


def _set_user(db: Session, user: User, **values) -> None:
    """Change a user row directly (legacy data, bypassing the workflow)."""

    for name, value in values.items():
        setattr(user, name, value)

    db.commit()


def test_customer_message_notifies_the_assigned_agent(world: SimpleNamespace) -> None:
    message = _send(world, world.customer, world.assigned)

    assert _notified(world.db, message.id) == [world.agent.id]


def test_customer_message_on_unassigned_ticket_notifies_every_active_admin(
    world: SimpleNamespace,
    create_user: Callable[..., User],
) -> None:
    admin_2 = create_user(email="admin2@example.test", role="admin", name="Abe Admin")
    retired = create_user(email="admin3@example.test", role="admin", name="Old Admin")
    _set_user(world.db, retired, is_active=False)

    message = _send(world, world.customer, world.free)

    assert _notified(world.db, message.id) == sorted([world.admin.id, admin_2.id])
    # Agents are not notified of the unassigned queue.
    assert world.agent.id not in _notified(world.db)
    assert world.agent_2.id not in _notified(world.db)


@pytest.mark.parametrize(
    "change",
    [{"is_active": False}, {"role": "customer"}, {"role": "Agent"}],
    ids=["inactive-assignee", "assignee-now-customer", "assignee-legacy-role"],
)
def test_customer_message_with_unusable_assignee_falls_back_to_admins(
    world: SimpleNamespace,
    change: dict,
) -> None:
    # Legacy data: the workflow would have unassigned the ticket.
    _set_user(world.db, world.agent, **change)

    message = _send(world, world.customer, world.assigned)

    assert _notified(world.db, message.id) == [world.admin.id]


def test_assignee_state_is_read_from_the_database_not_the_session(
    world: SimpleNamespace,
) -> None:
    # Changed behind the session's back: its copy of the agent is stale.
    world.db.execute(
        text("UPDATE users SET is_active = 0 WHERE id = :id"), {"id": world.agent.id}
    )
    world.db.commit()

    message = _send(world, world.customer, world.assigned)

    assert _notified(world.db, message.id) == [world.admin.id]


def test_customer_message_with_no_active_admins_is_still_saved(
    world: SimpleNamespace,
) -> None:
    _set_user(world.db, world.admin, is_active=False)

    message = _send(world, world.customer, world.free, "Anyone there?")

    assert world.db.get(TicketMessage, message.id).content == "Anyone there?"
    assert _count(world.db, Notification) == 0


def test_agent_message_notifies_the_customer(world: SimpleNamespace) -> None:
    message = _send(world, world.agent, world.assigned)

    assert _notified(world.db, message.id) == [world.customer.id]


def test_admin_message_notifies_the_customer(world: SimpleNamespace) -> None:
    message = _send(world, world.admin, world.free)

    assert _notified(world.db, message.id) == [world.customer.id]


def test_admin_message_on_assigned_ticket_notifies_customer_and_agent(
    world: SimpleNamespace,
) -> None:
    message = _send(world, world.admin, world.assigned)

    assert _notified(world.db, message.id) == sorted(
        [world.customer.id, world.agent.id]
    )
    # Never the sender.
    assert world.admin.id not in _notified(world.db)


def test_admin_is_never_notified_of_their_own_message(
    world: SimpleNamespace,
    create_user: Callable[..., User],
) -> None:
    create_user(email="admin2@example.test", role="admin", name="Abe Admin")
    # A ticket the administrator created themselves.
    own = Ticket(title="Admin's", description="Created by staff.", creator=world.admin)
    world.db.add(own)
    world.db.commit()

    message = _send(world, world.admin, own)

    assert _notified(world.db, message.id) == []


def test_staff_created_tickets_have_no_customer_recipient(
    world: SimpleNamespace,
) -> None:
    staff_ticket = Ticket(
        title="Internal",
        description="Opened by an agent.",
        creator=world.agent_2,
        status="In Progress",
        assigned_to_user_id=world.agent.id,
    )
    world.db.add(staff_ticket)
    world.db.commit()

    from_agent = _send(world, world.agent, staff_ticket)
    from_admin = _send(world, world.admin, staff_ticket)

    # The creating agent is not treated as a customer.
    assert _notified(world.db, from_agent.id) == []
    assert _notified(world.db, from_admin.id) == [world.agent.id]


def test_inactive_creator_is_skipped(world: SimpleNamespace) -> None:
    _set_user(world.db, world.customer, is_active=False)

    from_agent = _send(world, world.agent, world.assigned)
    from_admin = _send(world, world.admin, world.assigned)

    assert _notified(world.db, from_agent.id) == []
    assert _notified(world.db, from_admin.id) == [world.agent.id]


def test_inactive_assignee_is_skipped_for_admin_messages(
    world: SimpleNamespace,
) -> None:
    _set_user(world.db, world.agent, is_active=False)

    message = _send(world, world.admin, world.assigned)

    assert _notified(world.db, message.id) == [world.customer.id]


def test_sender_role_is_read_inside_the_transaction(world: SimpleNamespace) -> None:
    # The second agent becomes an administrator; their stale session copy
    # still says agent. As an administrator they may write and notify.
    world.db.execute(
        text("UPDATE users SET role = 'admin' WHERE id = :id"), {"id": world.agent_2.id}
    )
    world.db.commit()

    message = _send(world, world.agent_2, world.assigned)

    assert message.sender_role == "admin"
    assert _notified(world.db, message.id) == sorted(
        [world.customer.id, world.agent.id]
    )


def test_each_recipient_is_notified_once_per_message(
    world: SimpleNamespace,
    create_user: Callable[..., User],
) -> None:
    create_user(email="admin2@example.test", role="admin", name="Abe Admin")

    for sender, ticket in [
        (world.customer, world.assigned),
        (world.customer, world.free),
        (world.agent, world.assigned),
        (world.admin, world.assigned),
        (world.admin, world.free),
        (world.customer, world.free),
    ]:
        _send(world, sender, ticket)

    pairs = world.db.execute(
        select(Notification.message_id, Notification.recipient_user_id)
    ).all()

    assert len(pairs) == len(set(pairs)) == 1 + 2 + 1 + 2 + 1 + 2


def test_notification_references_come_from_the_new_message(
    world: SimpleNamespace,
) -> None:
    secret = "Order #A-1002, refund to card ending 4242"
    view = _send(world, world.admin, world.assigned, secret)

    message = world.db.get(TicketMessage, view.id)
    notifications = world.db.scalars(select(Notification)).all()

    assert len(notifications) == 2

    for notification in notifications:
        assert notification.actor_user_id == message.sender_user_id == world.admin.id
        assert notification.message_id == view.id
        assert notification.ticket_id == message.ticket_id == world.assigned.id
        assert notification.created_at == message.created_at
        assert notification.notification_type == "ticket_message"
        assert notification.read_at is None

    # The content lives only in the message.
    rows = world.db.execute(select(*Notification.__table__.columns)).all()

    assert all(secret not in str(value) for row in rows for value in row)

    # And no notification anywhere points at another ticket's message.
    assert world.db.scalar(
        select(func.count())
        .select_from(Notification)
        .join(TicketMessage, TicketMessage.id == Notification.message_id)
        .where(Notification.ticket_id != TicketMessage.ticket_id)
    ) == 0


def test_notification_failure_rolls_back_the_message(
    world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_helper = ticket_message_service.add_message_notifications
    seen: dict = {}

    def add_then_fail(db, message, recipient_ids):
        # The message was flushed: it has an ID, and rows are written.
        seen["message_id"] = message.id
        seen["added"] = len(real_helper(db, message, recipient_ids))
        raise RuntimeError("notification insert failed")

    monkeypatch.setattr(ticket_message_service, "add_message_notifications", add_then_fail)

    with pytest.raises(RuntimeError, match="notification insert failed"):
        _send(world, world.admin, world.assigned, "Never kept")

    assert seen["message_id"] is not None
    assert seen["added"] == 2
    assert _count(world.db, TicketMessage) == 0
    assert _count(world.db, Notification) == 0

    # The lock was released: the next message (and its notifications) work.
    monkeypatch.setattr(ticket_message_service, "add_message_notifications", real_helper)
    kept = _send(world, world.admin, world.assigned, "Kept")

    assert _notified(world.db, kept.id) == sorted([world.customer.id, world.agent.id])


def test_database_failure_inserting_notifications_rolls_back_everything(
    world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A recipient that does not exist: the notification flush violates
    # its foreign key.
    monkeypatch.setattr(
        ticket_message_service,
        "resolve_message_recipients",
        lambda *_args: {world.agent.id, 999_999},
    )

    with pytest.raises(IntegrityError):
        _send(world, world.customer, world.assigned)

    assert _count(world.db, TicketMessage) == 0
    assert _count(world.db, Notification) == 0


def test_rollback_failure_is_recorded_on_a_notification_failure(
    world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db.errors import rollback_failure_of

    rollback_error = _operational_error()
    original_rollback = world.db.rollback

    def failing_helper(*_args, **_kwargs):
        raise RuntimeError("primary failure")

    def failing_rollback():
        original_rollback()
        raise rollback_error

    monkeypatch.setattr(ticket_message_service, "add_message_notifications", failing_helper)
    monkeypatch.setattr(world.db, "rollback", failing_rollback)

    with pytest.raises(RuntimeError, match="primary failure") as error:
        _send(world, world.customer, world.assigned)

    assert rollback_failure_of(error.value) is rollback_error

    monkeypatch.setattr(world.db, "rollback", original_rollback)

    assert _count(world.db, TicketMessage) == 0


def test_message_with_nobody_to_notify_commits_normally(
    world: SimpleNamespace,
) -> None:
    # The creator is staff and nobody is assigned: no recipients at all.
    staff_ticket = Ticket(title="Internal", description="Staff only.", creator=world.admin)
    world.db.add(staff_ticket)
    world.db.commit()

    view = _send(world, world.admin, staff_ticket, "Note to self")

    assert world.db.get(TicketMessage, view.id).content == "Note to self"
    assert _count(world.db, Notification) == 0


def test_message_result_is_unchanged(world: SimpleNamespace) -> None:
    from dataclasses import fields

    from app.services.ticket_message_service import TicketMessageView

    view = _send(world, world.customer, world.assigned, "Hello")

    assert [field.name for field in fields(TicketMessageView)] == [
        "id",
        "ticket_id",
        "sender_user_id",
        "sender_name",
        "sender_role",
        "content",
        "created_at",
    ]
    assert (view.ticket_id, view.sender_user_id, view.sender_name, view.sender_role) == (
        world.assigned.id,
        world.customer.id,
        "Cara Customer",
        "customer",
    )
    assert view.content == "Hello"
    assert view.created_at.tzinfo is timezone.utc


def test_notifying_never_changes_the_ticket_or_its_history(
    world: SimpleNamespace,
) -> None:
    before = (
        world.assigned.version,
        world.assigned.status,
        world.assigned.assigned_to_user_id,
        world.assigned.updated_at,
    )
    events_before = _count(world.db, TicketEvent)

    _send(world, world.customer, world.assigned)
    _send(world, world.admin, world.assigned)
    world.db.refresh(world.assigned)

    assert _count(world.db, Notification) == 3
    assert (
        world.assigned.version,
        world.assigned.status,
        world.assigned.assigned_to_user_id,
        world.assigned.updated_at,
    ) == before
    assert _count(world.db, TicketEvent) == events_before


def test_only_the_actor_and_ticket_rows_are_locked(world: SimpleNamespace) -> None:
    """Recipient reads take no row locks (the global lock order is kept).

    SQLite never renders FOR UPDATE, so the ORM statements themselves are
    inspected: exactly two locking reads (actor, then ticket), and every
    read after them, including the active administrator IDs, is plain.
    """

    statements: list[tuple[str, bool]] = []

    @event.listens_for(world.db, "do_orm_execute")
    def record(state) -> None:
        if state.is_select:
            entity = state.statement.column_descriptions[0]["entity"]
            locking = state.statement._for_update_arg is not None
            statements.append((getattr(entity, "__name__", str(entity)), locking))

    try:
        _send(world, world.customer, world.free)
    finally:
        event.remove(world.db, "do_orm_execute", record)

    locking = [entity for entity, locks in statements if locks]

    # (Unlocked reads before the lock are the test's own expired objects
    # being refreshed.)
    assert locking == ["User", "Ticket"]

    after_ticket_lock = statements[statements.index(("Ticket", True)) + 1 :]

    # The recipient context (creator and active administrators) is read
    # after the ticket lock, and unlocked.
    assert after_ticket_lock.count(("User", False)) >= 2
    assert all(not locks for _entity, locks in after_ticket_lock)
    assert _notified(world.db) == [world.admin.id]
