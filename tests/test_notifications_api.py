"""The notification API: /api/notifications.

Uses conftest's in-memory database and role fixtures; resolveai.db is
never used. Notifications are created either through the real message
endpoint (end to end) or directly with the service helper, for states
the workflow cannot reach (for example a demoted agent).
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Notification, Ticket, TicketMessage, User
from app.services.notification_service import add_message_notifications
from app.services.ticket_service import create_ticket


URL = "/api/notifications"
UNREAD_URL = f"{URL}/unread-count"
READ_ALL_URL = f"{URL}/read-all"

NOTIFICATION_FIELDS = {
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

NOT_FOUND = {"detail": "Notification not found."}
FORBIDDEN = {"detail": "You do not have permission to perform this action."}

# Distinctive content, so tests can prove it never reaches the logs.
SECRET_CONTENT = "Notification-secret-9c2e card ends 4242"

T0 = datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc)


def _read_url(notification_id) -> str:
    return f"{URL}/{notification_id}/read"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def make_ticket(db_session: Session, customer_user: User) -> Callable[..., Ticket]:
    def _make(
        *,
        creator: User | None = None,
        assignee: User | None = None,
        title: str = "Payment failed",
    ) -> Ticket:
        ticket = create_ticket(
            db_session,
            creator=creator or customer_user,
            title=title,
            description="My card was declined.",
        )

        if assignee is not None:
            ticket.status = "In Progress"
            ticket.assigned_to_user_id = assignee.id
            db_session.commit()
            db_session.refresh(ticket)

        return ticket

    return _make


@pytest.fixture
def notify(db_session: Session) -> Callable[..., list[int]]:
    """Store a message and notify the given users directly (service helper).

    Returns the new notification IDs.
    """

    clock = {"tick": 0}

    def _notify(
        sender: User,
        ticket: Ticket,
        recipients: list[User],
        content: str = "Hello there",
        *,
        sender_role: str | None = None,
    ) -> list[int]:
        clock["tick"] += 1
        message = TicketMessage(
            ticket_id=ticket.id,
            sender_user_id=sender.id,
            sender_role=sender_role or sender.role,
            content=content,
            created_at=T0 + timedelta(seconds=clock["tick"]),
        )
        db_session.add(message)
        db_session.flush()
        notifications = add_message_notifications(
            db_session, message, [user.id for user in recipients]
        )
        db_session.commit()

        return [notification.id for notification in notifications]

    return _notify


@pytest.fixture
def second_agent(
    create_user: Callable[..., User],
    login_headers: Callable[..., dict[str, str]],
):
    user = create_user(email="agent2@example.test", role="agent", name="Bo Agent")

    return user, login_headers(user.email)


@pytest.fixture
def second_admin(
    create_user: Callable[..., User],
    login_headers: Callable[..., dict[str, str]],
):
    user = create_user(email="admin2@example.test", role="admin", name="Abe Admin")

    return user, login_headers(user.email)


def _list(client: TestClient, headers: dict, **params):
    return client.get(URL, params=params, headers=headers)


def _ids(response) -> list[int]:
    return [item["id"] for item in response.json()]


def _read_state(db: Session) -> dict[int, datetime | None]:
    db.expire_all()

    return dict(db.execute(select(Notification.id, Notification.read_at)).all())


def _send_message(client: TestClient, ticket: Ticket, headers: dict, content="Hello"):
    response = client.post(
        f"/api/tickets/{ticket.id}/messages", json={"content": content}, headers=headers
    )
    assert response.status_code == 201

    return response.json()


# ---------------------------------------------------------------------------
# End to end: messages create notifications the API returns
# ---------------------------------------------------------------------------


def test_message_endpoint_notifications_reach_each_role(
    client: TestClient,
    make_ticket,
    agent_user: User,
    admin_headers: dict[str, str],
    agent_headers: dict[str, str],
    customer_headers: dict[str, str],
) -> None:
    assigned = make_ticket(assignee=agent_user)
    free = make_ticket(title="Login issue")

    to_agent = _send_message(client, assigned, customer_headers, "Any news?")
    to_admin = _send_message(client, free, customer_headers, "Please help")
    to_customer = _send_message(client, assigned, agent_headers, "On it")

    [agent_item] = _list(client, agent_headers).json()
    [admin_item] = _list(client, admin_headers).json()
    [customer_item] = _list(client, customer_headers).json()

    assert (agent_item["message_id"], agent_item["preview"]) == (to_agent["id"], "Any news?")
    assert (admin_item["message_id"], admin_item["ticket_title"]) == (to_admin["id"], "Login issue")
    assert (customer_item["message_id"], customer_item["actor_role"]) == (
        to_customer["id"],
        "agent",
    )


# ---------------------------------------------------------------------------
# Authentication and authorization
# ---------------------------------------------------------------------------


ALL_REQUESTS = [
    ("get", URL, None),
    ("get", UNREAD_URL, None),
    ("post", _read_url(1), None),
    ("post", READ_ALL_URL, {}),
]


def _call(client: TestClient, method: str, url: str, body, headers=None):
    if method == "get":
        return client.get(url, headers=headers)

    return client.post(url, json=body, headers=headers)


@pytest.mark.parametrize("role", ["admin", "agent", "customer"])
def test_every_role_can_use_the_api(
    request: pytest.FixtureRequest,
    client: TestClient,
    role: str,
) -> None:
    headers = request.getfixturevalue(f"{role}_headers")

    assert _list(client, headers).status_code == 200
    assert client.get(UNREAD_URL, headers=headers).json() == {
        "unread_count": 0,
        "latest_id": None,
    }
    assert client.post(READ_ALL_URL, json={}, headers=headers).json() == {
        "marked_read": 0,
        "unread_count": 0,
    }
    assert client.post(_read_url(1), headers=headers).status_code == 404


@pytest.mark.parametrize(("method", "url", "body"), ALL_REQUESTS)
def test_missing_token_returns_401(client: TestClient, method, url, body) -> None:
    response = _call(client, method, url, body)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(("method", "url", "body"), ALL_REQUESTS)
def test_invalid_token_returns_401(client: TestClient, method, url, body) -> None:
    headers = {"Authorization": "Bearer not-a-real-token"}

    assert _call(client, method, url, body, headers).status_code == 401


@pytest.mark.parametrize("role", ["admin", "agent", "customer"])
def test_inactive_account_returns_401(
    request: pytest.FixtureRequest,
    client: TestClient,
    db_session: Session,
    role: str,
) -> None:
    headers = request.getfixturevalue(f"{role}_headers")
    user = request.getfixturevalue(f"{role}_user")
    user.is_active = False
    db_session.commit()

    for method, url, body in ALL_REQUESTS:
        assert _call(client, method, url, body, headers).status_code == 401


def test_unknown_stored_role_gets_403_and_changes_nothing(
    client: TestClient,
    db_session: Session,
    make_ticket,
    notify,
    customer_user: User,
    agent_user: User,
    agent_headers: dict[str, str],
) -> None:
    [notification_id] = notify(customer_user, make_ticket(assignee=agent_user), [agent_user])
    agent_user.role = "guest"
    db_session.commit()

    for method, url, body in [
        ("get", URL, None),
        ("get", UNREAD_URL, None),
        ("post", _read_url(notification_id), None),
        ("post", READ_ALL_URL, {}),
    ]:
        response = _call(client, method, url, body, agent_headers)

        assert response.status_code == 403
        assert response.json() == FORBIDDEN

    assert _read_state(db_session)[notification_id] is None


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_list_returns_the_documented_fields(
    client: TestClient,
    make_ticket,
    notify,
    customer_user: User,
    agent_user: User,
    agent_headers: dict[str, str],
) -> None:
    ticket = make_ticket(assignee=agent_user)
    [notification_id] = notify(customer_user, ticket, [agent_user], "Still waiting")

    response = _list(client, agent_headers)

    assert response.status_code == 200
    [item] = response.json()

    assert set(item) == NOTIFICATION_FIELDS
    assert item["id"] == notification_id
    assert item["notification_type"] == "ticket_message"
    assert item["ticket_id"] == ticket.id
    assert item["ticket_title"] == "Payment failed"
    assert isinstance(item["message_id"], int)
    assert item["actor_name"] == "Test Customer"
    assert item["actor_role"] == "customer"
    assert item["preview"] == "Still waiting"
    assert datetime.fromisoformat(item["created_at"]) == T0 + timedelta(seconds=1)
    assert item["read_at"] is None
    assert item["is_read"] is False


def test_list_is_newest_first_with_cursor_pagination(
    client: TestClient,
    make_ticket,
    notify,
    customer_user: User,
    agent_user: User,
    agent_headers: dict[str, str],
) -> None:
    ticket = make_ticket(assignee=agent_user)
    created = [notify(customer_user, ticket, [agent_user])[0] for _ in range(5)]

    first = _list(client, agent_headers, limit=2)
    second = _list(client, agent_headers, limit=2, before_id=_ids(first)[-1])
    third = _list(client, agent_headers, limit=2, before_id=_ids(second)[-1])
    end = _list(client, agent_headers, limit=2, before_id=_ids(third)[-1])

    assert _ids(first) + _ids(second) + _ids(third) == sorted(created, reverse=True)
    assert end.json() == []

    for response in (first, second, third, end):
        assert response.headers["X-Total-Count"] == "5"
        assert response.headers["X-Unread-Count"] == "5"


def test_default_and_maximum_page_sizes(
    client: TestClient,
    db_session: Session,
    make_ticket,
    customer_user: User,
    agent_user: User,
    agent_headers: dict[str, str],
) -> None:
    ticket = make_ticket(assignee=agent_user)

    for number in range(55):
        message = TicketMessage(
            ticket_id=ticket.id,
            sender_user_id=customer_user.id,
            sender_role="customer",
            content=f"bulk {number}",
        )
        db_session.add(message)
        db_session.flush()
        add_message_notifications(db_session, message, [agent_user.id])

    db_session.commit()

    default = _list(client, agent_headers)
    largest = _list(client, agent_headers, limit=50)
    smallest = _list(client, agent_headers, limit=1)

    assert len(default.json()) == 20
    assert len(largest.json()) == 50
    assert len(smallest.json()) == 1
    assert default.headers["X-Total-Count"] == "55"


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 51},
        {"limit": -1},
        {"limit": "ten"},
        {"before_id": 0},
        {"before_id": -4},
        {"before_id": "abc"},
        {"before_id": str(2**63)},
        {"unread_only": "maybe"},
    ],
)
def test_invalid_query_parameters_return_422(
    client: TestClient,
    agent_headers: dict[str, str],
    params: dict,
) -> None:
    assert _list(client, agent_headers, **params).status_code == 422


def test_unread_only_and_count_headers(
    client: TestClient,
    db_session: Session,
    make_ticket,
    notify,
    customer_user: User,
    agent_user: User,
    agent_headers: dict[str, str],
) -> None:
    ticket = make_ticket(assignee=agent_user)
    ids = [notify(customer_user, ticket, [agent_user])[0] for _ in range(4)]

    for notification_id in (ids[0], ids[2]):
        db_session.get(Notification, notification_id).read_at = T0

    db_session.commit()

    unread = _list(client, agent_headers, unread_only="true")
    everything = _list(client, agent_headers)

    assert _ids(unread) == [ids[3], ids[1]]
    assert all(item["is_read"] is False for item in unread.json())
    assert unread.headers["X-Total-Count"] == "2"
    assert everything.headers["X-Total-Count"] == "4"
    # The unread count does not depend on the filter.
    assert unread.headers["X-Unread-Count"] == "2"
    assert everything.headers["X-Unread-Count"] == "2"

    read_items = [item for item in everything.json() if item["is_read"]]

    assert {item["id"] for item in read_items} == {ids[0], ids[2]}
    assert all(datetime.fromisoformat(item["read_at"]) == T0 for item in read_items)


def test_empty_list(client: TestClient, customer_headers: dict[str, str]) -> None:
    response = _list(client, customer_headers)

    assert response.status_code == 200
    assert response.json() == []
    assert response.headers["X-Total-Count"] == "0"
    assert response.headers["X-Unread-Count"] == "0"


def test_no_private_user_data_is_exposed(
    client: TestClient,
    db_session: Session,
    make_ticket,
    notify,
    customer_user: User,
    agent_user: User,
    agent_headers: dict[str, str],
) -> None:
    notify(customer_user, make_ticket(assignee=agent_user), [agent_user])

    response = _list(client, agent_headers)
    body = response.text
    customer = db_session.get(User, customer_user.id)

    for secret in (customer.email, customer.password_hash, agent_user.email, "Bearer"):
        assert secret not in body

    for forbidden in ("email", "password", "token", "recipient_user_id", "actor_user_id"):
        assert forbidden not in body


def test_customer_cannot_see_another_customers_tickets(
    client: TestClient,
    make_ticket,
    notify,
    agent_user: User,
    customer_user: User,
    second_customer_user: User,
    customer_headers: dict[str, str],
    second_customer_headers: dict[str, str],
) -> None:
    mine = make_ticket(assignee=agent_user)
    theirs = make_ticket(creator=second_customer_user, assignee=agent_user)
    [own] = notify(agent_user, mine, [customer_user])
    [their_own] = notify(agent_user, theirs, [second_customer_user])
    # Inconsistent data: the first customer addressed on the other's ticket.
    [misaddressed] = notify(agent_user, theirs, [customer_user])

    assert _ids(_list(client, customer_headers)) == [own]
    assert _ids(_list(client, second_customer_headers)) == [their_own]
    assert client.get(UNREAD_URL, headers=customer_headers).json() == {
        "unread_count": 1,
        "latest_id": own,
    }
    assert client.post(_read_url(misaddressed), headers=customer_headers).json() == NOT_FOUND


def test_demoted_agent_loses_unrelated_notifications(
    client: TestClient,
    db_session: Session,
    make_ticket,
    notify,
    customer_user: User,
    second_customer_user: User,
    agent_user: User,
    agent_headers: dict[str, str],
) -> None:
    ids = [
        notify(customer_user, make_ticket(assignee=agent_user), [agent_user])[0],
        notify(
            second_customer_user,
            make_ticket(creator=second_customer_user, assignee=agent_user),
            [agent_user],
        )[0],
    ]

    assert len(_list(client, agent_headers).json()) == 2

    agent_user.role = "customer"
    db_session.commit()

    response = _list(client, agent_headers)

    assert response.json() == []
    assert response.headers["X-Total-Count"] == "0"
    assert client.get(UNREAD_URL, headers=agent_headers).json() == {
        "unread_count": 0,
        "latest_id": None,
    }
    assert client.post(_read_url(ids[0]), headers=agent_headers).json() == NOT_FOUND
    assert client.post(READ_ALL_URL, json={}, headers=agent_headers).json() == {
        "marked_read": 0,
        "unread_count": 0,
    }
    assert all(read_at is None for read_at in _read_state(db_session).values())


def test_actor_role_is_the_message_snapshot(
    client: TestClient,
    db_session: Session,
    make_ticket,
    agent_user: User,
    agent_headers: dict[str, str],
    customer_headers: dict[str, str],
) -> None:
    _send_message(client, make_ticket(assignee=agent_user), agent_headers, "Reply")

    # The sender later becomes an administrator.
    agent_user.role = "admin"
    db_session.commit()

    [item] = _list(client, customer_headers).json()

    assert item["actor_role"] == "agent"
    assert item["actor_name"] == "Test Agent"


def test_preview_is_at_most_140_characters(
    client: TestClient,
    make_ticket,
    notify,
    customer_user: User,
    agent_user: User,
    agent_headers: dict[str, str],
) -> None:
    ticket = make_ticket(assignee=agent_user)
    notify(customer_user, ticket, [agent_user], "x" * 140)
    notify(customer_user, ticket, [agent_user], "y" * 5000)
    notify(customer_user, ticket, [agent_user], "<b>html</b>\n\nkept   as text")

    html, long, exact = _list(client, agent_headers).json()

    assert exact["preview"] == "x" * 140
    assert len(long["preview"]) == 140
    assert long["preview"] == "y" * 139 + "…"
    assert html["preview"] == "<b>html</b> kept as text"


def test_notification_content_never_reaches_the_logs(
    client: TestClient,
    make_ticket,
    agent_user: User,
    admin_headers: dict[str, str],
    agent_headers: dict[str, str],
    customer_headers: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    ticket = make_ticket(assignee=agent_user)
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
        _send_message(client, ticket, customer_headers, SECRET_CONTENT)
        _send_message(client, ticket, admin_headers, SECRET_CONTENT)

        for headers in (agent_headers, customer_headers):
            items = _list(client, headers).json()
            assert items and SECRET_CONTENT[:20] in items[0]["preview"]
            assert client.post(_read_url(items[0]["id"]), headers=headers).status_code == 200
            assert client.post(READ_ALL_URL, json={}, headers=headers).status_code == 200
            assert client.get(UNREAD_URL, headers=headers).status_code == 200

        assert client.post(_read_url(9999), headers=agent_headers).status_code == 404
    finally:
        for logger in loggers:
            logger.removeHandler(handler)

    logged = caplog.text + "\n".join(
        f"{record.getMessage()} {record.args!r} {record.__dict__!r}" for record in records
    )

    assert "Notification-secret-9c2e" not in logged
    assert "4242" not in logged


# ---------------------------------------------------------------------------
# Unread summary
# ---------------------------------------------------------------------------


def test_unread_summary(
    client: TestClient,
    make_ticket,
    notify,
    customer_user: User,
    agent_user: User,
    admin_user: User,
    agent_headers: dict[str, str],
) -> None:
    ticket = make_ticket(assignee=agent_user)
    ids = [notify(customer_user, ticket, [agent_user])[0] for _ in range(3)]
    # Someone else's newer notification is not counted.
    notify(customer_user, make_ticket(), [admin_user])

    assert client.get(UNREAD_URL, headers=agent_headers).json() == {
        "unread_count": 3,
        "latest_id": ids[-1],
    }

    assert client.post(_read_url(ids[-1]), headers=agent_headers).status_code == 200

    # Marking read lowers the count; the newest ID still includes read ones.
    assert client.get(UNREAD_URL, headers=agent_headers).json() == {
        "unread_count": 2,
        "latest_id": ids[-1],
    }


def test_unread_summary_when_empty(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    response = client.get(UNREAD_URL, headers=admin_headers)

    assert response.status_code == 200
    assert response.json() == {"unread_count": 0, "latest_id": None}


# ---------------------------------------------------------------------------
# Marking one notification read
# ---------------------------------------------------------------------------


def test_recipient_marks_their_notification_read(
    client: TestClient,
    db_session: Session,
    make_ticket,
    notify,
    customer_user: User,
    agent_user: User,
    agent_headers: dict[str, str],
) -> None:
    [notification_id] = notify(customer_user, make_ticket(assignee=agent_user), [agent_user])
    before = datetime.now(timezone.utc)

    response = client.post(_read_url(notification_id), headers=agent_headers)

    assert response.status_code == 200
    item = response.json()

    assert set(item) == NOTIFICATION_FIELDS
    assert item["id"] == notification_id
    assert item["is_read"] is True
    assert before <= datetime.fromisoformat(item["read_at"]) <= datetime.now(timezone.utc)
    assert _read_state(db_session)[notification_id] is not None


def test_repeated_marking_keeps_the_original_read_at(
    client: TestClient,
    make_ticket,
    notify,
    customer_user: User,
    agent_user: User,
    agent_headers: dict[str, str],
) -> None:
    [notification_id] = notify(customer_user, make_ticket(assignee=agent_user), [agent_user])

    first = client.post(_read_url(notification_id), headers=agent_headers).json()
    second = client.post(_read_url(notification_id), headers=agent_headers).json()

    assert second == first


def test_existing_read_at_is_never_overwritten(
    client: TestClient,
    db_session: Session,
    make_ticket,
    notify,
    customer_user: User,
    agent_user: User,
    agent_headers: dict[str, str],
) -> None:
    [notification_id] = notify(customer_user, make_ticket(assignee=agent_user), [agent_user])
    db_session.get(Notification, notification_id).read_at = T0
    db_session.commit()

    item = client.post(_read_url(notification_id), headers=agent_headers).json()

    assert datetime.fromisoformat(item["read_at"]) == T0


@pytest.mark.parametrize("owner", ["admin", "agent", "customer"])
def test_each_role_can_mark_only_its_own(
    request: pytest.FixtureRequest,
    client: TestClient,
    db_session: Session,
    make_ticket,
    notify,
    customer_user: User,
    agent_user: User,
    admin_user: User,
    second_agent,
    second_admin,
    second_customer_headers: dict[str, str],
    owner: str,
) -> None:
    ticket = make_ticket(assignee=agent_user)
    recipient = {"admin": admin_user, "agent": agent_user, "customer": customer_user}[owner]
    sender = agent_user if owner == "customer" else customer_user
    [notification_id] = notify(sender, ticket, [recipient])

    others = {
        "admin": [second_admin[1], request.getfixturevalue("agent_headers")],
        "agent": [second_agent[1], request.getfixturevalue("admin_headers")],
        "customer": [second_customer_headers, request.getfixturevalue("agent_headers")],
    }[owner]

    for headers in others:
        response = client.post(_read_url(notification_id), headers=headers)

        assert response.status_code == 404
        assert response.json() == NOT_FOUND

    assert _read_state(db_session)[notification_id] is None

    own_headers = request.getfixturevalue(f"{owner}_headers")

    assert client.post(_read_url(notification_id), headers=own_headers).json()["is_read"] is True


def test_missing_and_other_users_notifications_look_identical(
    client: TestClient,
    make_ticket,
    notify,
    customer_user: User,
    agent_user: User,
    second_agent,
) -> None:
    [notification_id] = notify(customer_user, make_ticket(assignee=agent_user), [agent_user])
    headers = second_agent[1]

    others = client.post(_read_url(notification_id), headers=headers)
    missing = client.post(_read_url(notification_id + 1000), headers=headers)

    assert others.status_code == missing.status_code == 404
    assert others.json() == missing.json() == NOT_FOUND
    assert others.headers.get("content-length") == missing.headers.get("content-length")


@pytest.mark.parametrize("notification_id", ["0", "-1", "abc", "1.5", str(2**63)])
def test_invalid_notification_id_returns_422(
    client: TestClient,
    agent_headers: dict[str, str],
    notification_id: str,
) -> None:
    assert client.post(f"{URL}/{notification_id}/read", headers=agent_headers).status_code == 422


# ---------------------------------------------------------------------------
# Marking several notifications read
# ---------------------------------------------------------------------------


@pytest.fixture
def inbox(
    make_ticket,
    notify,
    customer_user: User,
    second_customer_user: User,
    agent_user: User,
    admin_user: User,
):
    """Agent: three on ``mine``, then two on ``theirs``; admin: one."""

    mine = make_ticket(assignee=agent_user)
    theirs = make_ticket(creator=second_customer_user, assignee=agent_user)

    return {
        "mine_ticket": mine.id,
        "theirs_ticket": theirs.id,
        "mine": [notify(customer_user, mine, [agent_user])[0] for _ in range(3)],
        "theirs": [notify(second_customer_user, theirs, [agent_user])[0] for _ in range(2)],
        "admin": notify(customer_user, make_ticket(), [admin_user])[0],
    }


def _unread_ids(db: Session) -> set[int]:
    return {
        notification_id
        for notification_id, read_at in _read_state(db).items()
        if read_at is None
    }


def test_empty_body_marks_every_visible_notification(
    client: TestClient,
    db_session: Session,
    agent_headers: dict[str, str],
    inbox: dict,
) -> None:
    response = client.post(READ_ALL_URL, json={}, headers=agent_headers)

    assert response.status_code == 200
    assert response.json() == {"marked_read": 5, "unread_count": 0}
    # The administrator's notification is untouched.
    assert _unread_ids(db_session) == {inbox["admin"]}


def test_up_to_id_leaves_newer_notifications_unread(
    client: TestClient,
    db_session: Session,
    agent_headers: dict[str, str],
    inbox: dict,
) -> None:
    response = client.post(
        READ_ALL_URL, json={"up_to_id": inbox["mine"][1]}, headers=agent_headers
    )

    assert response.json() == {"marked_read": 2, "unread_count": 3}
    assert _unread_ids(db_session) == {inbox["mine"][2], *inbox["theirs"], inbox["admin"]}


def test_ticket_id_marks_one_ticket(
    client: TestClient,
    db_session: Session,
    agent_headers: dict[str, str],
    inbox: dict,
) -> None:
    response = client.post(
        READ_ALL_URL, json={"ticket_id": inbox["theirs_ticket"]}, headers=agent_headers
    )

    assert response.json() == {"marked_read": 2, "unread_count": 3}
    assert _unread_ids(db_session) == {*inbox["mine"], inbox["admin"]}


def test_both_filters_together(
    client: TestClient,
    db_session: Session,
    agent_headers: dict[str, str],
    inbox: dict,
) -> None:
    response = client.post(
        READ_ALL_URL,
        json={"up_to_id": inbox["mine"][0], "ticket_id": inbox["mine_ticket"]},
        headers=agent_headers,
    )

    assert response.json() == {"marked_read": 1, "unread_count": 4}
    assert _unread_ids(db_session) == {*inbox["mine"][1:], *inbox["theirs"], inbox["admin"]}


def test_already_read_notifications_are_preserved(
    client: TestClient,
    db_session: Session,
    agent_headers: dict[str, str],
    inbox: dict,
) -> None:
    db_session.get(Notification, inbox["mine"][0]).read_at = T0
    db_session.commit()

    first = client.post(READ_ALL_URL, json={}, headers=agent_headers).json()
    second = client.post(READ_ALL_URL, json={}, headers=agent_headers).json()

    assert first == {"marked_read": 4, "unread_count": 0}
    assert second == {"marked_read": 0, "unread_count": 0}
    assert _read_state(db_session)[inbox["mine"][0]].replace(tzinfo=timezone.utc) == T0


def test_other_users_notifications_are_untouched(
    client: TestClient,
    db_session: Session,
    second_agent,
    customer_headers: dict[str, str],
    inbox: dict,
) -> None:
    assert client.post(READ_ALL_URL, json={}, headers=second_agent[1]).json() == {
        "marked_read": 0,
        "unread_count": 0,
    }
    # Naming someone else's ticket and IDs marks nothing either.
    assert client.post(
        READ_ALL_URL,
        json={"ticket_id": inbox["mine_ticket"], "up_to_id": inbox["admin"]},
        headers=customer_headers,
    ).json() == {"marked_read": 0, "unread_count": 0}
    assert len(_unread_ids(db_session)) == 6


@pytest.mark.parametrize(
    "body",
    [
        {"recipient_user_id": 1},
        {"up_to_id": 1, "role": "admin"},
        {"up_to_id": 0},
        {"up_to_id": -3},
        {"up_to_id": "5"},
        {"up_to_id": 5.0},
        {"up_to_id": True},
        {"ticket_id": 0},
        {"ticket_id": "1"},
        {"ticket_id": 2**63},
        [],
        "all",
    ],
)
def test_invalid_bodies_return_422(
    client: TestClient,
    db_session: Session,
    agent_headers: dict[str, str],
    inbox: dict,
    body,
) -> None:
    response = client.post(READ_ALL_URL, json=body, headers=agent_headers)

    assert response.status_code == 422
    assert len(_unread_ids(db_session)) == 6


def test_missing_or_non_json_body_returns_422(
    client: TestClient,
    agent_headers: dict[str, str],
) -> None:
    assert client.post(READ_ALL_URL, headers=agent_headers).status_code == 422
    assert client.post(
        READ_ALL_URL,
        content="up_to_id=3",
        headers={**agent_headers, "Content-Type": "application/x-www-form-urlencoded"},
    ).status_code == 422


def test_explicit_nulls_mean_no_filter(
    client: TestClient,
    agent_headers: dict[str, str],
    inbox: dict,
) -> None:
    response = client.post(
        READ_ALL_URL, json={"up_to_id": None, "ticket_id": None}, headers=agent_headers
    )

    assert response.json() == {"marked_read": 5, "unread_count": 0}
