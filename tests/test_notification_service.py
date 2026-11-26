"""Notification service: recipients, creation, visibility, reading, marking.

The recipient resolver is tested with plain stand-in objects (it never
touches the database). Everything else uses conftest's in-memory SQLite
session (foreign keys enforced). resolveai.db is never used.
"""

import inspect as python_inspect
import logging
from collections.abc import Callable
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.core.roles import UserRole
from app.db.errors import rollback_failure_of
from app.db.locking import TransactionAlreadyStartedError
from app.models import Notification, Ticket, TicketMessage, User
from app.services import notification_service
from app.services.notification_service import (
    MAX_PAGE_SIZE,
    PREVIEW_LENGTH,
    NotificationNotFoundError,
    NotificationView,
    add_message_notifications,
    get_unread_notification_summary,
    list_notifications,
    make_preview,
    mark_notification_read,
    mark_notifications_read,
    resolve_message_recipients,
)


T0 = datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc)


# ===========================================================================
# Recipient resolver (pure)
# ===========================================================================

CUSTOMER, AGENT, OTHER_AGENT, CREATOR_ADMIN = 10, 20, 21, 30
ADMINS = [1, 2, 3]


def _person(user_id: int, role: str, *, active: bool = True) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, role=role, is_active=active)


def _ticket_of(creator_id: int, assignee_id: int | None) -> SimpleNamespace:
    return SimpleNamespace(
        created_by_user_id=creator_id,
        assigned_to_user_id=assignee_id,
    )


def _resolve(
    sender_role,
    sender_id,
    *,
    creator=None,
    assignee=None,
    assignee_id=None,
    admins=ADMINS,
    creator_id=None,
):
    """Resolve for a ticket made from the given creator and assignee rows."""

    if assignee_id is None and assignee is not None:
        assignee_id = assignee.id

    if creator_id is None:
        creator_id = creator.id if creator is not None else CUSTOMER

    return resolve_message_recipients(
        sender_role,
        sender_id,
        _ticket_of(creator_id, assignee_id),
        assignee,
        creator,
        admins,
    )


CUSTOMER_ROW = _person(CUSTOMER, "customer")
AGENT_ROW = _person(AGENT, "agent")


@pytest.mark.parametrize(
    ("case", "kwargs", "expected"),
    [
        # 1. Customer, assigned to an active agent: the agent only.
        (
            "customer-assigned",
            dict(sender_role="customer", sender_id=CUSTOMER,
                 creator=CUSTOMER_ROW, assignee=AGENT_ROW),
            {AGENT},
        ),
        # 2. Customer, unassigned: every active administrator.
        (
            "customer-unassigned",
            dict(sender_role="customer", sender_id=CUSTOMER, creator=CUSTOMER_ROW),
            set(ADMINS),
        ),
        # 3. Customer, assignee inactive: fall back to the administrators.
        (
            "customer-inactive-assignee",
            dict(sender_role="customer", sender_id=CUSTOMER, creator=CUSTOMER_ROW,
                 assignee=_person(AGENT, "agent", active=False)),
            set(ADMINS),
        ),
        # 3. Customer, assignee is no longer an agent: administrators.
        (
            "customer-assignee-now-customer",
            dict(sender_role="customer", sender_id=CUSTOMER, creator=CUSTOMER_ROW,
                 assignee=_person(AGENT, "customer")),
            set(ADMINS),
        ),
        (
            "customer-assignee-legacy-role",
            dict(sender_role="customer", sender_id=CUSTOMER, creator=CUSTOMER_ROW,
                 assignee=_person(AGENT, "Agent")),
            set(ADMINS),
        ),
        # 3. Customer, assignee row missing: administrators.
        (
            "customer-assignee-row-missing",
            dict(sender_role="customer", sender_id=CUSTOMER, creator=CUSTOMER_ROW,
                 assignee=None, assignee_id=AGENT),
            set(ADMINS),
        ),
        # 4. Customer, unassigned, no active administrators: nobody.
        (
            "customer-no-admins",
            dict(sender_role="customer", sender_id=CUSTOMER, creator=CUSTOMER_ROW,
                 admins=[]),
            set(),
        ),
        (
            "customer-inactive-assignee-no-admins",
            dict(sender_role="customer", sender_id=CUSTOMER, creator=CUSTOMER_ROW,
                 assignee=_person(AGENT, "agent", active=False), admins=[]),
            set(),
        ),
        # 5. Agent: the active customer creator.
        (
            "agent-to-customer",
            dict(sender_role="agent", sender_id=AGENT,
                 creator=CUSTOMER_ROW, assignee=AGENT_ROW),
            {CUSTOMER},
        ),
        # 6. Agent: creator is staff, inactive, or no longer a customer.
        (
            "agent-staff-created-ticket",
            dict(sender_role="agent", sender_id=AGENT,
                 creator=_person(CREATOR_ADMIN, "admin"), assignee=AGENT_ROW),
            set(),
        ),
        (
            "agent-agent-created-ticket",
            dict(sender_role="agent", sender_id=AGENT,
                 creator=_person(OTHER_AGENT, "agent"), assignee=AGENT_ROW),
            set(),
        ),
        (
            "agent-inactive-customer",
            dict(sender_role="agent", sender_id=AGENT,
                 creator=_person(CUSTOMER, "customer", active=False),
                 assignee=AGENT_ROW),
            set(),
        ),
        (
            "agent-creator-now-agent",
            dict(sender_role="agent", sender_id=AGENT,
                 creator=_person(CUSTOMER, "agent"), assignee=AGENT_ROW),
            set(),
        ),
        (
            "agent-creator-unknown-role",
            dict(sender_role="agent", sender_id=AGENT,
                 creator=_person(CUSTOMER, "superuser"), assignee=AGENT_ROW),
            set(),
        ),
        # 7. Admin, unassigned: the customer creator only.
        (
            "admin-unassigned",
            dict(sender_role="admin", sender_id=1, creator=CUSTOMER_ROW),
            {CUSTOMER},
        ),
        # 7b. Admin, assigned to an active agent: creator and agent.
        (
            "admin-assigned",
            dict(sender_role="admin", sender_id=1,
                 creator=CUSTOMER_ROW, assignee=AGENT_ROW),
            {CUSTOMER, AGENT},
        ),
        (
            "admin-inactive-assignee",
            dict(sender_role="admin", sender_id=1, creator=CUSTOMER_ROW,
                 assignee=_person(AGENT, "agent", active=False)),
            {CUSTOMER},
        ),
        (
            "admin-inactive-customer-assigned",
            dict(sender_role="admin", sender_id=1,
                 creator=_person(CUSTOMER, "customer", active=False),
                 assignee=AGENT_ROW),
            {AGENT},
        ),
        # 8. Admin on a staff-created ticket: nobody, or just the agent.
        (
            "admin-own-ticket",
            dict(sender_role="admin", sender_id=CREATOR_ADMIN,
                 creator=_person(CREATOR_ADMIN, "admin")),
            set(),
        ),
        (
            "admin-staff-ticket-assigned",
            dict(sender_role="admin", sender_id=1,
                 creator=_person(CREATOR_ADMIN, "admin"), assignee=AGENT_ROW),
            {AGENT},
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_decision_table(case: str, kwargs: dict, expected: set[int]) -> None:
    assert _resolve(**kwargs) == expected


def test_every_active_admin_is_notified_once() -> None:
    assert _resolve(
        "customer", CUSTOMER, creator=CUSTOMER_ROW, admins=[5, 3, 3, 9, 5]
    ) == {3, 5, 9}


def test_unassigned_ticket_never_notifies_agents() -> None:
    """Only administrators: agents are not in the input, and none appear."""

    recipients = _resolve("customer", CUSTOMER, creator=CUSTOMER_ROW)

    assert AGENT not in recipients
    assert recipients == set(ADMINS)


def test_sender_is_never_notified() -> None:
    # Inconsistent data (the sender listed as an administrator) still
    # never notifies the sender.
    assert _resolve(
        "customer", CUSTOMER, creator=CUSTOMER_ROW, admins=[1, CUSTOMER]
    ) == {1}
    # An administrator who created the ticket as a "customer" row is
    # excluded as the sender too.
    assert _resolve(
        "admin", CUSTOMER, creator=_person(CUSTOMER, "customer")
    ) == set()


@pytest.mark.parametrize(
    "sender_role",
    ["superuser", "Admin", "CUSTOMER", "", None, 7],
)
def test_unknown_sender_role_notifies_nobody(sender_role) -> None:
    assert _resolve(
        sender_role, CUSTOMER, creator=CUSTOMER_ROW, assignee=AGENT_ROW
    ) == set()


def test_role_enum_members_are_accepted() -> None:
    assert _resolve(
        UserRole.AGENT, AGENT, creator=CUSTOMER_ROW, assignee=AGENT_ROW
    ) == {CUSTOMER}


def test_customer_who_did_not_create_the_ticket_notifies_nobody() -> None:
    assert _resolve(
        "customer", 99, creator=CUSTOMER_ROW, assignee=AGENT_ROW
    ) == set()


def test_rows_that_are_not_the_tickets_users_are_ignored() -> None:
    # The assignee row is a different user from the ticket's assignee.
    assert _resolve(
        "customer", CUSTOMER, creator=CUSTOMER_ROW,
        assignee=_person(OTHER_AGENT, "agent"), assignee_id=AGENT,
    ) == set(ADMINS)
    # The creator row is a different user from the ticket's creator.
    assert _resolve(
        "agent", AGENT, creator=_person(77, "customer"),
        creator_id=CUSTOMER, assignee=AGENT_ROW,
    ) == set()


def test_resolver_is_pure_and_deterministic() -> None:
    signature = python_inspect.signature(resolve_message_recipients)

    assert list(signature.parameters) == [
        "sender_role",
        "sender_id",
        "ticket",
        "assignee",
        "creator",
        "active_admin_ids",
    ]

    # Stand-in objects only (no session anywhere); same input, same output.
    results = {
        frozenset(
            _resolve("admin", 1, creator=CUSTOMER_ROW, assignee=AGENT_ROW)
        )
        for _ in range(5)
    }

    assert results == {frozenset({CUSTOMER, AGENT})}


# ===========================================================================
# Database fixtures
# ===========================================================================


@pytest.fixture
def world(db_session: Session, create_user: Callable[..., User]) -> SimpleNamespace:
    """Staff, customers and tickets.

    ``mine`` (customer's, assigned to ``agent``), ``free`` (customer's,
    unassigned) and ``theirs`` (customer_2's, assigned to ``agent``).
    """

    users = {
        "admin": create_user(email="admin@example.test", role="admin", name="Ada Admin"),
        "admin_2": create_user(email="admin2@example.test", role="admin", name="Abe Admin"),
        "agent": create_user(email="agent@example.test", role="agent", name="Alex Agent"),
        "agent_2": create_user(email="agent2@example.test", role="agent", name="Bo Agent"),
        "customer": create_user(email="c1@example.test", role="customer", name="Cara Customer"),
        "customer_2": create_user(email="c2@example.test", role="customer", name="Dan Customer"),
    }
    tickets = {
        "mine": Ticket(
            title="Payment failed",
            description="Card declined.",
            creator=users["customer"],
            status="In Progress",
            assigned_to_user_id=users["agent"].id,
        ),
        "free": Ticket(
            title="Login issue",
            description="Cannot sign in.",
            creator=users["customer"],
        ),
        "theirs": Ticket(
            title="Late order",
            description="Order is late.",
            creator=users["customer_2"],
            assigned_to_user_id=users["agent"].id,
        ),
    }
    db_session.add_all(tickets.values())
    db_session.commit()

    return SimpleNamespace(db=db_session, **users, **tickets)


_clock = {"tick": 0}


def _message(
    w: SimpleNamespace,
    sender: User,
    ticket: Ticket,
    content: str = "Hello there",
    *,
    sender_role: str | None = None,
) -> TicketMessage:
    """Insert (and flush) a message directly, with a distinct timestamp."""

    _clock["tick"] += 1
    message = TicketMessage(
        ticket_id=ticket.id,
        sender_user_id=sender.id,
        sender_role=sender_role or sender.role,
        content=content,
        created_at=T0 + timedelta(seconds=_clock["tick"]),
    )
    w.db.add(message)
    w.db.flush()

    return message


def _notify(
    w: SimpleNamespace,
    sender: User,
    ticket: Ticket,
    recipients: list[User],
    content: str = "Hello there",
    **message_fields,
) -> list[Notification]:
    """A committed message and its notifications."""

    message = _message(w, sender, ticket, content, **message_fields)
    notifications = add_message_notifications(
        w.db, message, [user.id for user in recipients]
    )
    w.db.commit()

    return notifications


def _count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Notification))


def _read_state(db: Session) -> dict[int, datetime | None]:
    return dict(db.execute(select(Notification.id, Notification.read_at)).all())


def _ids(page) -> list[int]:
    return [view.id for view in page.notifications]


def _write_lock_released(db: Session) -> bool:
    """The SQLite connection has no write transaction left open."""

    return not db.connection().connection.driver_connection.in_transaction


def _set_read(w: SimpleNamespace, notification_id: int, when: datetime = T0) -> None:
    w.db.get(Notification, notification_id).read_at = when
    w.db.commit()


# ===========================================================================
# Creating notifications
# ===========================================================================


def test_creation_uses_the_messages_references(world: SimpleNamespace) -> None:
    message = _message(world, world.customer, world.mine, "Any update?")
    sent_at = message.created_at
    created = add_message_notifications(world.db, message, [world.agent.id])
    world.db.commit()

    [notification] = created
    world.db.expire_all()
    stored = world.db.get(Notification, notification.id)

    assert stored.recipient_user_id == world.agent.id
    assert stored.actor_user_id == world.customer.id
    assert stored.ticket_id == world.mine.id
    assert stored.message_id == message.id
    assert stored.notification_type == "ticket_message"
    assert stored.read_at is None
    # SQLite returns the stored UTC time without its offset.
    assert stored.created_at.replace(tzinfo=timezone.utc) == sent_at


def test_created_at_is_the_messages_time(world: SimpleNamespace) -> None:
    message = _message(world, world.customer, world.free)
    notifications = add_message_notifications(
        world.db, message, [world.admin.id, world.admin_2.id]
    )

    assert {n.created_at for n in notifications} == {message.created_at}


def test_no_message_content_is_stored(world: SimpleNamespace) -> None:
    secret = "Card ending 4242, PIN never shared"
    message = _message(world, world.customer, world.mine, secret)
    [notification] = add_message_notifications(world.db, message, [world.agent.id])
    world.db.commit()

    column_names = {column.name for column in Notification.__table__.columns}

    assert column_names == {
        "id",
        "recipient_user_id",
        "actor_user_id",
        "ticket_id",
        "message_id",
        "notification_type",
        "created_at",
        "read_at",
    }

    row = world.db.execute(
        select(*Notification.__table__.columns).where(
            Notification.id == notification.id
        )
    ).one()

    assert all(secret not in str(value) for value in row)


def test_sender_is_skipped(world: SimpleNamespace) -> None:
    message = _message(world, world.admin, world.mine)
    created = add_message_notifications(
        world.db, message, [world.admin.id, world.customer.id]
    )

    assert [n.recipient_user_id for n in created] == [world.customer.id]


def test_duplicates_are_removed_in_recipient_order(world: SimpleNamespace) -> None:
    message = _message(world, world.customer, world.free)
    created = add_message_notifications(
        world.db,
        message,
        [world.admin_2.id, world.admin.id, world.admin_2.id, world.admin.id],
    )
    world.db.commit()

    assert [n.recipient_user_id for n in created] == sorted(
        [world.admin.id, world.admin_2.id]
    )
    assert _count(world.db) == 2


def test_nobody_to_notify_adds_nothing(world: SimpleNamespace) -> None:
    message = _message(world, world.customer, world.free)

    assert add_message_notifications(world.db, message, []) == []
    assert add_message_notifications(world.db, message, [world.customer.id]) == []
    assert not world.db.new


def test_helper_does_not_commit(world: SimpleNamespace) -> None:
    message = _message(world, world.customer, world.mine)
    add_message_notifications(world.db, message, [world.agent.id])

    assert world.db.in_transaction()

    # The caller's rollback removes the message and its notifications.
    world.db.rollback()

    assert _count(world.db) == 0
    assert world.db.scalar(select(func.count()).select_from(TicketMessage)) == 0


def test_database_failure_propagates(world: SimpleNamespace) -> None:
    message = _message(world, world.customer, world.mine)

    with pytest.raises(IntegrityError):
        add_message_notifications(world.db, message, [world.agent.id, 9999])

    world.db.rollback()

    assert _count(world.db) == 0


@pytest.mark.parametrize(
    ("bad", "error"),
    [(True, TypeError), ("2", TypeError), (2.0, TypeError), (0, ValueError), (-1, ValueError)],
    ids=["bool", "string", "float", "zero", "negative"],
)
def test_invalid_recipient_ids_add_nothing(
    world: SimpleNamespace,
    bad,
    error: type[Exception],
) -> None:
    message = _message(world, world.customer, world.mine)

    with pytest.raises(error):
        add_message_notifications(world.db, message, [world.agent.id, bad])

    assert not world.db.new


def test_unflushed_message_is_rejected(world: SimpleNamespace) -> None:
    message = TicketMessage(
        ticket_id=world.mine.id,
        sender_user_id=world.customer.id,
        sender_role="customer",
        content="Not flushed",
    )

    with pytest.raises(ValueError):
        add_message_notifications(world.db, message, [world.agent.id])

    assert _count(world.db) == 0


def test_ticket_always_comes_from_the_message(world: SimpleNamespace) -> None:
    # The helper takes no ticket argument at all.
    assert list(python_inspect.signature(add_message_notifications).parameters) == [
        "db",
        "message",
        "recipient_ids",
    ]

    _notify(world, world.customer, world.mine, [world.agent])
    _notify(world, world.customer_2, world.theirs, [world.agent])
    _notify(world, world.agent, world.mine, [world.customer])
    _notify(world, world.customer, world.free, [world.admin, world.admin_2])

    mismatched = world.db.scalar(
        select(func.count())
        .select_from(Notification)
        .join(TicketMessage, TicketMessage.id == Notification.message_id)
        .where(Notification.ticket_id != TicketMessage.ticket_id)
    )

    assert _count(world.db) == 5
    assert mismatched == 0


def test_resolved_recipients_feed_the_helper(world: SimpleNamespace) -> None:
    """End to end without the message service: resolver, then helper."""

    message = _message(world, world.admin, world.mine)
    recipients = resolve_message_recipients(
        world.admin.role,
        world.admin.id,
        world.mine,
        world.db.get(User, world.mine.assigned_to_user_id),
        world.db.get(User, world.mine.created_by_user_id),
        [world.admin.id, world.admin_2.id],
    )
    created = add_message_notifications(world.db, message, recipients)

    assert {n.recipient_user_id for n in created} == {
        world.customer.id,
        world.agent.id,
    }


# ===========================================================================
# Listing and visibility
# ===========================================================================


def test_recipients_only_see_their_own(world: SimpleNamespace) -> None:
    [to_agent] = _notify(world, world.customer, world.mine, [world.agent])
    to_admins = _notify(world, world.customer, world.free, [world.admin, world.admin_2])

    assert _ids(list_notifications(world.db, viewer=world.agent)) == [to_agent.id]
    assert _ids(list_notifications(world.db, viewer=world.admin)) == [to_admins[0].id]
    assert _ids(list_notifications(world.db, viewer=world.admin_2)) == [to_admins[1].id]
    assert _ids(list_notifications(world.db, viewer=world.agent_2)) == []
    assert _ids(list_notifications(world.db, viewer=world.customer)) == []


def test_customers_see_only_their_own_tickets(world: SimpleNamespace) -> None:
    [own] = _notify(world, world.agent, world.mine, [world.customer])
    # Inconsistent data: customer addressed on someone else's ticket.
    _notify(world, world.agent, world.theirs, [world.customer])

    assert _ids(list_notifications(world.db, viewer=world.customer)) == [own.id]
    assert list_notifications(world.db, viewer=world.customer).total == 1


def test_demoted_agent_loses_other_customers_notifications(
    world: SimpleNamespace,
) -> None:
    _notify(world, world.customer, world.mine, [world.agent])
    _notify(world, world.customer_2, world.theirs, [world.agent])

    assert list_notifications(world.db, viewer=world.agent).total == 2

    world.agent.role = "customer"
    world.db.commit()

    page = list_notifications(world.db, viewer=world.agent)

    assert page.notifications == []
    assert page.total == 0
    assert get_unread_notification_summary(world.db, world.agent) == (
        notification_service.NotificationSummary(unread_count=0, latest_id=None)
    )


@pytest.mark.parametrize(
    "change",
    [
        {"is_active": False},
        {"role": "superuser"},
        {"role": "Agent"},
    ],
    ids=["inactive", "unknown-role", "role-wrong-case"],
)
def test_inactive_or_unknown_role_viewers_see_nothing(
    world: SimpleNamespace,
    change: dict,
) -> None:
    _notify(world, world.customer, world.mine, [world.agent])

    for name, value in change.items():
        setattr(world.agent, name, value)

    world.db.commit()

    assert list_notifications(world.db, viewer=world.agent).total == 0
    assert get_unread_notification_summary(world.db, world.agent).latest_id is None


def test_missing_viewer_sees_nothing(world: SimpleNamespace) -> None:
    _notify(world, world.customer, world.mine, [world.agent])

    assert list_notifications(world.db, viewer=None).total == 0


def test_newest_first_with_gapless_cursor_pagination(world: SimpleNamespace) -> None:
    created = [
        _notify(world, world.customer, world.mine, [world.agent], f"m{n}")[0].id
        for n in range(7)
    ]

    seen: list[int] = []
    cursor = None

    while True:
        page = list_notifications(
            world.db, viewer=world.agent, limit=3, before_id=cursor
        )

        assert page.total == 7

        if not page.notifications:
            break

        seen.extend(_ids(page))
        cursor = page.notifications[-1].id

    assert seen == sorted(created, reverse=True)
    assert len(set(seen)) == 7


def test_cursor_is_stable_while_new_notifications_arrive(
    world: SimpleNamespace,
) -> None:
    first = [
        _notify(world, world.customer, world.mine, [world.agent])[0].id
        for _ in range(4)
    ]
    page = list_notifications(world.db, viewer=world.agent, limit=2)

    _notify(world, world.customer, world.mine, [world.agent])

    older = list_notifications(
        world.db, viewer=world.agent, limit=2, before_id=page.notifications[-1].id
    )

    assert _ids(page) + _ids(older) == sorted(first, reverse=True)


@pytest.mark.parametrize("limit", [1, MAX_PAGE_SIZE])
def test_limit_boundaries_are_accepted(world: SimpleNamespace, limit: int) -> None:
    for _ in range(MAX_PAGE_SIZE + 1):
        message = _message(world, world.customer, world.mine)
        add_message_notifications(world.db, message, [world.agent.id])

    world.db.commit()

    page = list_notifications(world.db, viewer=world.agent, limit=limit)

    assert len(page.notifications) == limit
    assert page.total == MAX_PAGE_SIZE + 1


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({"limit": 0}, ValueError),
        ({"limit": MAX_PAGE_SIZE + 1}, ValueError),
        ({"limit": -1}, ValueError),
        ({"limit": None}, ValueError),
        ({"limit": True}, TypeError),
        ({"limit": "10"}, TypeError),
        ({"before_id": 0}, ValueError),
        ({"before_id": -5}, ValueError),
        ({"before_id": "3"}, TypeError),
        ({"before_id": False}, TypeError),
        ({"unread_only": "yes"}, TypeError),
        ({"unread_only": 1}, TypeError),
    ],
)
def test_invalid_page_arguments_are_rejected(
    world: SimpleNamespace,
    arguments: dict,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        list_notifications(world.db, viewer=world.agent, **arguments)


def test_unread_only_and_filtered_total(world: SimpleNamespace) -> None:
    ids = [
        _notify(world, world.customer, world.mine, [world.agent])[0].id
        for _ in range(5)
    ]
    _set_read(world, ids[1])
    _set_read(world, ids[3])

    unread = list_notifications(world.db, viewer=world.agent, unread_only=True)
    everything = list_notifications(world.db, viewer=world.agent)

    assert _ids(unread) == [ids[4], ids[2], ids[0]]
    assert unread.total == 3
    assert everything.total == 5
    assert all(not view.is_read and view.read_at is None for view in unread.notifications)

    # The total ignores the cursor; the page honours both filters.
    paged = list_notifications(
        world.db, viewer=world.agent, unread_only=True, before_id=ids[2]
    )

    assert _ids(paged) == [ids[0]]
    assert paged.total == 3


def test_view_contents(world: SimpleNamespace) -> None:
    [notification] = _notify(
        world, world.customer, world.mine, [world.agent], "Still   waiting\n\nfor help"
    )
    [view] = list_notifications(world.db, viewer=world.agent).notifications

    assert view.id == notification.id
    assert view.notification_type == "ticket_message"
    assert view.ticket_id == world.mine.id
    assert view.ticket_title == "Payment failed"
    assert view.message_id == notification.message_id
    assert view.actor_name == "Cara Customer"
    assert view.actor_role == "customer"
    assert view.preview == "Still waiting for help"
    assert view.created_at.tzinfo is timezone.utc
    assert view.read_at is None
    assert view.is_read is False


def test_read_view_reports_utc_read_at(world: SimpleNamespace) -> None:
    [notification] = _notify(world, world.customer, world.mine, [world.agent])
    _set_read(world, notification.id, T0)

    [view] = list_notifications(world.db, viewer=world.agent).notifications

    assert view.is_read is True
    assert view.read_at == T0
    assert view.read_at.tzinfo is timezone.utc


def test_actor_role_is_the_message_snapshot(world: SimpleNamespace) -> None:
    _notify(world, world.agent, world.mine, [world.customer])

    # The sender later becomes an administrator.
    world.agent.role = "admin"
    world.db.commit()

    [view] = list_notifications(world.db, viewer=world.customer).notifications

    assert view.actor_role == "agent"
    assert view.actor_name == "Alex Agent"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("short", "short"),
        ("x" * PREVIEW_LENGTH, "x" * PREVIEW_LENGTH),
        ("x" * (PREVIEW_LENGTH + 1), "x" * (PREVIEW_LENGTH - 1) + "…"),
        ("a\n\tb   c", "a b c"),
        ("<b>bold</b> & <script>x</script>", "<b>bold</b> & <script>x</script>"),
        ("", ""),
        (None, ""),
    ],
    ids=["short", "exact", "one-over", "whitespace", "html-verbatim", "empty", "none"],
)
def test_make_preview(content, expected: str) -> None:
    assert make_preview(content) == expected
    assert len(make_preview(content)) <= PREVIEW_LENGTH


def test_long_message_preview_is_truncated(world: SimpleNamespace) -> None:
    _notify(world, world.customer, world.mine, [world.agent], "word " * 200)

    [view] = list_notifications(world.db, viewer=world.agent).notifications

    assert len(view.preview) <= PREVIEW_LENGTH
    assert view.preview.startswith("word word")
    assert view.preview.endswith("…")


def test_views_expose_no_private_user_data(world: SimpleNamespace) -> None:
    _notify(world, world.customer, world.mine, [world.agent])

    assert {field.name for field in fields(NotificationView)} == {
        "id",
        "notification_type",
        "ticket_id",
        "ticket_title",
        "message_id",
        "actor_name",
        "actor_role",
        "preview",
        "created_at",
        "read_at",
        "is_read",
    }

    [view] = list_notifications(world.db, viewer=world.agent).notifications
    customer = world.db.get(User, world.customer.id)
    values = [str(getattr(view, field.name)) for field in fields(view)]

    for secret in (customer.email, customer.password_hash, world.agent.email):
        assert all(secret not in value for value in values)


def test_reads_do_not_change_anything(world: SimpleNamespace) -> None:
    _notify(world, world.customer, world.mine, [world.agent])
    before = _read_state(world.db)

    list_notifications(world.db, viewer=world.agent)
    list_notifications(world.db, viewer=world.agent, unread_only=True)
    get_unread_notification_summary(world.db, world.agent)

    assert not world.db.dirty and not world.db.new
    assert _read_state(world.db) == before


def test_message_content_is_never_logged(
    world: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "do-not-log-this-content"

    with caplog.at_level(logging.DEBUG):
        [notification] = _notify(world, world.customer, world.mine, [world.agent], secret)
        list_notifications(world.db, viewer=world.agent)
        mark_notification_read(world.db, world.agent, notification.id)
        mark_notifications_read(world.db, world.agent)

    assert secret not in caplog.text


# ===========================================================================
# Unread summary
# ===========================================================================


def test_summary_counts_unread_and_reports_the_newest_id(
    world: SimpleNamespace,
) -> None:
    ids = [
        _notify(world, world.customer, world.mine, [world.agent])[0].id
        for _ in range(3)
    ]
    # Someone else's notification is newer, and not counted.
    _notify(world, world.customer, world.free, [world.admin])

    summary = get_unread_notification_summary(world.db, world.agent)

    assert summary.unread_count == 3
    assert summary.latest_id == ids[-1]


def test_summary_latest_id_includes_read_notifications(world: SimpleNamespace) -> None:
    ids = [
        _notify(world, world.customer, world.mine, [world.agent])[0].id
        for _ in range(3)
    ]
    _set_read(world, ids[0])
    _set_read(world, ids[2])

    summary = get_unread_notification_summary(world.db, world.agent)

    assert summary.unread_count == 1
    assert summary.latest_id == ids[2]


def test_summary_when_there_is_nothing(world: SimpleNamespace) -> None:
    summary = get_unread_notification_summary(world.db, world.customer)

    assert summary.unread_count == 0
    assert summary.latest_id is None


# ===========================================================================
# Marking one notification read
# ===========================================================================


def test_recipient_marks_their_notification_read(world: SimpleNamespace) -> None:
    [notification] = _notify(world, world.customer, world.mine, [world.agent])
    before = datetime.now(timezone.utc)

    view = mark_notification_read(world.db, world.agent, notification.id)

    assert view.id == notification.id
    assert view.is_read is True
    assert before <= view.read_at <= datetime.now(timezone.utc)
    assert _write_lock_released(world.db)
    assert get_unread_notification_summary(world.db, world.agent).unread_count == 0


def test_marking_again_keeps_the_first_read_at(world: SimpleNamespace) -> None:
    [notification] = _notify(world, world.customer, world.mine, [world.agent])

    first = mark_notification_read(world.db, world.agent, notification.id)
    second = mark_notification_read(world.db, world.agent, notification.id)

    assert second.read_at == first.read_at
    assert _read_state(world.db)[notification.id].replace(
        tzinfo=timezone.utc
    ) == first.read_at


def test_existing_read_at_is_never_overwritten(world: SimpleNamespace) -> None:
    [notification] = _notify(world, world.customer, world.mine, [world.agent])
    _set_read(world, notification.id, T0)

    view = mark_notification_read(world.db, world.agent, notification.id)

    assert view.read_at == T0


def _not_found_message(callable_) -> str:
    with pytest.raises(NotificationNotFoundError) as error:
        callable_()

    return str(error.value)


def test_missing_and_other_users_notifications_are_indistinguishable(
    world: SimpleNamespace,
) -> None:
    [to_agent] = _notify(world, world.customer, world.mine, [world.agent])
    before = _read_state(world.db)

    others = _not_found_message(
        lambda: mark_notification_read(world.db, world.agent_2, to_agent.id)
    )
    missing = _not_found_message(
        lambda: mark_notification_read(world.db, world.agent_2, 9999)
    )

    assert others == missing == "Notification not found."
    # Rolled back: the lock is released and nothing changed.
    assert _write_lock_released(world.db)
    assert _read_state(world.db) == before


def test_customer_cannot_mark_a_hidden_notification(world: SimpleNamespace) -> None:
    # Inconsistent data: customer addressed on someone else's ticket.
    [hidden] = _notify(world, world.agent, world.theirs, [world.customer])

    with pytest.raises(NotificationNotFoundError):
        mark_notification_read(world.db, world.customer, hidden.id)

    assert _read_state(world.db)[hidden.id] is None


def test_demoted_agent_cannot_mark_old_notifications(world: SimpleNamespace) -> None:
    [old] = _notify(world, world.customer_2, world.theirs, [world.agent])
    world.agent.role = "customer"
    world.db.commit()

    with pytest.raises(NotificationNotFoundError):
        mark_notification_read(world.db, world.agent, old.id)

    assert _read_state(world.db)[old.id] is None


def test_inactive_viewer_cannot_mark(world: SimpleNamespace) -> None:
    [notification] = _notify(world, world.customer, world.mine, [world.agent])
    world.agent.is_active = False
    world.db.commit()

    with pytest.raises(NotificationNotFoundError):
        mark_notification_read(world.db, world.agent, notification.id)


@pytest.mark.parametrize(
    ("bad", "error"),
    [(0, ValueError), (-1, ValueError), ("1", TypeError), (True, TypeError), (None, TypeError)],
)
def test_invalid_notification_id(world: SimpleNamespace, bad, error) -> None:
    with pytest.raises(error):
        mark_notification_read(world.db, world.agent, bad)


# ===========================================================================
# Marking several notifications read
# ===========================================================================


@pytest.fixture
def inbox(world: SimpleNamespace) -> SimpleNamespace:
    """Agent: three on ``mine`` then two on ``theirs``; the admin has one."""

    mine = [
        _notify(world, world.customer, world.mine, [world.agent])[0].id
        for _ in range(3)
    ]
    theirs = [
        _notify(world, world.customer_2, world.theirs, [world.agent])[0].id
        for _ in range(2)
    ]
    [admins] = _notify(world, world.customer, world.free, [world.admin])

    return SimpleNamespace(mine=mine, theirs=theirs, admins=admins.id)


def _unread_ids(w: SimpleNamespace) -> set[int]:
    return {
        notification_id
        for notification_id, read_at in _read_state(w.db).items()
        if read_at is None
    }


def test_mark_all(world: SimpleNamespace, inbox: SimpleNamespace) -> None:
    result = mark_notifications_read(world.db, world.agent)

    assert (result.marked_read, result.unread_count) == (5, 0)
    # The administrator's notification is untouched.
    assert _unread_ids(world) == {inbox.admins}


def test_mark_up_to_id_leaves_newer_ones_unread(
    world: SimpleNamespace,
    inbox: SimpleNamespace,
) -> None:
    result = mark_notifications_read(world.db, world.agent, up_to_id=inbox.mine[1])

    assert (result.marked_read, result.unread_count) == (2, 3)
    assert _unread_ids(world) == {inbox.mine[2], *inbox.theirs, inbox.admins}

    # A notification arriving later stays unread too.
    [newer] = _notify(world, world.customer, world.mine, [world.agent])
    again = mark_notifications_read(world.db, world.agent, up_to_id=inbox.theirs[-1])

    assert (again.marked_read, again.unread_count) == (3, 1)
    assert newer.id in _unread_ids(world)


def test_mark_one_ticket(world: SimpleNamespace, inbox: SimpleNamespace) -> None:
    result = mark_notifications_read(world.db, world.agent, ticket_id=world.theirs.id)

    assert (result.marked_read, result.unread_count) == (2, 3)
    assert _unread_ids(world) == {*inbox.mine, inbox.admins}


def test_both_filters_together(world: SimpleNamespace, inbox: SimpleNamespace) -> None:
    result = mark_notifications_read(
        world.db, world.agent, up_to_id=inbox.mine[0], ticket_id=world.mine.id
    )

    assert (result.marked_read, result.unread_count) == (1, 4)
    assert _unread_ids(world) == {*inbox.mine[1:], *inbox.theirs, inbox.admins}


def test_already_read_rows_keep_their_read_at(
    world: SimpleNamespace,
    inbox: SimpleNamespace,
) -> None:
    _set_read(world, inbox.mine[0], T0)

    result = mark_notifications_read(world.db, world.agent)

    assert result.marked_read == 4
    assert _read_state(world.db)[inbox.mine[0]].replace(tzinfo=timezone.utc) == T0

    # Repeating changes nothing.
    assert mark_notifications_read(world.db, world.agent).marked_read == 0


def test_other_users_rows_are_never_marked(
    world: SimpleNamespace,
    inbox: SimpleNamespace,
) -> None:
    result = mark_notifications_read(world.db, world.agent_2)

    assert (result.marked_read, result.unread_count) == (0, 0)
    assert len(_unread_ids(world)) == 6

    # Even naming another user's ticket or a later ID marks nothing.
    result = mark_notifications_read(
        world.db, world.customer, ticket_id=world.free.id, up_to_id=inbox.admins
    )

    assert result.marked_read == 0
    assert len(_unread_ids(world)) == 6


def test_demoted_agent_marks_only_visible_rows(
    world: SimpleNamespace,
    inbox: SimpleNamespace,
) -> None:
    world.agent.role = "customer"
    world.db.commit()

    # None of the agent's notifications are on tickets they created.
    result = mark_notifications_read(world.db, world.agent)

    assert (result.marked_read, result.unread_count) == (0, 0)
    assert len(_unread_ids(world)) == 6


def test_customer_marks_only_their_own_tickets(world: SimpleNamespace) -> None:
    [own] = _notify(world, world.agent, world.mine, [world.customer])
    [hidden] = _notify(world, world.agent, world.theirs, [world.customer])

    result = mark_notifications_read(world.db, world.customer)

    assert (result.marked_read, result.unread_count) == (1, 0)
    assert _unread_ids(world) == {hidden.id}
    assert own.id not in _unread_ids(world)


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({"up_to_id": 0}, ValueError),
        ({"up_to_id": "5"}, TypeError),
        ({"ticket_id": -1}, ValueError),
        ({"ticket_id": True}, TypeError),
    ],
)
def test_invalid_filters(world: SimpleNamespace, arguments: dict, error) -> None:
    with pytest.raises(error):
        mark_notifications_read(world.db, world.agent, **arguments)


# ===========================================================================
# Transactions
# ===========================================================================


def _operational_error() -> OperationalError:
    return OperationalError("UPDATE notifications ...", {}, Exception("disk I/O error"))


@pytest.mark.parametrize("operation", ["one", "many"])
def test_commit_failure_rolls_back_and_changes_nothing(
    world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    [notification] = _notify(world, world.customer, world.mine, [world.agent])
    original_commit = world.db.commit

    def failing_commit():
        raise _operational_error()

    def run():
        if operation == "one":
            return mark_notification_read(world.db, world.agent, notification.id)

        return mark_notifications_read(world.db, world.agent)

    monkeypatch.setattr(world.db, "commit", failing_commit)

    with pytest.raises(OperationalError):
        run()

    monkeypatch.setattr(world.db, "commit", original_commit)

    assert _read_state(world.db)[notification.id] is None

    # The lock was released: the next attempt succeeds.
    run()

    assert _read_state(world.db)[notification.id] is not None


def test_rollback_failure_is_recorded_without_replacing_the_error(
    world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    [notification] = _notify(world, world.customer, world.mine, [world.agent])
    rollback_error = _operational_error()
    original_rollback = world.db.rollback

    def failing_view(*_args, **_kwargs):
        raise RuntimeError("primary failure")

    def failing_rollback():
        original_rollback()
        raise rollback_error

    monkeypatch.setattr(notification_service, "_view", failing_view)
    monkeypatch.setattr(world.db, "rollback", failing_rollback)

    with pytest.raises(RuntimeError, match="primary failure") as error:
        mark_notification_read(world.db, world.agent, notification.id)

    assert rollback_failure_of(error.value) is rollback_error

    monkeypatch.setattr(world.db, "rollback", original_rollback)

    assert _read_state(world.db)[notification.id] is None


@pytest.mark.parametrize("operation", ["one", "many"])
def test_refuses_to_run_inside_an_open_write_transaction(
    world: SimpleNamespace,
    operation: str,
) -> None:
    [notification] = _notify(world, world.customer, world.mine, [world.agent])
    world.free.title = "Pending change"
    world.db.flush()

    with pytest.raises(TransactionAlreadyStartedError):
        if operation == "one":
            mark_notification_read(world.db, world.agent, notification.id)
        else:
            mark_notifications_read(world.db, world.agent)

    # Rolled back: neither the read mark nor the pending change was kept.
    assert _read_state(world.db)[notification.id] is None
    assert world.db.get(Ticket, world.free.id).title == "Login issue"


def test_mark_operations_take_the_serialised_lock(
    world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    [notification] = _notify(world, world.customer, world.mine, [world.agent])
    calls: list[str] = []
    original = notification_service._begin_change

    def recording_begin(db):
        calls.append("begin")
        original(db)

    monkeypatch.setattr(notification_service, "_begin_change", recording_begin)

    mark_notification_read(world.db, world.agent, notification.id)
    mark_notifications_read(world.db, world.agent)
    list_notifications(world.db, viewer=world.agent)
    get_unread_notification_summary(world.db, world.agent)

    # Only the two writes lock; reads never do.
    assert calls == ["begin", "begin"]
