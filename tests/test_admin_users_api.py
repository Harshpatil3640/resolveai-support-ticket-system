"""Admin-only user management API (/api/admin/users) and its services.

Most tests use the in-memory SQLite session and fixtures from conftest.py.
The concurrency tests use a temporary SQLite FILE (one connection per
thread), because the last-active-administrator rule must hold across
separate connections. resolveai.db is never used.
"""

import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import postgresql, sqlite as sqlite_dialect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.dependencies import FORBIDDEN_DETAIL
from app.api.routes.admin import (
    DUPLICATE_EMAIL_DETAIL,
    LAST_ADMIN_DETAIL,
    SELF_ROLE_CHANGE_DETAIL,
    SELF_STATUS_CHANGE_DETAIL,
    USER_NOT_FOUND_DETAIL,
)
from app.core.roles import UserRole
from app.core.security import create_access_token, verify_password
from app.db.base import Base
from app.db.database import get_db
from app.db.errors import (
    UNEXPECTED_ROLLBACK_FAILURE,
    describe_rollback_failure,
    rollback_failure_of,
)
from app.main import DATABASE_ERROR_DETAIL, app
from app.models import Ticket, User
from app.core.audit_logging import AUDIT_LOGGER_NAME
from app.services import user_service
from app.services.auth_service import UserNotFoundError
from app.services.user_service import (
    AdministratorRequiredError,
    LastActiveAdministratorError,
    SelfModificationError,
    TransactionAlreadyStartedError,
    UserAlreadyExistsError,
    UserServiceError,
    change_user_role,
    create_user_as_admin,
    set_user_active_status,
)


# The password conftest.py gives its fixture users.
TEST_PASSWORD = "StrongPassword123!"
USERS_URL = "/api/admin/users"
NEW_PASSWORD = "Brand-New-Password-42"
UNUSED_HASH = "not-a-real-password-hash"
PUBLIC_FIELDS = {"id", "name", "email", "role", "is_active", "created_at"}
BASE_TIME = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
# Operational errors of the admin routes (e.g. a failed rollback).
ADMIN_LOGGER = "app.api.routes.admin"


@pytest.fixture
def audit_records() -> list[logging.LogRecord]:
    """Every record the audit logger emits during the test.

    The audit logger does not propagate to the root logger (so caplog
    cannot see it); a collecting handler is attached to it directly, and
    the logger's own level and handlers are left as the app configured
    them.
    """

    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collect()
    audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)
    audit_logger.addHandler(handler)

    yield records

    audit_logger.removeHandler(handler)


def _user_url(user_id: int) -> str:
    return f"{USERS_URL}/{user_id}"


def _role_url(user_id: int) -> str:
    return f"{USERS_URL}/{user_id}/role"


def _status_url(user_id: int) -> str:
    return f"{USERS_URL}/{user_id}/status"


def _new_user_body(**overrides) -> dict:
    body = {
        "name": "New Person",
        "email": "new.person@example.test",
        "password": NEW_PASSWORD,
        "role": "agent",
    }
    body.update(overrides)

    return body


def _stored(db: Session, user_id: int) -> dict | None:
    """The user's row read straight from the database, bypassing the ORM."""

    row = db.execute(
        text(
            "SELECT name, email, role, is_active, token_version, password_hash "
            "FROM users WHERE id = :id"
        ),
        {"id": user_id},
    ).mappings().one_or_none()

    return None if row is None else dict(row)


def _all_rows(db: Session) -> list[tuple]:
    return db.execute(text("SELECT * FROM users ORDER BY id")).all()


def _insert(
    db: Session,
    *,
    email: str,
    name: str = "Listed User",
    role: str = UserRole.CUSTOMER.value,
    is_active: bool = True,
    created_at: datetime = BASE_TIME,
) -> int:
    """Insert a user directly (no password hashing, for speed)."""

    user = User(
        name=name,
        email=email,
        password_hash=UNUSED_HASH,
        role=role,
        is_active=is_active,
        created_at=created_at,
    )
    db.add(user)
    db.commit()

    return user.id


def _assert_public_user(data: dict) -> None:
    assert set(data) == PUBLIC_FIELDS


def _assert_no_private_fields(response_text: str) -> None:
    for fragment in ("password_hash", "token_version", "$argon2", TEST_PASSWORD):
        assert fragment not in response_text


# ---------------------------------------------------------------------------
# Authentication and authorisation for every endpoint
# ---------------------------------------------------------------------------


ENDPOINTS = [
    ("GET", USERS_URL, None),
    ("GET", _user_url(1), None),
    ("POST", USERS_URL, _new_user_body()),
    ("PATCH", _role_url(1), {"role": "agent"}),
    ("PATCH", _status_url(1), {"is_active": False}),
]
ENDPOINT_IDS = ["list", "retrieve", "create", "role", "status"]


@pytest.mark.parametrize(("method", "url", "body"), ENDPOINTS, ids=ENDPOINT_IDS)
def test_missing_token_returns_401(
    client: TestClient,
    method: str,
    url: str,
    body: dict | None,
) -> None:
    response = client.request(method, url, json=body)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(("method", "url", "body"), ENDPOINTS, ids=ENDPOINT_IDS)
def test_invalid_token_returns_401(
    client: TestClient,
    method: str,
    url: str,
    body: dict | None,
) -> None:
    response = client.request(
        method,
        url,
        json=body,
        headers={"Authorization": "Bearer not-a-valid-token"},
    )

    assert response.status_code == 401


@pytest.mark.parametrize("role_fixture", ["agent_headers", "customer_headers"])
@pytest.mark.parametrize(("method", "url", "body"), ENDPOINTS, ids=ENDPOINT_IDS)
def test_agents_and_customers_are_forbidden(
    request: pytest.FixtureRequest,
    client: TestClient,
    db_session: Session,
    admin_user: User,
    role_fixture: str,
    method: str,
    url: str,
    body: dict | None,
) -> None:
    headers = request.getfixturevalue(role_fixture)
    rows_before = _all_rows(db_session)

    response = client.request(method, url, json=body, headers=headers)

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert _all_rows(db_session) == rows_before


def test_non_admin_cannot_probe_request_validation(
    client: TestClient,
    agent_headers: dict[str, str],
) -> None:
    """Authorisation is decided before the request body is validated."""

    response = client.post(
        USERS_URL,
        json={"unexpected": "field"},
        headers=agent_headers,
    )

    assert response.status_code == 403


@pytest.mark.parametrize(("method", "url", "body"), ENDPOINTS, ids=ENDPOINT_IDS)
def test_admin_is_allowed(
    client: TestClient,
    admin_headers: dict[str, str],
    create_user: Callable[..., User],
    method: str,
    url: str,
    body: dict | None,
) -> None:
    # User 1 is the administrator; make the PATCH targets someone else.
    target = create_user(email="target@example.test", role="customer")
    url = url.replace("/1/", f"/{target.id}/")

    response = client.request(method, url, json=body, headers=admin_headers)

    assert response.status_code in (200, 201)


def test_admin_endpoints_are_documented_under_administration_tag() -> None:
    paths = app.openapi()["paths"]

    expected = {
        (USERS_URL, "get"),
        (USERS_URL, "post"),
        ("/api/admin/users/{user_id}", "get"),
        ("/api/admin/users/{user_id}/role", "patch"),
        ("/api/admin/users/{user_id}/status", "patch"),
    }

    documented = {
        (path, method)
        for path, operations in paths.items()
        if path.startswith("/api/admin")
        for method in operations
    }

    assert documented == expected

    for path, method in expected:
        assert paths[path][method]["tags"] == ["Administration"]

    # Existing routers are still registered.
    assert "/api/auth/login" in paths
    assert "/api/tickets" in paths
    assert "/api/health" in paths


# ---------------------------------------------------------------------------
# GET /api/admin/users
# ---------------------------------------------------------------------------


def test_list_returns_paginated_shape(
    client: TestClient,
    admin_user: User,
    admin_headers: dict[str, str],
) -> None:
    response = client.get(USERS_URL, headers=admin_headers)

    assert response.status_code == 200

    data = response.json()

    assert set(data) == {"items", "total", "skip", "limit"}
    assert data["total"] == 1
    assert data["skip"] == 0
    assert data["limit"] == 20

    [item] = data["items"]

    _assert_public_user(item)
    assert item["id"] == admin_user.id
    _assert_no_private_fields(response.text)


def test_list_pagination_and_total(
    client: TestClient,
    db_session: Session,
    admin_user: User,
    admin_headers: dict[str, str],
) -> None:
    customer_ids = [
        _insert(
            db_session,
            email=f"customer{index:02d}@example.test",
            created_at=BASE_TIME + timedelta(minutes=index),
        )
        for index in range(25)
    ]
    admin_id = admin_user.id

    # The administrator was created "now", so it is the newest.
    expected_order = [admin_id, *reversed(customer_ids)]

    pages = []

    for skip in (0, 10, 20):
        response = client.get(
            USERS_URL,
            params={"skip": skip, "limit": 10},
            headers=admin_headers,
        )
        data = response.json()

        assert response.status_code == 200
        assert data["total"] == 26
        assert data["skip"] == skip
        assert data["limit"] == 10

        pages.append([item["id"] for item in data["items"]])

    assert [len(page) for page in pages] == [10, 10, 6]
    assert [user_id for page in pages for user_id in page] == expected_order

    beyond = client.get(
        USERS_URL,
        params={"skip": 30},
        headers=admin_headers,
    ).json()

    assert beyond["items"] == []
    assert beyond["total"] == 26

    default_page = client.get(USERS_URL, headers=admin_headers).json()

    assert len(default_page["items"]) == 20
    assert default_page["limit"] == 20


def test_list_orders_equal_creation_times_by_descending_id(
    client: TestClient,
    db_session: Session,
    admin_headers: dict[str, str],
) -> None:
    same_time = [
        _insert(db_session, email=f"tie{index}@example.test", created_at=BASE_TIME)
        for index in range(3)
    ]
    older = _insert(
        db_session,
        email="older@example.test",
        created_at=BASE_TIME - timedelta(days=1),
    )

    data = client.get(
        USERS_URL,
        params={"role": "customer"},
        headers=admin_headers,
    ).json()

    assert [item["id"] for item in data["items"]] == [
        *reversed(same_time),
        older,
    ]


def test_list_search_is_case_insensitive_on_name_and_email(
    client: TestClient,
    db_session: Session,
    admin_headers: dict[str, str],
) -> None:
    alice = _insert(db_session, name="Alice Smith", email="alice.smith@example.test")
    # A legacy row whose email was stored with capitals.
    bob = _insert(db_session, name="Bob Jones", email="bjones@ALICE-Corp.test")
    _insert(db_session, name="Carol White", email="carol@example.test")

    def search(term: str) -> tuple[set[int], int]:
        data = client.get(
            USERS_URL,
            params={"search": term},
            headers=admin_headers,
        ).json()

        return {item["id"] for item in data["items"]}, data["total"]

    assert search("ALICE") == ({alice, bob}, 2)
    assert search("aLiCe") == ({alice, bob}, 2)
    assert search("sMiTh") == ({alice}, 1)
    assert search("JONES") == ({bob}, 1)
    assert search("alice-corp") == ({bob}, 1)
    assert search("  alice  ") == ({alice, bob}, 2)
    assert search("nobody") == (set(), 0)


def test_list_search_treats_wildcards_literally(
    client: TestClient,
    db_session: Session,
    admin_headers: dict[str, str],
) -> None:
    underscored = _insert(db_session, email="under_score@example.test")
    _insert(db_session, email="plain@example.test")

    def total(term: str) -> int:
        return client.get(
            USERS_URL,
            params={"search": term},
            headers=admin_headers,
        ).json()["total"]

    assert total("%") == 0
    assert total("_") == 1
    assert client.get(
        USERS_URL,
        params={"search": "_"},
        headers=admin_headers,
    ).json()["items"][0]["id"] == underscored


def test_list_filters_by_role_and_status_independently_and_together(
    client: TestClient,
    db_session: Session,
    admin_user: User,
    admin_headers: dict[str, str],
) -> None:
    ids = {
        "inactive_admin": _insert(
            db_session, email="a2@example.test", role="admin", is_active=False
        ),
        "agent": _insert(db_session, email="ag1@example.test", role="agent"),
        "inactive_agent": _insert(
            db_session, email="ag2@example.test", role="agent", is_active=False
        ),
        "customer": _insert(db_session, email="c1@example.test", name="Zed One"),
        "customer2": _insert(db_session, email="c2@example.test", name="Zed Two"),
        "inactive_customer": _insert(
            db_session, email="c3@example.test", is_active=False, name="Zed Three"
        ),
    }
    ids["admin"] = admin_user.id

    def listed(**params) -> tuple[set[int], int]:
        data = client.get(USERS_URL, params=params, headers=admin_headers).json()

        return {item["id"] for item in data["items"]}, data["total"]

    def expect(*names: str) -> tuple[set[int], int]:
        return {ids[name] for name in names}, len(names)

    assert listed(role="admin") == expect("admin", "inactive_admin")
    assert listed(role="agent") == expect("agent", "inactive_agent")
    assert listed(role="customer") == expect(
        "customer", "customer2", "inactive_customer"
    )
    assert listed(is_active="false") == expect(
        "inactive_admin", "inactive_agent", "inactive_customer"
    )
    assert listed(is_active="true") == expect(
        "admin", "agent", "customer", "customer2"
    )
    assert listed(role="agent", is_active="false") == expect("inactive_agent")
    assert listed(role="customer", is_active="true") == expect(
        "customer", "customer2"
    )
    assert listed(role="admin", is_active="true") == expect("admin")
    assert listed(role="customer", is_active="true", search="zed t") == expect(
        "customer2"
    )

    # The total counts the filtered rows, not only the returned page.
    page = client.get(
        USERS_URL,
        params={"role": "customer", "limit": 1, "skip": 1},
        headers=admin_headers,
    ).json()

    assert page["total"] == 3
    assert len(page["items"]) == 1


@pytest.mark.parametrize(
    "params",
    [
        {"skip": -1},
        {"skip": 2**63},
        {"limit": 0},
        {"limit": 101},
        {"search": ""},
        {"search": "x" * 201},
        {"role": "superuser"},
        {"role": "Admin"},
        {"is_active": "maybe"},
    ],
)
def test_list_rejects_invalid_query_parameters(
    client: TestClient,
    admin_headers: dict[str, str],
    params: dict,
) -> None:
    response = client.get(USERS_URL, params=params, headers=admin_headers)

    assert response.status_code == 422


def test_list_accepts_boundary_query_parameters(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    for params in ({"limit": 1}, {"limit": 100}, {"search": "x" * 200}):
        response = client.get(USERS_URL, params=params, headers=admin_headers)

        assert response.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/admin/users/{user_id}
# ---------------------------------------------------------------------------


def test_retrieve_user(
    client: TestClient,
    agent_user: User,
    admin_headers: dict[str, str],
) -> None:
    response = client.get(_user_url(agent_user.id), headers=admin_headers)

    assert response.status_code == 200

    data = response.json()

    _assert_public_user(data)
    assert data["id"] == agent_user.id
    assert data["email"] == "agent@example.test"
    assert data["role"] == "agent"
    assert data["is_active"] is True
    _assert_no_private_fields(response.text)


def test_retrieve_missing_user_returns_404(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    response = client.get(_user_url(999_999), headers=admin_headers)

    assert response.status_code == 404
    assert response.json() == {"detail": USER_NOT_FOUND_DETAIL}


@pytest.mark.parametrize("user_id", ["0", "-1", "abc", "1.5", str(2**63)])
def test_retrieve_requires_a_positive_integer_id(
    client: TestClient,
    admin_headers: dict[str, str],
    user_id: str,
) -> None:
    response = client.get(f"{USERS_URL}/{user_id}", headers=admin_headers)

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/admin/users
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "agent", "customer"])
def test_admin_creates_every_role(
    client: TestClient,
    db_session: Session,
    admin_headers: dict[str, str],
    login_headers: Callable[..., dict[str, str]],
    role: str,
) -> None:
    response = client.post(
        USERS_URL,
        json=_new_user_body(
            name="  New Person  ",
            email="  New.Person@Example.TEST ",
            role=role,
        ),
        headers=admin_headers,
    )

    assert response.status_code == 201

    data = response.json()

    _assert_public_user(data)
    assert data["name"] == "New Person"
    assert data["email"] == "new.person@example.test"
    assert data["role"] == role
    assert data["is_active"] is True
    assert NEW_PASSWORD not in response.text
    _assert_no_private_fields(response.text)

    stored = _stored(db_session, data["id"])

    assert stored["token_version"] == 0
    assert stored["password_hash"] != NEW_PASSWORD
    assert verify_password(NEW_PASSWORD, stored["password_hash"])

    # The new account can sign in, and only an admin reaches admin routes.
    headers = login_headers("new.person@example.test", NEW_PASSWORD)
    admin_access = client.get(USERS_URL, headers=headers)

    assert admin_access.status_code == (200 if role == "admin" else 403)


def test_password_whitespace_is_preserved(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    password = "  Spaced Passphrase 42  "

    response = client.post(
        USERS_URL,
        json=_new_user_body(password=password),
        headers=admin_headers,
    )

    assert response.status_code == 201

    def login(candidate: str) -> int:
        return client.post(
            "/api/auth/login",
            data={"username": "new.person@example.test", "password": candidate},
        ).status_code

    assert login(password) == 200
    assert login(password.strip()) == 401


@pytest.mark.parametrize("length", [12, 128])
def test_password_length_boundaries_are_accepted(
    client: TestClient,
    admin_headers: dict[str, str],
    length: int,
) -> None:
    response = client.post(
        USERS_URL,
        json=_new_user_body(password="P" * length),
        headers=admin_headers,
    )

    assert response.status_code == 201


def test_name_and_email_length_boundaries_are_accepted(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    domain = "@example.test"
    email = "e" * (255 - len(domain)) + domain

    response = client.post(
        USERS_URL,
        json=_new_user_body(name="N" * 100, email=email),
        headers=admin_headers,
    )

    assert response.status_code == 201
    assert response.json()["email"] == email


INVALID_CREATE_BODIES = {
    "missing role": {key: value for key, value in _new_user_body().items() if key != "role"},
    "null role": _new_user_body(role=None),
    "unknown role": _new_user_body(role="superuser"),
    "capitalised role": _new_user_body(role="Admin"),
    "short password": _new_user_body(password="Sh0rtSecret"),
    "long password": _new_user_body(password="L" * 129),
    "missing password": {key: value for key, value in _new_user_body().items() if key != "password"},
    "non-string password": _new_user_body(password=123456789012345),
    "invalid email": _new_user_body(email="not-an-email"),
    "two at-signs": _new_user_body(email="two@@example.test"),
    "email with space": _new_user_body(email="a b@example.test"),
    "long email": _new_user_body(email="e" * 244 + "@example.test"),
    "blank email": _new_user_body(email="   "),
    "blank name": _new_user_body(name="   "),
    "long name": _new_user_body(name="N" * 101),
    "extra is_active": _new_user_body(is_active=False),
    "extra password_hash": _new_user_body(password_hash="$argon2id$smuggled"),
    "extra token_version": _new_user_body(token_version=5),
}


@pytest.mark.parametrize(
    "body",
    list(INVALID_CREATE_BODIES.values()),
    ids=list(INVALID_CREATE_BODIES),
)
def test_invalid_create_requests_return_422_and_write_nothing(
    client: TestClient,
    db_session: Session,
    admin_headers: dict[str, str],
    body: dict,
) -> None:
    rows_before = _all_rows(db_session)

    response = client.post(USERS_URL, json=body, headers=admin_headers)

    assert response.status_code == 422
    assert _all_rows(db_session) == rows_before


@pytest.mark.parametrize(
    "body",
    [
        _new_user_body(password="Sh0rtSecret"),
        _new_user_body(password="Overlong-Secret-" * 9),
        _new_user_body(password_hash="$argon2id$v=19$smuggled-hash"),
    ],
    ids=["short password", "long password", "smuggled hash"],
)
def test_validation_errors_never_echo_submitted_values(
    client: TestClient,
    admin_headers: dict[str, str],
    body: dict,
) -> None:
    response = client.post(USERS_URL, json=body, headers=admin_headers)

    assert response.status_code == 422

    errors = response.json()["detail"]

    assert errors and all("input" not in error for error in errors)
    assert all({"type", "loc", "msg"} <= set(error) for error in errors)

    for value in body.values():
        if isinstance(value, str) and value not in ("agent",):
            assert value not in response.text


def test_service_validation_errors_return_fixed_messages(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    response = client.post(
        USERS_URL,
        json=_new_user_body(email="not-an-email"),
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "A valid email address is required."}


@pytest.mark.parametrize(
    "email",
    ["agent@example.test", "AGENT@Example.Test", "  agent@example.test  "],
)
def test_duplicate_email_returns_409(
    client: TestClient,
    db_session: Session,
    agent_user: User,
    admin_headers: dict[str, str],
    email: str,
) -> None:
    rows_before = _all_rows(db_session)

    response = client.post(
        USERS_URL,
        json=_new_user_body(email=email),
        headers=admin_headers,
    )

    assert response.status_code == 409
    assert response.json() == {"detail": DUPLICATE_EMAIL_DETAIL}
    assert _all_rows(db_session) == rows_before


def test_create_reuses_the_shared_user_service(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation, hashing and duplicate checks stay in create_user()."""

    calls: list[dict] = []
    real_create_user = user_service.create_user

    def recording_create_user(db, **fields):
        calls.append(fields)
        return real_create_user(db, **fields)

    monkeypatch.setattr(user_service, "create_user", recording_create_user)

    response = client.post(USERS_URL, json=_new_user_body(), headers=admin_headers)

    assert response.status_code == 201
    # commit=False: the admin wrapper owns (and commits) the transaction.
    assert calls == [
        {
            "name": "New Person",
            "email": "new.person@example.test",
            "password": NEW_PASSWORD,
            "role": UserRole.AGENT,
            "commit": False,
        }
    ]


# ---------------------------------------------------------------------------
# PATCH /api/admin/users/{user_id}/role
# ---------------------------------------------------------------------------


def test_promotion_grants_admin_access_to_the_existing_token(
    client: TestClient,
    db_session: Session,
    agent_user: User,
    agent_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    agent_id = agent_user.id

    assert client.get(USERS_URL, headers=agent_headers).status_code == 403

    response = client.patch(
        _role_url(agent_id),
        json={"role": "admin"},
        headers=admin_headers,
    )

    assert response.status_code == 200
    _assert_public_user(response.json())
    assert response.json()["role"] == "admin"

    # Same token, no new login: admin access is granted immediately.
    assert client.get(USERS_URL, headers=agent_headers).status_code == 200
    assert _stored(db_session, agent_id)["token_version"] == 0


def test_demotion_removes_admin_access_from_the_existing_token(
    client: TestClient,
    db_session: Session,
    create_user: Callable[..., User],
    login_headers: Callable[..., dict[str, str]],
    admin_headers: dict[str, str],
) -> None:
    second_admin = create_user(email="second.admin@example.test", role="admin")
    second_id = second_admin.id
    second_headers = login_headers(second_admin.email)

    assert client.get(USERS_URL, headers=second_headers).status_code == 200

    response = client.patch(
        _role_url(second_id),
        json={"role": "customer"},
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["role"] == "customer"

    # The token itself is still valid (role changes do not sign out)...
    me = client.get("/api/auth/me", headers=second_headers)

    assert me.status_code == 200
    assert me.json()["role"] == "customer"

    # ...but it no longer carries administrator access.
    assert client.get(USERS_URL, headers=second_headers).status_code == 403
    assert _stored(db_session, second_id)["token_version"] == 0


@pytest.mark.parametrize("role", ["admin", "agent", "customer"])
def test_role_change_to_every_role(
    client: TestClient,
    db_session: Session,
    customer_user: User,
    admin_headers: dict[str, str],
    role: str,
) -> None:
    customer_id = customer_user.id

    response = client.patch(
        _role_url(customer_id),
        json={"role": role},
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["role"] == role
    assert _stored(db_session, customer_id)["role"] == role


def test_unchanged_role_is_a_no_op(
    client: TestClient,
    db_session: Session,
    agent_user: User,
    admin_headers: dict[str, str],
) -> None:
    agent_id = agent_user.id
    before = _stored(db_session, agent_id)

    response = client.patch(
        _role_url(agent_id),
        json={"role": "agent"},
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["role"] == "agent"
    assert _stored(db_session, agent_id) == before


@pytest.mark.parametrize("role", ["admin", "agent", "customer"])
def test_admin_cannot_change_own_role(
    client: TestClient,
    db_session: Session,
    admin_user: User,
    admin_headers: dict[str, str],
    role: str,
) -> None:
    admin_id = admin_user.id
    before = _stored(db_session, admin_id)

    response = client.patch(
        _role_url(admin_id),
        json={"role": role},
        headers=admin_headers,
    )

    assert response.status_code == 409
    assert response.json() == {"detail": SELF_ROLE_CHANGE_DETAIL}
    assert _stored(db_session, admin_id) == before
    assert client.get(USERS_URL, headers=admin_headers).status_code == 200


def test_role_change_for_missing_user_returns_404(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    response = client.patch(
        _role_url(999_999),
        json={"role": "agent"},
        headers=admin_headers,
    )

    assert response.status_code == 404
    assert response.json() == {"detail": USER_NOT_FOUND_DETAIL}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"role": None},
        {"role": "superuser"},
        {"role": "AGENT"},
        {"role": 1},
        {"role": "agent", "is_active": False},
    ],
    ids=["empty", "null", "unknown", "capitalised", "number", "extra field"],
)
def test_invalid_role_requests_return_422(
    client: TestClient,
    db_session: Session,
    agent_user: User,
    admin_headers: dict[str, str],
    body: dict,
) -> None:
    rows_before = _all_rows(db_session)

    response = client.patch(_role_url(agent_user.id), json=body, headers=admin_headers)

    assert response.status_code == 422
    assert _all_rows(db_session) == rows_before


# ---------------------------------------------------------------------------
# PATCH /api/admin/users/{user_id}/status
# ---------------------------------------------------------------------------


def test_deactivation_invalidates_existing_token(
    client: TestClient,
    db_session: Session,
    agent_user: User,
    agent_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    agent_id = agent_user.id

    assert client.get("/api/auth/me", headers=agent_headers).status_code == 200

    response = client.patch(
        _status_url(agent_id),
        json={"is_active": False},
        headers=admin_headers,
    )

    assert response.status_code == 200
    _assert_public_user(response.json())
    assert response.json()["is_active"] is False

    stored = _stored(db_session, agent_id)

    assert stored["is_active"] == 0
    assert stored["token_version"] == 1
    assert client.get("/api/auth/me", headers=agent_headers).status_code == 401

    login = client.post(
        "/api/auth/login",
        data={"username": "agent@example.test", "password": TEST_PASSWORD},
    )

    assert login.status_code == 401


def test_reactivation_does_not_restore_the_old_token(
    client: TestClient,
    db_session: Session,
    agent_user: User,
    agent_headers: dict[str, str],
    admin_headers: dict[str, str],
    login_headers: Callable[..., dict[str, str]],
) -> None:
    agent_id = agent_user.id

    for is_active in (False, True):
        response = client.patch(
            _status_url(agent_id),
            json={"is_active": is_active},
            headers=admin_headers,
        )

        assert response.status_code == 200
        assert response.json()["is_active"] is is_active

    stored = _stored(db_session, agent_id)

    assert stored["is_active"] == 1
    assert stored["token_version"] == 2

    # The token issued before the deactivation stays invalid...
    assert client.get("/api/auth/me", headers=agent_headers).status_code == 401

    # ...while a fresh login works again.
    fresh_headers = login_headers("agent@example.test")

    assert client.get("/api/auth/me", headers=fresh_headers).status_code == 200


def test_deactivating_another_admin_invalidates_their_token(
    client: TestClient,
    db_session: Session,
    create_user: Callable[..., User],
    login_headers: Callable[..., dict[str, str]],
    admin_headers: dict[str, str],
) -> None:
    second_admin = create_user(email="second.admin@example.test", role="admin")
    second_id = second_admin.id
    second_headers = login_headers(second_admin.email)

    response = client.patch(
        _status_url(second_id),
        json={"is_active": False},
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert client.get(USERS_URL, headers=second_headers).status_code == 401
    assert client.get(USERS_URL, headers=admin_headers).status_code == 200


@pytest.mark.parametrize("is_active", [True, False])
def test_unchanged_status_is_idempotent(
    client: TestClient,
    db_session: Session,
    create_user: Callable[..., User],
    admin_headers: dict[str, str],
    audit_records: list[logging.LogRecord],
    is_active: bool,
) -> None:
    target = create_user(email="target@example.test", role="customer")
    target_id = target.id

    if not is_active:
        first = client.patch(
            _status_url(target_id),
            json={"is_active": False},
            headers=admin_headers,
        )
        assert first.status_code == 200
        assert len(audit_records) == 1

    before = _stored(db_session, target_id)
    audit_records.clear()

    for _repeat in range(2):
        response = client.patch(
            _status_url(target_id),
            json={"is_active": is_active},
            headers=admin_headers,
        )

        assert response.status_code == 200
        assert response.json()["is_active"] is is_active

    assert _stored(db_session, target_id) == before
    assert audit_records == []


@pytest.mark.parametrize("is_active", [True, False])
def test_admin_cannot_change_own_status(
    client: TestClient,
    db_session: Session,
    admin_user: User,
    admin_headers: dict[str, str],
    is_active: bool,
) -> None:
    admin_id = admin_user.id
    before = _stored(db_session, admin_id)

    response = client.patch(
        _status_url(admin_id),
        json={"is_active": is_active},
        headers=admin_headers,
    )

    assert response.status_code == 409
    assert response.json() == {"detail": SELF_STATUS_CHANGE_DETAIL}
    assert _stored(db_session, admin_id) == before
    assert client.get(USERS_URL, headers=admin_headers).status_code == 200


def test_status_change_for_missing_user_returns_404(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    response = client.patch(
        _status_url(999_999),
        json={"is_active": False},
        headers=admin_headers,
    )

    assert response.status_code == 404
    assert response.json() == {"detail": USER_NOT_FOUND_DETAIL}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"is_active": None},
        {"is_active": "false"},
        {"is_active": 0},
        {"is_active": "no"},
        {"is_active": False, "role": "admin"},
    ],
    ids=["empty", "null", "string", "integer", "word", "extra field"],
)
def test_invalid_status_requests_return_422(
    client: TestClient,
    db_session: Session,
    agent_user: User,
    admin_headers: dict[str, str],
    body: dict,
) -> None:
    rows_before = _all_rows(db_session)

    response = client.patch(
        _status_url(agent_user.id),
        json=body,
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert all("input" not in error for error in response.json()["detail"])
    assert _all_rows(db_session) == rows_before


def test_status_and_token_version_change_together_or_not_at_all(
    client: TestClient,
    db_session: Session,
    agent_user: User,
    agent_headers: dict[str, str],
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the token_version increment fails, the status change is undone."""

    agent_id = agent_user.id

    def failing_invalidate(*_args, **_kwargs):
        raise OperationalError(
            "UPDATE users SET token_version=?",
            (1,),
            Exception("simulated failure"),
        )

    monkeypatch.setattr(user_service, "invalidate_user_sessions", failing_invalidate)

    response = client.patch(
        _status_url(agent_id),
        json={"is_active": False},
        headers=admin_headers,
    )

    assert response.status_code == 500
    assert response.json() == {"detail": DATABASE_ERROR_DETAIL}

    stored = _stored(db_session, agent_id)

    assert stored["is_active"] == 1
    assert stored["token_version"] == 0
    assert client.get("/api/auth/me", headers=agent_headers).status_code == 200


# ---------------------------------------------------------------------------
# Last active administrator (sequential)
# ---------------------------------------------------------------------------


def test_sole_admin_cannot_demote_or_deactivate_themselves(
    client: TestClient,
    db_session: Session,
    admin_user: User,
    admin_headers: dict[str, str],
) -> None:
    admin_id = admin_user.id

    demote = client.patch(_role_url(admin_id), json={"role": "agent"}, headers=admin_headers)
    deactivate = client.patch(
        _status_url(admin_id), json={"is_active": False}, headers=admin_headers
    )

    assert demote.status_code == 409
    assert deactivate.status_code == 409

    stored = _stored(db_session, admin_id)

    assert (stored["role"], stored["is_active"]) == ("admin", 1)


@pytest.mark.parametrize(
    "remove",
    [
        lambda db, actor, target: change_user_role(
            db, actor_id=actor, user_id=target, role=UserRole.AGENT
        ),
        lambda db, actor, target: change_user_role(
            db, actor_id=actor, user_id=target, role="customer"
        ),
        lambda db, actor, target: set_user_active_status(
            db, actor_id=actor, user_id=target, is_active=False
        ),
    ],
    ids=["demote to agent", "demote to customer", "deactivate"],
)
def test_service_refuses_to_remove_the_last_active_admin(
    db_session: Session,
    admin_user: User,
    remove: Callable[[Session, int, int], object],
) -> None:
    """The actor was authorised earlier but has since been deactivated."""

    last_admin_id = admin_user.id
    former_admin_id = _insert(
        db_session,
        email="former.admin@example.test",
        role="admin",
        is_active=False,
    )
    before = _stored(db_session, last_admin_id)

    with pytest.raises(LastActiveAdministratorError):
        remove(db_session, former_admin_id, last_admin_id)

    assert _stored(db_session, last_admin_id) == before


def test_another_active_admin_allows_the_change(
    db_session: Session,
    admin_user: User,
) -> None:
    actor_id = admin_user.id
    other_id = _insert(db_session, email="other.admin@example.test", role="admin")

    change = set_user_active_status(
        db_session,
        actor_id=actor_id,
        user_id=other_id,
        is_active=False,
    )

    assert change.changed is True
    assert _stored(db_session, other_id)["is_active"] == 0


def test_inactive_admin_can_be_demoted(
    db_session: Session,
    admin_user: User,
) -> None:
    inactive_id = _insert(
        db_session,
        email="inactive.admin@example.test",
        role="admin",
        is_active=False,
    )

    change = change_user_role(
        db_session,
        actor_id=admin_user.id,
        user_id=inactive_id,
        role=UserRole.AGENT,
    )

    assert (change.previous_role, change.new_role, change.changed) == (
        "admin",
        "agent",
        True,
    )


def test_legacy_role_spelling_does_not_count_as_an_active_admin(
    db_session: Session,
    admin_user: User,
) -> None:
    """Only the exact stored role "admin" passes require_admin."""

    last_admin_id = admin_user.id
    legacy_id = _insert(db_session, email="legacy@example.test", role="ADMIN")

    with pytest.raises(LastActiveAdministratorError):
        set_user_active_status(
            db_session,
            actor_id=legacy_id,
            user_id=last_admin_id,
            is_active=False,
        )


def _commit_before_lock(
    monkeypatch: pytest.MonkeyPatch,
    statement: str,
    parameters: dict,
) -> None:
    """Simulate another request committing between authorisation and lock."""

    original = user_service._lock_for_change

    def concurrent_commit_then_lock(db: Session, user_id: int):
        db.execute(text(statement), parameters)
        db.commit()

        return original(db, user_id)

    monkeypatch.setattr(user_service, "_lock_for_change", concurrent_commit_then_lock)


@pytest.mark.parametrize(
    ("concurrent_change", "url_for", "body"),
    [
        ("UPDATE users SET is_active = 0 WHERE id = :actor", _status_url, {"is_active": False}),
        ("UPDATE users SET role = 'agent' WHERE id = :actor", _status_url, {"is_active": False}),
        ("UPDATE users SET is_active = 0 WHERE id = :actor", _role_url, {"role": "customer"}),
        ("UPDATE users SET role = 'agent' WHERE id = :actor", _role_url, {"role": "customer"}),
    ],
    ids=[
        "actor deactivated, deactivate other",
        "actor demoted, deactivate other",
        "actor deactivated, demote other",
        "actor demoted, demote other",
    ],
)
def test_api_refuses_to_remove_last_admin_after_concurrent_change(
    client: TestClient,
    db_session: Session,
    admin_user: User,
    admin_headers: dict[str, str],
    create_user: Callable[..., User],
    monkeypatch: pytest.MonkeyPatch,
    concurrent_change: str,
    url_for: Callable[[int], str],
    body: dict,
) -> None:
    actor_id = admin_user.id
    other_admin_id = create_user(email="other.admin@example.test", role="admin").id
    _commit_before_lock(monkeypatch, concurrent_change, {"actor": actor_id})

    response = client.patch(url_for(other_admin_id), json=body, headers=admin_headers)

    assert response.status_code == 409
    assert response.json() == {"detail": LAST_ADMIN_DETAIL}

    stored = _stored(db_session, other_admin_id)

    assert (stored["role"], stored["is_active"], stored["token_version"]) == (
        "admin",
        1,
        0,
    )


def test_api_refuses_changes_by_an_actor_demoted_meanwhile(
    client: TestClient,
    db_session: Session,
    admin_user: User,
    admin_headers: dict[str, str],
    create_user: Callable[..., User],
    customer_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_id = admin_user.id
    customer_id = customer_user.id
    create_user(email="other.admin@example.test", role="admin")
    _commit_before_lock(
        monkeypatch,
        "UPDATE users SET role = 'agent' WHERE id = :actor",
        {"actor": actor_id},
    )

    response = client.patch(
        _role_url(customer_id),
        json={"role": "admin"},
        headers=admin_headers,
    )

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert _stored(db_session, customer_id)["role"] == "customer"


# ---------------------------------------------------------------------------
# Last active administrator (concurrent, separate connections)
# ---------------------------------------------------------------------------


# Generous busy timeout: a change waiting for the lock must not give up
# while the first change is deliberately held.
LOCK_TIMEOUT_SECONDS = 30
WAIT_SECONDS = 15
BLOCKED_CHECK_SECONDS = 0.5


@pytest.fixture
def file_database(tmp_path):
    """A temporary SQLite file; every Session gets its own connection."""

    path = tmp_path / "admin-concurrency.db"
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={
            "check_same_thread": False,
            "timeout": LOCK_TIMEOUT_SECONDS,
        },
    )
    Base.metadata.create_all(engine)

    yield engine, path

    engine.dispose()


def _seed(engine, *specs: tuple[str, str, bool]) -> list[int]:
    """Insert users given as (label, role, is_active); return their IDs."""

    with Session(engine) as db:
        users = [
            User(
                name=label,
                email=f"{label}@example.test",
                password_hash=UNUSED_HASH,
                role=role,
                is_active=is_active,
            )
            for label, role, is_active in specs
        ]
        db.add_all(users)
        db.commit()

        return [user.id for user in users]


def _active_admin_ids(engine) -> list[int]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "SELECT id FROM users "
                    "WHERE role = 'admin' AND is_active = 1 ORDER BY id"
                )
            ).scalars()
        )


def _deactivate(engine, actor_id: int, user_id: int) -> bool:
    with Session(engine) as db:
        return set_user_active_status(
            db, actor_id=actor_id, user_id=user_id, is_active=False
        ).changed


def _demote(engine, actor_id: int, user_id: int) -> bool:
    with Session(engine) as db:
        return change_user_role(
            db, actor_id=actor_id, user_id=user_id, role=UserRole.AGENT
        ).changed


def _create_with(db: Session, actor_id: int, email: str):
    return create_user_as_admin(
        db,
        actor_id=actor_id,
        name="Raced Account",
        email=email,
        password=NEW_PASSWORD,
        role=UserRole.ADMIN,
    )


def _create_as(engine, actor_id: int, email: str) -> int:
    """Create an account as ``actor_id`` in its own session/connection."""

    with Session(engine) as db:
        return _create_with(db, actor_id, email).user_id


def _emails(engine) -> list[str]:
    with engine.connect() as connection:
        return list(
            connection.execute(text("SELECT email FROM users ORDER BY id")).scalars()
        )


def _bearer(user_id: int) -> dict[str, str]:
    token = create_access_token(subject=str(user_id), token_version=0)
    return {"Authorization": f"Bearer {token}"}


def _sessions_for(engine) -> Callable:
    """A get_db override giving every request its own Session."""

    def file_db():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    return file_db


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


class _HoldFirstChange:
    """Hold the first role/status change right after its locked reads.

    The held change has read the administrator rows and decided its
    checks; a second change started meanwhile shows whether it can read
    and write before the first one finishes (it must not).

    ``race(first, second)`` runs the two operations that way and returns
    (first outcome, second outcome, whether the second was still waiting
    while the first was held). Both threads are always released and
    joined before it returns, so a failing assertion can never leave a
    change (or an HTTP request) running in the background.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.holding = threading.Event()
        self.release = threading.Event()
        self._first = True
        self._guard = threading.Lock()
        original = user_service._lock_for_change

        def lock_then_hold(db: Session, user_id: int):
            result = original(db, user_id)

            with self._guard:
                hold, self._first = self._first, False

            if hold:
                self.holding.set()
                self.release.wait(WAIT_SECONDS)

            return result

        monkeypatch.setattr(user_service, "_lock_for_change", lock_then_hold)

    def race(
        self,
        first: Callable[[], object],
        second: Callable[[], object],
        *,
        wait_for_second: bool = False,
    ) -> tuple[dict, dict, bool]:
        """Start ``first``, hold it, start ``second``, then release.

        With ``wait_for_second`` the first change is released only after
        the second one has finished (used by the unprotected control).
        """

        threads: list[threading.Thread] = []
        second_was_waiting = False

        try:
            first_thread, first_outcome = _start(first)
            threads.append(first_thread)

            if not self.holding.wait(WAIT_SECONDS):
                raise AssertionError("the first change never reached its lock")

            second_thread, second_outcome = _start(second)
            threads.append(second_thread)
            second_thread.join(WAIT_SECONDS if wait_for_second else BLOCKED_CHECK_SECONDS)
            second_was_waiting = second_thread.is_alive()
        finally:
            self.release.set()

            for thread in threads:
                thread.join(WAIT_SECONDS)

        assert not any(thread.is_alive() for thread in threads)

        return first_outcome, second_outcome, second_was_waiting


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (_deactivate, _deactivate),
        (_demote, _demote),
        (_deactivate, _demote),
        (_demote, _deactivate),
    ],
    ids=["deactivate+deactivate", "demote+demote", "deactivate+demote", "demote+deactivate"],
)
def test_concurrent_changes_cannot_remove_both_remaining_admins(
    file_database,
    monkeypatch: pytest.MonkeyPatch,
    first: Callable,
    second: Callable,
) -> None:
    engine, _path = file_database
    admin_a, admin_b = _seed(engine, ("admin-a", "admin", True), ("admin-b", "admin", True))
    hold = _HoldFirstChange(monkeypatch)

    # B removes A and is held after reading "two active administrators";
    # meanwhile A tries to remove B.
    first_outcome, second_outcome, second_was_waiting = hold.race(
        lambda: first(engine, admin_b, admin_a),
        lambda: second(engine, admin_a, admin_b),
    )

    assert second_was_waiting, "the second change did not wait for the lock"
    assert first_outcome == {"result": True}
    assert isinstance(second_outcome.get("error"), LastActiveAdministratorError)
    assert _active_admin_ids(engine) == [admin_b]


def test_without_the_lock_the_same_race_removes_every_admin(
    file_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: proves the concurrency test above detects the race.

    With the write lock disabled, the change is an unprotected
    count-then-update, and the identical interleaving leaves no active
    administrator at all.
    """

    engine, _path = file_database
    admin_a, admin_b = _seed(engine, ("admin-a", "admin", True), ("admin-b", "admin", True))
    monkeypatch.setattr(user_service, "_begin_serialised_change", lambda db: None)
    hold = _HoldFirstChange(monkeypatch)

    first_outcome, second_outcome, second_was_waiting = hold.race(
        lambda: _deactivate(engine, admin_b, admin_a),
        lambda: _deactivate(engine, admin_a, admin_b),
        wait_for_second=True,
    )

    assert not second_was_waiting
    assert first_outcome == {"result": True}
    assert second_outcome == {"result": True}
    assert _active_admin_ids(engine) == []


def test_many_concurrent_admin_removals_always_leave_an_active_admin(
    file_database,
) -> None:
    engine, _path = file_database
    count = 8
    admin_ids = _seed(
        engine,
        *[(f"admin-{index}", "admin", True) for index in range(count)],
    )
    barrier = threading.Barrier(count)

    def remove(index: int) -> bool:
        operation = _deactivate if index % 2 else _demote
        actor_id = admin_ids[index]
        target_id = admin_ids[(index + 1) % count]
        barrier.wait(WAIT_SECONDS)

        return operation(engine, actor_id, target_id)

    started = [_start(lambda index=index: remove(index)) for index in range(count)]

    for thread, _outcome in started:
        thread.join(WAIT_SECONDS * 2)

    assert not any(thread.is_alive() for thread, _outcome in started)

    outcomes = [outcome for _thread, outcome in started]
    successes = sum(outcome.get("result") is True for outcome in outcomes)

    for outcome in outcomes:
        if "error" in outcome:
            assert isinstance(
                outcome["error"],
                (LastActiveAdministratorError, AdministratorRequiredError),
            )

    remaining = _active_admin_ids(engine)

    assert len(remaining) >= 1
    # Every success removed exactly one (distinct) administrator.
    assert successes == count - len(remaining)


def test_concurrent_api_requests_cannot_remove_both_remaining_admins(
    file_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _path = file_database
    admin_a, admin_b = _seed(engine, ("admin-a", "admin", True), ("admin-b", "admin", True))

    def headers_for(user_id: int) -> dict[str, str]:
        token = create_access_token(subject=str(user_id), token_version=0)
        return {"Authorization": f"Bearer {token}"}

    def file_db():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = file_db
    hold = _HoldFirstChange(monkeypatch)

    try:
        with TestClient(app) as client:
            # Both requests finish inside race(), before the client stops.
            first_outcome, second_outcome, second_was_waiting = hold.race(
                lambda: client.patch(
                    _status_url(admin_b),
                    json={"is_active": False},
                    headers=headers_for(admin_a),
                ),
                lambda: client.patch(
                    _role_url(admin_a),
                    json={"role": "customer"},
                    headers=headers_for(admin_b),
                ),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    first_response = first_outcome["result"]
    second_response = second_outcome["result"]

    assert second_was_waiting, "the second request did not wait for the lock"
    assert first_response.status_code == 200
    assert second_response.status_code == 409
    assert second_response.json() == {"detail": LAST_ADMIN_DETAIL}
    assert _active_admin_ids(engine) == [admin_a]


@pytest.mark.parametrize(
    "outcome",
    [
        "changed",
        "unchanged",
        "not found",
        "last admin",
        "actor not admin",
        "created",
        "create duplicate",
        "create invalid",
        "create by non-admin",
    ],
)
def test_lock_is_released_after_every_outcome(
    file_database,
    outcome: str,
) -> None:
    engine, path = file_database
    admin_a, admin_b, agent = _seed(
        engine,
        ("admin-a", "admin", True),
        ("admin-b", "admin", False),
        ("agent", "agent", True),
    )
    calls = {
        "changed": lambda db: set_user_active_status(
            db, actor_id=admin_a, user_id=agent, is_active=False
        ),
        "unchanged": lambda db: set_user_active_status(
            db, actor_id=admin_a, user_id=agent, is_active=True
        ),
        "not found": lambda db: change_user_role(
            db, actor_id=admin_a, user_id=999, role="agent"
        ),
        "last admin": lambda db: change_user_role(
            db, actor_id=admin_b, user_id=admin_a, role="agent"
        ),
        "actor not admin": lambda db: change_user_role(
            db, actor_id=agent, user_id=admin_b, role="customer"
        ),
        "created": lambda db: _create_with(db, admin_a, "fresh@example.test"),
        "create duplicate": lambda db: _create_with(db, admin_a, "agent@example.test"),
        "create invalid": lambda db: _create_with(db, admin_a, "not-an-email"),
        "create by non-admin": lambda db: _create_with(db, agent, "fresh@example.test"),
    }

    with Session(engine) as db:
        try:
            calls[outcome](db)
        except UserServiceError:
            pass
        except UserNotFoundError:
            pass

        # The session is still open: its transaction must already be over.
        other = sqlite3.connect(path, timeout=0.1)

        try:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
        finally:
            other.close()


def test_lock_held_elsewhere_fails_safely_after_the_busy_timeout(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A change that cannot get SQLite's write lock writes nothing."""

    path = tmp_path / "locked.db"
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 0.2},
    )
    Base.metadata.create_all(engine)
    admin_a, admin_b = _seed(engine, ("admin-a", "admin", True), ("admin-b", "admin", True))
    token = create_access_token(subject=str(admin_a), token_version=0)

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
            response = client.patch(
                _status_url(admin_b),
                json={"is_active": False},
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
    assert _active_admin_ids(engine) == [admin_a, admin_b]
    engine.dispose()


def test_lock_queries_use_row_locks_where_supported() -> None:
    admins = user_service._active_administrators_for_update()
    target = user_service._user_for_update(1)

    for statement in (admins, target):
        assert "FOR UPDATE" in str(statement.compile(dialect=postgresql.dialect()))
        # SQLite has no row locks; BEGIN IMMEDIATE is used instead.
        assert "FOR UPDATE" not in str(
            statement.compile(dialect=sqlite_dialect.dialect())
        )

    # Rows are locked in a fixed order so two changes cannot deadlock.
    assert "ORDER BY users.id" in str(admins.compile(dialect=postgresql.dialect()))


def test_change_refuses_to_join_a_transaction_that_already_wrote(
    db_session: Session,
    admin_user: User,
    agent_user: User,
) -> None:
    admin_id = admin_user.id
    agent_id = agent_user.id
    db_session.execute(
        text("UPDATE users SET name = 'Uncommitted' WHERE id = :id"),
        {"id": agent_id},
    )

    with pytest.raises(TransactionAlreadyStartedError):
        change_user_role(
            db_session, actor_id=admin_id, user_id=agent_id, role="customer"
        )

    stored = _stored(db_session, agent_id)

    # The function owns the transaction: everything was rolled back.
    assert (stored["name"], stored["role"]) == ("Test Agent", "agent")


def test_service_rejects_self_change_before_touching_the_database(
    db_session: Session,
    admin_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_id = admin_user.id

    def must_not_lock(*_args):
        raise AssertionError("locked the database for a self-change")

    monkeypatch.setattr(user_service, "_lock_for_change", must_not_lock)

    with pytest.raises(SelfModificationError):
        change_user_role(db_session, actor_id=admin_id, user_id=admin_id, role="agent")

    with pytest.raises(SelfModificationError):
        set_user_active_status(
            db_session, actor_id=admin_id, user_id=admin_id, is_active=False
        )


def test_service_rejects_non_boolean_status(
    db_session: Session,
    admin_user: User,
    agent_user: User,
) -> None:
    with pytest.raises(user_service.InvalidUserDataError):
        set_user_active_status(
            db_session,
            actor_id=admin_user.id,
            user_id=agent_user.id,
            is_active="false",
        )


# ---------------------------------------------------------------------------
# Database errors and failed rollbacks
# ---------------------------------------------------------------------------


OPERATION_SECRET = "$argon2id$v=19$m=65536,t=3,p=4$c2VjcmV0QQ$b3BlcmF0aW9uLUE"
ROLLBACK_SECRET = "$argon2id$v=19$m=65536,t=3,p=4$c2VjcmV0Qg$cm9sbGJhY2stQg"
OPERATION_DRIVER_TEXT = "operation driver detail"
ROLLBACK_TEXT = "rollback bug"

FORBIDDEN_OUTPUT = (
    OPERATION_SECRET,
    ROLLBACK_SECRET,
    "$argon2",
    OPERATION_DRIVER_TEXT,
    ROLLBACK_TEXT,
    "UPDATE users",
    "SELECT",
    "RuntimeError",
    "Traceback",
)


def _operation_error(statement: str) -> OperationalError:
    driver_error = sqlite3.OperationalError(
        f"{OPERATION_DRIVER_TEXT}: {OPERATION_SECRET}"
    )
    driver_error.sqlite_errorname = "SQLITE_IOERR_WRITE"

    return OperationalError(statement, (OPERATION_SECRET,), driver_error)


def _raiser(error: BaseException) -> Callable[..., None]:
    def _raise(*_args, **_kwargs) -> None:
        raise error

    return _raise


def _break_rollback(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        db,
        "rollback",
        _raiser(RuntimeError(f"{ROLLBACK_TEXT}: {ROLLBACK_SECRET} SELECT 1")),
    )


def _assert_safe(text_value: str) -> None:
    for fragment in FORBIDDEN_OUTPUT:
        assert fragment not in text_value, f"exposed {fragment!r}"


@pytest.mark.parametrize(
    "change",
    [
        lambda db, actor, target: change_user_role(
            db, actor_id=actor, user_id=target, role="customer"
        ),
        lambda db, actor, target: set_user_active_status(
            db, actor_id=actor, user_id=target, is_active=False
        ),
    ],
    ids=["role", "status"],
)
def test_service_keeps_commit_failure_primary_when_rollback_fails(
    db_session: Session,
    admin_user: User,
    agent_user: User,
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[Session, int, int], object],
) -> None:
    admin_id = admin_user.id
    agent_id = agent_user.id
    commit_error = _operation_error("UPDATE users SET role=?")
    monkeypatch.setattr(db_session, "commit", _raiser(commit_error))
    _break_rollback(db_session, monkeypatch)

    with pytest.raises(OperationalError) as error_info:
        change(db_session, admin_id, agent_id)

    assert error_info.value is commit_error

    secondary = rollback_failure_of(error_info.value)

    assert isinstance(secondary, RuntimeError)
    assert describe_rollback_failure(secondary) == UNEXPECTED_ROLLBACK_FAILURE


@pytest.mark.parametrize(
    ("url_for", "body"),
    [(_role_url, {"role": "customer"}), (_status_url, {"is_active": False})],
    ids=["role", "status"],
)
def test_commit_failure_is_rolled_back_and_answered_safely(
    client: TestClient,
    db_session: Session,
    agent_user: User,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    url_for: Callable[[int], str],
    body: dict,
) -> None:
    agent_id = agent_user.id
    before = _stored(db_session, agent_id)
    monkeypatch.setattr(
        db_session,
        "commit",
        _raiser(_operation_error("UPDATE users SET role=?")),
    )

    response = client.patch(url_for(agent_id), json=body, headers=admin_headers)

    monkeypatch.undo()
    captured = capsys.readouterr()

    assert response.status_code == 500
    assert response.json() == {"detail": DATABASE_ERROR_DETAIL}

    [record] = [r for r in caplog.records if r.name == "app.main"]

    assert record.getMessage() == (
        "Database error while handling a request (OperationalError: a disk "
        "I/O error occurred [SQLITE_IOERR_WRITE])."
    )
    assert record.exc_info is None

    for text_value in (response.text, caplog.text, captured.out, captured.err):
        _assert_safe(text_value)

    # Rolled back: neither the role/status nor token_version changed.
    assert _stored(db_session, agent_id) == before


@pytest.mark.parametrize(
    ("method", "url_for", "body"),
    [
        ("PATCH", _role_url, {"role": "customer"}),
        ("PATCH", _status_url, {"is_active": False}),
        ("POST", lambda _id: USERS_URL, _new_user_body()),
    ],
    ids=["role", "status", "create"],
)
def test_original_error_is_reported_when_rollback_also_fails(
    client: TestClient,
    db_session: Session,
    agent_user: User,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    method: str,
    url_for: Callable[[int], str],
    body: dict,
) -> None:
    agent_id = agent_user.id
    monkeypatch.setattr(
        db_session,
        "commit",
        _raiser(_operation_error("UPDATE users SET role=?")),
    )
    _break_rollback(db_session, monkeypatch)

    response = client.request(
        method,
        url_for(agent_id),
        json=body,
        headers=admin_headers,
    )
    captured = capsys.readouterr()

    assert response.status_code == 500
    assert response.json() == {"detail": DATABASE_ERROR_DETAIL}

    [record] = [r for r in caplog.records if r.name == "app.main"]

    assert record.getMessage() == (
        "Database error while handling a request (OperationalError: a disk "
        "I/O error occurred [SQLITE_IOERR_WRITE]). The rollback also failed "
        "(an unexpected non-database error occurred; details hidden)."
    )
    assert record.exc_info is None

    for text_value in (response.text, caplog.text, captured.out, captured.err):
        _assert_safe(text_value)
        assert NEW_PASSWORD not in text_value


def test_rejected_change_with_failing_rollback_is_reported_safely(
    client: TestClient,
    db_session: Session,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _break_rollback(db_session, monkeypatch)

    response = client.patch(
        _status_url(999_999),
        json={"is_active": False},
        headers=admin_headers,
    )
    captured = capsys.readouterr()

    # The client still gets the real outcome...
    assert response.status_code == 404
    assert response.json() == {"detail": USER_NOT_FOUND_DETAIL}

    # ...and the failed rollback is logged with fixed text only.
    [record] = [
        r for r in caplog.records
        if r.name == ADMIN_LOGGER and r.levelno == logging.ERROR
    ]

    assert record.getMessage() == (
        "Rolling back a rejected administrative request failed (an "
        "unexpected non-database error occurred; details hidden)."
    )

    for text_value in (response.text, caplog.text, captured.out, captured.err):
        _assert_safe(text_value)


def test_list_database_error_is_answered_safely(
    client: TestClient,
    db_session: Session,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        db_session,
        "scalar",
        _raiser(_operation_error("SELECT count(*) FROM users")),
    )

    response = client.get(USERS_URL, headers=admin_headers)

    assert response.status_code == 500
    assert response.json() == {"detail": DATABASE_ERROR_DETAIL}
    _assert_safe(response.text)
    _assert_safe(caplog.text)


# ---------------------------------------------------------------------------
# Administrative action log
# ---------------------------------------------------------------------------


def _audit_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if "admin_action" in line]


def test_admin_actions_are_logged_without_secrets(
    client: TestClient,
    db_session: Session,
    admin_user: User,
    admin_headers: dict[str, str],
    audit_records: list[logging.LogRecord],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No log level is changed here: the app's own configuration emits."""

    admin_id = admin_user.id

    created = client.post(USERS_URL, json=_new_user_body(), headers=admin_headers)
    user_id = created.json()["id"]

    responses = [
        created,
        client.patch(_role_url(user_id), json={"role": "customer"}, headers=admin_headers),
        client.patch(_status_url(user_id), json={"is_active": False}, headers=admin_headers),
        client.patch(_status_url(user_id), json={"is_active": True}, headers=admin_headers),
    ]

    assert [r.status_code for r in responses] == [201, 200, 200, 200]

    records = audit_records
    common = {"actor_id": admin_id, "target_user_id": user_id}

    assert all(r.name == AUDIT_LOGGER_NAME for r in records)

    assert [r.admin_action for r in records] == [
        {"action": "user.create", **common, "role": "agent", "is_active": True},
        {
            "action": "user.role_change",
            **common,
            "role_before": "agent",
            "role_after": "customer",
            # Phase 7.5: tickets unassigned by the change (none here).
            "unassigned_tickets": 0,
        },
        {
            "action": "user.deactivate",
            **common,
            "is_active_before": True,
            "is_active_after": False,
            "unassigned_tickets": 0,
        },
        {
            "action": "user.activate",
            **common,
            "is_active_before": False,
            "is_active_after": True,
        },
    ]
    assert all(r.levelno == logging.INFO for r in records)
    assert records[0].getMessage() == (
        f"admin_action action=user.create actor_id={admin_id} "
        f"target_user_id={user_id} role=agent is_active=True"
    )
    assert records[2].getMessage() == (
        f"admin_action action=user.deactivate actor_id={admin_id} "
        f"target_user_id={user_id} is_active_before=True is_active_after=False "
        "unassigned_tickets=0"
    )

    # The application's own handler wrote one line per action to stderr.
    captured = capsys.readouterr()
    lines = _audit_lines(captured.err)

    assert len(lines) == 4
    assert all(" INFO resolveai.audit: admin_action action=" in line for line in lines)
    assert lines[0].endswith(records[0].getMessage())

    stored_hash = _stored(db_session, user_id)["password_hash"]
    token = admin_headers["Authorization"].removeprefix("Bearer ")
    formatted = "\n".join(logging.Formatter().format(r) for r in records)

    for text_value in (formatted, captured.out, captured.err):
        for secret in (
            NEW_PASSWORD,
            TEST_PASSWORD,
            stored_hash,
            "$argon2",
            token,
            "new.person@example.test",
            "New Person",
            "admin@example.test",
            "Test Admin",
        ):
            assert secret not in text_value


def test_rejected_and_unchanged_requests_are_not_logged_as_actions(
    client: TestClient,
    admin_user: User,
    agent_user: User,
    admin_headers: dict[str, str],
    audit_records: list[logging.LogRecord],
    capsys: pytest.CaptureFixture[str],
) -> None:
    responses = [
        client.patch(_role_url(agent_user.id), json={"role": "agent"}, headers=admin_headers),
        client.patch(_role_url(admin_user.id), json={"role": "agent"}, headers=admin_headers),
        client.patch(_status_url(999_999), json={"is_active": False}, headers=admin_headers),
        client.post(
            USERS_URL,
            json=_new_user_body(email="agent@example.test"),
            headers=admin_headers,
        ),
        client.post(
            USERS_URL,
            json=_new_user_body(email="not-an-email"),
            headers=admin_headers,
        ),
    ]

    assert [r.status_code for r in responses] == [200, 409, 404, 409, 422]
    assert audit_records == []
    assert _audit_lines(capsys.readouterr().err) == []


def test_unrecognised_stored_role_is_not_echoed_in_the_log(
    client: TestClient,
    db_session: Session,
    admin_headers: dict[str, str],
    audit_records: list[logging.LogRecord],
    capsys: pytest.CaptureFixture[str],
) -> None:
    legacy_id = _insert(
        db_session,
        email="legacy@example.test",
        role="Admin\nforged=entry",
    )

    response = client.patch(
        _role_url(legacy_id),
        json={"role": "agent"},
        headers=admin_headers,
    )

    assert response.status_code == 200

    [record] = audit_records

    assert record.admin_action["role_before"] == "unrecognised"
    assert "forged" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Account creation re-checks the acting administrator inside the lock
# ---------------------------------------------------------------------------


DEMOTE_ACTOR = "UPDATE users SET role = 'agent' WHERE id = :actor"
DEACTIVATE_ACTOR = (
    "UPDATE users SET is_active = 0, token_version = token_version + 1 "
    "WHERE id = :actor"
)
ACTOR_LOSES_ACCESS = {
    "demoted to agent": DEMOTE_ACTOR,
    "demoted to customer": "UPDATE users SET role = 'customer' WHERE id = :actor",
    "deactivated": DEACTIVATE_ACTOR,
    "legacy role spelling": "UPDATE users SET role = 'ADMIN' WHERE id = :actor",
    "deleted": "DELETE FROM users WHERE id = :actor",
}


def _stored_by_email(db: Session, email: str) -> dict | None:
    row = db.execute(
        text("SELECT id FROM users WHERE email = :email"),
        {"email": email},
    ).scalar_one_or_none()

    return None if row is None else _stored(db, row)


def test_admin_creation_service_creates_and_commits(
    db_session: Session,
    admin_user: User,
) -> None:
    admin_id = admin_user.id

    created = create_user_as_admin(
        db_session,
        actor_id=admin_id,
        name="  Made By Service  ",
        email="  Made@Example.TEST ",
        password=NEW_PASSWORD,
        role="customer",
    )

    assert (created.role, created.is_active) == ("customer", True)

    stored = _stored(db_session, created.user_id)

    assert (stored["name"], stored["email"]) == ("Made By Service", "made@example.test")
    assert (stored["role"], stored["is_active"], stored["token_version"]) == (
        "customer",
        1,
        0,
    )
    assert verify_password(NEW_PASSWORD, stored["password_hash"])
    # Committed: the write transaction (and SQLite's lock) is over.
    assert not db_session.connection().connection.driver_connection.in_transaction


@pytest.mark.parametrize(
    "statement",
    list(ACTOR_LOSES_ACCESS.values()),
    ids=list(ACTOR_LOSES_ACCESS),
)
def test_admin_creation_service_rejects_actor_without_admin_access(
    db_session: Session,
    admin_user: User,
    monkeypatch: pytest.MonkeyPatch,
    statement: str,
) -> None:
    admin_id = admin_user.id
    db_session.execute(text(statement), {"actor": admin_id})
    db_session.commit()
    rows_before = _all_rows(db_session)
    calls: list[dict] = []
    monkeypatch.setattr(
        user_service,
        "create_user",
        lambda *_args, **fields: calls.append(fields),
    )

    with pytest.raises(AdministratorRequiredError):
        _create_with(db_session, admin_id, "never.created@example.test")

    # Refused before validation, hashing or the INSERT.
    assert calls == []
    assert _all_rows(db_session) == rows_before


@pytest.mark.parametrize(
    "statement",
    [DEMOTE_ACTOR, DEACTIVATE_ACTOR],
    ids=["demoted", "deactivated"],
)
def test_create_by_admin_who_lost_access_after_authorisation_returns_403(
    client: TestClient,
    db_session: Session,
    admin_user: User,
    admin_headers: dict[str, str],
    create_user: Callable[..., User],
    monkeypatch: pytest.MonkeyPatch,
    audit_records: list[logging.LogRecord],
    statement: str,
) -> None:
    """The actor loses access after require_admin, before the lock."""

    actor_id = admin_user.id
    create_user(email="other.admin@example.test", role="admin")
    _commit_before_lock(monkeypatch, statement, {"actor": actor_id})
    rows_before = _all_rows(db_session)

    response = client.post(
        USERS_URL,
        json=_new_user_body(role="admin"),
        headers=admin_headers,
    )

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_DETAIL}
    assert _stored_by_email(db_session, "new.person@example.test") is None
    # Only the simulated concurrent change was written.
    assert len(_all_rows(db_session)) == len(rows_before)
    assert audit_records == []


@pytest.mark.parametrize(
    ("scenario", "expected_status"),
    [
        ("commit fails", 500),
        ("duplicate email", 409),
        ("invalid email", 422),
        ("actor demoted", 403),
    ],
)
def test_failed_creation_writes_no_success_log(
    client: TestClient,
    db_session: Session,
    admin_user: User,
    agent_user: User,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    audit_records: list[logging.LogRecord],
    capsys: pytest.CaptureFixture[str],
    scenario: str,
    expected_status: int,
) -> None:
    email = "new.person@example.test"

    if scenario == "commit fails":
        monkeypatch.setattr(
            db_session,
            "commit",
            _raiser(_operation_error("INSERT INTO users (email) VALUES (?)")),
        )
    elif scenario == "duplicate email":
        email = agent_user.email
    elif scenario == "invalid email":
        email = "not-an-email"
    else:
        _commit_before_lock(monkeypatch, DEMOTE_ACTOR, {"actor": admin_user.id})

    response = client.post(
        USERS_URL,
        json=_new_user_body(email=email),
        headers=admin_headers,
    )
    monkeypatch.undo()

    assert response.status_code == expected_status
    assert audit_records == []
    assert _audit_lines(capsys.readouterr().err) == []

    if scenario != "duplicate email":
        assert _stored_by_email(db_session, "new.person@example.test") is None


@pytest.mark.parametrize(
    "case",
    ["commit failure", "duplicate email", "actor not admin"],
)
def test_admin_creation_keeps_original_error_when_rollback_fails(
    db_session: Session,
    admin_user: User,
    agent_user: User,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    admin_id = admin_user.id
    agent_id = agent_user.id
    commit_error = _operation_error("INSERT INTO users (email) VALUES (?)")

    if case == "commit failure":
        monkeypatch.setattr(db_session, "commit", _raiser(commit_error))
        actor_id, email, expected = admin_id, "fresh@example.test", OperationalError
    elif case == "duplicate email":
        actor_id, email, expected = admin_id, "agent@example.test", UserAlreadyExistsError
    else:
        actor_id, email, expected = agent_id, "fresh@example.test", AdministratorRequiredError

    _break_rollback(db_session, monkeypatch)

    with pytest.raises(expected) as error_info:
        _create_with(db_session, actor_id, email)

    if case == "commit failure":
        assert error_info.value is commit_error

    secondary = rollback_failure_of(error_info.value)

    assert isinstance(secondary, RuntimeError)
    assert describe_rollback_failure(secondary) == UNEXPECTED_ROLLBACK_FAILURE


def test_create_rejection_with_failing_rollback_is_reported_safely(
    client: TestClient,
    db_session: Session,
    admin_user: User,
    agent_user: User,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _break_rollback(db_session, monkeypatch)

    response = client.post(
        USERS_URL,
        json=_new_user_body(email=agent_user.email),
        headers=admin_headers,
    )
    captured = capsys.readouterr()

    assert response.status_code == 409
    assert response.json() == {"detail": DUPLICATE_EMAIL_DETAIL}

    [record] = [
        r for r in caplog.records
        if r.name == ADMIN_LOGGER and r.levelno == logging.ERROR
    ]

    assert record.getMessage() == (
        "Rolling back a rejected administrative request failed (an "
        "unexpected non-database error occurred; details hidden)."
    )

    for text_value in (response.text, caplog.text, captured.out, captured.err):
        _assert_safe(text_value)
        assert NEW_PASSWORD not in text_value


# -- Real concurrency: separate threads, sessions and SQLite connections -----


@pytest.mark.parametrize(
    "remove_actor",
    [_demote, _deactivate],
    ids=["demoted", "deactivated"],
)
def test_creation_waits_for_concurrent_removal_of_the_actor_and_is_refused(
    file_database,
    monkeypatch: pytest.MonkeyPatch,
    remove_actor: Callable,
) -> None:
    engine, _path = file_database
    admin_a, admin_b = _seed(engine, ("admin-a", "admin", True), ("admin-b", "admin", True))
    hold = _HoldFirstChange(monkeypatch)

    # B removes A's administrator access and is held inside its locked
    # transaction; meanwhile A (already authorised) creates an account.
    first, second, second_was_waiting = hold.race(
        lambda: remove_actor(engine, admin_b, admin_a),
        lambda: _create_as(engine, admin_a, "raced@example.test"),
    )

    assert second_was_waiting, "the creation did not wait for the lock"
    assert first == {"result": True}
    assert isinstance(second.get("error"), AdministratorRequiredError)
    assert "raced@example.test" not in _emails(engine)


@pytest.mark.parametrize(
    "remove_actor",
    [_demote, _deactivate],
    ids=["demoted", "deactivated"],
)
def test_removal_of_the_actor_waits_for_an_in_progress_creation(
    file_database,
    monkeypatch: pytest.MonkeyPatch,
    remove_actor: Callable,
) -> None:
    """The other order: the creation commits while A is still an admin."""

    engine, _path = file_database
    admin_a, admin_b = _seed(engine, ("admin-a", "admin", True), ("admin-b", "admin", True))
    hold = _HoldFirstChange(monkeypatch)

    first, second, second_was_waiting = hold.race(
        lambda: _create_as(engine, admin_a, "raced@example.test"),
        lambda: remove_actor(engine, admin_b, admin_a),
    )

    assert second_was_waiting, "the removal did not wait for the creation"
    assert isinstance(first.get("result"), int)
    assert second == {"result": True}
    assert "raced@example.test" in _emails(engine)
    # The new account (an administrator) exists; A lost access afterwards.
    assert _active_admin_ids(engine) == [admin_b, first["result"]]


def test_without_the_lock_a_removed_admin_can_still_create_an_account(
    file_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: proves the creation race tests detect the problem.

    With the write lock disabled, A's demotion commits while A's creation
    is between its check and its INSERT, and the account is still created
    by someone who is no longer an administrator.
    """

    engine, _path = file_database
    admin_a, admin_b = _seed(engine, ("admin-a", "admin", True), ("admin-b", "admin", True))
    monkeypatch.setattr(user_service, "_begin_serialised_change", lambda db: None)
    hold = _HoldFirstChange(monkeypatch)

    first, second, second_was_waiting = hold.race(
        lambda: _create_as(engine, admin_a, "raced@example.test"),
        lambda: _demote(engine, admin_b, admin_a),
        wait_for_second=True,
    )

    assert not second_was_waiting
    assert second == {"result": True}
    assert isinstance(first.get("result"), int)
    # A was demoted first, yet a new administrator account A created
    # afterwards exists: the privilege-persistence race the lock prevents.
    assert "raced@example.test" in _emails(engine)
    assert _active_admin_ids(engine) == [admin_b, first["result"]]


def test_concurrent_api_creation_by_a_removed_admin_is_refused(
    file_database,
    monkeypatch: pytest.MonkeyPatch,
    audit_records: list[logging.LogRecord],
) -> None:
    engine, _path = file_database
    admin_a, admin_b = _seed(engine, ("admin-a", "admin", True), ("admin-b", "admin", True))
    app.dependency_overrides[get_db] = _sessions_for(engine)
    hold = _HoldFirstChange(monkeypatch)

    try:
        with TestClient(app) as client:
            first, second, second_was_waiting = hold.race(
                lambda: client.patch(
                    _status_url(admin_a),
                    json={"is_active": False},
                    headers=_bearer(admin_b),
                ),
                lambda: client.post(
                    USERS_URL,
                    json=_new_user_body(email="raced@example.test", role="admin"),
                    headers=_bearer(admin_a),
                ),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert second_was_waiting, "the creation request did not wait for the lock"
    assert first["result"].status_code == 200
    assert second["result"].status_code == 403
    assert second["result"].json() == {"detail": FORBIDDEN_DETAIL}
    assert "raced@example.test" not in _emails(engine)
    # Only the deactivation is in the audit log.
    assert [r.admin_action["action"] for r in audit_records] == ["user.deactivate"]


# ---------------------------------------------------------------------------
# Phase 7.5: deactivation and agent role changes unassign tickets
# ---------------------------------------------------------------------------

TICKET_TITLE_SECRET = "Ticket-title-secret-51d2"
TICKET_DESCRIPTION_SECRET = "Ticket description secret 8e40"


def _assigned_ticket(db: Session, creator_id: int, assignee_id: int, status: str) -> int:
    ticket = Ticket(
        title=TICKET_TITLE_SECRET,
        description=TICKET_DESCRIPTION_SECRET,
        created_by_user_id=creator_id,
        status=status,
        assigned_to_user_id=assignee_id,
    )
    db.add(ticket)
    db.commit()

    return ticket.id


def _ticket_rows(db: Session) -> dict[int, tuple]:
    return {
        row[0]: tuple(row[1:])
        for row in db.execute(
            text("SELECT id, assigned_to_user_id, status, version FROM tickets")
        )
    }


def _ticket_event_reasons(db: Session) -> list[tuple]:
    return [
        tuple(row)
        for row in db.execute(
            text("SELECT ticket_id, event_type, reason FROM ticket_events ORDER BY id")
        )
    ]


@pytest.fixture
def agent_with_tickets(
    db_session: Session,
    agent_user: User,
    customer_user: User,
) -> dict[str, int]:
    return {
        status: _assigned_ticket(db_session, customer_user.id, agent_user.id, status)
        for status in ("Open", "In Progress", "Resolved")
    }


@pytest.mark.parametrize(
    ("url_for", "body", "action", "reason"),
    [
        (_status_url, {"is_active": False}, "user.deactivate", "assignee_deactivated"),
        (_role_url, {"role": "customer"}, "user.role_change", "assignee_role_changed"),
        (_role_url, {"role": "admin"}, "user.role_change", "assignee_role_changed"),
    ],
    ids=["deactivate", "to-customer", "to-admin"],
)
def test_user_change_via_api_unassigns_tickets_and_logs_the_count(
    client: TestClient,
    db_session: Session,
    admin_user: User,
    agent_user: User,
    admin_headers: dict[str, str],
    agent_with_tickets: dict[str, int],
    audit_records: list[logging.LogRecord],
    capsys: pytest.CaptureFixture[str],
    url_for: Callable[[int], str],
    body: dict,
    action: str,
    reason: str,
) -> None:
    agent_id, open_id, progress_id, resolved_id = (
        agent_user.id,
        agent_with_tickets["Open"],
        agent_with_tickets["In Progress"],
        agent_with_tickets["Resolved"],
    )

    response = client.patch(url_for(agent_id), json=body, headers=admin_headers)

    # Phase 6 response is unchanged.
    assert response.status_code == 200
    assert set(response.json()) == {"id", "name", "email", "role", "is_active", "created_at"}

    rows = _ticket_rows(db_session)

    assert rows[open_id] == (None, "Open", 2)
    assert rows[progress_id] == (None, "Open", 2)
    assert rows[resolved_id] == (agent_id, "Resolved", 1)
    assert _ticket_event_reasons(db_session) == [
        (open_id, "unassigned", reason),
        (progress_id, "unassigned", reason),
    ]

    [record] = audit_records

    assert record.admin_action["action"] == action
    assert record.admin_action["unassigned_tickets"] == 2
    assert record.getMessage().endswith(" unassigned_tickets=2")

    output = capsys.readouterr().err

    assert "unassigned_tickets=2" in output

    for secret in (
        agent_user.email,
        agent_user.name,
        admin_user.email,
        TICKET_TITLE_SECRET,
        TICKET_DESCRIPTION_SECRET,
    ):
        assert secret not in output
        assert secret not in record.getMessage()


def test_reactivation_logs_no_ticket_count_and_restores_nothing(
    client: TestClient,
    db_session: Session,
    agent_user: User,
    admin_headers: dict[str, str],
    agent_with_tickets: dict[str, int],
    audit_records: list[logging.LogRecord],
) -> None:
    client.patch(_status_url(agent_user.id), json={"is_active": False}, headers=admin_headers)
    after_deactivation = _ticket_rows(db_session)

    response = client.patch(
        _status_url(agent_user.id), json={"is_active": True}, headers=admin_headers
    )

    assert response.status_code == 200
    assert _ticket_rows(db_session) == after_deactivation
    assert "unassigned_tickets" not in audit_records[-1].admin_action
    assert audit_records[-1].admin_action["action"] == "user.activate"


@pytest.mark.parametrize(
    ("url_for", "body"),
    [(_status_url, {"is_active": False}), (_role_url, {"role": "customer"})],
    ids=["deactivate", "demote"],
)
def test_failed_transaction_writes_no_audit_log_and_changes_nothing(
    client: TestClient,
    db_session: Session,
    agent_user: User,
    admin_headers: dict[str, str],
    agent_with_tickets: dict[str, int],
    audit_records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
    url_for: Callable[[int], str],
    body: dict,
) -> None:
    from app.services import ticket_workflow

    agent_id = agent_user.id
    tickets_before = _ticket_rows(db_session)
    user_before = _stored(db_session, agent_id)
    monkeypatch.setattr(
        ticket_workflow,
        "record_ticket_event",
        _raiser(_operation_error("INSERT INTO ticket_events (reason) VALUES (?)")),
    )

    response = client.patch(url_for(agent_id), json=body, headers=admin_headers)

    assert response.status_code == 500
    assert response.json() == {"detail": DATABASE_ERROR_DETAIL}
    assert audit_records == []
    assert _ticket_rows(db_session) == tickets_before
    assert _ticket_event_reasons(db_session) == []
    assert _stored(db_session, agent_id) == user_before
