"""GET and POST /api/tickets/{ticket_id}/messages.

Uses conftest's in-memory database and role fixtures (the lock time-out
test uses a temporary SQLite file); resolveai.db is never used.
"""

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.db.base import Base
from app.db.database import get_db
from app.main import DATABASE_ERROR_DETAIL, app
from app.models import Ticket, TicketEvent, TicketMessage, User
from app.models.ticket_message import MAX_MESSAGE_LENGTH
from app.services.ticket_service import create_ticket


MESSAGE_FIELDS = {
    "id",
    "ticket_id",
    "sender_user_id",
    "sender_name",
    "sender_role",
    "content",
    "created_at",
}

FORBIDDEN_DETAIL = "You do not have permission to perform this action."
NOT_FOUND_DETAIL = "Ticket not found."
RESOLVED_DETAIL = (
    "This ticket is resolved. It must be reopened before another message "
    "can be sent."
)

# Distinctive content, so tests can prove it never reaches the logs.
SECRET_CONTENT = "Conversation-secret-4f1d my card ends 4242"


def _url(ticket_id: int) -> str:
    return f"/api/tickets/{ticket_id}/messages"


@pytest.fixture
def make_ticket(db_session: Session, customer_user: User) -> Callable[..., Ticket]:
    """A ticket created through the service, then given status/assignee."""

    def _make(
        *,
        creator: User | None = None,
        status: str = "Open",
        assignee: User | None = None,
    ) -> Ticket:
        ticket = create_ticket(
            db_session,
            creator=creator or customer_user,
            title="Payment failed",
            description="My card was declined.",
        )
        ticket.status = status
        ticket.assigned_to_user_id = assignee.id if assignee else None
        db_session.commit()
        db_session.refresh(ticket)

        return ticket

    return _make


@pytest.fixture
def other_agent_headers(
    create_user: Callable[..., User],
    login_headers: Callable[..., dict[str, str]],
) -> dict[str, str]:
    user = create_user(email="agent2@example.test", role="agent", name="Other Agent")

    return login_headers(user.email)


def _post(client: TestClient, ticket_id: int, headers: dict, content="Hello there"):
    return client.post(_url(ticket_id), json={"content": content}, headers=headers)


def _message_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(TicketMessage))


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def test_customer_sends_on_their_own_ticket(
    client: TestClient,
    make_ticket,
    customer_user: User,
    customer_headers: dict[str, str],
) -> None:
    ticket = make_ticket()

    response = _post(client, ticket.id, customer_headers, "  Any update?  ")

    assert response.status_code == 201
    body = response.json()

    assert set(body) == MESSAGE_FIELDS
    assert body["ticket_id"] == ticket.id
    assert body["sender_user_id"] == customer_user.id
    assert body["sender_name"] == "Test Customer"
    assert body["sender_role"] == "customer"
    assert body["content"] == "Any update?"
    # An explicit UTC timestamp.
    assert datetime.fromisoformat(body["created_at"]).utcoffset().total_seconds() == 0


def test_assigned_agent_sends(
    client: TestClient,
    make_ticket,
    agent_user: User,
    agent_headers: dict[str, str],
) -> None:
    ticket = make_ticket(status="In Progress", assignee=agent_user)

    response = _post(client, ticket.id, agent_headers)

    assert response.status_code == 201
    assert response.json()["sender_role"] == "agent"
    assert response.json()["sender_name"] == "Test Agent"


@pytest.mark.parametrize("assigned", [False, True], ids=["unassigned", "assigned"])
def test_admin_sends_on_any_unresolved_ticket(
    client: TestClient,
    make_ticket,
    agent_user: User,
    admin_headers: dict[str, str],
    assigned: bool,
) -> None:
    ticket = make_ticket(assignee=agent_user if assigned else None)

    response = _post(client, ticket.id, admin_headers)

    assert response.status_code == 201
    assert response.json()["sender_role"] == "admin"


def test_unassigned_agent_is_refused(
    client: TestClient,
    db_session: Session,
    make_ticket,
    agent_headers: dict[str, str],
) -> None:
    ticket = make_ticket()

    response = _post(client, ticket.id, agent_headers)

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert _message_count(db_session) == 0


def test_agent_not_assigned_to_the_ticket_is_refused(
    client: TestClient,
    db_session: Session,
    make_ticket,
    agent_user: User,
    other_agent_headers: dict[str, str],
) -> None:
    ticket = make_ticket(status="In Progress", assignee=agent_user)

    response = _post(client, ticket.id, other_agent_headers)

    assert response.status_code == 403
    assert _message_count(db_session) == 0


def test_agent_loses_assignment_and_can_no_longer_send(
    client: TestClient,
    db_session: Session,
    make_ticket,
    agent_user: User,
    agent_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    ticket = make_ticket(status="In Progress", assignee=agent_user)

    assert _post(client, ticket.id, agent_headers).status_code == 201

    unassign = client.put(
        f"/api/tickets/{ticket.id}/assignment",
        json={"assigned_to_user_id": None, "expected_version": ticket.version},
        headers=admin_headers,
    )

    assert unassign.status_code == 200
    assert _post(client, ticket.id, agent_headers).status_code == 403
    assert _message_count(db_session) == 1


@pytest.mark.parametrize("role", ["admin", "agent", "customer"])
def test_resolved_ticket_returns_409_for_everyone(
    request: pytest.FixtureRequest,
    client: TestClient,
    db_session: Session,
    make_ticket,
    agent_user: User,
    role: str,
) -> None:
    ticket = make_ticket(status="Resolved", assignee=agent_user)

    response = _post(client, ticket.id, request.getfixturevalue(f"{role}_headers"))

    assert response.status_code == 409
    assert response.json() == {"detail": RESOLVED_DETAIL}
    assert _message_count(db_session) == 0


def test_reopened_ticket_accepts_messages(
    client: TestClient,
    make_ticket,
    agent_user: User,
    agent_headers: dict[str, str],
    customer_headers: dict[str, str],
) -> None:
    ticket = make_ticket(status="Resolved", assignee=agent_user)

    assert _post(client, ticket.id, customer_headers).status_code == 409

    reopen = client.patch(
        f"/api/tickets/{ticket.id}/status",
        json={"status": "In Progress", "expected_version": ticket.version},
        headers=agent_headers,
    )

    assert reopen.status_code == 200
    assert _post(client, ticket.id, customer_headers).status_code == 201


def test_customer_posting_to_another_customers_ticket_gets_404(
    client: TestClient,
    db_session: Session,
    make_ticket,
    second_customer_user: User,
    customer_headers: dict[str, str],
) -> None:
    open_ticket = make_ticket(creator=second_customer_user)
    resolved_ticket = make_ticket(creator=second_customer_user, status="Resolved")

    for ticket in (open_ticket, resolved_ticket):
        response = _post(client, ticket.id, customer_headers)

        # Exactly the response for a missing ticket.
        assert response.status_code == 404
        assert response.json() == {"detail": NOT_FOUND_DETAIL}

    assert _post(client, 999_999, customer_headers).json() == {"detail": NOT_FOUND_DETAIL}
    assert _message_count(db_session) == 0


@pytest.mark.parametrize("role", ["admin", "agent", "customer"])
def test_missing_ticket_returns_404(
    request: pytest.FixtureRequest,
    client: TestClient,
    role: str,
) -> None:
    headers = request.getfixturevalue(f"{role}_headers")

    assert _post(client, 999_999, headers).status_code == 404
    assert client.get(_url(999_999), headers=headers).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"content": ""},
        {"content": "    "},
        {"content": "\n\t\r\n  "},
        {"content": "x" * (MAX_MESSAGE_LENGTH + 1)},
        {"content": 42},
        {"content": True},
        {"content": None},
        {"content": ["Hello"]},
        {"content": {"text": "Hello"}},
        {},
        {"content": "Hello", "sender_user_id": 1},
        {"content": "Hello", "sender_role": "admin"},
        {"content": "Hello", "ticket_id": 2},
        {"content": "Hello", "created_at": "2020-01-01T00:00:00Z"},
    ],
    ids=[
        "empty",
        "spaces",
        "whitespace",
        "too-long",
        "integer",
        "boolean",
        "null",
        "list",
        "object",
        "missing",
        "extra-sender-id",
        "extra-sender-role",
        "extra-ticket-id",
        "extra-created-at",
    ],
)
def test_invalid_bodies_return_422(
    client: TestClient,
    db_session: Session,
    make_ticket,
    customer_headers: dict[str, str],
    body: dict,
) -> None:
    ticket = make_ticket()

    response = client.post(_url(ticket.id), json=body, headers=customer_headers)

    assert response.status_code == 422
    assert _message_count(db_session) == 0


def test_non_json_body_returns_422(
    client: TestClient,
    make_ticket,
    customer_headers: dict[str, str],
) -> None:
    ticket = make_ticket()

    response = client.post(
        _url(ticket.id),
        content=b"just text",
        headers={**customer_headers, "Content-Type": "text/plain"},
    )

    assert response.status_code == 422


def test_maximum_length_message_is_accepted(
    client: TestClient,
    make_ticket,
    customer_headers: dict[str, str],
) -> None:
    ticket = make_ticket()
    content = "z" * MAX_MESSAGE_LENGTH

    response = _post(client, ticket.id, customer_headers, f"  {content}\n")

    assert response.status_code == 201
    assert response.json()["content"] == content


def test_sender_cannot_be_impersonated(
    client: TestClient,
    db_session: Session,
    make_ticket,
    admin_user: User,
    customer_user: User,
    customer_headers: dict[str, str],
) -> None:
    ticket = make_ticket()

    forged = client.post(
        _url(ticket.id),
        json={"content": "I am the admin", "sender_user_id": admin_user.id, "sender_role": "admin"},
        headers=customer_headers,
    )

    assert forged.status_code == 422
    assert _message_count(db_session) == 0

    genuine = _post(client, ticket.id, customer_headers, "I am the customer")
    stored = db_session.get(TicketMessage, genuine.json()["id"])

    assert (stored.sender_user_id, stored.sender_role) == (customer_user.id, "customer")


def test_messages_do_not_change_the_ticket_or_history(
    client: TestClient,
    db_session: Session,
    make_ticket,
    admin_headers: dict[str, str],
    customer_headers: dict[str, str],
) -> None:
    ticket = make_ticket()
    before = client.get(f"/api/tickets/{ticket.id}", headers=admin_headers).json()
    events = db_session.scalar(select(func.count()).select_from(TicketEvent))

    _post(client, ticket.id, customer_headers)
    _post(client, ticket.id, admin_headers)

    after = client.get(f"/api/tickets/{ticket.id}", headers=admin_headers).json()

    assert after == before  # including version and updated_at
    assert db_session.scalar(select(func.count()).select_from(TicketEvent)) == events
    history = client.get(f"/api/tickets/{ticket.id}/history", headers=admin_headers)
    assert [event["event_type"] for event in history.json()] == ["created"]


def test_html_like_content_is_returned_verbatim(
    client: TestClient,
    make_ticket,
    customer_headers: dict[str, str],
) -> None:
    ticket = make_ticket()
    content = '<script>alert("x")</script><img src=x onerror=alert(1)>'

    created = _post(client, ticket.id, customer_headers, content)
    listed = client.get(_url(ticket.id), headers=customer_headers)

    assert created.json()["content"] == content
    assert listed.json()[0]["content"] == content
    assert listed.headers["content-type"].startswith("application/json")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@pytest.fixture
def conversation(
    client: TestClient,
    make_ticket,
    agent_user: User,
    admin_headers: dict[str, str],
    agent_headers: dict[str, str],
    customer_headers: dict[str, str],
) -> tuple[Ticket, list[int]]:
    """A ticket assigned to the agent with three messages, one per role."""

    ticket = make_ticket(status="In Progress", assignee=agent_user)
    ids = [
        _post(client, ticket.id, headers, text).json()["id"]
        for headers, text in (
            (customer_headers, "First, from the customer"),
            (agent_headers, "Second, from the agent"),
            (admin_headers, "Third, from the admin"),
        )
    ]

    return ticket, ids


@pytest.mark.parametrize("role", ["admin", "agent", "customer"])
def test_participants_read_the_conversation_newest_first(
    request: pytest.FixtureRequest,
    client: TestClient,
    conversation,
    role: str,
) -> None:
    ticket, ids = conversation

    response = client.get(_url(ticket.id), headers=request.getfixturevalue(f"{role}_headers"))

    assert response.status_code == 200
    assert response.headers["X-Total-Count"] == "3"
    body = response.json()

    assert [message["id"] for message in body] == ids[::-1]
    assert [(m["sender_name"], m["sender_role"]) for m in body] == [
        ("Test Admin", "admin"),
        ("Test Agent", "agent"),
        ("Test Customer", "customer"),
    ]
    assert all(set(message) == MESSAGE_FIELDS for message in body)


def test_unassigned_agent_can_read_but_not_write(
    client: TestClient,
    conversation,
    other_agent_headers: dict[str, str],
) -> None:
    ticket, ids = conversation

    response = client.get(_url(ticket.id), headers=other_agent_headers)

    assert response.status_code == 200
    assert [message["id"] for message in response.json()] == ids[::-1]
    assert _post(client, ticket.id, other_agent_headers).status_code == 403


def test_resolved_conversation_remains_readable(
    client: TestClient,
    db_session: Session,
    conversation,
    request: pytest.FixtureRequest,
) -> None:
    ticket, ids = conversation
    ticket.status = "Resolved"
    db_session.commit()

    for role in ("admin", "agent", "customer"):
        response = client.get(_url(ticket.id), headers=request.getfixturevalue(f"{role}_headers"))

        assert response.status_code == 200
        assert len(response.json()) == 3


def test_customer_cannot_read_another_customers_conversation(
    client: TestClient,
    conversation,
    second_customer_headers: dict[str, str],
) -> None:
    ticket, _ids = conversation

    response = client.get(_url(ticket.id), headers=second_customer_headers)

    assert response.status_code == 404
    assert response.json() == {"detail": NOT_FOUND_DETAIL}
    assert "X-Total-Count" not in response.headers


def test_response_exposes_no_private_user_data(
    client: TestClient,
    conversation,
    customer_headers: dict[str, str],
) -> None:
    ticket, _ids = conversation

    text = client.get(_url(ticket.id), headers=customer_headers).text

    for fragment in ("@example.test", "argon2", "password", "token", "email", "assigned_to"):
        assert fragment not in text


def test_cursor_pagination(
    client: TestClient,
    make_ticket,
    customer_headers: dict[str, str],
) -> None:
    ticket = make_ticket()
    ids = [
        _post(client, ticket.id, customer_headers, f"message {number}").json()["id"]
        for number in range(5)
    ]

    first = client.get(_url(ticket.id), params={"limit": 2}, headers=customer_headers)
    second = client.get(
        _url(ticket.id),
        params={"limit": 2, "before_id": first.json()[-1]["id"]},
        headers=customer_headers,
    )
    third = client.get(
        _url(ticket.id),
        params={"limit": 2, "before_id": second.json()[-1]["id"]},
        headers=customer_headers,
    )
    past_the_start = client.get(
        _url(ticket.id),
        params={"before_id": ids[0]},
        headers=customer_headers,
    )

    assert [m["id"] for m in first.json()] == [ids[4], ids[3]]
    assert [m["id"] for m in second.json()] == [ids[2], ids[1]]
    assert [m["id"] for m in third.json()] == [ids[0]]
    assert past_the_start.json() == []

    for response in (first, second, third, past_the_start):
        assert response.status_code == 200
        assert response.headers["X-Total-Count"] == "5"


def test_empty_conversation(
    client: TestClient,
    make_ticket,
    customer_headers: dict[str, str],
) -> None:
    ticket = make_ticket()

    response = client.get(_url(ticket.id), headers=customer_headers)

    assert response.status_code == 200
    assert response.json() == []
    assert response.headers["X-Total-Count"] == "0"


@pytest.mark.parametrize(
    "params",
    [
        {"before_id": "0"},
        {"before_id": "-1"},
        {"before_id": "abc"},
        {"before_id": "1.5"},
        {"before_id": str(2**63)},
        {"limit": "0"},
        {"limit": "101"},
        {"limit": "ten"},
    ],
    ids=lambda params: "-".join(f"{k}={v}" for k, v in params.items()),
)
def test_invalid_cursor_and_limit_return_422(
    client: TestClient,
    make_ticket,
    customer_headers: dict[str, str],
    params: dict,
) -> None:
    ticket = make_ticket()

    response = client.get(_url(ticket.id), params=params, headers=customer_headers)

    assert response.status_code == 422


@pytest.mark.parametrize("ticket_id", ["0", "-3", "abc", str(2**63)])
def test_invalid_ticket_id_returns_422(
    client: TestClient,
    customer_headers: dict[str, str],
    ticket_id: str,
) -> None:
    assert client.get(f"/api/tickets/{ticket_id}/messages", headers=customer_headers).status_code == 422
    assert client.post(
        f"/api/tickets/{ticket_id}/messages", json={"content": "Hi"}, headers=customer_headers
    ).status_code == 422


def test_maximum_page_size_is_100(
    client: TestClient,
    db_session: Session,
    make_ticket,
    customer_user: User,
    customer_headers: dict[str, str],
) -> None:
    ticket = make_ticket()
    db_session.add_all(
        TicketMessage(
            ticket_id=ticket.id,
            sender_user_id=customer_user.id,
            sender_role="customer",
            content=f"bulk {number}",
        )
        for number in range(120)
    )
    db_session.commit()

    default = client.get(_url(ticket.id), headers=customer_headers)
    largest = client.get(_url(ticket.id), params={"limit": 100}, headers=customer_headers)

    assert len(default.json()) == 50
    assert len(largest.json()) == 100
    assert default.headers["X-Total-Count"] == "120"
    ids = [message["id"] for message in largest.json()]
    assert ids == sorted(ids, reverse=True)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_unauthenticated_requests_return_401(client: TestClient, make_ticket) -> None:
    ticket = make_ticket()

    assert client.get(_url(ticket.id)).status_code == 401
    assert client.post(_url(ticket.id), json={"content": "Hi"}).status_code == 401


def test_invalid_token_returns_401(client: TestClient, make_ticket) -> None:
    ticket = make_ticket()
    headers = {"Authorization": "Bearer not-a-real-token"}

    assert client.get(_url(ticket.id), headers=headers).status_code == 401
    assert _post(client, ticket.id, headers).status_code == 401


@pytest.mark.parametrize("role", ["admin", "agent", "customer"])
def test_inactive_user_returns_401(
    request: pytest.FixtureRequest,
    client: TestClient,
    db_session: Session,
    make_ticket,
    agent_user: User,
    role: str,
) -> None:
    ticket = make_ticket(status="In Progress", assignee=agent_user)
    headers = request.getfixturevalue(f"{role}_headers")
    user = request.getfixturevalue(f"{role}_user")

    user.is_active = False
    db_session.commit()

    assert client.get(_url(ticket.id), headers=headers).status_code == 401
    assert _post(client, ticket.id, headers).status_code == 401
    assert _message_count(db_session) == 0


def test_unknown_stored_role_returns_403(
    client: TestClient,
    db_session: Session,
    make_ticket,
    customer_user: User,
    customer_headers: dict[str, str],
) -> None:
    ticket = make_ticket()
    customer_user.role = "guest"
    db_session.commit()

    assert client.get(_url(ticket.id), headers=customer_headers).status_code == 403
    assert _post(client, ticket.id, customer_headers).status_code == 403


# ---------------------------------------------------------------------------
# Logging and database failures
# ---------------------------------------------------------------------------


def test_message_content_never_reaches_the_logs(
    client: TestClient,
    make_ticket,
    agent_user: User,
    admin_headers: dict[str, str],
    agent_headers: dict[str, str],
    customer_headers: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    ticket = make_ticket(status="In Progress", assignee=agent_user)
    resolved = make_ticket(status="Resolved")
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    # Every logger, including ones that do not propagate (the audit log).
    handler = _Collect(level=logging.DEBUG)
    loggers = [logging.getLogger()] + [
        logging.getLogger(name) for name in list(logging.root.manager.loggerDict)
    ]

    for logger in loggers:
        logger.addHandler(handler)

    caplog.set_level(logging.DEBUG)

    try:
        for headers in (customer_headers, agent_headers, admin_headers):
            assert _post(client, ticket.id, headers, SECRET_CONTENT).status_code == 201

        assert _post(client, resolved.id, customer_headers, SECRET_CONTENT).status_code == 409
        assert client.post(
            _url(ticket.id),
            json={"content": SECRET_CONTENT, "sender_role": "admin"},
            headers=customer_headers,
        ).status_code == 422
        assert client.get(_url(ticket.id), headers=admin_headers).status_code == 200
    finally:
        for logger in loggers:
            logger.removeHandler(handler)

    logged = caplog.text + "\n".join(
        f"{record.getMessage()} {record.args!r} {record.__dict__!r}" for record in records
    )

    assert "Conversation-secret-4f1d" not in logged
    assert "4242" not in logged


def test_lock_time_out_returns_safe_500_and_writes_nothing(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "locked.db"
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 0.2},
    )
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        admin = User(
            name="Admin", email="lock-admin@example.test", password_hash="unused", role="admin"
        )
        db.add(admin)
        db.flush()
        ticket = Ticket(title="Locked", description="Locked ticket.", creator=admin)
        db.add(ticket)
        db.commit()
        admin_id, ticket_id = admin.id, ticket.id

    token = create_access_token(subject=str(admin_id), token_version=0)

    def file_db():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    blocker = sqlite3.connect(path)
    blocker.execute("BEGIN IMMEDIATE")
    app.dependency_overrides[get_db] = file_db

    try:
        with TestClient(app) as client:
            response = client.post(
                _url(ticket_id),
                json={"content": SECRET_CONTENT},
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)
        blocker.rollback()
        blocker.close()

    assert response.status_code == 500
    assert response.json() == {"detail": DATABASE_ERROR_DETAIL}

    [record] = [r for r in caplog.records if r.name == "app.main"]

    assert record.getMessage() == (
        "Database error while handling a request "
        "(OperationalError: the database is locked [SQLITE_BUSY])."
    )
    assert "Conversation-secret" not in caplog.text
    assert "INSERT" not in response.text and "SELECT" not in response.text

    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(TicketMessage)) == 0

    engine.dispose()
