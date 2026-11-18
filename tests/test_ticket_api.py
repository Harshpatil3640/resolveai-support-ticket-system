from collections.abc import Callable

import pytest

from fastapi.testclient import TestClient
from app.services.ticket_service import create_ticket
from sqlalchemy.orm import Session

from app.models import Ticket, User


# ``client``, role users and their auth headers come from conftest.py.


@pytest.fixture
def api_user(agent_user: User) -> User:
    """Support agent used by the original ticket API tests."""

    return agent_user


@pytest.fixture
def auth_headers(agent_headers: dict[str, str]) -> dict[str, str]:
    """Authorization headers for the support agent."""

    return agent_headers


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
    admin_headers: dict[str, str],
) -> None:
    """Verify that an admin can delete an existing ticket."""

    ticket = sample_tickets[0]
    ticket_id = ticket.id

    response = client.delete(
        f"/api/tickets/{ticket_id}",
        headers=admin_headers,
    )

    assert response.status_code == 204
    assert response.content == b""

    # Verify that the ticket was actually removed from the database.
    deleted_ticket = db_session.get(Ticket, ticket_id)

    assert deleted_ticket is None


def test_delete_ticket_not_found(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    """Verify that an admin deleting a nonexistent ticket gets 404."""

    response = client.delete(
        "/api/tickets/9999",
        headers=admin_headers,
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
    admin_headers: dict[str, str],
    ticket_id: int,
) -> None:
    """Verify that ticket IDs must be positive integers."""

    response = client.delete(
        f"/api/tickets/{ticket_id}",
        headers=admin_headers,
    )
    assert response.status_code == 422


def test_create_ticket_saves_ai_prediction(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that an AI prediction is saved separately from final category."""

    def mock_predict_category(description: str) -> tuple[str, float]:
        assert description == "My card payment was declined."
        return "PAYMENT", 0.97

    monkeypatch.setattr(
        "app.api.routes.tickets.predict_category",
        mock_predict_category,
    )

    payload = {
        "title": "Card payment failed",
        "description": "My card payment was declined.",
        "priority": "High",
    }

    response = client.post(
        "/api/tickets",
        json=payload,
        headers=auth_headers,
    )

    assert response.status_code == 201

    data = response.json()

    assert data["predicted_category"] == "PAYMENT"
    assert data["prediction_confidence"] == pytest.approx(0.97)

    # AI prediction must not automatically set the final category.
    assert data["category"] is None

    saved_ticket = db_session.get(Ticket, data["id"])

    assert saved_ticket is not None
    assert saved_ticket.predicted_category == "PAYMENT"
    assert saved_ticket.category is None
    assert saved_ticket.prediction_confidence == pytest.approx(0.97)


def test_create_ticket_succeeds_when_ai_prediction_fails(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that prediction failure does not prevent ticket creation."""

    def mock_failed_prediction(description: str):
        raise RuntimeError("ML model unavailable")

    monkeypatch.setattr(
        "app.api.routes.tickets.predict_category",
        mock_failed_prediction,
    )

    payload = {
        "title": "Unable to complete checkout",
        "description": "My checkout process is failing.",
        "priority": "Medium",
    }

    response = client.post(
        "/api/tickets",
        json=payload,
        headers=auth_headers,
    )

    assert response.status_code == 201

    data = response.json()

    # Ticket should be created despite prediction failure.
    assert data["title"] == payload["title"]
    assert data["predicted_category"] is None
    assert data["prediction_confidence"] is None
    assert data["category"] is None

    saved_ticket = db_session.get(Ticket, data["id"])

    assert saved_ticket is not None
    assert saved_ticket.title == payload["title"]
    assert saved_ticket.predicted_category is None
    assert saved_ticket.prediction_confidence is None
    assert saved_ticket.category is None


def test_agent_can_correct_category_without_changing_ai_prediction(
    client: TestClient,
    db_session: Session,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that agent corrections preserve the original AI prediction."""

    monkeypatch.setattr(
        "app.api.routes.tickets.predict_category",
        lambda description: ("PAYMENT", 0.95),
    )

    # Step 1: Create ticket with AI prediction.
    create_response = client.post(
        "/api/tickets",
        json={
            "title": "Order cancellation request",
            "description": "I want to cancel my recent order.",
            "priority": "Medium",
        },
        headers=auth_headers,
    )

    assert create_response.status_code == 201

    created_ticket = create_response.json()
    ticket_id = created_ticket["id"]

    assert created_ticket["predicted_category"] == "PAYMENT"
    assert created_ticket["category"] is None

    # Step 2: Agent corrects the final category.
    update_response = client.patch(
        f"/api/tickets/{ticket_id}",
        json={
            "category": "ORDER",
        },
        headers=auth_headers,
    )

    assert update_response.status_code == 200

    updated_ticket = update_response.json()

    # Original AI prediction must remain unchanged.
    assert updated_ticket["predicted_category"] == "PAYMENT"
    assert updated_ticket["prediction_confidence"] == pytest.approx(0.95)

    # Human-assigned category should contain the correction.
    assert updated_ticket["category"] == "ORDER"

    # Step 3: Verify persisted database values.
    saved_ticket = db_session.get(Ticket, ticket_id)

    assert saved_ticket is not None
    assert saved_ticket.predicted_category == "PAYMENT"
    assert saved_ticket.category == "ORDER"


# ---------------------------------------------------------------------------
# Role-based access control
# ---------------------------------------------------------------------------


@pytest.fixture
def customer_tickets(
    db_session: Session,
    customer_user: User,
) -> list[Ticket]:
    """Tickets owned by the primary customer, oldest first."""

    return [
        create_ticket(
            db_session,
            creator=customer_user,
            title="Payment declined",
            description="My card payment was declined.",
            category="Billing",
            priority="High",
            status="Open",
        ),
        create_ticket(
            db_session,
            creator=customer_user,
            title="Parcel missing",
            description="My parcel never arrived.",
            category="Delivery",
            priority="Low",
            status="Resolved",
        ),
        create_ticket(
            db_session,
            creator=customer_user,
            title="Refund pending",
            description="Still waiting on a payment refund.",
            category="Billing",
            priority="Medium",
            status="In Progress",
        ),
    ]


@pytest.fixture
def other_customer_tickets(
    db_session: Session,
    second_customer_user: User,
) -> list[Ticket]:
    """Tickets owned by a different customer.

    These deliberately match the same filters as ``customer_tickets``
    so ownership isolation is tested alongside every filter.
    """

    return [
        create_ticket(
            db_session,
            creator=second_customer_user,
            title="Payment error",
            description="The payment page shows an error.",
            category="Billing",
            priority="High",
            status="Open",
        ),
        create_ticket(
            db_session,
            creator=second_customer_user,
            title="Late delivery",
            description="My delivery is late and the parcel is missing.",
            category="Delivery",
            priority="Low",
            status="Resolved",
        ),
    ]


# --- Customer ---------------------------------------------------------------


def test_customer_can_create_ticket(
    client: TestClient,
    db_session: Session,
    customer_user: User,
    customer_headers: dict[str, str],
) -> None:
    """Customers can create tickets and become the ticket creator."""

    response = client.post(
        "/api/tickets",
        json={
            "title": "Cannot reset password",
            "description": "The reset email never arrives.",
            "priority": "Medium",
        },
        headers=customer_headers,
    )

    assert response.status_code == 201

    data = response.json()

    assert data["created_by_user_id"] == customer_user.id

    saved_ticket = db_session.get(Ticket, data["id"])

    assert saved_ticket is not None
    assert saved_ticket.created_by_user_id == customer_user.id


def test_customer_created_ticket_is_associated_with_customer(
    client: TestClient,
    customer_user: User,
    customer_headers: dict[str, str],
    second_customer_headers: dict[str, str],
) -> None:
    """A new ticket is visible to its creator and hidden from others."""

    create_response = client.post(
        "/api/tickets",
        json={
            "title": "Order arrived damaged",
            "description": "The box was crushed on arrival.",
        },
        headers=customer_headers,
    )

    assert create_response.status_code == 201

    ticket_id = create_response.json()["id"]

    own_list = client.get("/api/tickets", headers=customer_headers).json()

    assert [ticket["id"] for ticket in own_list] == [ticket_id]
    assert own_list[0]["created_by_user_id"] == customer_user.id

    other_list = client.get(
        "/api/tickets",
        headers=second_customer_headers,
    ).json()

    assert other_list == []


def test_customer_cannot_supply_creator_id(
    client: TestClient,
    customer_headers: dict[str, str],
    second_customer_user: User,
) -> None:
    """Customers cannot create tickets on behalf of another user."""

    response = client.post(
        "/api/tickets",
        json={
            "title": "Spoofed ticket",
            "description": "Trying to impersonate someone.",
            "created_by_user_id": second_customer_user.id,
        },
        headers=customer_headers,
    )

    assert response.status_code == 422


def test_customer_list_returns_only_own_tickets(
    client: TestClient,
    sample_tickets: list[Ticket],
    customer_user: User,
    customer_tickets: list[Ticket],
    other_customer_tickets: list[Ticket],
    customer_headers: dict[str, str],
) -> None:
    """Customer listings exclude staff and other customers' tickets."""

    response = client.get("/api/tickets", headers=customer_headers)

    assert response.status_code == 200

    data = response.json()

    assert [ticket["id"] for ticket in data] == [
        ticket.id for ticket in reversed(customer_tickets)
    ]
    assert all(
        ticket["created_by_user_id"] == customer_user.id
        for ticket in data
    )


def test_customer_cannot_widen_listing_via_query_parameter(
    client: TestClient,
    customer_tickets: list[Ticket],
    other_customer_tickets: list[Ticket],
    second_customer_user: User,
    customer_headers: dict[str, str],
) -> None:
    """A client-supplied owner ID in the query string is ignored."""

    response = client.get(
        "/api/tickets",
        params={"created_by_user_id": second_customer_user.id},
        headers=customer_headers,
    )

    assert response.status_code == 200

    returned_ids = {ticket["id"] for ticket in response.json()}

    assert returned_ids == {ticket.id for ticket in customer_tickets}


@pytest.mark.parametrize(
    ("params", "expected_indexes"),
    [
        ({"status": "Open"}, [0]),
        ({"priority": "High"}, [0]),
        ({"category": "Billing"}, [2, 0]),
        ({"search": "payment"}, [2, 0]),
        ({"search": "parcel"}, [1]),
        ({"status": "Resolved", "category": "Delivery"}, [1]),
        ({"skip": 1, "limit": 1}, [1]),
        ({"category": "Billing", "search": "payment", "limit": 1}, [2]),
        ({"category": "Billing", "skip": 2}, []),
    ],
)
def test_customer_ownership_filter_combines_with_query_filters(
    client: TestClient,
    customer_tickets: list[Ticket],
    other_customer_tickets: list[Ticket],
    customer_headers: dict[str, str],
    params: dict[str, object],
    expected_indexes: list[int],
) -> None:
    """Ownership scoping applies together with every listing filter."""

    response = client.get(
        "/api/tickets",
        params=params,
        headers=customer_headers,
    )

    assert response.status_code == 200

    assert [ticket["id"] for ticket in response.json()] == [
        customer_tickets[index].id for index in expected_indexes
    ]


def test_customer_can_retrieve_own_ticket(
    client: TestClient,
    customer_tickets: list[Ticket],
    customer_headers: dict[str, str],
) -> None:
    """Customers can retrieve a ticket they created."""

    ticket = customer_tickets[0]

    response = client.get(
        f"/api/tickets/{ticket.id}",
        headers=customer_headers,
    )

    assert response.status_code == 200
    assert response.json()["id"] == ticket.id
    assert response.json()["title"] == ticket.title


def test_customer_cannot_view_another_customers_ticket(
    client: TestClient,
    other_customer_tickets: list[Ticket],
    customer_headers: dict[str, str],
) -> None:
    """Another customer's ticket is never returned to a customer."""

    response = client.get(
        f"/api/tickets/{other_customer_tickets[0].id}",
        headers=customer_headers,
    )

    assert response.status_code != 200
    assert "title" not in response.json()


def test_customer_gets_404_for_another_customers_ticket(
    client: TestClient,
    other_customer_tickets: list[Ticket],
    customer_headers: dict[str, str],
) -> None:
    """A non-owned ticket looks exactly like a nonexistent ticket."""

    not_owned = client.get(
        f"/api/tickets/{other_customer_tickets[0].id}",
        headers=customer_headers,
    )
    missing = client.get(
        "/api/tickets/9999",
        headers=customer_headers,
    )

    assert not_owned.status_code == 404
    assert not_owned.json() == {"detail": "Ticket not found."}

    assert missing.status_code == not_owned.status_code
    assert missing.json() == not_owned.json()


def test_customer_cannot_view_staff_created_ticket(
    client: TestClient,
    sample_tickets: list[Ticket],
    customer_headers: dict[str, str],
) -> None:
    """Customers cannot retrieve tickets created by support staff."""

    response = client.get(
        f"/api/tickets/{sample_tickets[0].id}",
        headers=customer_headers,
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Ticket not found."}


@pytest.mark.parametrize("owned", [True, False])
def test_customer_cannot_update_ticket(
    client: TestClient,
    db_session: Session,
    customer_tickets: list[Ticket],
    other_customer_tickets: list[Ticket],
    customer_headers: dict[str, str],
    owned: bool,
) -> None:
    """Customers receive 403 when updating any ticket, even their own."""

    ticket = customer_tickets[0] if owned else other_customer_tickets[0]

    response = client.patch(
        f"/api/tickets/{ticket.id}",
        json={"status": "Resolved"},
        headers=customer_headers,
    )

    assert response.status_code == 403

    db_session.refresh(ticket)

    assert ticket.status == "Open"


@pytest.mark.parametrize("owned", [True, False])
def test_customer_cannot_delete_ticket(
    client: TestClient,
    db_session: Session,
    customer_tickets: list[Ticket],
    other_customer_tickets: list[Ticket],
    customer_headers: dict[str, str],
    owned: bool,
) -> None:
    """Customers receive 403 when deleting any ticket, even their own."""

    ticket = customer_tickets[0] if owned else other_customer_tickets[0]
    ticket_id = ticket.id

    response = client.delete(
        f"/api/tickets/{ticket_id}",
        headers=customer_headers,
    )

    assert response.status_code == 403
    assert db_session.get(Ticket, ticket_id) is not None


# --- Agent ------------------------------------------------------------------


def test_agent_can_list_all_tickets(
    client: TestClient,
    sample_tickets: list[Ticket],
    customer_tickets: list[Ticket],
    other_customer_tickets: list[Ticket],
    agent_headers: dict[str, str],
) -> None:
    """Agents see the whole shared support queue."""

    response = client.get("/api/tickets", headers=agent_headers)

    assert response.status_code == 200

    expected_ids = {
        ticket.id
        for ticket in sample_tickets + customer_tickets + other_customer_tickets
    }

    assert {ticket["id"] for ticket in response.json()} == expected_ids


def test_agent_can_retrieve_customer_ticket(
    client: TestClient,
    customer_tickets: list[Ticket],
    agent_headers: dict[str, str],
) -> None:
    """Agents can retrieve tickets created by customers."""

    ticket = customer_tickets[0]

    response = client.get(
        f"/api/tickets/{ticket.id}",
        headers=agent_headers,
    )

    assert response.status_code == 200
    assert response.json()["id"] == ticket.id


def test_agent_can_update_customer_ticket(
    client: TestClient,
    customer_user: User,
    customer_tickets: list[Ticket],
    agent_headers: dict[str, str],
) -> None:
    """Agents can update customer tickets without changing ownership."""

    ticket = customer_tickets[0]

    response = client.patch(
        f"/api/tickets/{ticket.id}",
        json={"status": "In Progress", "category": "Payments"},
        headers=agent_headers,
    )

    assert response.status_code == 200

    data = response.json()

    assert data["status"] == "In Progress"
    assert data["category"] == "Payments"
    assert data["created_by_user_id"] == customer_user.id


def test_agent_cannot_change_ticket_owner(
    client: TestClient,
    db_session: Session,
    customer_user: User,
    customer_tickets: list[Ticket],
    agent_user: User,
    agent_headers: dict[str, str],
) -> None:
    """Agents cannot reassign the protected creator field."""

    ticket = customer_tickets[0]

    response = client.patch(
        f"/api/tickets/{ticket.id}",
        json={"created_by_user_id": agent_user.id},
        headers=agent_headers,
    )

    assert response.status_code == 422

    db_session.refresh(ticket)

    assert ticket.created_by_user_id == customer_user.id


def test_agent_cannot_delete_ticket(
    client: TestClient,
    db_session: Session,
    sample_tickets: list[Ticket],
    agent_headers: dict[str, str],
) -> None:
    """Agents receive 403 when deleting tickets."""

    ticket_id = sample_tickets[0].id

    response = client.delete(
        f"/api/tickets/{ticket_id}",
        headers=agent_headers,
    )

    assert response.status_code == 403
    assert db_session.get(Ticket, ticket_id) is not None


# --- Admin ------------------------------------------------------------------


def test_admin_can_list_all_tickets(
    client: TestClient,
    sample_tickets: list[Ticket],
    customer_tickets: list[Ticket],
    other_customer_tickets: list[Ticket],
    admin_headers: dict[str, str],
) -> None:
    """Admins see every ticket."""

    response = client.get("/api/tickets", headers=admin_headers)

    assert response.status_code == 200

    expected_ids = {
        ticket.id
        for ticket in sample_tickets + customer_tickets + other_customer_tickets
    }

    assert {ticket["id"] for ticket in response.json()} == expected_ids


def test_admin_can_retrieve_customer_ticket(
    client: TestClient,
    customer_tickets: list[Ticket],
    admin_headers: dict[str, str],
) -> None:
    """Admins can retrieve tickets created by customers."""

    ticket = customer_tickets[0]

    response = client.get(
        f"/api/tickets/{ticket.id}",
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["id"] == ticket.id


def test_admin_can_update_ticket(
    client: TestClient,
    customer_user: User,
    customer_tickets: list[Ticket],
    admin_headers: dict[str, str],
) -> None:
    """Admins can update tickets."""

    ticket = customer_tickets[0]

    response = client.patch(
        f"/api/tickets/{ticket.id}",
        json={"priority": "Low", "status": "Resolved"},
        headers=admin_headers,
    )

    assert response.status_code == 200

    data = response.json()

    assert data["priority"] == "Low"
    assert data["status"] == "Resolved"
    assert data["created_by_user_id"] == customer_user.id


def test_admin_can_delete_customer_ticket(
    client: TestClient,
    db_session: Session,
    customer_tickets: list[Ticket],
    admin_headers: dict[str, str],
) -> None:
    """Admins can delete tickets created by any user."""

    ticket_id = customer_tickets[0].id

    response = client.delete(
        f"/api/tickets/{ticket_id}",
        headers=admin_headers,
    )

    assert response.status_code == 204
    assert db_session.get(Ticket, ticket_id) is None


# --- Unknown / malformed roles ---------------------------------------------


@pytest.mark.parametrize(
    "stored_role",
    ["superuser", "Admin", "", "agent "],
)
@pytest.mark.parametrize(
    ("method", "path_template", "body"),
    [
        (
            "POST",
            "/api/tickets",
            {"title": "Role probe", "description": "Should be denied."},
        ),
        ("GET", "/api/tickets", None),
        ("GET", "/api/tickets/{ticket_id}", None),
        ("PATCH", "/api/tickets/{ticket_id}", {"status": "Resolved"}),
        ("DELETE", "/api/tickets/{ticket_id}", None),
    ],
)
def test_unknown_role_cannot_access_ticket_apis(
    client: TestClient,
    db_session: Session,
    create_user: Callable[..., User],
    login_headers: Callable[..., dict[str, str]],
    sample_tickets: list[Ticket],
    stored_role: str,
    method: str,
    path_template: str,
    body: dict[str, str] | None,
) -> None:
    """Users with unrecognised stored roles are forbidden everywhere."""

    user = create_user(email="unknown@example.test", role=stored_role)
    headers = login_headers(user.email)

    ticket = sample_tickets[0]
    ticket_count = db_session.query(Ticket).count()

    response = client.request(
        method,
        path_template.format(ticket_id=ticket.id),
        json=body,
        headers=headers,
    )

    assert response.status_code == 403

    db_session.refresh(ticket)

    assert ticket.status == "Open"
    assert db_session.query(Ticket).count() == ticket_count
