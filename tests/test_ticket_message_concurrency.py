"""Concurrent message sending and ticket workflow / user changes.

Every test uses a temporary SQLite FILE with one connection per thread,
following test_user_ticket_concurrency.py. The first operation is held
right after its locked reads (inside its BEGIN IMMEDIATE transaction);
the second must wait for it. Whatever the order, a message is stored only
if its sender was allowed to write when it was committed: an agent never
writes on a ticket they no longer hold, nobody writes on a resolved
ticket, and a refused message leaves nothing behind. A stored message's
notifications go to whoever was assigned and active at its serialised
point, and roll back with it. resolveai.db is never used.
"""

import threading
from collections.abc import Callable
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import Ticket, User
from app.services import ticket_message_service, ticket_workflow, user_service
from app.services.ticket_message_service import (
    TicketResolvedError,
    create_ticket_message,
)
from app.services.ticket_workflow import (
    TicketPermissionError,
    assign_ticket,
    change_ticket_status,
)


LOCK_TIMEOUT_SECONDS = 30
WAIT_SECONDS = 15
BLOCKED_CHECK_SECONDS = 0.5


# ---------------------------------------------------------------------------
# Database and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def file_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'messages.db'}",
        connect_args={"check_same_thread": False, "timeout": LOCK_TIMEOUT_SECONDS},
    )

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)

    yield engine

    engine.dispose()


@pytest.fixture
def world(file_engine) -> SimpleNamespace:
    """Two administrators, an agent, a customer and a ticket.

    ``held`` is In Progress, assigned to the agent, at version 1.
    """

    with Session(file_engine) as db:
        users = {
            label: User(
                name=f"{label} name",
                email=f"{label}@example.test",
                password_hash="unused",
                role=role,
            )
            for label, role in [
                ("admin_a", "admin"),
                ("admin_b", "admin"),
                ("agent", "agent"),
                ("customer", "customer"),
            ]
        }
        db.add_all(users.values())
        db.flush()
        held = Ticket(
            title="Held ticket",
            description="Assigned to the agent.",
            created_by_user_id=users["customer"].id,
            status="In Progress",
            assigned_to_user_id=users["agent"].id,
        )
        db.add(held)
        db.commit()

        return SimpleNamespace(
            engine=file_engine,
            **{label: user.id for label, user in users.items()},
            held=held.id,
        )


def _messages(engine, ticket_id: int) -> list[tuple]:
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT sender_user_id, sender_role, content FROM ticket_messages "
                    "WHERE ticket_id = :id ORDER BY id"
                ),
                {"id": ticket_id},
            )
        ]


def _ticket(engine, ticket_id: int) -> tuple:
    with engine.connect() as connection:
        return tuple(
            connection.execute(
                text(
                    "SELECT assigned_to_user_id, status, version FROM tickets "
                    "WHERE id = :id"
                ),
                {"id": ticket_id},
            ).one()
        )


def _event_types(engine, ticket_id: int) -> list[str]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "SELECT event_type FROM ticket_events WHERE ticket_id = :id "
                    "ORDER BY id"
                ),
                {"id": ticket_id},
            ).scalars()
        )


def _assert_no_write_lock_left(engine) -> None:
    """Another connection can take the write lock immediately."""

    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        connection.exec_driver_sql("ROLLBACK")


# --- Operations, each in its own session/connection ------------------------


def _send(w: SimpleNamespace, sender: str, content: str) -> Callable[[], object]:
    def run():
        with Session(w.engine) as db:
            return create_ticket_message(
                db,
                actor_id=getattr(w, sender),
                ticket_id=w.held,
                content=content,
            ).id

    return run


def _unassign(w: SimpleNamespace) -> Callable[[], object]:
    def run():
        with Session(w.engine) as db:
            return assign_ticket(
                db,
                actor_id=w.admin_a,
                ticket_id=w.held,
                assignee_id=None,
                expected_version=1,
            ).assigned_to_user_id

    return run


def _resolve(w: SimpleNamespace) -> Callable[[], object]:
    def run():
        with Session(w.engine) as db:
            return change_ticket_status(
                db,
                actor_id=w.agent,
                ticket_id=w.held,
                new_status="Resolved",
                expected_version=1,
            ).status

    return run


def _deactivate_agent(w: SimpleNamespace) -> Callable[[], object]:
    def run():
        with Session(w.engine) as db:
            return user_service.set_user_active_status(
                db, actor_id=w.admin_b, user_id=w.agent, is_active=False
            ).unassigned_ticket_ids

    return run


# --- Holding the first operation inside its lock ---------------------------


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


def _race(
    monkeypatch: pytest.MonkeyPatch,
    hold_in: tuple[ModuleType, str],
    first: Callable[[], object],
    second: Callable[[], object],
) -> tuple[dict, dict, bool]:
    """Run ``first`` held after its locked reads, then ``second``.

    ``hold_in`` names the locked-read function of the first operation's
    service (only its first call is held). Returns both outcomes and
    whether the second operation was still waiting while the first was
    held. Both threads are always released and joined.
    """

    module, name = hold_in
    holding, release = threading.Event(), threading.Event()
    state = {"first": True}
    guard = threading.Lock()
    original = getattr(module, name)

    def lock_then_hold(*args, **kwargs):
        result = original(*args, **kwargs)

        with guard:
            hold, state["first"] = state["first"], False

        if hold:
            holding.set()
            release.wait(WAIT_SECONDS)

        return result

    monkeypatch.setattr(module, name, lock_then_hold)
    threads: list[threading.Thread] = []
    second_was_waiting = False

    try:
        first_thread, first_outcome = _start(first)
        threads.append(first_thread)

        if not holding.wait(WAIT_SECONDS):
            raise AssertionError("the first operation never reached its lock")

        second_thread, second_outcome = _start(second)
        threads.append(second_thread)
        second_thread.join(BLOCKED_CHECK_SECONDS)
        second_was_waiting = second_thread.is_alive()
    finally:
        release.set()

        for thread in threads:
            thread.join(WAIT_SECONDS)

    assert not any(thread.is_alive() for thread in threads)

    return first_outcome, second_outcome, second_was_waiting


MESSAGE_LOCK = (ticket_message_service, "_lock_for_message")
TICKET_LOCK = (ticket_workflow, "_lock_for_ticket_change")
USER_LOCK = (user_service, "_lock_for_change")


# ---------------------------------------------------------------------------
# The agent loses the assignment while sending
# ---------------------------------------------------------------------------


def test_unassignment_first_refuses_the_agents_message(
    monkeypatch: pytest.MonkeyPatch,
    world: SimpleNamespace,
) -> None:
    first, second, waited = _race(
        monkeypatch, TICKET_LOCK, _unassign(world), _send(world, "agent", "late reply")
    )

    assert waited
    assert first == {"result": None}
    assert isinstance(second["error"], TicketPermissionError)
    assert _messages(world.engine, world.held) == []
    # Unassigning an In Progress ticket returns it to Open (one event).
    assert _ticket(world.engine, world.held) == (None, "Open", 2)
    _assert_no_write_lock_left(world.engine)


def test_message_first_commits_before_the_unassignment(
    monkeypatch: pytest.MonkeyPatch,
    world: SimpleNamespace,
) -> None:
    first, second, waited = _race(
        monkeypatch, MESSAGE_LOCK, _send(world, "agent", "in time"), _unassign(world)
    )

    assert waited
    assert isinstance(first["result"], int)
    # The message did not change the ticket, so the unassignment's
    # expected_version (1) still matches.
    assert second == {"result": None}
    assert _messages(world.engine, world.held) == [(world.agent, "agent", "in time")]
    assert _ticket(world.engine, world.held) == (None, "Open", 2)
    assert _event_types(world.engine, world.held) == ["unassigned"]


def test_deactivation_first_refuses_the_agents_message(
    monkeypatch: pytest.MonkeyPatch,
    world: SimpleNamespace,
) -> None:
    first, second, waited = _race(
        monkeypatch,
        USER_LOCK,
        _deactivate_agent(world),
        _send(world, "agent", "after deactivation"),
    )

    assert waited
    assert first == {"result": (world.held,)}
    assert isinstance(second["error"], TicketPermissionError)
    assert _messages(world.engine, world.held) == []


# ---------------------------------------------------------------------------
# The ticket is resolved while a message is being sent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sender", ["customer", "agent", "admin_a"])
def test_resolution_first_refuses_the_message(
    monkeypatch: pytest.MonkeyPatch,
    world: SimpleNamespace,
    sender: str,
) -> None:
    first, second, waited = _race(
        monkeypatch, TICKET_LOCK, _resolve(world), _send(world, sender, "too late")
    )

    assert waited
    assert first == {"result": "Resolved"}
    assert isinstance(second["error"], TicketResolvedError)
    assert _messages(world.engine, world.held) == []
    assert _ticket(world.engine, world.held) == (world.agent, "Resolved", 2)


def test_message_first_commits_before_the_resolution(
    monkeypatch: pytest.MonkeyPatch,
    world: SimpleNamespace,
) -> None:
    first, second, waited = _race(
        monkeypatch, MESSAGE_LOCK, _send(world, "customer", "just in time"), _resolve(world)
    )

    assert waited
    assert isinstance(first["result"], int)
    assert second == {"result": "Resolved"}
    assert _messages(world.engine, world.held) == [
        (world.customer, "customer", "just in time")
    ]
    assert _ticket(world.engine, world.held) == (world.agent, "Resolved", 2)


# ---------------------------------------------------------------------------
# Two messages at once
# ---------------------------------------------------------------------------


def test_two_simultaneous_messages_are_serialised_and_both_kept(
    monkeypatch: pytest.MonkeyPatch,
    world: SimpleNamespace,
) -> None:
    first, second, waited = _race(
        monkeypatch,
        MESSAGE_LOCK,
        _send(world, "customer", "from the customer"),
        _send(world, "agent", "from the agent"),
    )

    assert waited
    assert first["result"] < second["result"]
    assert _messages(world.engine, world.held) == [
        (world.customer, "customer", "from the customer"),
        (world.agent, "agent", "from the agent"),
    ]
    # Neither message touched the ticket.
    assert _ticket(world.engine, world.held) == (world.agent, "In Progress", 1)
    assert _event_types(world.engine, world.held) == []


def test_many_concurrent_messages_without_holding(world: SimpleNamespace) -> None:
    senders = ["customer", "agent", "admin_a", "admin_b"] * 3
    outcomes = []
    threads = []

    for number, sender in enumerate(senders):
        thread, outcome = _start(_send(world, sender, f"message {number}"))
        threads.append(thread)
        outcomes.append(outcome)

    for thread in threads:
        thread.join(WAIT_SECONDS)

    assert all("result" in outcome for outcome in outcomes), outcomes
    assert len({outcome["result"] for outcome in outcomes}) == len(senders)
    assert len(_messages(world.engine, world.held)) == len(senders)
    assert _ticket(world.engine, world.held) == (world.agent, "In Progress", 1)


# ---------------------------------------------------------------------------
# A failed transaction leaves nothing behind
# ---------------------------------------------------------------------------


def test_failure_inside_the_lock_rolls_back_and_releases_it(
    monkeypatch: pytest.MonkeyPatch,
    world: SimpleNamespace,
) -> None:
    original_view = ticket_message_service._view
    calls = {"count": 0}

    def fail_first_time(*args, **kwargs):
        calls["count"] += 1

        if calls["count"] == 1:
            raise RuntimeError("failure after the INSERT was flushed")

        return original_view(*args, **kwargs)

    monkeypatch.setattr(ticket_message_service, "_view", fail_first_time)

    with pytest.raises(RuntimeError):
        _send(world, "customer", "never stored")()

    assert _messages(world.engine, world.held) == []
    _assert_no_write_lock_left(world.engine)

    # The next message and workflow change succeed normally.
    _send(world, "customer", "stored")()

    assert _resolve(world)() == "Resolved"
    assert _messages(world.engine, world.held) == [(world.customer, "customer", "stored")]


def test_lock_time_out_writes_nothing(world: SimpleNamespace) -> None:
    from sqlalchemy.exc import OperationalError

    short_engine = create_engine(
        world.engine.url,
        connect_args={"check_same_thread": False, "timeout": 0.2},
    )

    try:
        with world.engine.connect() as blocker:
            blocker.exec_driver_sql("BEGIN IMMEDIATE")

            with Session(short_engine) as db:
                with pytest.raises(OperationalError):
                    create_ticket_message(
                        db, actor_id=world.customer, ticket_id=world.held, content="blocked"
                    )

            blocker.exec_driver_sql("ROLLBACK")
    finally:
        short_engine.dispose()

    assert _messages(world.engine, world.held) == []


# ---------------------------------------------------------------------------
# Notifications (Phase 10): recipients follow the serialised order
# ---------------------------------------------------------------------------


def _notified(engine) -> dict[int, list[int]]:
    """Recipient IDs per message ID, as committed."""

    recipients: dict[int, list[int]] = {}

    with engine.connect() as connection:
        for message_id, recipient_id in connection.execute(
            text(
                "SELECT message_id, recipient_user_id FROM notifications "
                "ORDER BY message_id, recipient_user_id"
            )
        ):
            recipients.setdefault(message_id, []).append(recipient_id)

    return recipients


def _notification_rows(engine) -> list[tuple]:
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT n.message_id, n.recipient_user_id, n.actor_user_id, "
                    "n.ticket_id, m.ticket_id, m.sender_user_id "
                    "FROM notifications n JOIN ticket_messages m "
                    "ON m.id = n.message_id ORDER BY n.id"
                )
            )
        ]


def test_unassignment_first_sends_the_customers_message_to_admins(
    monkeypatch: pytest.MonkeyPatch,
    world: SimpleNamespace,
) -> None:
    first, second, waited = _race(
        monkeypatch, TICKET_LOCK, _unassign(world), _send(world, "customer", "help?")
    )

    assert waited
    assert first == {"result": None}
    message_id = second["result"]

    # The ticket was unassigned before the message: the former agent is
    # not notified; every active administrator is.
    assert _notified(world.engine) == {message_id: [world.admin_a, world.admin_b]}
    _assert_no_write_lock_left(world.engine)


def test_customer_message_first_notifies_the_agent_assigned_then(
    monkeypatch: pytest.MonkeyPatch,
    world: SimpleNamespace,
) -> None:
    first, second, waited = _race(
        monkeypatch, MESSAGE_LOCK, _send(world, "customer", "help?"), _unassign(world)
    )

    assert waited
    assert second == {"result": None}
    # The agent held the ticket when the message committed.
    assert _notified(world.engine) == {first["result"]: [world.agent]}
    assert _ticket(world.engine, world.held) == (None, "Open", 2)


def test_deactivation_first_sends_the_customers_message_to_admins(
    monkeypatch: pytest.MonkeyPatch,
    world: SimpleNamespace,
) -> None:
    first, second, waited = _race(
        monkeypatch,
        USER_LOCK,
        _deactivate_agent(world),
        _send(world, "customer", "help?"),
    )

    assert waited
    assert first == {"result": (world.held,)}
    assert _notified(world.engine) == {
        second["result"]: [world.admin_a, world.admin_b]
    }


def test_customer_message_first_notifies_the_agent_before_deactivation(
    monkeypatch: pytest.MonkeyPatch,
    world: SimpleNamespace,
) -> None:
    first, second, waited = _race(
        monkeypatch,
        MESSAGE_LOCK,
        _send(world, "customer", "help?"),
        _deactivate_agent(world),
    )

    assert waited
    assert second == {"result": (world.held,)}
    # The agent was still the active assignee when the message committed.
    assert _notified(world.engine) == {first["result"]: [world.agent]}


def test_simultaneous_messages_each_notify_their_recipients_once(
    monkeypatch: pytest.MonkeyPatch,
    world: SimpleNamespace,
) -> None:
    first, second, waited = _race(
        monkeypatch,
        MESSAGE_LOCK,
        _send(world, "customer", "from the customer"),
        _send(world, "agent", "from the agent"),
    )

    assert waited
    assert _notified(world.engine) == {
        first["result"]: [world.agent],
        second["result"]: [world.customer],
    }


def test_many_concurrent_messages_notify_exactly_the_intended_recipients(
    world: SimpleNamespace,
) -> None:
    expected_by_sender = {
        "customer": [world.agent],
        "agent": [world.customer],
        # Rule 7b: an administrator notifies the customer and the agent.
        "admin_a": sorted([world.customer, world.agent]),
        "admin_b": sorted([world.customer, world.agent]),
    }
    senders = list(expected_by_sender) * 3
    outcomes = []
    threads = []

    for number, sender in enumerate(senders):
        thread, outcome = _start(_send(world, sender, f"message {number}"))
        threads.append(thread)
        outcomes.append((sender, outcome))

    for thread in threads:
        thread.join(WAIT_SECONDS)

    assert all("result" in outcome for _sender, outcome in outcomes), outcomes

    notified = _notified(world.engine)

    assert notified == {
        outcome["result"]: expected_by_sender[sender] for sender, outcome in outcomes
    }

    rows = _notification_rows(world.engine)
    pairs = [(message_id, recipient) for message_id, recipient, *_ in rows]

    assert len(pairs) == len(set(pairs)) == 3 * (1 + 1 + 2 + 2)

    for _message, recipient, actor, ticket_id, message_ticket, sender in rows:
        assert actor == sender != recipient
        assert ticket_id == message_ticket == world.held

    _assert_no_write_lock_left(world.engine)


def test_notification_failure_inside_the_lock_rolls_back_and_releases_it(
    monkeypatch: pytest.MonkeyPatch,
    world: SimpleNamespace,
) -> None:
    real_helper = ticket_message_service.add_message_notifications
    calls = {"count": 0}

    def fail_first_time(db, message, recipient_ids):
        added = real_helper(db, message, recipient_ids)
        calls["count"] += 1

        if calls["count"] == 1:
            assert added  # the rows were flushed inside the transaction
            raise RuntimeError("notification insert failed")

        return added

    monkeypatch.setattr(
        ticket_message_service, "add_message_notifications", fail_first_time
    )

    with pytest.raises(RuntimeError, match="notification insert failed"):
        _send(world, "admin_a", "never stored")()

    assert _messages(world.engine, world.held) == []
    assert _notified(world.engine) == {}
    _assert_no_write_lock_left(world.engine)

    # The next message, its notifications and a workflow change succeed.
    message_id = _send(world, "admin_a", "stored")()

    assert _notified(world.engine) == {
        message_id: sorted([world.customer, world.agent])
    }
    assert _resolve(world)() == "Resolved"
    assert _ticket(world.engine, world.held) == (world.agent, "Resolved", 2)
