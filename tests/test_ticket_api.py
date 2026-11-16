
import pytest

from fastapi.testclient import TestClient
from app.services.ticket_service import create_ticket
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.database import get_db
from app.main import app
from app.models import Ticket, User


@pytest.fixture
def client(db_session: Session):
    """Create a test client using an isolated test database."""

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def api_user(db_session: Session) -> User:
    """Create a support agent for ticket API tests."""

    user = User(
        name="Ticket Test Agent",
        email="ticketagent@example.test",
        password_hash=hash_password("StrongPassword123!"),
    )

    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    return user


@pytest.fixture
def auth_headers(
    client: TestClient,
    api_user: User,
) -> dict[str, str]:
    """Authenticate the agent and return authorization headers."""

    login_response = client.post(
        "/api/auth/login",
        data={
            "username": "ticketagent@example.test",
            "password": "StrongPassword123!",
        },
    )

    assert login_response.status_code == 200

    token = login_response.json()["access_token"]

    return {
        "Authorization": f"Bearer {token}"
    }


def test_create_ticket_success(
    client: TestClient,
    db_session: Session,
    api_user: User,
    auth_headers: dict[str, str],
) -> None:
    """Verify that an authenticated agent can create a ticket."""

    payload = {
        "title": "Unable to access my account",
        "description": (
            "I cannot log in even after resetting my password."
        ),
        "requester_name": "John Smith",
        "priority": "High",
    }

    response = client.post(
        "/api/tickets",
        json=payload,
        headers=auth_headers,
    )

    assert response.status_code == 201

    data = response.json()

    assert data["title"] == payload["title"]
    assert data["description"] == payload["description"]
    assert data["requester_name"] == "John Smith"
    assert data["priority"] == "High"
    assert data["status"] == "Open"
    assert data["created_by_user_id"] == api_user.id

    # Verify that the ticket was actually stored in the database.
    saved_ticket = db_session.get(Ticket, data["id"])

    assert saved_ticket is not None
    assert saved_ticket.title == payload["title"]
    assert saved_ticket.created_by_user_id == api_user.id


def test_create_ticket_requires_authentication(
    client: TestClient,
) -> None:
    """Verify that unauthenticated users cannot create tickets."""

    response = client.post(
        "/api/tickets",
        json={
            "title": "Unable to access my account",
            "description": "I cannot log in to the application.",
            "priority": "High",
        },
    )

    assert response.status_code == 401


def test_create_ticket_rejects_invalid_payload(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Verify that invalid ticket data is rejected."""

    response = client.post(
        "/api/tickets",
        json={
            "title": "Hi",
            "description": "I cannot log in.",
            "priority": "High",
        },
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_create_ticket_rejects_client_supplied_user_id(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Prevent clients from choosing the ticket creator."""

    response = client.post(
        "/api/tickets",
        json={
            "title": "Unable to access my account",
            "description": "I cannot log in to the application.",
            "priority": "High",
            "created_by_user_id": 999,
        },
        headers=auth_headers,
    )

    assert response.status_code == 422



@pytest.fixture
def sample_tickets(
    db_session: Session,
    api_user: User,
) -> list[Ticket]:
    """Create sample tickets for listing API tests."""

    tickets = [
        create_ticket(
            db_session,
            creator=api_user,
            title="Cannot access account",
            description="Unable to sign in after password reset.",
            requester_name="John Smith",
            category="Technical",
            priority="High",
            status="Open",
        ),
        create_ticket(
            db_session,
            creator=api_user,
            title="Payment failed",
            description="Checkout returns a declined payment message.",
            requester_name="Maya Patel",
            category="Billing",
            priority="Low",
            status="Resolved",
        ),
        create_ticket(
            db_session,
            creator=api_user,
            title="Mobile login issue",
            description="The login page freezes on mobile.",
            requester_name="Alex Smith",
            category="Technical",
            priority="Medium",
            status="In Progress",
        ),
    ]

    return tickets




def test_list_tickets_requires_authentication(
    client: TestClient,
) -> None:
    """Verify that listing tickets requires authentication."""

    response = client.get("/api/tickets")

    assert response.status_code == 401


def test_list_tickets_success(
    client: TestClient,
    sample_tickets: list[Ticket],
    auth_headers: dict[str, str],
) -> None:
    """Verify that an authenticated agent can retrieve tickets."""

    response = client.get(
        "/api/tickets",
        headers=auth_headers,
    )

    assert response.status_code == 200

    data = response.json()

    assert len(data) == 3

    # Tickets should be returned newest first.
    expected_ids = [
        ticket.id for ticket in reversed(sample_tickets)
    ]

    actual_ids = [ticket["id"] for ticket in data]

    assert actual_ids == expected_ids


def test_list_tickets_with_filters(
    client: TestClient,
    sample_tickets: list[Ticket],
    auth_headers: dict[str, str],
) -> None:
    """Verify filtering by status, priority and category."""

    response = client.get(
        "/api/tickets",
        params={
            "status": "Open",
            "priority": "High",
            "category": "Technical",
        },
        headers=auth_headers,
    )

    assert response.status_code == 200

    data = response.json()

    assert len(data) == 1
    assert data[0]["id"] == sample_tickets[0].id


def test_list_tickets_with_search(
    client: TestClient,
    sample_tickets: list[Ticket],
    auth_headers: dict[str, str],
) -> None:
    """Verify searching ticket titles and descriptions."""

    response = client.get(
        "/api/tickets",
        params={"search": "freezes"},
        headers=auth_headers,
    )

    assert response.status_code == 200

    data = response.json()

    assert len(data) == 1
    assert data[0]["id"] == sample_tickets[2].id


def test_list_tickets_with_pagination(
    client: TestClient,
    sample_tickets: list[Ticket],
    auth_headers: dict[str, str],
) -> None:
    """Verify pagination using skip and limit."""

    response = client.get(
        "/api/tickets",
        params={
            "skip": 1,
            "limit": 1,
        },
        headers=auth_headers,
    )

    assert response.status_code == 200

    data = response.json()

    assert len(data) == 1
    assert data[0]["id"] == sample_tickets[-2].id


@pytest.mark.parametrize(
    "params",
    [
        {"skip": -1},
        {"limit": 101},
        {"status": "Pending"},
        {"priority": "Urgent"},
    ],
)
def test_list_tickets_rejects_invalid_parameters(
    client: TestClient,
    auth_headers: dict[str, str],
    params: dict[str, object],
) -> None:
    """Verify that invalid query parameters are rejected."""

    response = client.get(
        "/api/tickets",
        params=params,
        headers=auth_headers,
    )

    assert response.status_code == 422



def test_get_ticket_by_id_success(
    client: TestClient,
    sample_tickets: list[Ticket],
    auth_headers: dict[str, str],
) -> None:
    """Verify retrieving an existing ticket."""

    ticket = sample_tickets[0]

    response = client.get(
        f"/api/tickets/{ticket.id}",
        headers=auth_headers,
    )

    assert response.status_code == 200

    data = response.json()

    assert data["id"] == ticket.id
    assert data["title"] == ticket.title
    assert data["requester_name"] == ticket.requester_name
    assert data["priority"] == ticket.priority
    assert data["status"] == ticket.status


def test_get_ticket_by_id_not_found(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Verify that a nonexistent ticket returns 404."""

    response = client.get(
        "/api/tickets/9999",
        headers=auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Ticket not found."


def test_get_ticket_by_id_requires_authentication(
    client: TestClient,
    sample_tickets: list[Ticket],
) -> None:
    """Verify that ticket retrieval requires authentication."""

    ticket = sample_tickets[0]

    response = client.get(
        f"/api/tickets/{ticket.id}",
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "ticket_id",
    [0, -1],
)
def test_get_ticket_by_id_rejects_invalid_id(
    client: TestClient,
    auth_headers: dict[str, str],
    ticket_id: int,
) -> None:
    """Verify that ticket IDs must be positive integers."""

    response = client.get(
        f"/api/tickets/{ticket_id}",
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_update_ticket_success(
    client: TestClient,
    sample_tickets: list[Ticket],
    auth_headers: dict[str, str],
) -> None:
    """Verify that an agent can update a ticket."""

    ticket = sample_tickets[0]

    response = client.patch(
        f"/api/tickets/{ticket.id}",
        json={
            "status": "In Progress",
            "priority": "Medium",
        },
        headers=auth_headers,
    )

    assert response.status_code == 200

    data = response.json()

    assert data["id"] == ticket.id
    assert data["status"] == "In Progress"
    assert data["priority"] == "Medium"


def test_update_ticket_partial_update(
    client: TestClient,
    sample_tickets: list[Ticket],
    auth_headers: dict[str, str],
) -> None:
    """Verify that updating one field preserves other fields."""

    ticket = sample_tickets[0]

    response = client.patch(
        f"/api/tickets/{ticket.id}",
        json={
            "status": "Resolved",
        },
        headers=auth_headers,
    )

    assert response.status_code == 200

    data = response.json()

    assert data["status"] == "Resolved"
    assert data["title"] == ticket.title
    assert data["priority"] == ticket.priority


def test_update_ticket_not_found(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Verify updating a nonexistent ticket returns 404."""

    response = client.patch(
        "/api/tickets/9999",
        json={"status": "Resolved"},
        headers=auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Ticket not found."


def test_update_ticket_requires_authentication(
    client: TestClient,
    sample_tickets: list[Ticket],
) -> None:
    """Verify that updating a ticket requires authentication."""

    ticket = sample_tickets[0]

    response = client.patch(
        f"/api/tickets/{ticket.id}",
        json={"status": "Resolved"},
    )

    assert response.status_code == 401


def test_update_ticket_rejects_empty_payload(
    client: TestClient,
    sample_tickets: list[Ticket],
    auth_headers: dict[str, str],
) -> None:
    """Verify that an empty update request is rejected."""

    ticket = sample_tickets[0]

    response = client.patch(
        f"/api/tickets/{ticket.id}",
        json={},
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_update_ticket_rejects_invalid_status(
    client: TestClient,
    sample_tickets: list[Ticket],
    auth_headers: dict[str, str],
) -> None:
    """Verify that unsupported ticket statuses are rejected."""

    ticket = sample_tickets[0]

    response = client.patch(
        f"/api/tickets/{ticket.id}",
        json={"status": "Pending"},
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_update_ticket_rejects_protected_fields(
    client: TestClient,
    sample_tickets: list[Ticket],
    auth_headers: dict[str, str],
) -> None:
    """Prevent clients from changing the ticket creator."""

    ticket = sample_tickets[0]

    response = client.patch(
        f"/api/tickets/{ticket.id}",
        json={"created_by_user_id": 999},
        headers=auth_headers,
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "ticket_id",
    [0, -1],
)
def test_update_ticket_rejects_invalid_id(
    client: TestClient,
    auth_headers: dict[str, str],
    ticket_id: int,
) -> None:
    """Verify that ticket IDs must be positive integers."""

    response = client.patch(
        f"/api/tickets/{ticket_id}",
        json={"status": "Resolved"},
        headers=auth_headers,
    )

    assert response.status_code == 422



def test_delete_ticket_success(
    client: TestClient,
    db_session: Session,
    sample_tickets: list[Ticket],
    auth_headers: dict[str, str],
) -> None:
    """Verify that an agent can delete an existing ticket."""

    ticket = sample_tickets[0]
    ticket_id = ticket.id

    response = client.delete(
        f"/api/tickets/{ticket_id}",
        headers=auth_headers,
    )

    assert response.status_code == 204
    assert response.content == b""

    # Verify that the ticket was actually removed from the database.
    deleted_ticket = db_session.get(Ticket, ticket_id)

    assert deleted_ticket is None


def test_delete_ticket_not_found(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Verify that deleting a nonexistent ticket returns 404."""

    response = client.delete(
        "/api/tickets/9999",
        headers=auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Ticket not found."


def test_delete_ticket_requires_authentication(
    client: TestClient,
    sample_tickets: list[Ticket],
) -> None:
    """Verify that deleting a ticket requires authentication."""

    ticket = sample_tickets[0]

    response = client.delete(
        f"/api/tickets/{ticket.id}",
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "ticket_id",
    [0, -1],
)
def test_delete_ticket_rejects_invalid_id(
    client: TestClient,
    auth_headers: dict[str, str],
    ticket_id: int,
) -> None:
    """Verify that ticket IDs must be positive integers."""

    response = client.delete(
        f"/api/tickets/{ticket_id}",
        headers=auth_headers,
    )
    assert response.status_code == 422
