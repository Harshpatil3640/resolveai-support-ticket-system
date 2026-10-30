
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class TokenResponse(BaseModel):
    """Response returned after successful login."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"


class UserRead(BaseModel):
    """Public information about an authenticated user."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    role: str
    is_active: bool
    created_at: datetime


class MessageResponse(BaseModel):
    """Standard application message response."""

    message: str
