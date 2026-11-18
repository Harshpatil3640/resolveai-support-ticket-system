from typing import Annotated

import pytest

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.testclient import TestClient

from app.api.dependencies import (
    FORBIDDEN_DETAIL,
    get_current_user,
    require_admin,
    require_agent_or_admin,
    require_any_role,
    require_roles,
)
from app.core.roles import UserRole
from app.models import User


def make_user(role: str) -> User:
    """Build an in-memory user with the given stored role."""

    return User(
        id=1,
        name="Role Test",
        email=f"{role}@example.test",
        password_hash="not-used",
        role=role,
        is_active=True,
    )


def assert_forbidden(exc_info: pytest.ExceptionInfo[HTTPException]) -> None:
    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    assert exc_info.value.detail == FORBIDDEN_DETAIL


# ---------------------------------------------------------------------------
# require_admin
# ---------------------------------------------------------------------------


def test_admin_passes_require_admin() -> None:
    user = make_user(UserRole.ADMIN.value)

    assert require_admin(user) is user


def test_agent_rejected_by_require_admin() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_admin(make_user(UserRole.AGENT.value))

    assert_forbidden(exc_info)


def test_customer_rejected_by_require_admin() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_admin(make_user(UserRole.CUSTOMER.value))

    assert_forbidden(exc_info)


# ---------------------------------------------------------------------------
# require_agent_or_admin
# ---------------------------------------------------------------------------


def test_agent_passes_require_agent_or_admin() -> None:
    user = make_user(UserRole.AGENT.value)

    assert require_agent_or_admin(user) is user


def test_admin_passes_require_agent_or_admin() -> None:
    user = make_user(UserRole.ADMIN.value)

    assert require_agent_or_admin(user) is user


def test_customer_rejected_by_require_agent_or_admin() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_agent_or_admin(make_user(UserRole.CUSTOMER.value))

    assert_forbidden(exc_info)


# ---------------------------------------------------------------------------
# require_any_role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(UserRole))
def test_every_valid_role_passes_require_any_role(role: UserRole) -> None:
    user = make_user(role.value)

    assert require_any_role(user) is user


def test_unknown_role_rejected_by_require_any_role() -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_any_role(make_user("superuser"))

    assert_forbidden(exc_info)


# ---------------------------------------------------------------------------
# require_roles factory
# ---------------------------------------------------------------------------


def test_customer_specific_dependency_permits_customer() -> None:
    require_customer = require_roles(UserRole.CUSTOMER)
    user = make_user(UserRole.CUSTOMER.value)

    assert require_customer(user) is user


@pytest.mark.parametrize(
    "stored_role",
    ["superuser", "", "Admin", " admin"],
)
def test_unknown_stored_role_is_denied(stored_role: str) -> None:
    dependency = require_roles(
        UserRole.ADMIN,
        UserRole.AGENT,
        UserRole.CUSTOMER,
    )

    with pytest.raises(HTTPException) as exc_info:
        dependency(make_user(stored_role))

    assert_forbidden(exc_info)


def test_require_roles_without_roles_is_rejected() -> None:
    with pytest.raises(ValueError):
        require_roles()


def test_require_roles_rejects_non_enum_roles() -> None:
    with pytest.raises(TypeError):
        require_roles("admin")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Integration: authentication (401) stays distinct from authorization (403)
# ---------------------------------------------------------------------------


def build_protected_app() -> FastAPI:
    test_app = FastAPI()

    @test_app.get("/admin-only")
    def admin_only(
        user: Annotated[User, Depends(require_admin)],
    ) -> dict[str, str]:
        return {"role": user.role}

    return test_app


def test_missing_token_returns_401_not_403() -> None:
    with TestClient(build_protected_app()) as client:
        response = client.get("/admin-only")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_authenticated_wrong_role_returns_403() -> None:
    test_app = build_protected_app()
    test_app.dependency_overrides[get_current_user] = (
        lambda: make_user(UserRole.AGENT.value)
    )

    with TestClient(test_app) as client:
        response = client.get("/admin-only")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json() == {"detail": FORBIDDEN_DETAIL}


def test_authenticated_allowed_role_returns_200() -> None:
    test_app = build_protected_app()
    test_app.dependency_overrides[get_current_user] = (
        lambda: make_user(UserRole.ADMIN.value)
    )

    with TestClient(test_app) as client:
        response = client.get("/admin-only")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"role": "admin"}
