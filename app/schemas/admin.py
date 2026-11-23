"""Request and response models for the admin-only user management API.

Responses reuse ``UserRead``, which has no password, password hash or
``token_version`` field.
"""

from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
)

from app.core.roles import UserRole
from app.schemas.auth import UserRead
from app.services.user_service import (
    MAX_EMAIL_LENGTH,
    MAX_NAME_LENGTH,
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
)


# Unlike every other string, the password is never trimmed: whitespace is
# part of the secret, and login compares the password exactly as typed.
# The length limits are checked here so an oversized password is rejected
# before it reaches the service (and its hashing).
Password = Annotated[
    str,
    StringConstraints(
        strip_whitespace=False,
        min_length=MIN_PASSWORD_LENGTH,
        max_length=MAX_PASSWORD_LENGTH,
    ),
    Field(repr=False),
]


class _AdminRequest(BaseModel):
    """Common configuration for admin request bodies."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        # Never include submitted values in the text of a validation error.
        hide_input_in_errors=True,
    )


class AdminUserCreate(_AdminRequest):
    """Data an administrator submits to create an account.

    The role is mandatory (there is no default). Email format,
    normalisation and duplicate checks are performed by
    ``app.services.user_service.create_user``.
    """

    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    email: str = Field(min_length=1, max_length=MAX_EMAIL_LENGTH)
    password: Password
    role: UserRole


class AdminUserRoleUpdate(_AdminRequest):
    """New role for an account."""

    role: UserRole


class AdminUserStatusUpdate(_AdminRequest):
    """New active status for an account.

    Only a JSON boolean is accepted (not "false", 0 or similar).
    """

    is_active: StrictBool


class AdminUserListResponse(BaseModel):
    """One page of users with the total number matching the filters."""

    items: list[UserRead]
    total: int
    skip: int
    limit: int
