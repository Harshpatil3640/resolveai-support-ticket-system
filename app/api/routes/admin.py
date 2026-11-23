"""Admin-only user management API (mounted at /api/admin).

Every endpoint requires an authenticated, active administrator: the
router itself depends on ``require_admin`` (so any endpoint added here
later is protected too) and each endpoint also declares it. FastAPI
evaluates the dependency once per request.

Database work lives in ``app.services.user_service``; this module maps
service outcomes to HTTP responses and writes the administrative action
log (``app.core.audit_logging``) after the service has committed.
Unexpected database errors propagate to the application's safe database
error handler (generic 500, no driver text).
"""

import logging
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

from app.api.dependencies import FORBIDDEN_DETAIL, require_admin
from app.core.audit_logging import configure_audit_logging
from app.core.roles import UserRole
from app.db.database import get_db
from app.db.errors import describe_rollback_failure, rollback_failure_of
from app.models import User
from app.schemas import (
    AdminUserCreate,
    AdminUserListResponse,
    AdminUserRoleUpdate,
    AdminUserStatusUpdate,
    UserRead,
)
from app.services.auth_service import UserNotFoundError
from app.services.user_service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    MAX_SEARCH_LENGTH,
    AdministratorRequiredError,
    InvalidUserDataError,
    LastActiveAdministratorError,
    SelfModificationError,
    UserAlreadyExistsError,
    change_user_role,
    create_user_as_admin,
    get_user,
    list_users,
    set_user_active_status,
)


# Operational problems (e.g. a failed rollback) are logged here; the
# administrative actions themselves go to the dedicated audit logger
# (see log_admin_action).
logger = logging.getLogger(__name__)

# SQLite's largest INTEGER. Larger IDs or offsets cannot be sent to the
# driver (it raises OverflowError), so they are rejected as invalid input.
MAX_DATABASE_INTEGER = 2**63 - 1

USER_NOT_FOUND_DETAIL = "User not found."
DUPLICATE_EMAIL_DETAIL = "An account with this email already exists."
SELF_ROLE_CHANGE_DETAIL = "Administrators cannot change their own role."
SELF_STATUS_CHANGE_DETAIL = "Administrators cannot change their own status."
LAST_ADMIN_DETAIL = "At least one active administrator must remain."


class AdminRoute(APIRoute):
    """Route that never echoes submitted values in a 422 response.

    FastAPI's default validation response includes each rejected value
    under "input", which here could be a password (too short, too long)
    or a smuggled field such as "password_hash". The values are removed;
    the error type, location, message and constraints are kept, and the
    application's normal 422 handler still formats the response.
    """

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handler_without_input_echo(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as error:
                errors = [
                    {key: value for key, value in item.items() if key != "input"}
                    if isinstance(item, dict)
                    else item
                    for item in error.errors()
                ]

                raise RequestValidationError(errors) from None

        return handler_without_input_echo


router = APIRouter(
    tags=["Administration"],
    dependencies=[Depends(require_admin)],
    route_class=AdminRoute,
)

UserId = Annotated[int, Path(ge=1, le=MAX_DATABASE_INTEGER)]


# ---------------------------------------------------------------------------
# Administrative action log
# ---------------------------------------------------------------------------


_KNOWN_ROLES = frozenset(role.value for role in UserRole)


def _loggable_role(role: object) -> str:
    """A stored role safe to log (legacy values are not echoed)."""

    return role if isinstance(role, str) and role in _KNOWN_ROLES else "unrecognised"


def log_admin_action(
    action: str,
    *,
    actor_id: int,
    target_user_id: int,
    **values: str | bool,
) -> None:
    """Record an administrative action in the audit log (INFO).

    Called only after the action's transaction has committed. Only IDs,
    the action name and role/status values are logged; never names,
    emails, passwords, hashes, tokens or request bodies. The same fields
    are attached to the record as ``record.admin_action`` for structured
    log handlers.
    """

    fields: dict[str, str | int | bool] = {
        "action": action,
        "actor_id": actor_id,
        "target_user_id": target_user_id,
        **values,
    }

    # Idempotent and operator-respecting. Makes the audit log work in an
    # application that mounts this router without importing app.main, and
    # re-enables the logger if a later logging configuration disabled it.
    audit_logger = configure_audit_logging()

    audit_logger.info(
        "admin_action %s",
        " ".join(f"{key}={value}" for key, value in fields.items()),
        extra={"admin_action": fields},
    )


def _log_rollback_failure(error: BaseException) -> None:
    """Report, safely, a failed rollback recorded on a rejected change."""

    failure = rollback_failure_of(error)

    if failure is not None:
        logger.error(
            "Rolling back a rejected administrative request failed (%s).",
            describe_rollback_failure(failure),
        )


def _http_error(
    error: BaseException,
    status_code: int,
    detail: str,
) -> HTTPException:
    _log_rollback_failure(error)

    return HTTPException(status_code=status_code, detail=detail)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/users",
    response_model=AdminUserListResponse,
    summary="List user accounts",
)
def list_users_endpoint(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[User, Depends(require_admin)],
    skip: Annotated[int, Query(ge=0, le=MAX_DATABASE_INTEGER)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    search: Annotated[
        str | None,
        Query(min_length=1, max_length=MAX_SEARCH_LENGTH),
    ] = None,
    role: UserRole | None = None,
    is_active: bool | None = None,
) -> AdminUserListResponse:
    """List users, newest first, with optional search and filters.

    ``search`` matches name or email case-insensitively; ``total`` is the
    number of matching users before pagination.
    """

    users, total = list_users(
        db,
        skip=skip,
        limit=limit,
        search=search,
        role=role,
        is_active=is_active,
    )

    return AdminUserListResponse(
        items=[UserRead.model_validate(user) for user in users],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get(
    "/users/{user_id}",
    response_model=UserRead,
    summary="Retrieve a user account",
)
def get_user_endpoint(
    user_id: UserId,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[User, Depends(require_admin)],
) -> UserRead:
    """Return one user account."""

    user = get_user(db, user_id)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=USER_NOT_FOUND_DETAIL,
        )

    return UserRead.model_validate(user)


@router.post(
    "/users",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user account",
)
def create_user_endpoint(
    payload: AdminUserCreate,
    db: Annotated[Session, Depends(get_db)],
    current_admin: Annotated[User, Depends(require_admin)],
) -> UserRead:
    """Create an administrator, agent or customer account.

    The acting administrator is re-checked inside the serialised
    transaction that creates the account (403 if their access was removed
    after this request was authorised). Validation, email normalisation,
    password hashing and duplicate detection are performed by the shared
    ``create_user`` service.
    """

    actor_id = current_admin.id

    try:
        created = create_user_as_admin(
            db,
            actor_id=actor_id,
            name=payload.name,
            email=payload.email,
            password=payload.password,
            role=payload.role,
        )
    except AdministratorRequiredError as error:
        raise _http_error(
            error, status.HTTP_403_FORBIDDEN, FORBIDDEN_DETAIL
        ) from None
    except UserAlreadyExistsError as error:
        raise _http_error(
            error, status.HTTP_409_CONFLICT, DUPLICATE_EMAIL_DETAIL
        ) from None
    except InvalidUserDataError as error:
        # The service's messages are fixed text (never the submitted value).
        raise _http_error(
            error, status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)
        ) from None

    # The transaction has committed. These values were captured before
    # the commit, so logging needs no further database access.
    log_admin_action(
        "user.create",
        actor_id=actor_id,
        target_user_id=created.user_id,
        role=_loggable_role(created.role),
        is_active=created.is_active,
    )

    return UserRead.model_validate(created.user)


@router.patch(
    "/users/{user_id}/role",
    response_model=UserRead,
    summary="Change a user's role",
)
def change_user_role_endpoint(
    user_id: UserId,
    payload: AdminUserRoleUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_admin: Annotated[User, Depends(require_admin)],
) -> UserRead:
    """Change a user's role; it takes effect on the user's next request.

    Administrators cannot change their own role, and the last active
    administrator cannot be demoted. Existing tokens stay valid: the role
    is read from the database on every request.
    """

    actor_id = current_admin.id

    try:
        change = change_user_role(
            db,
            actor_id=actor_id,
            user_id=user_id,
            role=payload.role,
        )
    except SelfModificationError as error:
        raise _http_error(
            error, status.HTTP_409_CONFLICT, SELF_ROLE_CHANGE_DETAIL
        ) from None
    except UserNotFoundError as error:
        raise _http_error(
            error, status.HTTP_404_NOT_FOUND, USER_NOT_FOUND_DETAIL
        ) from None
    except LastActiveAdministratorError as error:
        raise _http_error(
            error, status.HTTP_409_CONFLICT, LAST_ADMIN_DETAIL
        ) from None
    except AdministratorRequiredError as error:
        raise _http_error(
            error, status.HTTP_403_FORBIDDEN, FORBIDDEN_DETAIL
        ) from None

    if change.changed:
        log_admin_action(
            "user.role_change",
            actor_id=actor_id,
            target_user_id=user_id,
            role_before=_loggable_role(change.previous_role),
            role_after=_loggable_role(change.new_role),
        )

    return UserRead.model_validate(change.user)


@router.patch(
    "/users/{user_id}/status",
    response_model=UserRead,
    summary="Activate or deactivate a user account",
)
def set_user_status_endpoint(
    user_id: UserId,
    payload: AdminUserStatusUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_admin: Annotated[User, Depends(require_admin)],
) -> UserRead:
    """Activate or deactivate an account.

    Any real change signs the user out everywhere (``token_version`` is
    incremented in the same transaction); repeating the current status
    changes nothing. Administrators cannot change their own status, and
    the last active administrator cannot be deactivated.
    """

    actor_id = current_admin.id

    try:
        change = set_user_active_status(
            db,
            actor_id=actor_id,
            user_id=user_id,
            is_active=payload.is_active,
        )
    except SelfModificationError as error:
        raise _http_error(
            error, status.HTTP_409_CONFLICT, SELF_STATUS_CHANGE_DETAIL
        ) from None
    except UserNotFoundError as error:
        raise _http_error(
            error, status.HTTP_404_NOT_FOUND, USER_NOT_FOUND_DETAIL
        ) from None
    except LastActiveAdministratorError as error:
        raise _http_error(
            error, status.HTTP_409_CONFLICT, LAST_ADMIN_DETAIL
        ) from None
    except AdministratorRequiredError as error:
        raise _http_error(
            error, status.HTTP_403_FORBIDDEN, FORBIDDEN_DETAIL
        ) from None

    if change.changed:
        log_admin_action(
            "user.activate" if change.is_active else "user.deactivate",
            actor_id=actor_id,
            target_user_id=user_id,
            is_active_before=change.was_active,
            is_active_after=change.is_active,
        )

    return UserRead.model_validate(change.user)
