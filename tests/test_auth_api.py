
import pytest

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import decode_access_token, hash_password
from app.db.database import get_db
from app.main import app
from app.models import User
from app.services.auth_service import invalidate_user_sessions


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
    """Create a user for authentication API tests."""

    user = User(
        name="Harsh Test",
        email="harsh@example.test",
        password_hash=hash_password("StrongPassword123!"),
    )

    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    return user


def test_login_success(
    client: TestClient,
    api_user: User,
) -> None:

    response = client.post(
        "/api/auth/login",
        data={
            "username": "harsh@example.test",
            "password": "StrongPassword123!",
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["access_token"]
    assert data["token_type"] == "bearer"


def test_login_wrong_password(
    client: TestClient,
    api_user: User,
) -> None:

    response = client.post(
        "/api/auth/login",
        data={
            "username": "harsh@example.test",
            "password": "WrongPassword123!",
        },
    )

    assert response.status_code == 401


def test_login_unknown_email(
    client: TestClient,
) -> None:

    response = client.post(
        "/api/auth/login",
        data={
            "username": "unknown@example.test",
            "password": "SomePassword123!",
        },
    )

    assert response.status_code == 401


def test_inactive_user_cannot_login(
    client: TestClient,
    db_session: Session,
    api_user: User,
) -> None:

    api_user.is_active = False
    db_session.commit()

    response = client.post(
        "/api/auth/login",
        data={
            "username": "harsh@example.test",
            "password": "StrongPassword123!",
        },
    )

    assert response.status_code == 401


def test_profile_requires_authentication(
    client: TestClient,
) -> None:

    response = client.get("/api/auth/me")

    assert response.status_code == 401


def test_profile_with_valid_token(
    client: TestClient,
    api_user: User,
) -> None:

    login_response = client.post(
        "/api/auth/login",
        data={
            "username": "harsh@example.test",
            "password": "StrongPassword123!",
        },
    )

    token = login_response.json()["access_token"]

    response = client.get(
        "/api/auth/me",
        headers={
            "Authorization": f"Bearer {token}"
        },
    )

    assert response.status_code == 200

    profile = response.json()

    assert profile["id"] == api_user.id
    assert profile["email"] == "harsh@example.test"

    # Never expose the stored password hash.
    assert "password_hash" not in profile


def test_logout_revokes_access_token(
    client: TestClient,
    api_user: User,
) -> None:

    login_response = client.post(
        "/api/auth/login",
        data={
            "username": "harsh@example.test",
            "password": "StrongPassword123!",
        },
    )

    token = login_response.json()["access_token"]

    headers = {
        "Authorization": f"Bearer {token}"
    }

    logout_response = client.post(
        "/api/auth/logout",
        headers=headers,
    )

    assert logout_response.status_code == 200
    assert logout_response.json()["message"] == (
        "Successfully logged out."
    )

    # The same token must no longer access protected resources.
    profile_response = client.get(
        "/api/auth/me",
        headers=headers,
    )

    assert profile_response.status_code == 401


# ---------------------------------------------------------------------------
# Token version
# ---------------------------------------------------------------------------


def _login_token(client: TestClient, email: str = "harsh@example.test") -> str:
    response = client.post(
        "/api/auth/login",
        data={"username": email, "password": "StrongPassword123!"},
    )

    assert response.status_code == 200

    return response.json()["access_token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_login_token_contains_current_token_version(
    client: TestClient,
    db_session: Session,
    api_user: User,
) -> None:
    api_user.token_version = 3
    db_session.commit()

    payload = decode_access_token(_login_token(client))

    assert payload["token_version"] == 3
    assert "role" not in payload
    assert "is_active" not in payload


def test_invalidated_sessions_return_401(
    client: TestClient,
    db_session: Session,
    api_user: User,
) -> None:
    old_token = _login_token(client)

    assert client.get("/api/auth/me", headers=_bearer(old_token)).status_code == 200

    invalidate_user_sessions(db_session, api_user)

    response = client.get("/api/auth/me", headers=_bearer(old_token))

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_fresh_login_after_invalidation_works(
    client: TestClient,
    db_session: Session,
    api_user: User,
) -> None:
    _login_token(client)
    invalidate_user_sessions(db_session, api_user)

    fresh_token = _login_token(client)

    assert decode_access_token(fresh_token)["token_version"] == 1

    response = client.get("/api/auth/me", headers=_bearer(fresh_token))

    assert response.status_code == 200
    assert response.json()["id"] == api_user.id


def test_legacy_token_without_version_is_rejected(
    client: TestClient,
    api_user: User,
    forge_access_token,
) -> None:
    legacy_token = forge_access_token(
        subject=str(api_user.id),
        omit=("token_version",),
    )

    response = client.get("/api/auth/me", headers=_bearer(legacy_token))

    assert response.status_code == 401


def test_token_with_wrong_version_is_rejected(
    client: TestClient,
    api_user: User,
    forge_access_token,
) -> None:
    token = forge_access_token(
        subject=str(api_user.id),
        token_version=api_user.token_version + 1,
    )

    response = client.get("/api/auth/me", headers=_bearer(token))

    assert response.status_code == 401


def test_deactivated_user_existing_token_is_rejected(
    client: TestClient,
    db_session: Session,
    api_user: User,
) -> None:
    token = _login_token(client)

    api_user.is_active = False
    db_session.commit()

    response = client.get("/api/auth/me", headers=_bearer(token))

    assert response.status_code == 401


def test_logout_still_revokes_versioned_token(
    client: TestClient,
    api_user: User,
) -> None:
    headers = _bearer(_login_token(client))

    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401

    # A fresh login is unaffected by the earlier logout.
    assert client.get(
        "/api/auth/me",
        headers=_bearer(_login_token(client)),
    ).status_code == 200


def test_role_change_applies_to_existing_token_without_invalidation(
    client: TestClient,
    db_session: Session,
    api_user: User,
) -> None:
    """Roles are read from the database per request, not from the JWT."""

    headers = _bearer(_login_token(client))

    create_response = client.post(
        "/api/tickets",
        json={"title": "Role check", "description": "Ticket for role test."},
        headers=headers,
    )
    assert create_response.status_code == 201
    ticket_id = create_response.json()["id"]

    # Agent (default role) cannot delete.
    assert client.delete(
        f"/api/tickets/{ticket_id}",
        headers=headers,
    ).status_code == 403

    # Promote to admin: the same token now has admin permissions.
    api_user.role = "admin"
    db_session.commit()

    assert client.delete(
        f"/api/tickets/{ticket_id}",
        headers=headers,
    ).status_code == 204

    # Demote to customer: the same token immediately loses staff access.
    api_user.role = "customer"
    db_session.commit()

    assert client.get(
        "/api/analytics/summary",
        headers=headers,
    ).status_code == 403

    db_session.refresh(api_user)

    assert api_user.token_version == 0
