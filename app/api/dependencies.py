
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.roles import UserRole
from app.db.database import get_db
from app.models import User
from app.services.auth_service import get_active_user_from_token


oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/api/auth/login"
)


def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    """Retrieve the authenticated user from the JWT."""

    user = get_active_user_from_token(
        db=db,
        token=token,
    )

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


FORBIDDEN_DETAIL = "You do not have permission to perform this action."


def require_roles(
    *allowed_roles: UserRole,
) -> Callable[[User], User]:
    """Build a dependency that only admits users holding an allowed role.

    Authentication (401) is delegated to ``get_current_user``; this
    dependency only performs authorization and raises 403 when the
    authenticated user's role is not permitted.
    """

    if not allowed_roles:
        raise ValueError("require_roles() needs at least one allowed role.")

    for role in allowed_roles:
        if not isinstance(role, UserRole):
            raise TypeError(
                f"Allowed roles must be UserRole members, got {role!r}."
            )

    permitted: frozenset[UserRole] = frozenset(allowed_roles)

    def role_checker(
        current_user: Annotated[User, Depends(get_current_user)],
    ) -> User:
        try:
            user_role = UserRole(current_user.role)
        except ValueError:
            # Unknown or malformed stored role: deny by default.
            user_role = None

        if user_role is None or user_role not in permitted:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=FORBIDDEN_DETAIL,
            )

        return current_user

    return role_checker


require_any_role = require_roles(
    UserRole.ADMIN,
    UserRole.AGENT,
    UserRole.CUSTOMER,
)

require_admin = require_roles(UserRole.ADMIN)

require_agent = require_roles(UserRole.AGENT)

require_agent_or_admin = require_roles(UserRole.AGENT, UserRole.ADMIN)
