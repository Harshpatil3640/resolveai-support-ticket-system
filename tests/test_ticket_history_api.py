"""GET /api/tickets/{ticket_id}/history (staff only).

Uses conftest's in-memory database and role fixtures; resolveai.db is
never used.
"""

from collections.abc import Callable
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Ticket, TicketEvent, User
from app.services.ticket_service import create_ticket


EVENT_FIELDS = {
    "id",
    "ticket_id",
    "event_type",
    "actor_user_id",
    "from_assignee_id",
    "to_assignee_id",
    "from_status",
    "to_status",
    "reason",
    "created_at",
}

TITLE_SECRET = "History-title-secret-0c7e"
DESCRIPTION_SECRET = "History description secret 6a21"


def _history_url(ticket_id: int) -> str:
    return f"/api/tickets/{ticket_id}/history"


@pytest.fixture
def make_ticket(db_session: Session, customer_user: User) -> Callable[..., Ticket]:
    """A customer's ticket created through the service (with its event)."""

    def _make(title: str = TITLE_SECRET) -> Ticket:
        return create_ticket(
            db_session,
            creator=customer_user,
            title=title,
            description=DESCRIPTION_SECRET,
            requester_name="Private Requester",
        )

    return _make


def _event_summary(response) -> list[tuple]:
    return [
        (
            event["event_type"],
            event["actor_user_id"],
            event["from_assignee_id"],
            event["to_assignee_id"],
            event["from_status"],
            event["to_status"],
        )
        for event in response.json()
    ]


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "agent"])
def test_staff_can_view_history(
    request: pytest.FixtureRequest,
    client: TestClient,
    make_ticket,
    customer_user: User,
    role: str,
) -> None:
    ticket = make_ticket()

    response = client.get(
        _history_url(ticket.id), headers=request.getfixturevalue(f"{role}_headers")
    )

    assert response.status_code == 200
    assert _event_summary(response) == [
        ("created", customer_user.id, None, None, None, None),
    ]


def test_customer_receives_403_even_for_their_own_ticket(
    client: TestClient,
    make_ticket,
    customer_headers: dict[str, str],
) -> None:
    ticket = make_ticket()

    response = client.get(_history_url(ticket.id), headers=customer_headers)

    assert response.status_code == 403
    assert response.json() == {
        "detail": "You do not have permission to perform this action."
    }


def test_unauthenticated_request_receives_401(client: TestClient, make_ticket) -> None:
    ticket = make_ticket()

    response = client.get(_history_url(ticket.id))

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("role", ["admin", "agent"])
def test_inactive_user_receives_401(
    request: pytest.FixtureRequest,
    client: TestClient,
    db_session: Session,
    make_ticket,
    role: str,
) -> None:
    ticket = make_ticket()
    headers = request.getfixturevalue(f"{role}_headers")
    user = request.getfixturevalue(f"{role}_user")
    user.is_active = False
    db_session.commit()

    response = client.get(_history_url(ticket.id), headers=headers)

    assert response.status_code == 401


def test_missing_ticket_receives_404(client: TestClient, admin_headers: dict[str, str]) -> None:
    response = client.get(_history_url(999), headers=admin_headers)

    assert response.status_code == 404
    assert response.json() == {"detail": "Ticket not found."}


@pytest.mark.parametrize("ticket_id", ["0", "-1", "abc", str(2**63)])
def test_invalid_ticket_id_receives_422(
    client: TestClient,
    admin_headers: dict[str, str],
    ticket_id: str,
) -> None:
    response = client.get(f"/api/tickets/{ticket_id}/history", headers=admin_headers)

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Content and ordering
# ---------------------------------------------------------------------------


def test_history_follows_assignment_claim_and_status_changes(
    client: TestClient,
    make_ticket,
    customer_user: User,
    admin_user: User,
    admin_headers: dict[str, str],
    agent_user: User,
    agent_headers: dict[str, str],
) -> None:
    ticket = make_ticket()
    steps = [
        (
            "put",
            f"/api/tickets/{ticket.id}/assignment",
            {"assigned_to_user_id": agent_user.id, "expected_version": 1},
            admin_headers,
        ),
        (
            "patch",
            f"/api/tickets/{ticket.id}/status",
            {"status": "In Progress", "expected_version": 2},
            agent_headers,
        ),
        (
            "put",
            f"/api/tickets/{ticket.id}/assignment",
            {"assigned_to_user_id": None, "expected_version": 3},
            admin_headers,
        ),
        (
            "post",
            f"/api/tickets/{ticket.id}/claim",
            {"expected_version": 4},
            agent_headers,
        ),
        (
            "patch",
            f"/api/tickets/{ticket.id}/status",
            {"status": "Resolved", "expected_version": 5},
            agent_headers,
        ),
    ]

    for method, url, body, headers in steps:
        assert client.request(method, url, json=body, headers=headers).status_code == 200

    response = client.get(_history_url(ticket.id), headers=agent_headers)

    assert response.status_code == 200
    assert _event_summary(response) == [
        ("created", customer_user.id, None, None, None, None),
        ("assigned", admin_user.id, None, agent_user.id, None, None),
        ("status_changed", agent_user.id, None, None, "Open", "In Progress"),
        ("unassigned", admin_user.id, agent_user.id, None, "In Progress", "Open"),
        ("assigned", agent_user.id, None, agent_user.id, None, None),
        ("status_changed", agent_user.id, None, None, "Open", "Resolved"),
    ]
    assert all(event["ticket_id"] == ticket.id for event in response.json())
    assert all(event["reason"] is None for event in response.json())

    # Oldest first, and consistent with the ticket's final state.
    stamps = [event["created_at"] for event in response.json()]
    detail = client.get(f"/api/tickets/{ticket.id}", headers=agent_headers).json()

    assert stamps == sorted(stamps)
    assert (detail["status"], detail["assigned_to_user_id"], detail["version"]) == (
        "Resolved",
        agent_user.id,
        6,
    )
    assert len(response.json()) == detail["version"]


def test_timestamp_ties_are_ordered_by_event_id(
    client: TestClient,
    db_session: Session,
    make_ticket,
    admin_user: User,
    admin_headers: dict[str, str],
) -> None:
    ticket = make_ticket()
    tied = datetime(2099, 1, 1, tzinfo=timezone.utc)
    later = datetime(2099, 1, 2, tzinfo=timezone.utc)

    # Inserted newest first; two share a timestamp.
    rows = [
        TicketEvent(ticket_id=ticket.id, actor_user_id=admin_user.id,
                    event_type="status_changed", from_status="Open",
                    to_status="In Progress", created_at=later),
        TicketEvent(ticket_id=ticket.id, actor_user_id=admin_user.id,
                    event_type="assigned", created_at=tied),
        TicketEvent(ticket_id=ticket.id, actor_user_id=admin_user.id,
                    event_type="unassigned", created_at=tied),
    ]

    for row in rows:
        db_session.add(row)
        db_session.flush()

    db_session.commit()
    latest, tie_low, tie_high = (row.id for row in rows)

    response = client.get(_history_url(ticket.id), headers=admin_headers)
    ids = [event["id"] for event in response.json()]

    # The "created" event (earliest), the tie in id order, then the latest.
    assert ids[1:] == [tie_low, tie_high, latest]
    assert tie_low < tie_high


def test_history_never_includes_another_tickets_events(
    client: TestClient,
    make_ticket,
    agent_user: User,
    admin_headers: dict[str, str],
) -> None:
    first = make_ticket(title="First ticket")
    second = make_ticket(title="Second ticket")

    assigned = client.put(
        f"/api/tickets/{second.id}/assignment",
        json={"assigned_to_user_id": agent_user.id, "expected_version": 1},
        headers=admin_headers,
    )
    assert assigned.status_code == 200

    first_history = client.get(_history_url(first.id), headers=admin_headers).json()
    second_history = client.get(_history_url(second.id), headers=admin_headers).json()

    assert {event["ticket_id"] for event in first_history} == {first.id}
    assert [event["event_type"] for event in first_history] == ["created"]
    assert {event["ticket_id"] for event in second_history} == {second.id}
    assert [event["event_type"] for event in second_history] == ["created", "assigned"]


def test_ticket_without_history_returns_an_empty_list(
    client: TestClient,
    db_session: Session,
    customer_user: User,
    agent_headers: dict[str, str],
) -> None:
    """A ticket from before Phase 7 (no created event) still has a history."""

    legacy = Ticket(
        title="Legacy ticket",
        description="Created before ticket history existed.",
        created_by_user_id=customer_user.id,
    )
    db_session.add(legacy)
    db_session.commit()

    response = client.get(_history_url(legacy.id), headers=agent_headers)

    assert response.status_code == 200
    assert response.json() == []


def test_history_exposes_only_the_approved_fields(
    client: TestClient,
    make_ticket,
    customer_user: User,
    admin_user: User,
    agent_user: User,
    admin_headers: dict[str, str],
) -> None:
    ticket = make_ticket()
    client.put(
        f"/api/tickets/{ticket.id}/assignment",
        json={"assigned_to_user_id": agent_user.id, "expected_version": 1},
        headers=admin_headers,
    )

    response = client.get(_history_url(ticket.id), headers=admin_headers)

    assert response.status_code == 200
    assert all(set(event) == EVENT_FIELDS for event in response.json())

    for secret in (
        TITLE_SECRET,
        DESCRIPTION_SECRET,
        "Private Requester",
        customer_user.email,
        customer_user.name,
        admin_user.email,
        admin_user.name,
        agent_user.email,
        agent_user.name,
        "$argon2",
    ):
        assert secret not in response.text
