"""Ticket assignment, claim and status APIs; filters, privacy, stale writes.

Uses conftest's in-memory database and role fixtures. The HTTP claim race
uses a temporary SQLite FILE with a Session per request. resolveai.db is
never used.
"""

import logging
import threading
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.audit_logging import AUDIT_LOGGER_NAME
from app.core.security import create_access_token
from app.db.base import Base
from app.db.database import get_db
from app.main import app
from app.models import Ticket, TicketEvent, User
from app.services import ticket_workflow
from app.services.ticket_service import create_ticket


# The password conftest.py gives its fixture users.
TEST_PASSWORD = "StrongPassword123!"

FORBIDDEN = {"detail": "You do not have permission to perform this action."}
NOT_FOUND = {"detail": "Ticket not found."}
CONFLICT = {"detail": "The ticket was changed by someone else. Reload and try again."}
NOT_ASSIGNABLE = {"detail": "Assignee must be an active agent."}

STAFF_ONLY_FIELDS = {"assigned_to_user_id", "assignee", "assigned_at", "version"}


def _assignment_url(ticket_id: int) -> str:
    return f"/api/tickets/{ticket_id}/assignment"


def _claim_url(ticket_id: int) -> str:
    return f"/api/tickets/{ticket_id}/claim"


def _status_url(ticket_id: int) -> str:
    return f"/api/tickets/{ticket_id}/status"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def other_agent(create_user: Callable[..., User]) -> User:
    return create_user(email="other.agent@example.test", role="agent", name="Other Agent")


@pytest.fixture
def other_agent_headers(
    other_agent: User,
    login_headers: Callable[..., dict[str, str]],
) -> dict[str, str]:
    return login_headers(other_agent.email)


@pytest.fixture
def make_ticket(
    db_session: Session,
    customer_user: User,
) -> Callable[..., Ticket]:
    """A customer's ticket, optionally assigned (directly, as test setup)."""

    def _make(*, status: str = "Open", assignee: User | None = None, **fields) -> Ticket:
        ticket = create_ticket(
            db_session,
            creator=customer_user,
            title=fields.pop("title", "Card payment declined"),
            description=fields.pop("description", "My card was declined twice."),
            status=status,
            **fields,
        )

        if assignee is not None:
            ticket.assigned_to_user_id = assignee.id
            db_session.commit()

        return ticket

    return _make


def _state(db: Session, ticket_id: int) -> tuple:
    """(assigned_to_user_id, status, version) straight from SQL."""

    return tuple(
        db.execute(
            text(
                "SELECT assigned_to_user_id, status, version FROM tickets "
                "WHERE id = :id"
            ),
            {"id": ticket_id},
        ).one()
    )


def _events(db: Session, ticket_id: int) -> list[tuple]:
    """Workflow events (the ``created`` event is left out)."""

    return [
        tuple(row)
        for row in db.execute(
            text(
                "SELECT event_type, actor_user_id, from_assignee_id, "
                "to_assignee_id, from_status, to_status FROM ticket_events "
                "WHERE ticket_id = :id AND event_type <> 'created' "
                "ORDER BY created_at, id"
            ),
            {"id": ticket_id},
        )
    ]


def _deactivate(db: Session, user: User) -> None:
    user.is_active = False
    db.commit()


# ---------------------------------------------------------------------------
# PUT /api/tickets/{id}/assignment
# ---------------------------------------------------------------------------


def test_admin_assigns_a_ticket(
    client: TestClient,
    db_session: Session,
    make_ticket,
    admin_user: User,
    admin_headers: dict[str, str],
    agent_user: User,
) -> None:
    ticket = make_ticket()

    response = client.put(
        _assignment_url(ticket.id),
        json={"assigned_to_user_id": agent_user.id, "expected_version": 1},
        headers=admin_headers,
    )

    assert response.status_code == 200

    body = response.json()

    assert body["assigned_to_user_id"] == agent_user.id
    # Only the ID and display name: no email or other account data.
    assert body["assignee"] == {"id": agent_user.id, "name": "Test Agent"}
    assert body["is_assigned"] is True
    assert body["assigned_at"] is not None
    assert body["version"] == 2
    assert agent_user.email not in response.text
    assert _events(db_session, ticket.id) == [
        ("assigned", admin_user.id, None, agent_user.id, None, None),
    ]


def test_admin_reassigns_and_unassigns(
    client: TestClient,
    db_session: Session,
    make_ticket,
    admin_user: User,
    admin_headers: dict[str, str],
    agent_user: User,
    other_agent: User,
) -> None:
    ticket = make_ticket(status="In Progress", assignee=agent_user)

    reassigned = client.put(
        _assignment_url(ticket.id),
        json={"assigned_to_user_id": other_agent.id, "expected_version": 2},
        headers=admin_headers,
    )

    assert reassigned.status_code == 200
    assert reassigned.json()["assignee"] == {"id": other_agent.id, "name": "Other Agent"}

    unassigned = client.put(
        _assignment_url(ticket.id),
        json={"assigned_to_user_id": None, "expected_version": 3},
        headers=admin_headers,
    )

    assert unassigned.status_code == 200

    body = unassigned.json()

    assert (body["assigned_to_user_id"], body["assignee"], body["assigned_at"]) == (
        None,
        None,
        None,
    )
    assert body["is_assigned"] is False
    # Unassigning an In Progress ticket returns it to Open.
    assert body["status"] == "Open"
    assert _events(db_session, ticket.id) == [
        ("reassigned", admin_user.id, agent_user.id, other_agent.id, None, None),
        ("unassigned", admin_user.id, other_agent.id, None, "In Progress", "Open"),
    ]


def test_repeated_assignment_is_idempotent(
    client: TestClient,
    db_session: Session,
    make_ticket,
    admin_headers: dict[str, str],
    agent_user: User,
) -> None:
    ticket = make_ticket(assignee=agent_user)

    response = client.put(
        _assignment_url(ticket.id),
        json={"assigned_to_user_id": agent_user.id, "expected_version": 2},
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert _state(db_session, ticket.id) == (agent_user.id, "Open", 2)
    assert _events(db_session, ticket.id) == []


@pytest.mark.parametrize("role", ["agent", "customer"])
def test_only_admins_can_assign(
    request: pytest.FixtureRequest,
    client: TestClient,
    db_session: Session,
    make_ticket,
    agent_user: User,
    role: str,
) -> None:
    ticket = make_ticket()
    headers = request.getfixturevalue(f"{role}_headers")

    response = client.put(
        _assignment_url(ticket.id),
        json={"assigned_to_user_id": agent_user.id, "expected_version": 1},
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json() == FORBIDDEN
    assert _state(db_session, ticket.id) == (None, "Open", 1)


@pytest.mark.parametrize("assignee", ["inactive agent", "customer", "admin", "missing user"])
def test_only_active_agents_can_be_assigned(
    client: TestClient,
    db_session: Session,
    make_ticket,
    admin_user: User,
    admin_headers: dict[str, str],
    customer_user: User,
    create_user: Callable[..., User],
    assignee: str,
) -> None:
    ticket = make_ticket()

    if assignee == "inactive agent":
        inactive = create_user(email="inactive@example.test", role="agent")
        _deactivate(db_session, inactive)
        target_id = inactive.id
    else:
        target_id = {
            "customer": customer_user.id,
            "admin": admin_user.id,
            "missing user": 999,
        }[assignee]

    response = client.put(
        _assignment_url(ticket.id),
        json={"assigned_to_user_id": target_id, "expected_version": 1},
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert response.json() == NOT_ASSIGNABLE
    assert _state(db_session, ticket.id) == (None, "Open", 1)


def test_stale_assignment_returns_409(
    client: TestClient,
    db_session: Session,
    make_ticket,
    admin_headers: dict[str, str],
    agent_user: User,
) -> None:
    ticket = make_ticket()

    response = client.put(
        _assignment_url(ticket.id),
        json={"assigned_to_user_id": agent_user.id, "expected_version": 5},
        headers=admin_headers,
    )

    assert response.status_code == 409
    assert response.json() == CONFLICT
    assert _state(db_session, ticket.id) == (None, "Open", 1)


def test_assignment_of_a_missing_ticket_returns_404(
    client: TestClient,
    admin_headers: dict[str, str],
    agent_user: User,
) -> None:
    response = client.put(
        _assignment_url(999),
        json={"assigned_to_user_id": agent_user.id, "expected_version": 1},
        headers=admin_headers,
    )

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


@pytest.mark.parametrize(
    "body",
    [
        {"expected_version": 1},  # assigned_to_user_id must be explicit
        {"assigned_to_user_id": None},
        {"assigned_to_user_id": None, "expected_version": "1"},
        {"assigned_to_user_id": None, "expected_version": True},
        {"assigned_to_user_id": None, "expected_version": 0},
        {"assigned_to_user_id": "7", "expected_version": 1},
        {"assigned_to_user_id": 0, "expected_version": 1},
        {"assigned_to_user_id": None, "expected_version": 1, "status": "Open"},
    ],
)
def test_assignment_body_is_validated(
    client: TestClient,
    db_session: Session,
    make_ticket,
    admin_headers: dict[str, str],
    body: dict,
) -> None:
    ticket = make_ticket()

    response = client.put(_assignment_url(ticket.id), json=body, headers=admin_headers)

    assert response.status_code == 422
    assert _state(db_session, ticket.id) == (None, "Open", 1)


# ---------------------------------------------------------------------------
# POST /api/tickets/{id}/claim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["Open", "In Progress"])
def test_agent_claims_an_unassigned_ticket(
    client: TestClient,
    db_session: Session,
    make_ticket,
    agent_user: User,
    agent_headers: dict[str, str],
    status: str,
) -> None:
    ticket = make_ticket(status=status)

    response = client.post(
        _claim_url(ticket.id), json={"expected_version": 1}, headers=agent_headers
    )

    assert response.status_code == 200

    body = response.json()

    assert body["assignee"] == {"id": agent_user.id, "name": "Test Agent"}
    assert (body["status"], body["version"]) == (status, 2)
    assert _events(db_session, ticket.id) == [
        ("assigned", agent_user.id, None, agent_user.id, None, None),
    ]


def test_resolved_ticket_cannot_be_claimed(
    client: TestClient,
    db_session: Session,
    make_ticket,
    agent_headers: dict[str, str],
) -> None:
    ticket = make_ticket(status="Resolved")

    response = client.post(
        _claim_url(ticket.id), json={"expected_version": 1}, headers=agent_headers
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Resolved tickets cannot be claimed."}
    assert _state(db_session, ticket.id) == (None, "Resolved", 1)


def test_agent_cannot_claim_another_agents_ticket(
    client: TestClient,
    db_session: Session,
    make_ticket,
    other_agent: User,
    agent_headers: dict[str, str],
) -> None:
    ticket = make_ticket(assignee=other_agent)

    response = client.post(
        _claim_url(ticket.id), json={"expected_version": 2}, headers=agent_headers
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Ticket is already assigned."}
    assert _state(db_session, ticket.id) == (other_agent.id, "Open", 2)


@pytest.mark.parametrize("role", ["admin", "customer"])
def test_admins_and_customers_cannot_claim(
    request: pytest.FixtureRequest,
    client: TestClient,
    db_session: Session,
    make_ticket,
    role: str,
) -> None:
    ticket = make_ticket()

    response = client.post(
        _claim_url(ticket.id),
        json={"expected_version": 1},
        headers=request.getfixturevalue(f"{role}_headers"),
    )

    assert response.status_code == 403
    assert response.json() == FORBIDDEN
    assert _state(db_session, ticket.id) == (None, "Open", 1)


def test_stale_claim_returns_409(
    client: TestClient,
    db_session: Session,
    make_ticket,
    agent_headers: dict[str, str],
) -> None:
    ticket = make_ticket()

    response = client.post(
        _claim_url(ticket.id), json={"expected_version": 4}, headers=agent_headers
    )

    assert response.status_code == 409
    assert response.json() == CONFLICT


# ---------------------------------------------------------------------------
# PATCH /api/tickets/{id}/status
# ---------------------------------------------------------------------------


APPROVED_TRANSITIONS = [
    ("Open", "In Progress"),
    ("Open", "Resolved"),
    ("In Progress", "Open"),
    ("In Progress", "Resolved"),
    ("Resolved", "In Progress"),
]


@pytest.mark.parametrize(("from_status", "to_status"), APPROVED_TRANSITIONS)
@pytest.mark.parametrize("actor", ["assigned agent", "admin"])
def test_every_approved_transition_over_http(
    client: TestClient,
    db_session: Session,
    make_ticket,
    agent_user: User,
    agent_headers: dict[str, str],
    admin_user: User,
    admin_headers: dict[str, str],
    from_status: str,
    to_status: str,
    actor: str,
) -> None:
    ticket = make_ticket(status=from_status, assignee=agent_user)
    actor_id, headers = (
        (agent_user.id, agent_headers) if actor == "assigned agent" else (admin_user.id, admin_headers)
    )

    response = client.patch(
        _status_url(ticket.id),
        json={"status": to_status, "expected_version": 2},
        headers=headers,
    )

    assert response.status_code == 200
    assert (response.json()["status"], response.json()["version"]) == (to_status, 3)
    assert _events(db_session, ticket.id) == [
        ("status_changed", actor_id, None, None, from_status, to_status),
    ]


@pytest.mark.parametrize("assigned_to", ["nobody", "another agent"])
def test_agent_cannot_change_a_ticket_not_assigned_to_them(
    client: TestClient,
    db_session: Session,
    make_ticket,
    other_agent: User,
    agent_headers: dict[str, str],
    assigned_to: str,
) -> None:
    ticket = make_ticket(assignee=other_agent if assigned_to == "another agent" else None)
    before = _state(db_session, ticket.id)

    response = client.patch(
        _status_url(ticket.id),
        json={"status": "In Progress", "expected_version": before[2]},
        headers=agent_headers,
    )

    assert response.status_code == 403
    assert response.json() == FORBIDDEN
    assert _state(db_session, ticket.id) == before


def test_customer_cannot_change_status(
    client: TestClient,
    db_session: Session,
    make_ticket,
    agent_user: User,
    customer_headers: dict[str, str],
) -> None:
    ticket = make_ticket(assignee=agent_user)

    response = client.patch(
        _status_url(ticket.id),
        json={"status": "Resolved", "expected_version": 2},
        headers=customer_headers,
    )

    assert response.status_code == 403
    assert _state(db_session, ticket.id) == (agent_user.id, "Open", 2)


@pytest.mark.parametrize(
    ("status", "assigned", "new_status", "detail"),
    [
        ("Resolved", True, "Open", "Status cannot change from Resolved to Open."),
        ("Open", False, "In Progress", "The ticket must be assigned before this status change."),
        ("Open", False, "Resolved", "The ticket must be assigned before this status change."),
    ],
    ids=["resolved-to-open", "in-progress-needs-assignee", "resolve-needs-assignee"],
)
def test_invalid_transitions_return_409(
    client: TestClient,
    db_session: Session,
    make_ticket,
    agent_user: User,
    admin_headers: dict[str, str],
    status: str,
    assigned: bool,
    new_status: str,
    detail: str,
) -> None:
    ticket = make_ticket(status=status, assignee=agent_user if assigned else None)
    before = _state(db_session, ticket.id)

    response = client.patch(
        _status_url(ticket.id),
        json={"status": new_status, "expected_version": before[2]},
        headers=admin_headers,
    )

    assert response.status_code == 409
    assert response.json() == {"detail": detail}
    assert _state(db_session, ticket.id) == before


def test_status_change_stale_version_and_no_op(
    client: TestClient,
    db_session: Session,
    make_ticket,
    agent_user: User,
    agent_headers: dict[str, str],
) -> None:
    ticket = make_ticket(assignee=agent_user)

    stale = client.patch(
        _status_url(ticket.id),
        json={"status": "In Progress", "expected_version": 1},
        headers=agent_headers,
    )

    assert stale.status_code == 409
    assert stale.json() == CONFLICT

    repeated = client.patch(
        _status_url(ticket.id),
        json={"status": "Open", "expected_version": 2},
        headers=agent_headers,
    )

    assert repeated.status_code == 200
    assert repeated.json()["version"] == 2
    assert _events(db_session, ticket.id) == []


@pytest.mark.parametrize(
    "body",
    [{"status": "Closed", "expected_version": 1}, {"status": "Open"}, {"expected_version": 1}],
)
def test_status_body_is_validated(
    client: TestClient, make_ticket, admin_headers: dict[str, str], body: dict
) -> None:
    ticket = make_ticket()

    response = client.patch(_status_url(ticket.id), json=body, headers=admin_headers)

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Generic PATCH and DELETE
# ---------------------------------------------------------------------------


def test_patch_with_expected_version(
    client: TestClient,
    db_session: Session,
    make_ticket,
    agent_user: User,
    agent_headers: dict[str, str],
) -> None:
    ticket = make_ticket(assignee=agent_user)

    stale = client.patch(
        f"/api/tickets/{ticket.id}",
        json={"category": "PAYMENT", "expected_version": 1},
        headers=agent_headers,
    )

    assert stale.status_code == 409
    assert stale.json() == CONFLICT

    current = client.patch(
        f"/api/tickets/{ticket.id}",
        json={"category": "PAYMENT", "status": "In Progress", "expected_version": 2},
        headers=agent_headers,
    )

    assert current.status_code == 200
    assert (current.json()["category"], current.json()["version"]) == ("PAYMENT", 3)
    # A status change made through PATCH is recorded like /status.
    assert _events(db_session, ticket.id) == [
        ("status_changed", agent_user.id, None, None, "Open", "In Progress"),
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"assigned_to_user_id": 1},
        {"assigned_at": "2026-11-23T10:00:00Z"},
        {"version": 9},
        {"expected_version": 2},  # no field to change
        {"category": "PAYMENT", "expected_version": "2"},
    ],
)
def test_patch_cannot_change_assignment_or_version(
    client: TestClient,
    db_session: Session,
    make_ticket,
    agent_user: User,
    agent_headers: dict[str, str],
    body: dict,
) -> None:
    ticket = make_ticket(assignee=agent_user)

    response = client.patch(f"/api/tickets/{ticket.id}", json=body, headers=agent_headers)

    assert response.status_code == 422
    assert _state(db_session, ticket.id) == (agent_user.id, "Open", 2)


def test_patch_by_inactive_admin_is_refused(
    client: TestClient,
    db_session: Session,
    make_ticket,
    admin_user: User,
    admin_headers: dict[str, str],
) -> None:
    ticket = make_ticket()
    _deactivate(db_session, admin_user)

    response = client.patch(
        f"/api/tickets/{ticket.id}", json={"priority": "Low"}, headers=admin_headers
    )

    # An inactive account's token is no longer accepted at all.
    assert response.status_code == 401
    assert _state(db_session, ticket.id) == (None, "Open", 1)


def test_stale_data_error_from_an_edit_is_409_not_500(
    client: TestClient,
    make_ticket,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = make_ticket()

    def stale_edit(*_args, **_kwargs):
        raise StaleDataError("UPDATE statement on table 'tickets' expected 1 row")

    monkeypatch.setattr("app.api.routes.tickets.edit_ticket_with_result", stale_edit)

    response = client.patch(
        f"/api/tickets/{ticket.id}", json={"priority": "Low"}, headers=admin_headers
    )

    assert response.status_code == 409
    assert response.json() == CONFLICT
    assert "UPDATE" not in response.text


def test_stale_orm_update_inside_the_edit_is_409(
    client: TestClient,
    db_session: Session,
    make_ticket,
    agent_user: User,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row changes between the locked read and the versioned UPDATE."""

    ticket = make_ticket(assignee=agent_user)
    original = ticket_workflow._lock_for_ticket_change

    def lock_then_bump(db: Session, **kwargs):
        state = original(db, **kwargs)
        db.execute(
            text("UPDATE tickets SET version = version + 1 WHERE id = :id"),
            {"id": ticket.id},
        )
        return state

    monkeypatch.setattr(ticket_workflow, "_lock_for_ticket_change", lock_then_bump)

    response = client.patch(
        f"/api/tickets/{ticket.id}", json={"priority": "Low"}, headers=admin_headers
    )

    assert response.status_code == 409
    assert response.json() == CONFLICT
    assert _state(db_session, ticket.id) == (agent_user.id, "Open", 2)


def test_stale_delete_returns_409_and_deletes_nothing(
    client: TestClient,
    db_session: Session,
    make_ticket,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = make_ticket()
    ticket_id = ticket.id

    from app.api.routes import tickets as tickets_routes

    real_get = tickets_routes.get_ticket_by_id

    def get_then_change(db: Session, ticket_id: int, **kwargs):
        loaded = real_get(db=db, ticket_id=ticket_id, **kwargs)
        # Another writer changes the row after it was loaded.
        db.execute(
            text("UPDATE tickets SET version = version + 1 WHERE id = :id"),
            {"id": ticket_id},
        )
        return loaded

    monkeypatch.setattr(tickets_routes, "get_ticket_by_id", get_then_change)

    response = client.delete(f"/api/tickets/{ticket_id}", headers=admin_headers)

    assert response.status_code == 409
    assert response.json() == CONFLICT

    monkeypatch.undo()
    db_session.rollback()

    assert db_session.get(Ticket, ticket_id) is not None
    assert db_session.scalar(
        select(func.count()).select_from(TicketEvent).where(TicketEvent.ticket_id == ticket_id)
    ) == 1


def test_delete_cascades_to_the_ticket_history(
    client: TestClient,
    db_session: Session,
    make_ticket,
    agent_user: User,
    admin_headers: dict[str, str],
) -> None:
    ticket = make_ticket()
    kept = make_ticket(title="Kept ticket")

    assigned = client.put(
        _assignment_url(ticket.id),
        json={"assigned_to_user_id": agent_user.id, "expected_version": 1},
        headers=admin_headers,
    )
    assert assigned.status_code == 200

    response = client.delete(f"/api/tickets/{ticket.id}", headers=admin_headers)

    assert response.status_code == 204
    assert db_session.scalars(select(TicketEvent.ticket_id)).all() == [kept.id]


# ---------------------------------------------------------------------------
# GET /api/tickets: assignment filter, X-Total-Count, privacy
# ---------------------------------------------------------------------------


@pytest.fixture
def queue(make_ticket, agent_user: User, other_agent: User) -> dict[str, Ticket]:
    """Created oldest first; the list returns newest first."""

    return {
        "mine_1": make_ticket(title="Mine one", assignee=agent_user),
        "theirs": make_ticket(title="Theirs", assignee=other_agent),
        "free_1": make_ticket(title="Free one"),
        "mine_2": make_ticket(title="Mine two", status="In Progress", assignee=agent_user),
        "free_2": make_ticket(title="Free two", status="Resolved"),
    }


def _ids(response) -> list[int]:
    return [ticket["id"] for ticket in response.json()]


@pytest.mark.parametrize(
    ("assigned_to", "expected"),
    [
        ("me", ["mine_2", "mine_1"]),
        ("none", ["free_2", "free_1"]),
        ("OTHER", ["theirs"]),
        (None, ["free_2", "mine_2", "free_1", "theirs", "mine_1"]),
    ],
)
def test_assigned_to_filter(
    client: TestClient,
    queue: dict[str, Ticket],
    other_agent: User,
    agent_headers: dict[str, str],
    assigned_to: str | None,
    expected: list[str],
) -> None:
    params = {}

    if assigned_to is not None:
        params["assigned_to"] = (
            str(other_agent.id) if assigned_to == "OTHER" else assigned_to
        )

    response = client.get("/api/tickets", params=params, headers=agent_headers)

    assert response.status_code == 200
    # Still a bare JSON list, newest first.
    assert isinstance(response.json(), list)
    assert _ids(response) == [queue[name].id for name in expected]
    assert response.headers["X-Total-Count"] == str(len(expected))


def test_assigned_to_combines_with_other_filters(
    client: TestClient,
    queue: dict[str, Ticket],
    agent_headers: dict[str, str],
) -> None:
    response = client.get(
        "/api/tickets",
        params={"assigned_to": "me", "status": "In Progress", "search": "mine"},
        headers=agent_headers,
    )

    assert _ids(response) == [queue["mine_2"].id]
    assert response.headers["X-Total-Count"] == "1"


def test_total_count_is_before_pagination(
    client: TestClient,
    queue: dict[str, Ticket],
    admin_headers: dict[str, str],
) -> None:
    first_page = client.get(
        "/api/tickets", params={"skip": 0, "limit": 2}, headers=admin_headers
    )
    beyond = client.get(
        "/api/tickets", params={"skip": 50, "limit": 2}, headers=admin_headers
    )

    assert len(first_page.json()) == 2
    assert first_page.headers["X-Total-Count"] == "5"
    assert beyond.json() == []
    assert beyond.headers["X-Total-Count"] == "5"


@pytest.mark.parametrize("value", ["ME", "None", "0", "-1", "abc", "1.5", "", "1" * 20])
def test_invalid_assigned_to_values_return_422(
    client: TestClient,
    queue: dict[str, Ticket],
    agent_headers: dict[str, str],
    value: str,
) -> None:
    response = client.get(
        "/api/tickets", params={"assigned_to": value}, headers=agent_headers
    )

    assert response.status_code == 422


def test_out_of_range_user_id_returns_422(
    client: TestClient,
    agent_headers: dict[str, str],
) -> None:
    response = client.get(
        "/api/tickets", params={"assigned_to": "9" * 19}, headers=agent_headers
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "assigned_to must be 'me', 'none' or a user ID."}


@pytest.mark.parametrize("value", ["me", "none", "1"])
def test_customers_cannot_use_the_assignment_filter(
    client: TestClient,
    queue: dict[str, Ticket],
    customer_headers: dict[str, str],
    value: str,
) -> None:
    response = client.get(
        "/api/tickets", params={"assigned_to": value}, headers=customer_headers
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "The assigned_to filter is only available to staff."
    }


def test_customer_list_keeps_ownership_and_hides_assignee_identity(
    client: TestClient,
    db_session: Session,
    queue: dict[str, Ticket],
    agent_user: User,
    other_agent: User,
    second_customer_user: User,
    customer_headers: dict[str, str],
) -> None:
    foreign = create_ticket(
        db_session,
        creator=second_customer_user,
        title="Someone else's",
        description="Not visible to the first customer.",
    )

    response = client.get("/api/tickets", headers=customer_headers)

    assert response.status_code == 200
    assert foreign.id not in _ids(response)
    assert response.headers["X-Total-Count"] == "5"

    for ticket in response.json():
        assert STAFF_ONLY_FIELDS.isdisjoint(ticket)
        assert isinstance(ticket["is_assigned"], bool)

    by_id = {ticket["id"]: ticket for ticket in response.json()}

    assert by_id[queue["mine_1"].id]["is_assigned"] is True
    assert by_id[queue["free_1"].id]["is_assigned"] is False

    for secret in ("Test Agent", "Other Agent", agent_user.email, other_agent.email):
        assert secret not in response.text


def test_customer_detail_and_create_hide_assignee_identity(
    client: TestClient,
    make_ticket,
    agent_user: User,
    customer_headers: dict[str, str],
) -> None:
    ticket = make_ticket(assignee=agent_user)

    detail = client.get(f"/api/tickets/{ticket.id}", headers=customer_headers)
    created = client.post(
        "/api/tickets",
        json={"title": "New problem", "description": "Something broke again."},
        headers=customer_headers,
    )

    for response, assigned in ((detail, True), (created, False)):
        assert response.status_code in (200, 201)
        assert STAFF_ONLY_FIELDS.isdisjoint(response.json())
        assert response.json()["is_assigned"] is assigned
        assert "Test Agent" not in response.text


def test_staff_responses_include_assignment_details(
    client: TestClient,
    queue: dict[str, Ticket],
    agent_user: User,
    admin_headers: dict[str, str],
) -> None:
    listing = client.get("/api/tickets", headers=admin_headers)
    detail = client.get(f"/api/tickets/{queue['mine_1'].id}", headers=admin_headers)

    for ticket in listing.json():
        assert STAFF_ONLY_FIELDS <= set(ticket)

    body = detail.json()

    assert body["assignee"] == {"id": agent_user.id, "name": "Test Agent"}
    assert body["assigned_to_user_id"] == agent_user.id
    assert body["version"] == 2
    assert agent_user.email not in detail.text


def test_staff_list_loads_assignees_without_a_query_per_ticket(
    client: TestClient,
    db_session: Session,
    customer_user: User,
    agent_user: User,
    other_agent: User,
    admin_headers: dict[str, str],
) -> None:
    """The query count does not grow with the number of assigned tickets.

    The session is emptied before each request, so assignees cannot be
    served from the identity map (which would hide one query per ticket).
    """

    customer_id, agent_ids = customer_user.id, [agent_user.id, other_agent.id]

    def add_tickets(count: int) -> None:
        for index in range(count):
            db_session.add(
                Ticket(
                    title=f"Ticket {index}",
                    description="Query count test.",
                    created_by_user_id=customer_id,
                    assigned_to_user_id=agent_ids[index % 2],
                )
            )

        db_session.commit()

    def count_queries(expected_tickets: int) -> int:
        statements: list[str] = []
        engine = db_session.get_bind()

        def _record(_conn, _cursor, statement, *_args):
            statements.append(statement)

        db_session.expunge_all()
        event.listen(engine, "before_cursor_execute", _record)

        try:
            response = client.get("/api/tickets", headers=admin_headers)
        finally:
            event.remove(engine, "before_cursor_execute", _record)

        assert len(response.json()) == expected_tickets
        assert all(ticket["assignee"] for ticket in response.json())

        return len(statements)

    add_tickets(1)
    few = count_queries(1)

    add_tickets(6)

    assert count_queries(7) == few


# ---------------------------------------------------------------------------
# Ticket creation history
# ---------------------------------------------------------------------------


def test_api_creation_writes_one_created_event(
    client: TestClient,
    db_session: Session,
    customer_user: User,
    customer_headers: dict[str, str],
) -> None:
    response = client.post(
        "/api/tickets",
        json={"title": "Login fails", "description": "Cannot sign in at all."},
        headers=customer_headers,
    )

    assert response.status_code == 201

    ticket_id = response.json()["id"]
    rows = db_session.execute(
        text("SELECT ticket_id, event_type, actor_user_id FROM ticket_events")
    ).all()

    assert [tuple(row) for row in rows] == [(ticket_id, "created", customer_user.id)]


# ---------------------------------------------------------------------------
# Concurrent claims over HTTP (separate connections)
# ---------------------------------------------------------------------------

WAIT_SECONDS = 15
BLOCKED_CHECK_SECONDS = 0.5


@pytest.fixture
def file_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'claims.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)

    yield engine

    engine.dispose()


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


def test_two_agents_racing_to_claim_over_http(
    file_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Session(file_engine) as db:
        users = [
            User(name=label, email=f"{label}@example.test", password_hash="unused", role=role)
            for label, role in [("customer", "customer"), ("agent-a", "agent"), ("agent-b", "agent")]
        ]
        db.add_all(users)
        db.commit()
        customer, agent_a, agent_b = (user.id for user in users)
        ticket = create_ticket(
            db,
            creator=db.get(User, customer),
            title="Race me",
            description="Two agents want this one.",
        )
        ticket_id = ticket.id

    def headers_for(user_id: int) -> dict[str, str]:
        token = create_access_token(subject=str(user_id), token_version=0)
        return {"Authorization": f"Bearer {token}"}

    def file_db():
        db = Session(file_engine)
        try:
            yield db
        finally:
            db.close()

    holding, release = threading.Event(), threading.Event()
    first = {"value": True}
    guard = threading.Lock()
    original = ticket_workflow._lock_for_ticket_change

    def lock_then_hold(db: Session, **kwargs):
        state = original(db, **kwargs)

        with guard:
            hold, first["value"] = first["value"], False

        if hold:
            holding.set()
            release.wait(WAIT_SECONDS)

        return state

    monkeypatch.setattr(ticket_workflow, "_lock_for_ticket_change", lock_then_hold)
    app.dependency_overrides[get_db] = file_db
    threads: list[threading.Thread] = []

    try:
        with TestClient(app) as client:
            try:
                thread_a, outcome_a = _start(
                    lambda: client.post(
                        _claim_url(ticket_id),
                        json={"expected_version": 1},
                        headers=headers_for(agent_a),
                    )
                )
                threads.append(thread_a)

                assert holding.wait(WAIT_SECONDS), "the first claim never locked"

                thread_b, outcome_b = _start(
                    lambda: client.post(
                        _claim_url(ticket_id),
                        json={"expected_version": 1},
                        headers=headers_for(agent_b),
                    )
                )
                threads.append(thread_b)
                thread_b.join(BLOCKED_CHECK_SECONDS)
                second_was_waiting = thread_b.is_alive()
            finally:
                release.set()

                for thread in threads:
                    thread.join(WAIT_SECONDS)
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert not any(thread.is_alive() for thread in threads)
    assert second_was_waiting, "the second claim did not wait for the lock"
    assert outcome_a["result"].status_code == 200
    assert outcome_a["result"].json()["assigned_to_user_id"] == agent_a
    assert outcome_b["result"].status_code == 409
    assert outcome_b["result"].json() == {"detail": "Ticket is already assigned."}

    with Session(file_engine) as db:
        assert _state(db, ticket_id) == (agent_a, "Open", 2)
        assert len(_events(db, ticket_id)) == 1


# ---------------------------------------------------------------------------
# Ticket audit records (resolveai.audit), Phase 7.6
# ---------------------------------------------------------------------------


@pytest.fixture
def ticket_audit() -> list[logging.LogRecord]:
    """Every ticket_action record the audit logger emits during the test.

    A collecting handler is attached to the audit logger itself (it does
    not propagate); the logger's own level and handlers are left alone.
    """

    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if hasattr(record, "ticket_action"):
                records.append(record)

    handler = _Collect()
    audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)
    audit_logger.addHandler(handler)

    yield records

    audit_logger.removeHandler(handler)


def _actions(records: list[logging.LogRecord]) -> list[dict]:
    return [record.ticket_action for record in records]


def test_assignment_reassignment_and_unassignment_are_audited(
    client: TestClient,
    make_ticket,
    admin_user: User,
    admin_headers: dict[str, str],
    agent_user: User,
    other_agent: User,
    ticket_audit: list[logging.LogRecord],
) -> None:
    ticket = make_ticket(status="Open")
    admin_id, agent_id, other_id = admin_user.id, agent_user.id, other_agent.id

    for assignee, version in ((agent_id, 1), (other_id, 2), (None, 3)):
        response = client.put(
            _assignment_url(ticket.id),
            json={"assigned_to_user_id": assignee, "expected_version": version},
            headers=admin_headers,
        )
        assert response.status_code == 200

    common = {"action": "ticket.assign", "actor_id": admin_id, "ticket_id": ticket.id}

    assert _actions(ticket_audit) == [
        {**common, "event_type": "assigned", "previous_assignee_id": None, "new_assignee_id": agent_id},
        {**common, "event_type": "reassigned", "previous_assignee_id": agent_id, "new_assignee_id": other_id},
        {**common, "event_type": "unassigned", "previous_assignee_id": other_id, "new_assignee_id": None},
    ]
    assert ticket_audit[2].getMessage() == (
        f"ticket_action action=ticket.assign actor_id={admin_id} "
        f"ticket_id={ticket.id} event_type=unassigned "
        f"previous_assignee_id={other_id} new_assignee_id=none"
    )
    assert all(record.levelno == logging.INFO for record in ticket_audit)


def test_claim_is_audited_once(
    client: TestClient,
    make_ticket,
    agent_user: User,
    agent_headers: dict[str, str],
    ticket_audit: list[logging.LogRecord],
) -> None:
    ticket = make_ticket()

    response = client.post(
        _claim_url(ticket.id), json={"expected_version": 1}, headers=agent_headers
    )

    assert response.status_code == 200
    assert _actions(ticket_audit) == [
        {
            "action": "ticket.claim",
            "actor_id": agent_user.id,
            "ticket_id": ticket.id,
            "assignee_id": agent_user.id,
        }
    ]


@pytest.mark.parametrize("route", ["status endpoint", "generic patch"])
def test_status_change_is_audited_once(
    client: TestClient,
    make_ticket,
    agent_user: User,
    agent_headers: dict[str, str],
    ticket_audit: list[logging.LogRecord],
    route: str,
) -> None:
    ticket = make_ticket(assignee=agent_user)

    if route == "status endpoint":
        response = client.patch(
            _status_url(ticket.id),
            json={"status": "In Progress", "expected_version": 2},
            headers=agent_headers,
        )
    else:
        response = client.patch(
            f"/api/tickets/{ticket.id}",
            json={"status": "In Progress", "category": "PAYMENT"},
            headers=agent_headers,
        )

    assert response.status_code == 200
    assert _actions(ticket_audit) == [
        {
            "action": "ticket.status_change",
            "actor_id": agent_user.id,
            "ticket_id": ticket.id,
            "status_before": "Open",
            "status_after": "In Progress",
        }
    ]
    # Multi-word statuses stay one key=value pair.
    assert ticket_audit[0].getMessage().endswith(
        'status_before=Open status_after="In Progress"'
    )


def test_delete_is_audited_once(
    client: TestClient,
    db_session: Session,
    make_ticket,
    admin_user: User,
    admin_headers: dict[str, str],
    ticket_audit: list[logging.LogRecord],
) -> None:
    ticket = make_ticket()
    ticket_id = ticket.id

    response = client.delete(f"/api/tickets/{ticket_id}", headers=admin_headers)

    assert response.status_code == 204
    # The history went with the ticket; the audit record remains.
    assert db_session.scalar(
        select(func.count()).select_from(TicketEvent).where(TicketEvent.ticket_id == ticket_id)
    ) == 0
    assert _actions(ticket_audit) == [
        {"action": "ticket.delete", "actor_id": admin_user.id, "ticket_id": ticket_id}
    ]


def test_field_only_edit_is_not_audited(
    client: TestClient,
    make_ticket,
    agent_user: User,
    agent_headers: dict[str, str],
    ticket_audit: list[logging.LogRecord],
) -> None:
    ticket = make_ticket(assignee=agent_user)

    response = client.patch(
        f"/api/tickets/{ticket.id}", json={"category": "PAYMENT"}, headers=agent_headers
    )

    assert response.status_code == 200
    assert ticket_audit == []


@pytest.fixture
def failing_requests(
    db_session: Session,
    make_ticket,
    create_user: Callable[..., User],
    agent_user: User,
    other_agent: User,
    customer_user: User,
    admin_headers: dict[str, str],
    agent_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Callable[[TestClient], object]]:
    """Requests that fail or change nothing, each with its expected status."""

    open_ticket = make_ticket(title="Open ticket")
    mine = make_ticket(title="Mine", assignee=agent_user)
    resolved = make_ticket(title="Resolved", status="Resolved", assignee=agent_user)
    free_resolved = make_ticket(title="Free resolved", status="Resolved")
    theirs = make_ticket(title="Theirs", assignee=other_agent)

    def stale_delete(client: TestClient):
        from app.api.routes import tickets as tickets_routes

        real_get = tickets_routes.get_ticket_by_id

        def get_then_change(db: Session, ticket_id: int, **kwargs):
            loaded = real_get(db=db, ticket_id=ticket_id, **kwargs)
            db.execute(
                text("UPDATE tickets SET version = version + 1 WHERE id = :id"),
                {"id": ticket_id},
            )
            return loaded

        monkeypatch.setattr(tickets_routes, "get_ticket_by_id", get_then_change)

        try:
            return client.delete(f"/api/tickets/{open_ticket.id}", headers=admin_headers)
        finally:
            monkeypatch.setattr(tickets_routes, "get_ticket_by_id", real_get)

    return {
        "assign to customer (422)": lambda c: c.put(
            _assignment_url(open_ticket.id),
            json={"assigned_to_user_id": customer_user.id, "expected_version": 1},
            headers=admin_headers,
        ),
        "assign stale (409)": lambda c: c.put(
            _assignment_url(open_ticket.id),
            json={"assigned_to_user_id": agent_user.id, "expected_version": 9},
            headers=admin_headers,
        ),
        "assign by agent (403)": lambda c: c.put(
            _assignment_url(open_ticket.id),
            json={"assigned_to_user_id": agent_user.id, "expected_version": 1},
            headers=agent_headers,
        ),
        "no-op assignment (200)": lambda c: c.put(
            _assignment_url(mine.id),
            json={"assigned_to_user_id": agent_user.id, "expected_version": 2},
            headers=admin_headers,
        ),
        "claim resolved (409)": lambda c: c.post(
            _claim_url(free_resolved.id), json={"expected_version": 1}, headers=agent_headers
        ),
        "claim assigned (409)": lambda c: c.post(
            _claim_url(theirs.id), json={"expected_version": 2}, headers=agent_headers
        ),
        "claim by admin (403)": lambda c: c.post(
            _claim_url(open_ticket.id), json={"expected_version": 1}, headers=admin_headers
        ),
        "invalid transition (409)": lambda c: c.patch(
            _status_url(resolved.id),
            json={"status": "Open", "expected_version": 2},
            headers=agent_headers,
        ),
        "status stale (409)": lambda c: c.patch(
            _status_url(mine.id),
            json={"status": "In Progress", "expected_version": 1},
            headers=agent_headers,
        ),
        "status not mine (403)": lambda c: c.patch(
            _status_url(theirs.id),
            json={"status": "In Progress", "expected_version": 2},
            headers=agent_headers,
        ),
        "no-op status (200)": lambda c: c.patch(
            _status_url(mine.id),
            json={"status": "Open", "expected_version": 2},
            headers=agent_headers,
        ),
        "patch bypass (409)": lambda c: c.patch(
            f"/api/tickets/{open_ticket.id}",
            json={"status": "Resolved"},
            headers=admin_headers,
        ),
        "delete missing (404)": lambda c: c.delete("/api/tickets/999", headers=admin_headers),
        "delete by agent (403)": lambda c: c.delete(
            f"/api/tickets/{open_ticket.id}", headers=agent_headers
        ),
        "delete stale (409)": stale_delete,
    }


FAILING_CASES = [
    "assign to customer (422)",
    "assign stale (409)",
    "assign by agent (403)",
    "no-op assignment (200)",
    "claim resolved (409)",
    "claim assigned (409)",
    "claim by admin (403)",
    "invalid transition (409)",
    "status stale (409)",
    "status not mine (403)",
    "no-op status (200)",
    "patch bypass (409)",
    "delete missing (404)",
    "delete by agent (403)",
    "delete stale (409)",
]


@pytest.mark.parametrize("case", FAILING_CASES)
def test_failed_and_no_op_requests_write_no_audit_record(
    client: TestClient,
    failing_requests: dict,
    ticket_audit: list[logging.LogRecord],
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    capsys.readouterr()

    response = failing_requests[case](client)
    expected_status = int(case.rsplit("(", 1)[1].rstrip(")"))

    assert response.status_code == expected_status
    assert ticket_audit == []
    assert "ticket_action" not in capsys.readouterr().err


def test_ticket_audit_output_contains_no_personal_or_ticket_data(
    client: TestClient,
    db_session: Session,
    make_ticket,
    customer_user: User,
    admin_user: User,
    admin_headers: dict[str, str],
    agent_user: User,
    agent_headers: dict[str, str],
    ticket_audit: list[logging.LogRecord],
    capsys: pytest.CaptureFixture[str],
) -> None:
    title, description = "Audit-title-secret-91af", "Audit description secret c0de"
    ticket = make_ticket(title=title, description=description, requester_name="Private Person")
    other = make_ticket(title=title, description=description)
    capsys.readouterr()

    requests = [
        client.put(_assignment_url(ticket.id), json={"assigned_to_user_id": agent_user.id, "expected_version": 1}, headers=admin_headers),
        client.patch(_status_url(ticket.id), json={"status": "In Progress", "expected_version": 2}, headers=agent_headers),
        client.put(_assignment_url(ticket.id), json={"assigned_to_user_id": None, "expected_version": 3}, headers=admin_headers),
        client.post(_claim_url(ticket.id), json={"expected_version": 4}, headers=agent_headers),
        client.patch(f"/api/tickets/{ticket.id}", json={"status": "Resolved", "expected_version": 5}, headers=agent_headers),
        client.delete(f"/api/tickets/{other.id}", headers=admin_headers),
    ]

    assert [r.status_code for r in requests] == [200, 200, 200, 200, 200, 204]

    output = capsys.readouterr().err
    lines = [line for line in output.splitlines() if "ticket_action" in line]

    # The application's own handler wrote one line per action to stderr.
    assert len(lines) == len(ticket_audit) == 6
    assert [r.ticket_action["action"] for r in ticket_audit] == [
        "ticket.assign",
        "ticket.status_change",
        "ticket.assign",
        "ticket.claim",
        "ticket.status_change",
        "ticket.delete",
    ]

    stored_hashes = [
        db_session.execute(
            text("SELECT password_hash FROM users WHERE id = :id"), {"id": user.id}
        ).scalar_one()
        for user in (customer_user, admin_user, agent_user)
    ]
    tokens = [
        headers["Authorization"].removeprefix("Bearer ")
        for headers in (admin_headers, agent_headers)
    ]
    formatted = "\n".join(record.getMessage() for record in ticket_audit)

    for secret in (
        title,
        description,
        "Private Person",
        customer_user.name,
        customer_user.email,
        admin_user.name,
        admin_user.email,
        agent_user.name,
        agent_user.email,
        TEST_PASSWORD,
        "$argon2",
        *stored_hashes,
        *tokens,
    ):
        assert secret not in output
        assert secret not in formatted

    # Only IDs, fixed names, statuses and none appear as values.
    allowed_values = {
        "ticket.assign", "ticket.claim", "ticket.status_change", "ticket.delete",
        "assigned", "reassigned", "unassigned", "Open", "In Progress", "Resolved",
    }

    for record in ticket_audit:
        for value in record.ticket_action.values():
            assert value is None or isinstance(value, int) or value in allowed_values
