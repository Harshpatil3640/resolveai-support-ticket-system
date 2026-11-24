"""Concurrent user administration and ticket workflow changes.

Every test uses a temporary SQLite FILE with one connection per thread,
following the Phase 6 admin concurrency tests. The first operation is held
right after its locked reads (inside its BEGIN IMMEDIATE transaction);
the second must wait for it. Whatever the order, the final state never
leaves an Open or In Progress ticket with an inactive user or a user who
is no longer an agent. resolveai.db is never used.
"""

import threading
from collections.abc import Callable
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import Ticket, User
from app.services import ticket_workflow, user_service
from app.services.ticket_workflow import (
    AssigneeNotAssignableError,
    TicketPermissionError,
    assign_ticket,
    change_ticket_status,
    claim_ticket,
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
        f"sqlite:///{tmp_path / 'user-ticket.db'}",
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
    """Two administrators, an agent, a customer and two tickets.

    ``free`` is unassigned and Open; ``held`` is assigned to the agent and
    In Progress. Both are at version 1.
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
        free = Ticket(
            title="Free ticket",
            description="Unassigned.",
            created_by_user_id=users["customer"].id,
        )
        held = Ticket(
            title="Held ticket",
            description="Assigned to the agent.",
            created_by_user_id=users["customer"].id,
            status="In Progress",
            assigned_to_user_id=users["agent"].id,
        )
        db.add_all([free, held])
        db.commit()

        return SimpleNamespace(
            engine=file_engine,
            **{label: user.id for label, user in users.items()},
            free=free.id,
            held=held.id,
        )


def _row(engine, ticket_id: int) -> tuple:
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


def _events(engine, ticket_id: int) -> list[tuple]:
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT event_type, reason FROM ticket_events "
                    "WHERE ticket_id = :id ORDER BY id"
                ),
                {"id": ticket_id},
            )
        ]


def _violations(engine) -> list[tuple]:
    """Unresolved tickets held by an inactive user or a non-agent."""

    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT t.id, u.role, u.is_active FROM tickets t "
                    "JOIN users u ON u.id = t.assigned_to_user_id "
                    "WHERE t.status IN ('Open', 'In Progress') "
                    "AND (u.is_active = 0 OR u.role <> 'agent')"
                )
            )
        ]


# --- Operations, each in its own session/connection ------------------------


def _assign(w: SimpleNamespace) -> Callable[[], object]:
    def run():
        with Session(w.engine) as db:
            return assign_ticket(
                db,
                actor_id=w.admin_a,
                ticket_id=w.free,
                assignee_id=w.agent,
                expected_version=1,
            ).assigned_to_user_id

    return run


def _claim(w: SimpleNamespace) -> Callable[[], object]:
    def run():
        with Session(w.engine) as db:
            return claim_ticket(
                db, actor_id=w.agent, ticket_id=w.free, expected_version=1
            ).assigned_to_user_id

    return run


def _resolve_held(w: SimpleNamespace) -> Callable[[], object]:
    """The agent works on their own ticket: In Progress -> Resolved."""

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


def _deactivate(w: SimpleNamespace, actor: str = "admin_b") -> Callable[[], object]:
    def run():
        with Session(w.engine) as db:
            return user_service.set_user_active_status(
                db, actor_id=getattr(w, actor), user_id=w.agent, is_active=False
            ).unassigned_ticket_ids

    return run


def _change_role(w: SimpleNamespace, role: str, actor: str = "admin_b") -> Callable[[], object]:
    def run():
        with Session(w.engine) as db:
            return user_service.change_user_role(
                db, actor_id=getattr(w, actor), user_id=w.agent, role=role
            ).unassigned_ticket_ids

    return run


USER_CHANGES = {
    "deactivate": (_deactivate, "assignee_deactivated"),
    "to-customer": (lambda w: _change_role(w, "customer"), "assignee_role_changed"),
    "to-admin": (lambda w: _change_role(w, "admin"), "assignee_role_changed"),
}


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


TICKET_LOCK = (ticket_workflow, "_lock_for_ticket_change")
USER_LOCK = (user_service, "_lock_for_change")


# ---------------------------------------------------------------------------
# 1 and 3: assignment versus deactivation / role change, both orders
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("user_change", list(USER_CHANGES))
def test_assignment_first_then_user_change_unassigns_it(
    world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    user_change: str,
) -> None:
    make_change, reason = USER_CHANGES[user_change]

    first, second, second_was_waiting = _race(
        monkeypatch, TICKET_LOCK, _assign(world), make_change(world)
    )

    assert second_was_waiting, "the user change did not wait for the assignment"
    assert first == {"result": world.agent}
    # The user change ran after the assignment committed and undid it,
    # together with the agent's other unresolved ticket.
    assert second == {"result": tuple(sorted([world.free, world.held]))}
    assert _row(world.engine, world.free) == (None, "Open", 3)
    assert _row(world.engine, world.held) == (None, "Open", 2)
    assert _events(world.engine, world.free) == [
        ("assigned", None),
        ("unassigned", reason),
    ]
    assert _violations(world.engine) == []


@pytest.mark.parametrize("user_change", list(USER_CHANGES))
def test_user_change_first_then_assignment_is_rejected(
    world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    user_change: str,
) -> None:
    make_change, _reason = USER_CHANGES[user_change]

    first, second, second_was_waiting = _race(
        monkeypatch, USER_LOCK, make_change(world), _assign(world)
    )

    assert second_was_waiting, "the assignment did not wait for the user change"
    assert first == {"result": (world.held,)}
    # The assignment read the user only after the change committed.
    assert isinstance(second.get("error"), AssigneeNotAssignableError)
    assert _row(world.engine, world.free) == (None, "Open", 1)
    assert _events(world.engine, world.free) == []
    assert _violations(world.engine) == []


# ---------------------------------------------------------------------------
# 2: the agent's own ticket actions versus deactivation, both orders
# ---------------------------------------------------------------------------


def test_claim_first_then_deactivation_unassigns_the_claim(
    world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second, second_was_waiting = _race(
        monkeypatch, TICKET_LOCK, _claim(world), _deactivate(world)
    )

    assert second_was_waiting
    assert first == {"result": world.agent}
    assert second == {"result": tuple(sorted([world.free, world.held]))}
    assert _row(world.engine, world.free) == (None, "Open", 3)
    assert _violations(world.engine) == []


def test_deactivation_first_then_claim_is_refused(
    world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second, second_was_waiting = _race(
        monkeypatch, USER_LOCK, _deactivate(world), _claim(world)
    )

    assert second_was_waiting
    assert first == {"result": (world.held,)}
    assert isinstance(second.get("error"), TicketPermissionError)
    assert _row(world.engine, world.free) == (None, "Open", 1)
    assert _violations(world.engine) == []


def test_status_change_first_then_deactivation_keeps_the_resolved_ticket(
    world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second, second_was_waiting = _race(
        monkeypatch, TICKET_LOCK, _resolve_held(world), _deactivate(world)
    )

    assert second_was_waiting
    assert first == {"result": "Resolved"}
    # Resolved before the deactivation read it: it keeps its assignee.
    assert second == {"result": ()}
    assert _row(world.engine, world.held) == (world.agent, "Resolved", 2)
    assert _violations(world.engine) == []


def test_deactivation_first_then_status_change_is_refused(
    world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second, second_was_waiting = _race(
        monkeypatch, USER_LOCK, _deactivate(world), _resolve_held(world)
    )

    assert second_was_waiting
    assert first == {"result": (world.held,)}
    assert isinstance(second.get("error"), TicketPermissionError)
    assert _row(world.engine, world.held) == (None, "Open", 2)
    assert _events(world.engine, world.held) == [("unassigned", "assignee_deactivated")]
    assert _violations(world.engine) == []


# ---------------------------------------------------------------------------
# 4: two user changes affecting the same agent's tickets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first_change", "second_change", "first_reason"),
    [
        ("deactivate", "to-customer", "assignee_deactivated"),
        ("to-customer", "deactivate", "assignee_role_changed"),
    ],
)
def test_two_user_changes_unassign_each_ticket_once(
    world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    first_change: str,
    second_change: str,
    first_reason: str,
) -> None:
    def build(name: str, actor: str):
        if name == "deactivate":
            return _deactivate(world, actor)

        return _change_role(world, "customer", actor)

    first, second, second_was_waiting = _race(
        monkeypatch,
        USER_LOCK,
        build(first_change, "admin_a"),
        build(second_change, "admin_b"),
    )

    assert second_was_waiting
    assert first == {"result": (world.held,)}
    # Nothing was left for the second change to unassign.
    assert second == {"result": ()}
    assert _row(world.engine, world.held) == (None, "Open", 2)
    assert _events(world.engine, world.held) == [("unassigned", first_reason)]
    assert _violations(world.engine) == []


def test_many_concurrent_assignments_and_a_deactivation_leave_no_violation(
    world: SimpleNamespace,
) -> None:
    """Unheld free-for-all: whatever order SQLite serialises them in."""

    with Session(world.engine) as db:
        extra = [
            Ticket(
                title=f"Extra {index}",
                description="Concurrency test.",
                created_by_user_id=world.customer,
            )
            for index in range(5)
        ]
        db.add_all(extra)
        db.commit()
        extra_ids = [ticket.id for ticket in extra]

    barrier = threading.Barrier(len(extra_ids) + 1)

    def assign(ticket_id: int):
        barrier.wait()

        with Session(world.engine) as db:
            return assign_ticket(
                db,
                actor_id=world.admin_a,
                ticket_id=ticket_id,
                assignee_id=world.agent,
                expected_version=1,
            ).assigned_to_user_id

    def deactivate():
        barrier.wait()
        return _deactivate(world)()

    started = [_start(lambda t=t: assign(t)) for t in extra_ids] + [_start(deactivate)]

    for thread, _ in started:
        thread.join(WAIT_SECONDS * 2)

    assert not any(thread.is_alive() for thread, _ in started)

    for _, outcome in started[:-1]:
        # Each assignment either committed before the deactivation (and
        # was undone by it) or was refused after it.
        assert "result" in outcome or isinstance(
            outcome["error"], AssigneeNotAssignableError
        )

    assert "result" in started[-1][1]
    assert _violations(world.engine) == []
