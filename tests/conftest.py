
import secrets
from collections.abc import Callable, Generator
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
import pytest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import ALGORITHM, SECRET_KEY
from app.core.roles import UserRole
from app.core.security import hash_password
from app.db.base import Base
from app.db.database import get_db
from app.main import app
from app.models import User


TEST_PASSWORD = "StrongPassword123!"


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Provide an isolated in-memory SQLite session for each test."""

    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # Enable foreign-key enforcement in the test database.
    @event.listens_for(test_engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    # Create the tables inside the test database.
    Base.metadata.create_all(bind=test_engine)

    TestingSessionLocal = sessionmaker(
        bind=test_engine,
        autoflush=False,
        autocommit=False,
    )

    try:
        with TestingSessionLocal() as session:
            yield session

    finally:
        Base.metadata.drop_all(bind=test_engine)
        test_engine.dispose()


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    """Create a test client using an isolated test database."""

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def create_user(db_session: Session) -> Callable[..., User]:
    """Factory that persists a user with the given email and role.

    Passing ``role=None`` leaves the model's default role in place.
    """

    def _create_user(
        *,
        email: str,
        role: str | None,
        name: str = "Test User",
        password: str = TEST_PASSWORD,
    ) -> User:
        user = User(
            name=name,
            email=email,
            password_hash=hash_password(password),
        )

        if role is not None:
            user.role = role

        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        return user

    return _create_user


@pytest.fixture
def login_headers(client: TestClient) -> Callable[..., dict[str, str]]:
    """Factory that logs a user in and returns bearer auth headers."""

    def _login_headers(
        email: str,
        password: str = TEST_PASSWORD,
    ) -> dict[str, str]:
        response = client.post(
            "/api/auth/login",
            data={"username": email, "password": password},
        )

        assert response.status_code == 200

        token = response.json()["access_token"]

        return {"Authorization": f"Bearer {token}"}

    return _login_headers


@pytest.fixture
def admin_user(create_user: Callable[..., User]) -> User:
    return create_user(
        email="admin@example.test",
        role=UserRole.ADMIN.value,
        name="Test Admin",
    )


@pytest.fixture
def admin_headers(
    admin_user: User,
    login_headers: Callable[..., dict[str, str]],
) -> dict[str, str]:
    return login_headers(admin_user.email)


@pytest.fixture
def agent_user(create_user: Callable[..., User]) -> User:
    """Support agent created with the model's default role ("agent")."""

    return create_user(
        email="agent@example.test",
        role=None,
        name="Test Agent",
    )


@pytest.fixture
def agent_headers(
    agent_user: User,
    login_headers: Callable[..., dict[str, str]],
) -> dict[str, str]:
    return login_headers(agent_user.email)


@pytest.fixture
def customer_user(create_user: Callable[..., User]) -> User:
    return create_user(
        email="customer@example.test",
        role=UserRole.CUSTOMER.value,
        name="Test Customer",
    )


@pytest.fixture
def customer_headers(
    customer_user: User,
    login_headers: Callable[..., dict[str, str]],
) -> dict[str, str]:
    return login_headers(customer_user.email)


@pytest.fixture
def second_customer_user(create_user: Callable[..., User]) -> User:
    return create_user(
        email="customer2@example.test",
        role=UserRole.CUSTOMER.value,
        name="Second Customer",
    )


@pytest.fixture
def second_customer_headers(
    second_customer_user: User,
    login_headers: Callable[..., dict[str, str]],
) -> dict[str, str]:
    return login_headers(second_customer_user.email)


@pytest.fixture
def forge_access_token() -> Callable[..., str]:
    """Factory for correctly signed access tokens with custom claims.

    Starts from a valid claim set; keyword arguments override claims and
    ``omit`` removes them (e.g. to build a legacy token without a
    ``token_version`` claim).
    """

    def _forge(
        *,
        subject: str,
        omit: tuple[str, ...] = (),
        **overrides: Any,
    ) -> str:
        now = datetime.now(timezone.utc)

        claims: dict[str, Any] = {
            "sub": subject,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "jti": secrets.token_urlsafe(16),
            "token_type": "access",
            "token_version": 0,
        }
        claims.update(overrides)

        for claim in omit:
            claims.pop(claim, None)

        return jwt.encode(claims, SECRET_KEY, algorithm=ALGORITHM)

    return _forge
