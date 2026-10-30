
import pytest

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.database import get_db
from app.main import app
from app.models import User


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
