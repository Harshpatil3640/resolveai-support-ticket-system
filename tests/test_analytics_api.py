from collections.abc import Callable

import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Ticket, User
from app.services.ticket_service import create_ticket


# ``client``, role users and their auth headers come from conftest.py.

ANALYTICS_ENDPOINTS = [
    "/api/analytics/summary",
    "/api/analytics/operations",
    "/api/analytics/model-performance",
]


@pytest.fixture
def api_user(agent_user: User) -> User:
    """Support agent used by the original analytics API tests."""

    return agent_user


@pytest.fixture
def auth_headers(agent_headers: dict[str, str]) -> dict[str, str]:
    """Authorization headers for the support agent."""

    return agent_headers



@pytest.fixture
def sample_tickets(
    db_session: Session,
    api_user: User,
) -> list[Ticket]:
    """Create tickets covering all statuses and analytics categories."""

    ticket_data = [
        {
            "title": "Unable to access account",
            "description": "Customer cannot log in.",
            "status": "Open",
            "priority": "High",
            "category": "ACCOUNT",
            "predicted_category": "ACCOUNT",
            "created_at": datetime(2026, 11, 15, 10, tzinfo=timezone.utc),
        },
        {
            "title": "Payment issue",
            "description": "Customer payment is pending.",
            "status": "Open",
            "priority": "Medium",
            "category": "ORDER",
            "predicted_category": "PAYMENT",
            "created_at": datetime(2026, 11, 15, 14, tzinfo=timezone.utc),
        },
        {
            "title": "Order delivery delay",
            "description": "Customer order is delayed.",
            "status": "In Progress",
            "priority": "High",
            "category": "DELIVERY",
            "predicted_category": "DELIVERY",
            "created_at": datetime(2026, 11, 16, 11, tzinfo=timezone.utc),
        },
        {
            "title": "Refund completed",
            "description": "Customer refund was processed.",
            "status": "Resolved",
            "priority": "Low",
            "category": "REFUND",
            "predicted_category": "REFUND",
            "created_at": datetime(2026, 11, 17, 9, tzinfo=timezone.utc),
        },
        {
            "title": "Invoice generated",
            "description": "Customer invoice is available.",
            "status": "Resolved",
            "priority": "Medium",
            "category": None,
            "predicted_category": None,
            "created_at": datetime(2026, 11, 17, 15, tzinfo=timezone.utc),
        },
    ]

    tickets = []

    for data in ticket_data:
        ticket = create_ticket(
            db_session,
            creator=api_user,
            title=data["title"],
            description=data["description"],
            status=data["status"],
            priority=data["priority"],
            category=data["category"],
            predicted_category=data["predicted_category"],
        )

        ticket.created_at = data["created_at"]

        tickets.append(ticket)

    db_session.commit()

    return tickets



def test_analytics_requires_authentication(
    client: TestClient,
) -> None:
    """Verify that analytics require authentication."""

    response = client.get("/api/analytics/summary")

    assert response.status_code == 401


def test_analytics_returns_zero_counts_for_empty_database(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Verify that an empty database returns zero counts."""

    response = client.get(
        "/api/analytics/summary",
        headers=auth_headers,
    )

    assert response.status_code == 200

    assert response.json() == {
        "total_tickets": 0,
        "open_tickets": 0,
        "in_progress_tickets": 0,
        "resolved_tickets": 0,
    }


def test_analytics_returns_correct_ticket_counts(
    client: TestClient,
    auth_headers: dict[str, str],
    sample_tickets: None,
) -> None:
    """Verify that ticket statistics match database records."""

    response = client.get(
        "/api/analytics/summary",
        headers=auth_headers,
    )

    assert response.status_code == 200

    data = response.json()

    assert data == {
        "total_tickets": 5,
        "open_tickets": 2,
        "in_progress_tickets": 1,
        "resolved_tickets": 2,
    }


def test_analytics_response_contains_expected_fields(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Verify the analytics response structure and value types."""

    response = client.get(
        "/api/analytics/summary",
        headers=auth_headers,
    )

    assert response.status_code == 200

    data = response.json()

    expected_fields = {
        "total_tickets",
        "open_tickets",
        "in_progress_tickets",
        "resolved_tickets",
    }

    assert set(data.keys()) == expected_fields

    for value in data.values():
        assert isinstance(value, int)
        assert value >= 0



def test_operations_analytics_requires_authentication(
    client: TestClient,
) -> None:
    """Verify operational analytics require authentication."""

    response = client.get("/api/analytics/operations")

    assert response.status_code == 401


def test_operations_analytics_handles_empty_database(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """Verify empty database analytics return empty distributions."""

    response = client.get(
        "/api/analytics/operations",
        headers=auth_headers,
    )

    assert response.status_code == 200

    data = response.json()

    assert data["summary"] == {
        "total_tickets": 0,
        "open_tickets": 0,
        "in_progress_tickets": 0,
        "resolved_tickets": 0,
    }

    assert data["category_distribution"] == []
    assert data["ai_prediction_distribution"] == []

    assert data["priority_distribution"] == [
        {"priority": "Low", "count": 0},
        {"priority": "Medium", "count": 0},
        {"priority": "High", "count": 0},
    ]

    assert data["ticket_trends"] == []


def test_operations_analytics_returns_confirmed_categories(
    client: TestClient,
    auth_headers: dict[str, str],
    sample_tickets: list[Ticket],
) -> None:
    """Verify category distribution uses agent-confirmed categories."""

    response = client.get(
        "/api/analytics/operations",
        headers=auth_headers,
    )

    assert response.status_code == 200

    data = response.json()

    assert data["category_distribution"] == [
        {"category": "ACCOUNT", "count": 1},
        {"category": "DELIVERY", "count": 1},
        {"category": "ORDER", "count": 1},
        {"category": "REFUND", "count": 1},
        {"category": "Unassigned", "count": 1},
    ]


def test_operations_analytics_returns_ai_predictions(
    client: TestClient,
    auth_headers: dict[str, str],
    sample_tickets: list[Ticket],
) -> None:
    """Verify AI prediction distribution is independent of final categories."""

    response = client.get(
        "/api/analytics/operations",
        headers=auth_headers,
    )

    assert response.status_code == 200

    data = response.json()

    assert data["ai_prediction_distribution"] == [
        {"category": "ACCOUNT", "count": 1},
        {"category": "DELIVERY", "count": 1},
        {"category": "PAYMENT", "count": 1},
        {"category": "REFUND", "count": 1},
        {"category": "Unavailable", "count": 1},
    ]


def test_operations_analytics_returns_priority_distribution(
    client: TestClient,
    auth_headers: dict[str, str],
    sample_tickets: list[Ticket],
) -> None:
    """Verify all priority levels are represented in analytics."""

    response = client.get(
        "/api/analytics/operations",
        headers=auth_headers,
    )

    assert response.status_code == 200

    data = response.json()

    assert data["priority_distribution"] == [
        {"priority": "Low", "count": 1},
        {"priority": "Medium", "count": 2},
        {"priority": "High", "count": 2},
    ]


def test_operations_analytics_returns_daily_ticket_trends(
    client: TestClient,
    auth_headers: dict[str, str],
    sample_tickets: list[Ticket],
) -> None:
    """Verify ticket creation counts are grouped by actual date."""

    response = client.get(
        "/api/analytics/operations",
        headers=auth_headers,
    )

    assert response.status_code == 200

    data = response.json()

    assert data["ticket_trends"] == [
        {"date": "2026-11-15", "count": 2},
        {"date": "2026-11-16", "count": 1},
        {"date": "2026-11-17", "count": 2},
    ]


def test_model_performance_requires_authentication(client):
    response = client.get("/api/analytics/model-performance")

    assert response.status_code == 401


def test_model_performance_returns_report(client, auth_headers):
    response = client.get(
        "/api/analytics/model-performance",
        headers=auth_headers,
    )

    assert response.status_code == 200

    data = response.json()

    assert data["model"] == "Character TF-IDF + Logistic Regression"
    assert data["training_records"] == 19708
    assert data["test_records"] == 4927

    assert data["accuracy"] > 0.99
    assert data["macro_f1"] > 0.99
    assert data["weighted_f1"] > 0.99

    assert len(data["category_metrics"]) == 11

    assert data["confusion_matrix"]["labels"] == [
        "ACCOUNT",
        "CANCEL",
        "CONTACT",
        "DELIVERY",
        "FEEDBACK",
        "INVOICE",
        "ORDER",
        "PAYMENT",
        "REFUND",
        "SHIPPING",
        "SUBSCRIPTION",
    ]

    assert len(data["confusion_matrix"]["values"]) == 11
    assert all(
        len(row) == 11
        for row in data["confusion_matrix"]["values"]
    )

# ---------------------------------------------------------------------------
# Role-based access control
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", ANALYTICS_ENDPOINTS)
def test_admin_can_access_analytics(
    client: TestClient,
    admin_headers: dict[str, str],
    endpoint: str,
) -> None:
    response = client.get(endpoint, headers=admin_headers)

    assert response.status_code == 200


@pytest.mark.parametrize("endpoint", ANALYTICS_ENDPOINTS)
def test_agent_can_access_analytics(
    client: TestClient,
    agent_headers: dict[str, str],
    endpoint: str,
) -> None:
    response = client.get(endpoint, headers=agent_headers)

    assert response.status_code == 200


@pytest.mark.parametrize("endpoint", ANALYTICS_ENDPOINTS)
def test_customer_cannot_access_analytics(
    client: TestClient,
    customer_headers: dict[str, str],
    endpoint: str,
) -> None:
    response = client.get(endpoint, headers=customer_headers)

    assert response.status_code == 403
    assert "total_tickets" not in response.text


@pytest.mark.parametrize("endpoint", ANALYTICS_ENDPOINTS)
def test_unauthenticated_cannot_access_analytics(
    client: TestClient,
    endpoint: str,
) -> None:
    response = client.get(endpoint)

    assert response.status_code == 401


@pytest.mark.parametrize("endpoint", ANALYTICS_ENDPOINTS)
def test_unknown_role_cannot_access_analytics(
    client: TestClient,
    create_user: Callable[..., User],
    login_headers: Callable[..., dict[str, str]],
    endpoint: str,
) -> None:
    user = create_user(email="unknown@example.test", role="superuser")

    response = client.get(endpoint, headers=login_headers(user.email))

    assert response.status_code == 403
